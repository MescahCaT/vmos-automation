"""轮询网页验证码；验证码仅驻留内存，不进入日志、报告或错误消息。"""
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "阶段1-环境准备与配置"))
from check_vmos_connection import extract_mail_code


class EmailVerifier:
    def __init__(self, config, email):
        url = urlsplit(config["endpoint"])
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
            raise ValueError("取码接口地址无效")
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            raise ValueError("邮箱地址无效")
        self.endpoint, self.timeout, self.email = config["endpoint"], config["timeout"], email
        self.session = requests.Session()
        self.session.trust_env = False

    def fetch(self):
        try:
            response = self.session.get(self.endpoint, params={"username": self.email},
                                        timeout=(5, self.timeout), allow_redirects=False)
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
        deadline = time.monotonic() + timeout
        while True:
            try:
                code = self.fetch()
                if code is not None and code != previous:
                    return code
            except ValueError:
                pass
            if time.monotonic() >= deadline:
                raise ValueError("等待新邮箱验证码超时；未使用历史验证码")
            log("等待新邮箱验证码")
            time.sleep(min(interval, max(0, deadline - time.monotonic())))
