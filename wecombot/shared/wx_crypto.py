"""
企业微信消息加解密工具
基于官方 WXBizMsgCrypt 协议实现
"""
import base64
import hashlib
import struct
import time
import random
import string
from Crypto.Cipher import AES


class WXBizMsgCrypt:
    """企业微信回调消息的加解密处理"""

    def __init__(self, token, encoding_aes_key, corp_id):
        self.token = token
        self.corp_id = corp_id
        # EncodingAESKey 标准为 43 位，补 "=" 后 Base64 解码得到 32 字节
        key = encoding_aes_key.strip()
        if len(key) != 43:
            raise ValueError(
                f"EncodingAESKey 长度应为 43 位，当前为 {len(key)} 位: [{key}]"
                f"\n请检查企业微信后台 → 自建应用 → 接收消息 → EncodingAESKey 是否完整复制"
            )
        self.aes_key = base64.b64decode(key + "=")

    def _pkcs7_pad(self, data: bytes, block_size: int = 32) -> bytes:
        """PKCS7 填充"""
        pad_len = block_size - (len(data) % block_size)
        return data + bytes([pad_len] * pad_len)

    def _pkcs7_unpad(self, data: bytes) -> bytes:
        """PKCS7 去填充"""
        pad_len = data[-1]
        if pad_len < 1 or pad_len > 32:
            raise ValueError("PKCS7 填充校验失败")
        return data[:-pad_len]

    def _get_random_str(self, length: int = 16) -> str:
        return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

    def _aes_encrypt(self, plaintext: bytes) -> bytes:
        """AES-256-CBC 加密"""
        cipher = AES.new(self.aes_key, AES.MODE_CBC, self.aes_key[:16])
        return cipher.encrypt(self._pkcs7_pad(plaintext))

    def _aes_decrypt(self, ciphertext: bytes) -> bytes:
        """AES-256-CBC 解密"""
        cipher = AES.new(self.aes_key, AES.MODE_CBC, self.aes_key[:16])
        return self._pkcs7_unpad(cipher.decrypt(ciphertext))

    def _signature(self, timestamp, nonce, encrypt_msg=""):
        """生成 SHA1 签名"""
        params = sorted([self.token, str(timestamp), str(nonce), encrypt_msg])
        sha1 = hashlib.sha1()
        sha1.update(''.join(params).encode())
        return sha1.hexdigest()

    def verify_url(self, msg_signature, timestamp, nonce, echostr):
        """
        GET 请求时验证回调 URL
        返回解密后的 echostr，验证失败返回 None
        """
        signature = self._signature(timestamp, nonce, echostr)
        if signature != msg_signature:
            return None
        try:
            ciphertext = base64.b64decode(echostr)
            decrypted = self._aes_decrypt(ciphertext)
            # 去除 16 字节随机前缀 + 4 字节消息长度 + corp_id
            content = decrypted[16:]
            msg_len = struct.unpack("!I", content[:4])[0]
            plaintext = content[4:4 + msg_len].decode("utf-8")
            return plaintext
        except Exception:
            return None

    def decrypt_msg(self, msg_signature, timestamp, nonce, encrypt_body):
        """
        POST 请求时解密消息体
        返回 (err_code, xml_string)，err_code=0 表示成功
        """
        # 从 XML 中提取 Encrypt 字段
        import re
        match = re.search(r'<Encrypt><!\[CDATA\[(.*?)\]\]></Encrypt>', encrypt_body)
        if not match:
            return -40001, "消息体中没有 Encrypt 字段"
        encrypt_msg = match.group(1)

        # 验签
        signature = self._signature(timestamp, nonce, encrypt_msg)
        if signature != msg_signature:
            return -40001, "签名验证失败"

        # 解密
        try:
            ciphertext = base64.b64decode(encrypt_msg)
            decrypted = self._aes_decrypt(ciphertext)
            content = decrypted[16:]
            msg_len = struct.unpack("!I", content[:4])[0]
            plaintext = content[4:4 + msg_len].decode("utf-8")
            # 校验 corp_id
            corp_id_from_msg = content[4 + msg_len:].decode("utf-8")
            if corp_id_from_msg != self.corp_id:
                return -40004, "corp_id 不匹配"
            return 0, plaintext
        except Exception as e:
            return -40002, f"解密失败: {str(e)}"

    def encrypt_msg(self, reply_xml, timestamp=None, nonce=None):
        """
        加密回复消息
        返回 (err_code, encrypted_xml)
        """
        if timestamp is None:
            timestamp = str(int(time.time()))
        if nonce is None:
            nonce = self._get_random_str(16)

        # 组装明文: 16字节随机 + 4字节长度(网络序) + 消息 + corp_id
        random_bytes = self._get_random_str(16).encode()
        msg_bytes = reply_xml.encode("utf-8")
        msg_len = struct.pack("!I", len(msg_bytes))
        plaintext = random_bytes + msg_len + msg_bytes + self.corp_id.encode("utf-8")

        # 加密
        ciphertext = self._aes_encrypt(plaintext)
        encrypt_msg = base64.b64encode(ciphertext).decode()

        # 签名
        signature = self._signature(timestamp, nonce, encrypt_msg)

        # 组装返回 XML
        xml = (
            f"<xml>"
            f"<Encrypt><![CDATA[{encrypt_msg}]]></Encrypt>"
            f"<MsgSignature><![CDATA[{signature}]]></MsgSignature>"
            f"<TimeStamp>{timestamp}</TimeStamp>"
            f"<Nonce><![CDATA[{nonce}]]></Nonce>"
            f"</xml>"
        )
        return 0, xml
