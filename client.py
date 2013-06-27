#!/usr/bin/env python

import sys
import base64
import logging
import socket
import select
import string
import struct
import threading
import Queue
import urwid
import urwid.curses_display
import nacl.utils
import nacl.public
import nacl.secret
import nacl.exceptions
import ConfigParser

logging.basicConfig(filename="deadchat.log", level=logging.DEBUG)

# Packet
# [header] [packet len except header (4)] [type (1)] [payload]
class Command():
    CMD_MSGALL, CMD_MSGTO, CMD_IDENT, CMD_AUTH, CMD_GETPK, CMD_WHO = range(6)

    MSG_REQKEY, MSG_SENDKEY, MSG_PUBKEYENC, MSG_SHAREKEYENC = range(4)

    def __init__(self, txq):
        self.queue = txq

    def packetize(self, command, payload=""):
        pktlen = len(payload) + 1
        return struct.pack("!cIB", '\xde', pktlen, command) + payload

    def msgall_enc(self, data):
        payload = struct.pack("!B", Command.MSG_SHAREKEYENC) + data
        packet = self.packetize(Command.CMD_MSGALL, payload)
        self.queue.put(packet)

    def msgall_reqkey(self):
        payload = struct.pack("!B", Command.MSG_REQKEY)
        packet = self.packetize(Command.CMD_MSGALL, payload)
        self.queue.put(packet)

    # [target name length (2)] [target name] [msgtype (1)] [data]
    def msgto_enc(self, recipient, data):
        payload = struct.pack("!H", len(recipient))
        payload += recipient.encode('utf-8')
        payload += struct.pack("!B", Command.MSG_PUBKEYENC)
        payload += data
        packet = self.packetize(Command.CMD_MSGTO, payload)
        self.queue.put(packet)

    def msgto_sendkey(self, recipient, data):
        payload = struct.pack("!H", len(recipient))
        payload += recipient.encode('utf-8')
        payload += struct.pack("!B", Command.MSG_SENDKEY)
        payload += data
        packet = self.packetize(Command.CMD_MSGTO, payload)
        self.queue.put(packet)

    def ident(self, name):
        packet = self.packetize(Command.CMD_IDENT, name.encode('utf-8'))
        self.queue.put(packet)

    def auth(self):
        # TODO
        pass

    def getpk(self, name):
        packet = self.packetize(Command.CMD_GETPK, name.encode('utf-8'))
        self.queue.put(packet)

    def who(self):
        packet = self.packetize(Command.CMD_WHO)
        self.queue.put(packet)


class Response():
    SVR_NOTICE, SVR_MSG, SVR_IDENT, SVR_AUTH_VALID, SVR_PK, SVR_WHO, DISCONNECTED = range(6, 13)

    def __init__(self, rtype):
        self.type = rtype


class TransmitThread(threading.Thread):
    def __init__(self, sock, queue):
        super(TransmitThread, self).__init__()
        self.sock = sock
        self.queue = queue
        self.enable = threading.Event()
        self.enable.set()

    def run(self):
        while self.enable.is_set():
            try:
                packet = self.queue.get(True, 0.125)
                sent_bytes = 0
                pktlen = len(packet)
                while sent_bytes < pktlen:
                    sent_bytes += self.sock.send(packet[sent_bytes:])
            except Queue.Empty:
                continue

    def stop(self):
        self.enable.clear()
        threading.Thread.join(self)


class ReceiveThread(threading.Thread):
    def __init__(self, sock, queue):
        super(ReceiveThread, self).__init__()
        self.sock = sock
        self.queue = queue
        self.enable = threading.Event()
        self.enable.set()

    def run(self):
        while self.enable.is_set():
            r, w, e = select.select([self.sock], [], [], 0.125)
            for sock in r:
                if sock == self.sock:
                    try:
                        read_bytes = 0
                        packet = ""
                        have_pktlen = False
                        # Receive data until we have length field from packet
                        while not have_pktlen:
                            tmp = sock.recv(4096)
                            if not tmp:
                                self.queue.put(Response(Response.DISCONNECTED))
                                self.enable.clear()
                                return
                            else:
                                packet += tmp
                                read_bytes += len(tmp)
                                header_index = tmp.find('\xde')
                                if header_index + 4 <= read_bytes:
                                    have_pktlen = True

                        # Drop bytes before header
                        packet = packet[header_index:]
                        pktlen = struct.unpack("!I", packet[1:5])[0]
                        read_bytes = len(packet) - 1
                        while read_bytes < pktlen:
                            tmp = sock.recv(4096)
                            if not tmp:
                                self.queue.put(Response(Response.DISCONNECTED))
                                self.enable.clear()
                                return
                            else:
                                packet.append(tmp)
                                read_bytes += len(tmp)
                        self.queue.put(packet)
                    except socket.error:
                        continue

    def stop(self):
        self.enable.clear()
        threading.Thread.join(self)


class DeadChatClient():
    MAX_NAME_LENGTH = 65535

    def __init__(self):

        self.name = None
        self.id_public_key = None
        self.id_private_key = None
        
        self.shared_key = None
        self.secretbox = None
        
        self.sock = None
        self.connected = False
        
        self.txq = Queue.Queue()
        self.rxq = Queue.Queue()

        self.tx_thread = None
        self.rx_thread = None

        self.send_cmd = Command(self.txq)

        self.enable = True
        self.display_size = None	# cols, rows tuple

        # Generate user interface
        self.chatlog = urwid.SimpleListWalker([])
        self.ui_listbox = urwid.ListBox(self.chatlog)
        self.ui_listbox.set_focus(len(self.chatlog)-1)
        self.ui_status = urwid.Text(" deadchat")
        # Use unicode for parameters here otherwise urwid won't
        # accept unicode user input
        self.ui_input = urwid.Edit(u">> ")
        ui_header = urwid.AttrMap(urwid.Text(""), 'header')
        ui_footer = urwid.Pile([
            urwid.AttrMap(self.ui_status, 'status'),
            self.ui_input,
        ], focus_item=self.ui_input)
        self.ui_frame = urwid.Frame(self.ui_listbox, \
                                    header = ui_header, \
                                    footer = ui_footer, \
                                    focus_part = "footer")

        self.display = urwid.curses_display.Screen()
        self.display.register_palette([
            ('header', 'black', 'dark cyan', 'standout'),
            ('status', 'black', 'dark cyan', 'standout')
        ])

        # Run main loop
        try:
            self.display.run_wrapper(self.run)
        except KeyboardInterrupt:
            if self.connected:
                self.user_disconnect()
            if self.tx_thread:
                self.tx_thread.stop()
            if self.rx_thread:
                self.rx_thread.stop()
            self.enable = False
            sys.exit()


    def run(self):
        self.display_size = self.display.get_cols_rows()
        self.display.set_input_timeouts(max_wait=0.125)

        self.config = ConfigParser.ConfigParser()
        self.load_config()

        while self.enable:
            try:
                packet = self.rxq.get(False)
                self.parse_rx(packet)
            except Queue.Empty:
                pass

            self.draw_screen()
            keys = self.display.get_input()

            for key in keys:
                if key == "window resize":
                    self.display_size = self.display.get_cols_rows()
                    continue
                else:
                    self.keypress(key)


    def keypress(self, key):
        if key == "enter":
            text = self.ui_input.get_edit_text()
            if text != "":
                self.ui_input.set_edit_text("")
                self.parse_user_input(text)
        elif key == "page down":
            self.ui_listbox.keypress(self.display_size, key)
        elif key == "page up":
            self.ui_listbox.keypress(self.display_size, key)
        else:
            self.ui_frame.keypress(self.display_size, key)


    def draw_screen(self):
        canvas = self.ui_frame.render(self.display_size, focus=True)
        self.display.draw_screen(self.display_size, canvas)


    def chatlog_print(self, text):
        self.chatlog.append(urwid.Text(text))
        self.ui_listbox.set_focus(self.ui_listbox.get_focus()[1] + 1, \
                                  coming_from='below')


    def parse_rx(self, rx):
        if isinstance(rx, Response):
            # DISCONNECTED
            if rx.type == Response.DISCONNECTED:
                self.user_disconnect()
        else:
            rxtype = struct.unpack("!B", rx[5])[0]

            # SVR_NOTICE
            if rxtype == Response.SVR_NOTICE:
                msg = rx[6:]
                self.chatlog_print(msg)

            # SVR_MSG
            elif rxtype == Response.SVR_MSG:
                namelen = struct.unpack("!H", rx[6:8])[0]
                name = rx[8:8+namelen]
                msgtype = struct.unpack("!B", rx[8+namelen])[0]

                if msgtype == Command.MSG_REQKEY:
                    self.svr_msg_requestkey(name)
                elif msgtype == Command.MSG_SENDKEY:
                    data = rx[8+namelen+1:]
                    self.svr_msg_sendkey(name, data)
                elif msgtype == Command.MSG_PUBKEYENC:
                    data = rx[8+namelen+1:]
                    self.svr_msg_pubkeyenc(name, data)
                elif msgtype == Command.MSG_SHAREKEYENC:
                    data = rx[8+namelen+1:]
                    self.svr_msg_sharekeyenc(name, data)

            # SVR_IDENT
            elif rxtype == Response.SVR_IDENT:
                # TODO
                pass

            # SVR_AUTH_VALID
            elif rxtype == Response.SVR_AUTH_VALID:
                # TODO
                self.chatlog_print("Identity confirmed")

            # SVR_PK
            elif rxtype == Response.SVR_PK:
                # TODO
                pass

            # SVR_WHO
            elif rxtype == Response.SVR_WHO:
                # TODO
                pass


    def parse_user_input(self, text):
        # /quit
        if string.find(text, "/quit") == 0:
            if self.connected:
                self.user_disconnect()
            self.enable = False

        # /createid <name>
        elif string.find(text, "/createid") == 0:
            idstr = text.split(" ")
            if len(idstr) > 1:
                self.user_createid(idstr[1])
            else:
                self.chatlog_print("Missing name")

        # /connect
        elif string.find(text, "/connect") == 0:
            if self.connected:
                self.chatlog_print("Already connected")
            else:
                if self.name:
                    self.user_connect("localhost", 4000)
                    self.send_cmd.ident(self.name)
                else:
                    self.chatlog_print("Missing name, set using /createid")

        # /disconnect
        elif string.find(text, "/disconnect") == 0:
            if self.connected:
                self.user_disconnect()
            else:
                self.chatlog_print("Not connected")

        # /genkey
        elif string.find(text, "/genkey") == 0:
            self.user_genkey()

        # /requestkey
        elif string.find(text, "/requestkey") == 0:
            if self.connected:
                self.user_requestkey()
            else:
                self.chatlog_print("Not connected")

        # /sendkey <name>
        elif string.find(text, "/sendkey") == 0:
            sendkeystr = text.split(" ")
            if len(sendkeystr) > 1:
                if self.connected:
                    self.user_sendkey(sendkeystr[1])
                else:
                    self.chatlog_print("Not connected")
            else:
                self.chatlog_print("Missing name")

        # not a command
        else:
            if self.connected:
                if not self.secretbox:
                    self.chatlog_print("Missing room key")
                else:
                    # TODO: fix this nonce so it has a unique prefix
                    nonce = nacl.utils.random(24)
                    enc = self.secretbox.encrypt(text.encode('utf-8'), nonce)
                    self.send_cmd.msgall_enc(enc)
            else:
                self.chatlog_print("Not connected")

    
    def load_config(self):
        self.config.read("deadchat.cfg")
        if self.config.has_section("id"):
            try:
                self.id_private_key = nacl.public.PrivateKey(base64.b64decode(self.config.get("id", "id_private_key")))
                self.id_public_key = nacl.public.PublicKey(base64.b64decode(self.config.get("id", "id_public_key")))
                self.name = self.config.get("id", "name")
                self.chatlog_print("Name set to " + self.name)
            except:
                pass

        if self.config.has_section("room"):
            try:
                self.shared_key = base64.b64decode(self.config.get("room", "room_key"))
                self.secretbox = nacl.secret.SecretBox(self.shared_key)
            except:
                pass


    def user_createid(self, name):
        if len(name) > DeadChatClient.MAX_NAME_LENGTH:
            self.chatlog_print("That name is too long")
            return

        self.name = name
        key = nacl.public.PrivateKey.generate()
        self.id_private_key = key
        self.id_public_key = key.public_key

        self.chatlog_print("Created identity " + self.name)
        if not self.config.has_section("id"):
            self.config.add_section("id")
        self.config.set("id", "id_private_key", base64.b64encode(self.id_private_key.encode()))
        self.config.set("id", "id_public_key", base64.b64encode(self.id_public_key.encode()))
        self.config.set("id", "name", self.name)
        with open("deadchat.cfg", "wb") as configfile:
            self.config.write(configfile)

        
    def user_connect(self, host, port):
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.connect((host, port))

            self.tx_thread = TransmitThread(self.sock, self.txq)
            self.tx_thread.start()

            self.rx_thread = ReceiveThread(self.sock, self.rxq)
            self.rx_thread.start()

            self.connected = True
            self.chatlog_print("Connected to " + host)

        except Exception as e:
            self.chatlog_print("Unable to connect to " + host + \
                               " on port " + str(port))
        

    def user_disconnect(self):
        self.connected = False
        self.rx_thread.stop()
        self.tx_thread.stop()
        self.sock.close()
        self.chatlog_print("Disconnected from server")


    def user_genkey(self):
        self.shared_key = nacl.utils.random(32)
        self.secretbox = nacl.secret.SecretBox(self.shared_key)
        if not self.config.has_section("room"):
            self.config.add_section("room")
        self.config.set("room", "room_key", base64.b64encode(self.shared_key))
        with open("deadchat.cfg", "wb") as configfile:
            self.config.write(configfile)
        self.chatlog_print("Key generated")


    def user_requestkey(self):
        self.send_cmd.msgall_reqkey()


    def user_sendkey(self, name):
        # TODO: fix this so it encrypts key with recipient's public key first
        key = self.shared_key
        self.send_cmd.msgto_sendkey(name, key)


    def svr_msg_requestkey(self, sender):
        self.chatlog_print(sender + " requests the key")


    def svr_msg_sendkey(self, sender, data):
        # TODO: decrypt shared key using private key
        self.shared_key = data
        self.secretbox = nacl.secret.SecretBox(self.shared_key)
        if not self.config.has_section("room"):
            self.config.add_section("room")
        self.config.set("room", "room_key", base64.b64encode(self.shared_key))
        self.chatlog_print(sender + " has sent you the key")


    def svr_msg_pubkeyenc(self, sender, data):
        # TODO
        pass


    def svr_msg_sharekeyenc(self, sender, data):
        nonce = data[0:24]
        enc = data[24:]
        if self.secretbox:
            try:
                msg = self.secretbox.decrypt(enc, nonce)
                self.chatlog_print("<" + sender + "> " + msg)
                return
            except nacl.exceptions.CryptoError:
                pass
        self.chatlog_print("<" + sender + "> ( encrypted )")

        
def main():
    DeadChatClient().run()
    
if __name__ == "__main__":
    main()
    
