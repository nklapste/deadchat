#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""FTP client that wraps all sent/given files with an encryption"""

import base64
import hashlib
import io
from ftplib import FTP
from logging import getLogger

import nacl.secret
import nacl.utils

__log__ = getLogger(__name__)


class EncryptedFTPClient(FTP):
    """Simple wrapper class of :class:`ftplib.FTP` the encrypts both a
    file's name and contents before sending it to the remote FTP server"""

    def __init__(self, secretbox: nacl.secret.SecretBox, **kwargs):
        self.secretbox = secretbox
        FTP.__init__(self, **kwargs)

    def ftp_encrypt(self, string: str) -> str:
        """Encrypt a string for usage in the FTP server using the shared room
        key obtained from the deadchat client"""
        sha = hashlib.new("sha256")
        sha.update(string.encode("utf-8"))
        computed_hash = sha.hexdigest()
        base_content = "{}{}".format(computed_hash, string)
        enc_string = self.secretbox.encrypt(base_content.encode('utf-8'))
        safe_enc_string = base64.urlsafe_b64encode(enc_string).decode("utf-8")
        return safe_enc_string.strip()

    def ftp_decrypt(self, safe_enc_string: str) -> str:
        """Decrypt a string form the FTP server using the shared room
        key obtained from the deadchat client"""
        try:
            enc_string = base64.urlsafe_b64decode(safe_enc_string)
            nonce = enc_string[0:nacl.secret.SecretBox.NONCE_SIZE]
            enc = enc_string[nacl.secret.SecretBox.NONCE_SIZE:]
            base_content = self.secretbox.decrypt(enc, nonce)
            given_hash = base_content[0:64].decode("utf-8")
            string = base_content[64:]
            sha = hashlib.new("sha256")
            sha.update(string.decode("utf8").encode("utf-8"))
            computed_hash = sha.hexdigest()
            if computed_hash == given_hash:
                __log__.info("decrypted FTP message: {}".format(string))
                return string.decode("utf-8")
            __log__.error(
                    "checksum error given hash:{} computed hash: {}".format(
                        given_hash, computed_hash))
        except Exception:
            __log__.exception(
                "failed to decrypt FTP message: {}".format(safe_enc_string))
        __log__.warning("detected unauthorized modification of "
                        "remote filesystem")
        return safe_enc_string

    def get_pwd_encrypted_path(self, path: str):
        for enc_filename in super().nlst():
            dec_filename = self.ftp_decrypt(enc_filename)
            if path == dec_filename:
                __log__.info("found match for name: {} -> {}".format(
                    path, enc_filename))
                return enc_filename
        raise FileNotFoundError("path: {} does not exist in PWD".format(path))

    def nlst(self, dirname: str, *args):
        if dirname == "" or dirname is None:
            enc_dirs = super().nlst(*args)
        else:
            enc_dirs = super().nlst(self.ftp_encrypt(dirname), *args)
        return list(map(self.ftp_decrypt, enc_dirs))

    def mkd(self, dirname: str):
        return super().mkd(self.ftp_encrypt(dirname))

    def rmd(self, dirname: str):
        return super().rmd(self.get_pwd_encrypted_path(dirname))

    def cwd(self, dirname: str):
        if dirname == "..":  # TODO: more elegant solution
            return super().cwd(dirname)
        return super().cwd(self.get_pwd_encrypted_path(dirname))

    def delete(self, filename: str):
        return super().delete(self.get_pwd_encrypted_path(filename))

    def storefile(self, filename: str, content: str):
        try:
            enc_filename = self.get_pwd_encrypted_path(filename)
        except FileNotFoundError:
            enc_filename = self.ftp_encrypt(filename)
        cmd = "STOR {}".format(enc_filename)

        buf = io.BytesIO(self.ftp_encrypt(content).encode("utf8"))
        return super().storbinary(cmd, buf)

    def readfile(self, filename: str):
        enc_filename = self.get_pwd_encrypted_path(filename)
        cmd = "RETR {}".format(enc_filename)
        # TODO: improve i/o usage
        f = open("tempcrypt", "w")

        def callback(data: bytes):
            f.write(data.decode("utf-8"))

        super().retrbinary(cmd, callback)
        f.close()
        f = open("tempcrypt", "rb")
        content = self.ftp_decrypt(f.read().decode("utf-8"))
        return content
