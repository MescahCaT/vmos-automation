"""VLESS TCP 中转的本机探测；临时核心只监听回环地址，退出后删除认证配置。"""
import json
import re
import socket
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "tools" / "mihomo" / "mihomo.exe"


def relay_node(proxy):
    relay = proxy.get("transit")
    if (not isinstance(relay, dict) or relay.get("protocol") != "vless"
            or relay.get("network", "tcp") != "tcp" or relay.get("tls", False) is not False
            or relay.get("enabled", True) is not True):
        raise ValueError("中转仅支持已启用的 VLESS TCP、security=none 节点")
    host, port = relay.get("host"), relay.get("port")
    if not isinstance(host, str) or not host or re.search(r"[\s/@?#]", host):
        raise ValueError("中转主机地址无效")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("中转端口无效")
    try:
        account = str(uuid.UUID(relay.get("uuid", "")))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("中转 UUID 无效") from None
    return {"ip": host, "port": port, "proxyName": "vless", "uuid": account,
            "network": "tcp", "tls": False, "udp": False}


def chain_config(proxy, port):
    relay = relay_node(proxy)
    if proxy.get("protocol") != "socks5":
        raise ValueError("VLESS 中转的出口必须为 SOCKS5")
    return {"mixed-port": port, "bind-address": "127.0.0.1", "allow-lan": False,
            "mode": "rule", "log-level": "silent", "ipv6": False, "dns": {"enable": False},
            "proxies": [
                {"name": "relay", "type": "vless", "server": relay["ip"], "port": relay["port"],
                 "uuid": relay["uuid"], "network": "tcp", "tls": False, "udp": False},
                {"name": "exit", "type": "socks5", "server": proxy["host"], "port": proxy["port"],
                 "username": proxy.get("username", ""), "password": proxy.get("password", ""),
                 "udp": True, "dialer-proxy": "relay"}], "rules": ["MATCH,exit"]}


@contextmanager
def local_proxy(proxy):
    if not CORE.is_file():
        raise ValueError("中转探测需要项目 tools/mihomo/mihomo.exe")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    config = chain_config(proxy, port)
    with tempfile.TemporaryDirectory(prefix="vmos-chain-") as temp:
        path = Path(temp) / "config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        process = subprocess.Popen([str(CORE), "-d", temp, "-f", str(path)],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            deadline = time.monotonic() + 8
            while True:
                if process.poll() is not None:
                    raise ValueError("中转探测核心启动失败")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                        break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise ValueError("中转探测监听等待超时") from None
                    time.sleep(0.2)
            yield {"id": proxy["id"], "protocol": "socks5", "host": "127.0.0.1",
                   "port": port, "enabled": True}
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
