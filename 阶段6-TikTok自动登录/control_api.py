"""VMOS Android UI 控制；错误消息不包含响应正文或输入文本。"""
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "阶段1-环境准备与配置"))
from check_vmos_connection import poll_readback, request_timeout


class ControlAPI:
    def __init__(self, host, db_id, proxy=None):
        if not re.fullmatch(r"EDGE[A-Za-z0-9]+", db_id):
            raise ValueError("云机ID无效")
        self.host, self.db_id, self.proxy = host, db_id, proxy
        self.session = requests.Session()
        self.session.trust_env = False

    @property
    def base(self):
        if self.proxy is None:
            sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "阶段3-网络代理配置"))
            from setup_proxy import ProxySetup
            self.proxy = ProxySetup(self.host)
        return self.proxy.control_base(self.db_id)

    def call(self, path, body=None):
        response = self.session.get(self.base + "/" + path, timeout=request_timeout(20)) if body is None else self.session.post(
            self.base + "/" + path, json=body, timeout=request_timeout(20))
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or str(payload.get("code")) != "200":
            raise ValueError("UI接口调用失败: " + path)
        return payload.get("data")

    def dump(self):
        def read():
            xml = self.call("accessibility/dump")
            if isinstance(xml, str):
                try:
                    return ET.fromstring(xml)
                except ET.ParseError:
                    pass
            raise ValueError("页面节点XML无效") from None
        return poll_readback(read, lambda message: None, "页面节点查询", timeout=20, interval=1)

    def click(self, node):
        bounds = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
        if not bounds or node.get("enabled") == "false":
            raise ValueError("页面控件不可点击")
        left, top, right, bottom = map(int, bounds.groups())
        if right <= left or bottom <= top:
            raise ValueError("页面控件范围无效")
        return self.call("input/click", {"x": (left + right) // 2, "y": (top + bottom) // 2})

    def text(self, text):
        self.call("input/text", {"text": text})

    def fill(self, node, text):
        """优先原生 set_text；回查输入确实生效，API code=200 本身不足以确认。"""
        rid = node.get("resource-id")
        selector = {"resource_id": rid} if rid else {"class": "android.widget.EditText"}
        if not rid:
            bounds = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
            if not bounds:
                raise ValueError("输入控件缺少唯一位置，未输入")
            left, top, right, bottom = map(int, bounds.groups())
            selector.update(center_x=(left + right) // 2, center_y=(top + bottom) // 2)
        try:
            self.call("accessibility/node", {"selector": selector, "action": "set_text",
                                              "action_params": {"text": text}})
        except requests.HTTPError as error:
            if error.response is not None and error.response.status_code in (404, 405):
                try:
                    self.native_fill(node, text)
                except (requests.RequestException, ValueError):
                    pass  # 原生输入响应不明确也只读回查，不追加输入。
        except (requests.RequestException, ValueError):
            pass  # 写响应不明确时只读核对，不重发输入。
        def confirm():
            tree = self.dump()
            fields = [n for n in tree.iter("node") if n.get("class") == "android.widget.EditText"
                      and (n.get("resource-id") == rid if rid else n.get("password", "false") == node.get("password", "false"))]
            if len(fields) == 1:
                actual = fields[0].get("text", "")
                masked = (node.get("password") == "true" and len(actual) == len(text) and bool(actual)
                          and all(char in "*•●‧" for char in actual))
                if actual == text or masked:
                    return
            raise ValueError("输入文本未确认生效")
        poll_readback(confirm, lambda message: None, "输入文本回查", timeout=15, interval=0.5)

    def native_fill(self, node, text):
        """仅404/405明确不支持set_text时使用；超时不追加另一条输入。"""
        rid = node.get("resource-id")
        fields = [n for n in self.dump().iter("node") if n.get("class") == "android.widget.EditText"
                  and (n.get("resource-id") == rid if rid else n.get("password", "false") == node.get("password", "false"))]
        if len(fields) != 1:
            raise ValueError("原生输入目标不唯一，未输入")
        current = fields[0]
        if current.get("text") == text:
            return
        self.click(current)
        self.call("input/keyevent", {"key_codes": [113, 29]})
        self.call("input/keyevent", {"key_code": 67})
        focused = [n for n in self.dump().iter("node") if n.get("class") == "android.widget.EditText" and n.get("focused") == "true"]
        hints = {"", "email", "email or username", "email / username", "username", "password", "enter password", "邮箱", "邮箱/用户名", "密码", "输入密码"}
        if (len(focused) != 1 or (rid and focused[0].get("resource-id") != rid)
                or focused[0].get("password", "false") != node.get("password", "false")
                or focused[0].get("text", "").casefold() not in hints):
            raise ValueError("原生输入焦点或清空结果未确认，未输入")
        self.text(text)

    def screenshot(self, path):
        response = self.session.get(self.base + "/screenshot/format", params={"format": "png"}, timeout=request_timeout(20))
        response.raise_for_status()
        if not response.headers.get("Content-Type", "").startswith("image/"):
            raise ValueError("截图接口未返回图片")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(response.content)
