"""VMOS Android UI 控制；错误消息不包含响应正文或输入文本。"""
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests


class ControlAPI:
    def __init__(self, host, db_id):
        if not re.fullmatch(r"EDGE[A-Za-z0-9]+", db_id):
            raise ValueError("云机ID无效")
        self.base = host.rstrip("/") + "/android_api/v2/" + db_id
        self.session = requests.Session()
        self.session.trust_env = False

    def call(self, path, body=None):
        response = self.session.get(self.base + "/" + path, timeout=(5, 20)) if body is None else self.session.post(
            self.base + "/" + path, json=body, timeout=(5, 20))
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or str(payload.get("code")) != "200":
            raise ValueError("UI接口调用失败: " + path)
        return payload.get("data")

    def dump(self):
        for attempt in range(3):
            xml = self.call("accessibility/dump")
            if isinstance(xml, str):
                try:
                    return ET.fromstring(xml)
                except ET.ParseError:
                    pass
            if attempt < 2:
                time.sleep(1)
        raise ValueError("页面节点XML在等待后仍无效") from None

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
        self.call("accessibility/node", {"selector": selector, "action": "set_text",
                                          "action_params": {"text": text}})
        deadline = time.monotonic() + 3
        while True:
            tree = self.dump()
            fields = [n for n in tree.iter("node") if n.get("class") == "android.widget.EditText"
                      and (not rid or n.get("resource-id") == rid)]
            if len(fields) == 1:
                actual = fields[0].get("text", "")
                masked = (node.get("password") == "true" and len(actual) == len(text) and bool(actual)
                          and all(char in "*•●‧" for char in actual))
                if actual == text or masked:
                    return
            if time.monotonic() >= deadline:
                raise ValueError("输入文本未确认生效")
            time.sleep(0.25)

    def screenshot(self, path):
        response = self.session.get(self.base + "/screenshot/format", params={"format": "png"}, timeout=(5, 20))
        response.raise_for_status()
        if not response.headers.get("Content-Type", "").startswith("image/"):
            raise ValueError("截图接口未返回图片")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(response.content)
