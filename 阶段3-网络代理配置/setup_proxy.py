"""指定单台云机绑定代理、同步地区，并从云机内部验证实际出口和网页。"""
import argparse
import ipaddress
import json
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "阶段1-环境准备与配置"))
sys.path.insert(0, str(ROOT / "阶段5-TikTok应用安装"))
sys.path.insert(0, str(ROOT / "阶段4-系统设置与环境配置"))
from check_vmos_connection import check_proxy, read_json, save_json
from relay_proxy import relay_node
from install_tiktok import TikTokInstaller, device_ids
from setup_system import SystemSetup, region_settings


def timestamp():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def proxy_body(proxy):
    """结构化认证仅发送给 VMOS，不写入报告。"""
    if proxy.get("protocol") not in ("socks5", "http") or proxy.get("enabled", True) is not True:
        raise ValueError("代理必须是已启用的 SOCKS5 或 HTTP")
    host, port, name = proxy.get("host"), proxy.get("port"), proxy.get("id")
    if not isinstance(host, str) or not host or re.search(r"[\s/@?#]", host):
        raise ValueError("代理主机地址无效")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("代理端口必须为1至65535的整数")
    if not isinstance(name, str) or not name or re.search(r"[\r\n\0]", name):
        raise ValueError("代理ID无效")
    extra = {}
    for key in ("username", "password"):
        value = proxy.get(key, "")
        if not isinstance(value, str) or re.search(r"[\r\n\0]", value):
            raise ValueError("代理认证信息包含非法字符")
        if value:
            extra[key] = value
    body = {"name": name, "proxyType": proxy["protocol"], "server": host, "port": port,
            "extra": extra, "udp": True, "udpDisabled": False}
    if proxy.get("transit") is not None:
        relay = relay_node(proxy)
        if proxy["protocol"] != "socks5":
            raise ValueError("VLESS 中转的出口必须为 SOCKS5")
        body["transit"] = {"type": "vless", "server": relay["ip"], "port": relay["port"],
                           "extra": {"uuid": relay["uuid"], "network": "tcp", "tls": False}}
    return body


def legacy_proxy_body(proxy):
    body = {"ip": proxy["host"], "port": proxy["port"], "proxyName": proxy["protocol"],
            "account": proxy.get("username", ""), "password": proxy.get("password", ""),
            "udpDisabled": False}
    if proxy.get("transit") is not None:
        body.update(proxyType="proxy", nodes=[relay_node(proxy), dict(body)],
                    dnsServers=["223.5.5.5"], dnsOverProxyDisabled=False)
    return body


def status_fields(payload):
    """只保留状态字段，禁止保存 VMOS 回显的认证配置和消息。"""
    data = payload.get("data")
    if not isinstance(data, dict) or str(payload.get("code")) not in ("0", "200"):
        raise ValueError("代理状态业务响应无效")
    result = {"code": payload.get("code")}
    for key in ("enabled", "healthy", "accepted", "engineType", "rev", "applied_rev"):
        if isinstance(data.get(key), (bool, int)):
            result[key] = data[key]
    # 部分主机将 cbs_proxy_set 路由到 legacy 引擎，状态仍从 proxy_config 返回。
    config = data.get("proxy_config")
    if isinstance(config, dict) and "enabled" not in result:
        if "enabled" in config:
            result["enabled"] = config["enabled"] is True or str(config["enabled"]).lower() == "true"
        elif "enable" in config:
            result["enabled"] = config["enable"] is True or str(config["enable"]).lower() == "true"
    return result


def proxy_matches(config, proxy):
    if not isinstance(config, dict):
        return False
    nodes = config.get("nodes")
    if proxy.get("transit") is not None:
        if not isinstance(nodes, list) or len(nodes) != 2 or any(not isinstance(n, dict) for n in nodes):
            return False
        expected = relay_node(proxy)
        relay = nodes[0]
        if (relay.get("ip", relay.get("server")) != expected["ip"]
                or str(relay.get("port")) != str(expected["port"])
                or relay.get("proxyType", relay.get("proxyName")) != "vless"
                or relay.get("uuid") != expected["uuid"]
                or relay.get("network", "tcp") != "tcp" or relay.get("tls", False) is not False):
            return False
        config = nodes[1]
    elif nodes is not None:
        if not isinstance(nodes, list) or len(nodes) != 1 or not isinstance(nodes[0], dict):
            return False
        config = nodes[0]
    extra = config.get("extra", {})
    if not isinstance(extra, dict):
        return False
    return (config.get("ip", config.get("server")) == proxy["host"]
            and str(config.get("port")) == str(proxy["port"])
            and config.get("proxyType", config.get("proxyName")) == proxy["protocol"]
            and config.get("account", config.get("username", extra.get("username", ""))) == proxy.get("username", "")
            and config.get("password", extra.get("password", "")) == proxy.get("password", ""))


def probe_command(url, timeout, ignore_env_proxy=True):
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password or re.search(r"[\r\n\0]", url):
        raise ValueError("出网探测URL无效")
    # 出口核对忽略环境代理；网页探测使用 Android 用户的默认请求路径。
    bypass = " --noproxy '*'" if ignore_env_proxy else ""
    return f"/bin/curl --disable --silent --show-error --fail{bypass} --connect-timeout 8 --max-time {timeout} {shlex.quote(url)}"


class ProxySetup:
    def __init__(self, host, output_dir=None, mapping_path=None):
        self.host = host.rstrip("/")
        self.output_dir = Path(output_dir) if output_dir else Path(__file__).resolve().parent
        self.mapping_path = Path(mapping_path) if mapping_path else ROOT / "数据" / "device_mapping.json"
        self.device = TikTokInstaller(self.host)
        self.session = self.device.session
        self.device.log = self.log
        self.logs = []

    def log(self, message):
        line = f"[{timestamp()}] {message}"
        self.logs.append(line)
        print(line, flush=True)

    def api(self, method, action, db_id, body=None, timeout=30):
        response = self.session.request(method, f"{self.host}/android_api/v1/{action}/{db_id}",
                                        json=body, timeout=(5, timeout))
        try:
            payload = response.json()
        except ValueError:
            raise ValueError(f"{action} 返回非JSON（HTTP {response.status_code}）") from None
        if not isinstance(payload, dict):
            raise ValueError(f"{action} 响应格式无效")
        return response.status_code, payload

    def stop_proxy(self, db_id):
        http, payload = self.api("GET", "proxy_stop", db_id)
        if 200 <= http < 300 and str(payload.get("code")) == "200":
            return
        # 新实例可能没有旧代理可停止。只有明确回查为空或已关闭，才允许继续首次绑定。
        query_http, current = self.api("GET", "proxy_get", db_id)
        data = current.get("data")
        if (200 <= query_http < 300 and str(current.get("code")) == "200"
                and isinstance(data, dict) and "proxy_config" in data):
            config = data["proxy_config"]
            disabled = isinstance(config, dict) and str(config.get("enabled", config.get("enable"))).lower() == "false"
            if config is None or config == {} or disabled:
                self.log("回查确认没有已启用的旧代理，继续绑定")
                return
        raise ValueError(f"旧代理停止未确认（HTTP {http}，业务码 {payload.get('code')}），代理回查也未确认已关闭")

    def apply(self, db_id, body, proxy=None, engine_type=2):
        if proxy is not None and proxy.get("transit") is not None and engine_type != 0:
            engine_type = 2
        if engine_type in (0, 1):
            if proxy is None:
                raise ValueError("legacy代理配置缺少代理资源")
            # legacy 引擎需要先停旧绑定再重挂；否则部分主机会保留记录但不恢复透明路由。
            self.stop_proxy(db_id)
            legacy_body = legacy_proxy_body(proxy)
            if engine_type == 0:
                legacy_body["engineType"] = 0
            http, payload = self.api("POST", "proxy_set", db_id, legacy_body)
            safe = {"http_status": http, "code": payload.get("code"), "accepted": str(payload.get("code")) == "200", "engine": "legacy"}
            if not 200 <= http < 300 or not safe["accepted"]:
                raise ValueError(f"legacy代理配置被拒绝（HTTP {http}，业务码 {safe['code']}）")
            return safe
        http, payload = self.api("POST", "cbs_proxy_set", db_id, body)
        # 仅明确的 legacy 冲突允许停旧代理、重试一次；超时绝不重发。
        if (http == 409 or str(payload.get("code")) == "409") and "legacy proxy in use" in str(payload.get("msg", "")).lower():
            self.log("检测到旧代理引擎冲突，停止该云机旧代理后切换")
            self.stop_proxy(db_id)
            http, payload = self.api("POST", "cbs_proxy_set", db_id, body)
        safe = {"http_status": http, "code": payload.get("code")}
        data = payload.get("data", {})
        accepted = data.get("accepted") if isinstance(data, dict) else None
        safe["accepted"] = accepted is True or (accepted is None and str(payload.get("code")) == "200")
        if not 200 <= http < 300 or str(payload.get("code")) not in ("0", "200") or not safe["accepted"]:
            raise ValueError(f"代理配置被拒绝（HTTP {http}，业务码 {safe['code']}）")
        return safe

    def current_proxy(self, db_id, proxy, wait_time=60):
        deadline = time.monotonic() + wait_time
        while True:
            http, payload = self.api("GET", "cbs_proxy_get", db_id)
            if http == 409 or str(payload.get("code")) == "409":
                http, payload = self.api("GET", "proxy_get", db_id)
            data = payload.get("data")
            if not 200 <= http < 300 or not isinstance(data, dict):
                raise ValueError("无法查询云机代理引擎")
            safe = status_fields(payload)
            # ROM 先就绪时，代理接口仍可能只返回 code=0 的主机元数据。
            # 必须读到实际代理配置（允许明确为空）或 CBS 启用状态，才可开始写入。
            if "proxy_config" in data or "enabled" in safe:
                value = data.get("engineType", 1 if isinstance(data.get("proxy_config"), dict) else 0)
                if isinstance(value, bool) or value not in (0, 1, 2):
                    raise ValueError("云机代理引擎字段无效")
                safe.update(engineType=value, matches=proxy_matches(data.get("proxy_config"), proxy))
                return safe
            if time.monotonic() >= deadline:
                raise ValueError("ROM已就绪但代理服务尚未就绪，未修改代理")
            self.log("等待云机代理服务就绪")
            time.sleep(min(5, max(0, deadline - time.monotonic())))

    def cloud_json(self, db_id, url, timeout):
        http, payload = self.api("POST", "shell", db_id, {"cmd": probe_command(url, timeout)}, timeout + 10)
        if not 200 <= http < 300 or str(payload.get("code")) != "200":
            raise ValueError("云机探测命令执行失败")
        data = payload.get("data")
        output = data.get("message") if isinstance(data, dict) else None
        if not isinstance(output, str):
            raise ValueError("云机探测未返回执行结果")
        try:
            result = json.loads(output)
        except ValueError:
            raise ValueError("云机探测未返回有效JSON") from None
        if not isinstance(result, dict):
            raise ValueError("云机探测结果不是对象")
        return result

    def cloud_http(self, db_id, url, timeout, root=False):
        """不保存网页正文；检查所选请求上下文的退出码和 HTTP 状态。"""
        command = probe_command(url, timeout, ignore_env_proxy=False) + " --output /dev/null --write-out '%{http_code}'"
        if root:
            # 标准模式中 system UID 的请求不代表普通应用；必须标注 root 探测范围。
            command += " 2>/dev/null; curl_status=$?; printf '\\nCURL_EXIT=%s\\n' \"$curl_status\""
            http, payload = self.api("POST", "shell", db_id, {"cmd": command}, timeout + 10)
            raw_data = payload.get("data")
            output = raw_data.get("message") if isinstance(raw_data, dict) else None
            result = re.fullmatch(r"([0-9]{3})\s+CURL_EXIT=([0-9]+)", output.strip()) if isinstance(output, str) else None
            if not 200 <= http < 300 or str(payload.get("code")) != "200" or result is None:
                raise ValueError("云机网页命令执行结果未确认")
            data = {"output": result[1], "exit_code": int(result[2])}
        else:
            response = self.session.post(f"{self.host}/android_api/v2/{db_id}/system/shell",
                                         json={"command": command, "is_root": False}, timeout=(5, timeout + 10))
            response.raise_for_status()
            payload = response.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(payload, dict) or str(payload.get("code")) != "200" or not isinstance(data, dict):
                raise ValueError("云机网页命令执行未确认")
        exit_code = data.get("exit_code")
        if type(exit_code) is not int:
            raise ValueError("云机网页命令未返回有效退出码")
        if exit_code != 0:
            raise ValueError(f"云机网页请求失败（curl退出码 {exit_code}）")
        status = data.get("output", "")
        if not isinstance(status, str) or not re.fullmatch(r"2[0-9]{2}", status.strip()):
            safe_status = status.strip() if isinstance(status, str) and re.fullmatch(r"[0-9]{3}", status.strip()) else "未确认"
            raise ValueError(f"云机网页未返回成功状态（HTTP {safe_status}）")
        return {"url": url, "http_status": int(status.strip())}

    def verify(self, db_id, expected_ip, probe_config, wait_time, proxy=None):
        deadline = time.monotonic() + wait_time
        urls = probe_config.get("probe_urls", [])
        rounds = probe_config.get("verify_rounds", 2 if urls else 1)
        if not isinstance(urls, list) or any(not isinstance(url, str) for url in urls) or type(rounds) is not int or not 1 <= rounds <= 3:
            raise ValueError("网页探测配置无效")
        for url in urls:
            probe_command(url, probe_config["timeout"])
        last = {"verified": False, "scope": "proxy_exit_and_web" if urls else "proxy_exit_only", "tiktok_app_verified": False}
        while True:
            try:
                http, payload = self.api("GET", "cbs_proxy_get", db_id)
                action = "cbs_proxy_get"
                if http == 409 or str(payload.get("code")) == "409":
                    http, payload = self.api("GET", "proxy_get", db_id)
                    action = "proxy_get"
                if not 200 <= http < 300:
                    raise ValueError(f"代理状态查询失败（HTTP {http}）")
                last["api"] = dict(status_fields(payload), http_status=http, action=action)
                if proxy is not None:
                    last["configuration_matches"] = proxy_matches(payload.get("data", {}).get("proxy_config"), proxy)
                    if not last["configuration_matches"]:
                        raise ValueError("云机实际代理节点与指定配置不一致")
                # 标准模式不返回 healthy；启用后仍须通过实际出口和网页请求。
                if last["api"].get("enabled") is not True or ("healthy" in last["api"] and last["api"]["healthy"] is not True):
                    raise ValueError("代理未启用或尚未健康")
                output = self.cloud_json(db_id, probe_config["probe_url"], probe_config["timeout"])
                last["exit_ip"] = str(ipaddress.ip_address(output.get("ip", "")))
                if last["exit_ip"] != expected_ip:
                    raise ValueError("云机出口与指定代理本机探测出口不一致")
                last["web_checks"] = []
                last["completed_rounds"] = 0
                root_web = last["api"].get("engineType") == 0
                last["web_probe_context"] = "root" if root_web else "system"
                last["scope"] = ("proxy_exit_and_root_web" if root_web else "proxy_exit_and_web") if urls else "proxy_exit_only"
                for attempt in range(rounds if urls else 0):
                    for url in urls:
                        check = {"url": url, "round": attempt + 1, "success": False}
                        last["web_checks"].append(check)
                        try:
                            check.update(self.cloud_http(db_id, url, probe_config["timeout"], root=root_web), success=True)
                        except (requests.RequestException, ValueError, TypeError) as error:
                            check["error"] = str(error) if type(error) is ValueError else type(error).__name__
                            raise
                    last["completed_rounds"] += 1
                last.update(verified=True, verified_at=timestamp())
                last.pop("error", None)
                return last
            except (requests.RequestException, ValueError, TypeError) as error:
                last["error"] = str(error) if type(error) is ValueError else type(error).__name__
            if time.monotonic() >= deadline:
                return last
            self.log("等待代理状态与云机出口验证")
            time.sleep(min(3, max(0, deadline - time.monotonic())))

    def update_mapping(self, db_id, proxy_id, verification, state):
        mapping = read_json(self.mapping_path)
        if mapping.get("host") != self.host or not isinstance(mapping.get("devices"), dict) or db_id not in mapping["devices"]:
            raise ValueError("设备映射主机或目标不匹配，原映射已保留")
        entry = mapping["devices"][db_id]
        entry.update(proxy_id=proxy_id, proxy_exit_ip=verification["exit_ip"],
                     proxy_verified_at=verification["verified_at"], state=state,
                     last_seen=timestamp())
        mapping["timestamp"] = timestamp()
        save_json(self.mapping_path, mapping)

    def run(self, db_id, proxy, probe_config, start_stopped=False, keep_running=False,
            verify_only=False, wait_time=60, sync_system=False, restore_state=True):
        if device_ids(db_id) != [db_id] or wait_time < 0:
            raise ValueError("必须明确指定一个云机ID，等待时间不能为负数")
        body = proxy_body(proxy)
        requested_engine = proxy.get("engine_type", 0 if proxy.get("transit") else None)
        if requested_engine is not None and (type(requested_engine) is not int or requested_engine not in (0, 1, 2)):
            raise ValueError("代理 engine_type 必须为0、1或2")
        if sync_system:
            region_settings(proxy)  # 写入代理前发现缺失或错误的地区配置。
        if not isinstance(probe_config.get("timeout"), int) or not 1 <= probe_config["timeout"] <= 120:
            raise ValueError("代理探测超时必须为1至120秒")
        probe_command(probe_config["probe_url"], probe_config["timeout"])
        report = {"timestamp": timestamp(), "host": self.host, "db_id": db_id,
                  "mode": "verify" if verify_only else "apply", "success": False,
                  "proxy": {k: proxy[k] for k in ("id", "protocol", "host", "port")},
                  "started_by_script": False}
        if proxy.get("transit") is not None:
            report["proxy"]["transit"] = {k: proxy["transit"][k] for k in ("protocol", "host", "port")}
        self.log(f"目标云机: {db_id}，代理: {proxy['id']}")
        try:
            # 启动或设置前先查明目标、映射和代理，避免错设备或不可用资源。
            mapping = read_json(self.mapping_path)
            if mapping.get("host") != self.host or not isinstance(mapping.get("devices"), dict) or not isinstance(mapping["devices"].get(db_id), dict):
                raise ValueError("设备映射主机或目标不匹配")
            instance = self.device.list_instances().get(db_id)
            if instance is None:
                raise ValueError("目标云机不存在")
            state = instance.get("state") or instance.get("status")
            report.update(name=instance.get("user_name") or instance.get("name"), original_state=state)
            local = check_proxy(proxy, probe_config)
            report["expected_exit_ip"] = local["exit_ip"]
            self.log(f"本机代理探测通过，预期出口: {local['exit_ip']}")
            if state == "stopped" and start_stopped and not verify_only:
                report["started_by_script"] = True
                self.log("启动指定云机")
                try:
                    response = self.device.post_action("run", [db_id])
                    if str(response.get("code")) != "200":
                        raise ValueError("VMOS拒绝启动目标云机")
                except requests.RequestException:
                    self.log("启动请求结果不明确，回查ROM状态")
            elif state != "running":
                raise ValueError("云机未运行；配置时可指定--start-stopped，只读验证需先手动启动")
            if not self.device.wait_ready([db_id]):
                raise ValueError("ROM未就绪，未配置代理")
            system = SystemSetup(self) if sync_system else None
            expected_region = system.resolve(db_id, proxy, local["exit_ip"]) if system else None
            if not verify_only:
                current = self.current_proxy(db_id, proxy)
                engine_type = requested_engine if requested_engine is not None else current["engineType"]
                report["engine_type"] = engine_type
                # 正确且已出网的配置直接回查，重复运行不会重挂 legacy 路由。
                existing = self.verify(db_id, local["exit_ip"], probe_config, 0, proxy) if current.get("matches") and current["engineType"] == engine_type else None
                if existing and existing["verified"]:
                    report["set_api"] = {"action": "already_configured", "accepted": True}
                    self.log("已有代理配置匹配且出网正常，跳过写入")
                else:
                    self.log("配置目标云机代理（legacy兼容模式）" if engine_type in (0, 1) else "配置目标云机代理（CBS接口）")
                    try:
                        report["set_api"] = self.apply(db_id, body, proxy, engine_type)
                    except requests.RequestException as error:
                        report["set_api"] = {"accepted": None, "error": type(error).__name__}
                        self.log("配置请求结果不明确，回查实际状态和出口；未重发")
            if system:
                # 配置结果不明确时，先核对节点及引擎，不能继续改国家或重启。
                active = self.current_proxy(db_id, proxy)
                if not active.get("matches") or active.get("enabled") is False or (requested_engine is not None and active["engineType"] != requested_engine):
                    raise ValueError("代理节点或引擎未确认，未继续地区同步")
                report["system"] = {}
                self.log("核验代理地区与系统参数" if verify_only else "同步国家、时区、语言和GPS；已确认的相同配置跳过写入")
                system.configure(db_id, expected_region, report["system"], verify_only, mapping["devices"][db_id].get("system_config"))
            report["verification"] = self.verify(db_id, local["exit_ip"], probe_config, wait_time, proxy)
            actual_engine = report["verification"].get("api", {}).get("engineType")
            if actual_engine in (0, 1, 2):
                report["engine_type"] = actual_engine
            if requested_engine is not None and actual_engine != requested_engine:
                report["verification"].update(verified=False, error="实际代理引擎与配置不一致")
            report["success"] = report["verification"]["verified"]
            if not report["success"]:
                report["error"] = report["verification"].get("error", "代理尚未确认")
            else:
                self.log(f"代理出口和配置的网页探测通过: {report['verification']['exit_ip']}；TikTok应用尚未验证")
                try:
                    location = self.cloud_json(db_id, "https://ipwho.is/" + local["exit_ip"], probe_config["timeout"])
                    if location.get("success") is True and location.get("ip") == local["exit_ip"]:
                        report["location"] = {key: location.get(key) for key in ("ip", "country", "country_code", "region", "city", "latitude", "longitude")}
                    else:
                        report["location"] = {"status": "unavailable"}
                except (requests.RequestException, ValueError, TypeError):
                    report["location"] = {"status": "unavailable"}
        except (requests.RequestException, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
            report["error"] = str(error) if type(error) is ValueError else type(error).__name__
            self.log("处理失败: " + report["error"])
        finally:
            if restore_state and report["started_by_script"] and (not keep_running or not report["success"]):
                report["restore_state"] = self.device.restore_stopped([db_id])
                # 只保留清理状态，避免任意接口原文进入报告。
                report["restore_state"] = {"success": report["restore_state"].get("success") is True}
                if not report["restore_state"]["success"]:
                    report.update(success=False, error="恢复原停机状态未确认")
            try:
                final = self.device.list_instances().get(db_id, {})
                report["final_state"] = final.get("state") or final.get("status") or "unknown"
                if report["success"] and not verify_only:
                    self.update_mapping(db_id, proxy["id"], report["verification"], report["final_state"])
                    report["mapping_updated"] = True
                # 地区已完成的事实不依赖网页验证；避免网络波动导致下次重复设置和重启。
                if report.get("system", {}).get("success") and not verify_only:
                    latest = read_json(self.mapping_path)
                    latest["devices"][db_id]["system_config"] = {
                        "expected": expected_region, "country_api_confirmed": report["system"].get("country_api_confirmed") is True,
                        "verified_at": timestamp()}
                    save_json(self.mapping_path, latest)
                    report["system_mapping_updated"] = True
            except (requests.RequestException, OSError, ValueError, KeyError, TypeError) as error:
                report.update(success=False, error="最终状态或映射更新失败: " + type(error).__name__)
            self.log("完成: " + ("代理配置与出网探测已确认" if report["success"] else "未完成，请查看结果报告"))
            filename = "proxy_verify" if verify_only else "proxy_status"
            save_json(self.output_dir / (filename + ".json"), report)
            (self.output_dir / ("proxy_verify_log.txt" if verify_only else "proxy_log.txt")).write_text("\n".join(self.logs) + "\n", encoding="utf-8")
            if report.get("location") and not verify_only:
                save_json(self.output_dir / "ip_location.json", {"db_id": db_id, "timestamp": timestamp(), **report["location"]})
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-id", required=True, help="明确指定单台云机ID；无默认批量操作")
    parser.add_argument("--proxy-id", required=True, help="本地代理列表中的ID")
    parser.add_argument("--config", type=Path, default=ROOT / "配置文件" / "config.json")
    parser.add_argument("--start-stopped", action="store_true")
    parser.add_argument("--keep-running", action="store_true", help="成功后保留本脚本启动的云机运行，供下一阶段使用")
    parser.add_argument("--verify-only", action="store_true", help="只回查代理、云机出口；不启动或修改云机代理")
    parser.add_argument("--wait-time", type=int, default=60, help="代理就绪重试窗口秒数")
    parser.add_argument("--proxy-only", action="store_true", help="仅处理代理；默认同步代理的region国家、时区、语言和GPS")
    args = parser.parse_args()
    try:
        config = read_json(args.config)
        entries = read_json(ROOT / config["proxy"]["config_file"])["proxies"]
        selected = [entry for entry in entries if entry.get("id") == args.proxy_id]
        if len(selected) != 1:
            raise ValueError("代理ID不存在或重复")
        host = f"http://{config['vmos']['host_ip']}:{int(config['vmos']['port'])}"
        result = ProxySetup(host).run(args.db_id, selected[0], config["proxy"],
                                     args.start_stopped, args.keep_running, args.verify_only, args.wait_time, not args.proxy_only)
        return 0 if result["success"] else 1
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(str(error) if type(error) is ValueError else type(error).__name__)


if __name__ == "__main__":
    raise SystemExit(main())
