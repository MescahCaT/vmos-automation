"""有限轮询网页验证码；续跑基线仅存Windows用户加密状态，不记录明文。"""
import re
import base64
import ctypes
from ctypes import wintypes
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "阶段1-环境准备与配置"))
from check_vmos_connection import extract_mail_code, poll_readback, request_timeout


def protected_session(data=None, token=None):
    """用当前Windows用户的DPAPI保护续跑状态；不保存明文基线验证码。"""
    if os.name != "nt":
        raise ValueError("安全验证码续跑需要当前Windows用户的DPAPI")
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    try:
        raw = base64.b64decode(token, validate=True) if token is not None else json.dumps(data).encode("utf-8")
        buffer = ctypes.create_string_buffer(raw)
        source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
        target = Blob()
        library = ctypes.WinDLL("crypt32", use_last_error=True)
        function = library.CryptUnprotectData if token is not None else library.CryptProtectData
        function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        function.restype = wintypes.BOOL
        if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
            raise ValueError("无法验证或保护登录续跑状态")
        try:
            result = ctypes.string_at(target.data, target.size)
        finally:
            ctypes.memset(target.data, 0, target.size)
            free = ctypes.WinDLL("kernel32").LocalFree
            free.argtypes, free.restype = [ctypes.c_void_p], ctypes.c_void_p
            free(target.data)
        return json.loads(result) if token is not None else base64.b64encode(result).decode("ascii")
    except (ValueError, TypeError, OSError) as error:
        raise ValueError("登录续跑状态无法由当前Windows用户读取或保存") from None


class EmailVerifier:
    def __init__(self, config, email):
        url = urlsplit(config["endpoint"])
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
            raise ValueError("取码接口地址无效")
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            raise ValueError("邮箱地址无效")
        self.endpoint, self.timeout, self.email = config["endpoint"], config["timeout"], email
        if type(self.timeout) is not int or not 1 <= self.timeout <= 120:
            raise ValueError("取码接口超时必须为1至120秒")
        self.session = requests.Session()
        self.session.trust_env = False

    def fetch(self):
        try:
            response = self.session.get(self.endpoint, params={"username": self.email},
                                        timeout=request_timeout(self.timeout), allow_redirects=False)
            if response.status_code != 200:
                raise ValueError("取码接口HTTP状态不是200")
            content_type = response.headers.get("Content-Type", "").lower()
            if "json" in content_type:
                payload = response.json()
            else:
                payload = response.text
            return extract_mail_code(payload)
        except (requests.RequestException, ValueError, TypeError):
            raise ValueError("取码接口请求失败或数据格式无效") from None

    def wait_new(self, previous, timeout=120, interval=3, log=lambda message: None):
        def check():
            code = self.fetch()
            if code is None:
                raise ValueError("取码接口未返回可识别的六位验证码")
            if code == previous:
                raise ValueError("取码接口仍返回发码前已有验证码，尚未取得新码")
            return code
        return poll_readback(check, log, "新邮箱验证码查询", timeout=max(0, timeout),
                             interval=max(interval, timeout / 8) if timeout else interval)
