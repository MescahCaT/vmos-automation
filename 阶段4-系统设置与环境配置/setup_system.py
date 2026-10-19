"""单台代理地区同步；由阶段3复用，写入后重启并回查。"""
import argparse
import json
import math
import re
import sys
import time
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parents[1]


def region_settings(proxy):
    region = proxy.get("region")
    if not isinstance(region, dict):
        raise ValueError("代理缺少 region 配置（country/language/timezone）")
    result = {key: region.get(key) for key in ("country", "language", "timezone")}
    if not isinstance(result["country"], str) or not re.fullmatch(r"[A-Z]{2}", result["country"]):
        raise ValueError("国家必须为两位大写代码")
    if not isinstance(result["language"], str) or not re.fullmatch(r"[a-z]{2,3}", result["language"]):
        raise ValueError("语言代码无效")
    if not isinstance(result["timezone"], str) or not re.fullmatch(r"[A-Za-z_+-]+(?:/[A-Za-z0-9_+-]+)+|UTC", result["timezone"]):
        raise ValueError("时区配置无效；请按代理实际城市配置，不能仅根据国家猜测")
    for key, limit in (("latitude", 90), ("longitude", 180)):
        if key in region:
            value = region[key]
            if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > limit:
                raise ValueError("GPS经纬度无效")
            result[key] = value
    if ("latitude" in result) != ("longitude" in result):
        raise ValueError("GPS经纬度必须同时配置")
    return result


class SystemSetup:
    def __init__(self, proxy_setup):
        self.proxy = proxy_setup

    def request(self, action, db_id, body=None):
        if action == "timezone_set":
            http, payload = self.proxy.control_api("POST", "system/update_settings", db_id,
                                                   {"timezone": body["timeZone"]})
        else:
            http, payload = self.proxy.api("GET" if body is None else "POST", action, db_id, body)
        if http != 200 or str(payload.get("code")) != "200":
            raise ValueError(f"{action}未确认成功（HTTP {http}，业务码 {payload.get('code')}）")
        return payload.get("data")

    def shell(self, db_id, command):
        data = self.request("shell", db_id, {"cmd": command})
        if not isinstance(data, dict) or not isinstance(data.get("message"), str):
            raise ValueError("系统参数命令未返回结果")
        return data["message"].strip()

    def resolve(self, db_id, proxy, exit_ip):
        region = region_settings(proxy)
        # 多时区国家需明确配置时区；GPS默认取实际出口IP，避免沿用旧地址。
        for attempt in range(3):
            try:
                geo = self.request("ip_geo", db_id + "?ip=" + exit_ip)
                break
            except (requests.RequestException, ValueError):
                if attempt == 2:
                    raise
                time.sleep(1)
        if not isinstance(geo, dict) or geo.get("ip") != exit_ip:
            raise ValueError("出口IP地理信息与代理国家配置不一致或查询失败")
        country = geo.get("country_code") or geo.get("countryCode") or geo.get("country")
        if not isinstance(country, str) or not re.fullmatch(r"[A-Z]{2}", country):
            # VMOS轮换定位数据源，country有时是全名。用同一出口的代码核验，不猜测全名。
            fallback = self.proxy.cloud_json(db_id, "https://ipwho.is/" + exit_ip, 15)
            if fallback.get("success") is not True or fallback.get("ip") != exit_ip:
                raise ValueError("出口国家代码查询失败")
            country = fallback.get("country_code")
        if country != region["country"]:
            raise ValueError("出口IP地理信息与代理国家配置不一致或查询失败")
        if "latitude" not in region:
            region.update(latitude=geo.get("lat"), longitude=geo.get("lon"))
        return region_settings({"region": region})

    def snapshot(self, db_id, require_consistent=True):
        region = self.request("get_timezone_locale", db_id)
        http, payload = self.proxy.control_api("GET", "location/get_data", db_id, timeout=20)
        if not 200 <= http < 300 or not isinstance(region, dict) or str(payload.get("code")) != "200":
            raise ValueError("地区或GPS回查失败")
        location = payload.get("data")
        result = {key: region.get(key) for key in ("country", "locale", "timezone")}
        if not isinstance(location, dict):
            raise ValueError("GPS回查格式无效")
        result.update(latitude=location.get("latitude"), longitude=location.get("longitude"))
        for key, prop in (("country", "persist.sys.country"), ("locale", "persist.sys.locale"), ("timezone", "persist.sys.timezone")):
            actual = self.shell(db_id, "getprop " + prop)
            if not actual or actual != result[key]:
                if require_consistent:
                    raise ValueError("地区接口与Android实际参数不一致: " + key)
                # 新机写入前允许接口与实际值尚未同步；保留差异，设置后仍严格回查。
                result["api_region"] = {field: region.get(field) for field in ("country", "locale", "timezone")}
            result[key] = actual
        return result

    @staticmethod
    def matches(actual, expected):
        if any(actual.get(k) != v for k, v in (("country", expected["country"]),
                ("locale", expected["language"] + "-" + expected["country"]), ("timezone", expected["timezone"]))):
            return False
        return all(type(actual.get(k)) in (int, float) and math.isfinite(actual[k])
                   and abs(actual[k] - expected[k]) < 0.001 for k in ("latitude", "longitude"))

    def reboot(self, db_id, report, timeout=180):
        before = self.shell(db_id, "cat /proc/sys/kernel/random/boot_id")
        if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", before):
            raise ValueError("重启前启动ID无效，未发送重启请求")
        report["boot_id_before"] = before
        report["reboot_requested"] = True
        try:
            result = self.proxy.device.post_action("reboot", [db_id])
            if str(result.get("code")) != "200":
                raise ValueError("VMOS拒绝重启指定云机")
        except requests.RequestException:
            report["reboot_response_uncertain"] = True
        started = time.monotonic()
        deadline = started + timeout
        while time.monotonic() < deadline:
            time.sleep(3)
            try:
                after = self.shell(db_id, "cat /proc/sys/kernel/random/boot_id")
                boot = self.shell(db_id, "getprop sys.boot_completed")
                report.update(boot_id_after=after, boot_completed=boot,
                              reboot_wait_seconds=round(time.monotonic() - started, 1))
                if (after != before and re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", after)
                        and boot == "1"):
                    self.snapshot(db_id)  # Android启动后，地区与定位API也必须就绪。
                    report["reboot_verified"] = True
                    return
                report["reboot_last_error"] = "启动ID尚未变化或Android启动未完成"
            except (requests.RequestException, ValueError, TypeError, IndexError) as error:
                report["reboot_last_error"] = str(error) if type(error) is ValueError else type(error).__name__
            self.proxy.log("等待重启及地区API就绪：" + report["reboot_last_error"])
        raise ValueError("重启或地区API就绪未确认；未重发重启请求")

    def configure(self, db_id, expected, report, verify_only=False, previous=None):
        expected = region_settings({"region": expected})
        if "latitude" not in expected:
            raise ValueError("执行地区同步前必须解析GPS经纬度")
        report.update(expected=expected, success=False, steps=[], sim_details_verified=False)
        report["before"] = self.snapshot(db_id, require_consistent=verify_only)
        matches = self.matches(report["before"], expected) and "api_region" not in report["before"]
        previous_region = previous.get("expected") if isinstance(previous, dict) else None
        recorded = (isinstance(previous_region, dict) and previous.get("country_api_confirmed") is True
                    and previous_region.get("language") == expected["language"]
                    and self.matches(dict(previous_region, locale=previous_region.get("language", "") + "-" + previous_region.get("country", "")), expected))
        if verify_only:
            report.update(action="verify", after=report["before"], success=matches,
                          country_api_confirmed=recorded)
            if not matches:
                raise ValueError("云机国家、时区、语言或GPS与代理地区不一致")
            return report
        if matches and recorded and previous.get("gps_persisted") is True:
            report.update(action="already_configured", after=report["before"], success=True,
                          country_api_confirmed=True, gps_persisted=True)
            return report
        report["action"] = "apply"
        steps = [("country_set", {"country": expected["country"]}),
                 ("timezone_set", {"timeZone": expected["timezone"]}),
                 ("language_set", {"country": expected["country"], "language": expected["language"]}),
                 ("gps_inject", {key: expected[key] for key in ("latitude", "longitude")})]
        for action, body in steps:
            step = {"action": action, "accepted": False}
            if action == "timezone_set":
                step.update(transport="instance_port", api="system/update_settings")
            report["steps"].append(step)
            try:
                self.request(action, db_id, body)
                step["accepted"] = True
            except (requests.RequestException, ValueError) as error:
                step["error"] = str(error) if type(error) is ValueError else type(error).__name__
                if action not in ("timezone_set", "language_set"):
                    raise
                field, label, target = (("timezone", "时区", expected["timezone"]) if action == "timezone_set"
                                        else ("locale", "语言", expected["language"] + "-" + expected["country"]))
                self.proxy.log(f"{label}设置响应未确认，先读Android实际值，再核对地区接口；未重发设置请求")
                for attempt in range(3):
                    try:
                        actual = self.shell(db_id, "getprop persist.sys." + field)
                        step["android_" + field] = actual
                        if actual == target:
                            region = self.request("get_timezone_locale", db_id)
                            step["api_" + field] = region.get(field) if isinstance(region, dict) else None
                            if step["api_" + field] == target:
                                step["readback_verified"] = True
                                break
                    except (requests.RequestException, ValueError) as query_error:
                        step["readback_error"] = str(query_error) if type(query_error) is ValueError else type(query_error).__name__
                    if attempt < 2:
                        time.sleep(1)
                if not step.get("readback_verified"):
                    raise ValueError(step["error"] + f"；{label}回查未确认，未重发设置请求") from None
        report["country_api_confirmed"] = True
        # gps_inject只更新运行时坐标；GNSS启动会从这两个属性重新加载。
        step = {"action": "gps_persist", "accepted": False}
        report["steps"].append(step)
        response = self.proxy.session.post(self.proxy.host + "/container_api/v1/update_user_prop",
            json={"db_ids": [db_id], "isRestart": False, "user_prop": json.dumps({
                "persist.sys.cloud.gps.lat": str(expected["latitude"]),
                "persist.sys.cloud.gps.lon": str(expected["longitude"])})}, timeout=(5, 30))
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or str(payload.get("code")) != "200":
            raise ValueError("GPS持久属性保存未确认，未重启")
        step["accepted"] = True
        self.reboot(db_id, report)
        report["after"] = self.snapshot(db_id)
        report["gps_properties"] = {}
        for key, prop in (("latitude", "persist.sys.cloud.gps.lat"), ("longitude", "persist.sys.cloud.gps.lon")):
            actual = self.shell(db_id, "getprop " + prop)
            report["gps_properties"][key] = actual
            try:
                value = float(actual)
            except ValueError:
                raise ValueError("重启后GPS持久属性无效: " + key) from None
            if not math.isfinite(value) or abs(value - expected[key]) >= 0.001:
                raise ValueError(f"重启后GPS持久属性不匹配: {key}，期望 {expected[key]}，实际 {actual}")
        if not self.matches(report["after"], expected):
            raise ValueError(f"重启后地区参数或GPS未保留，期望 {expected}，实际 {report['after']}，未写入完成标记")
        report.update(success=True, gps_persisted=True)
        return report


def main():
    sys.path.insert(0, str(ROOT / "阶段3-网络代理配置"))
    from setup_proxy import ProxySetup, read_json, save_json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-id", required=True)
    parser.add_argument("--proxy-id", required=True)
    parser.add_argument("--config", type=Path, default=ROOT / "配置文件" / "config.json")
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--start-stopped", action="store_true")
    parser.add_argument("--keep-running", action="store_true")
    args = parser.parse_args()
    try:
        config = read_json(args.config)
        proxies = read_json(ROOT / config["proxy"]["config_file"])["proxies"]
        selected = [p for p in proxies if p.get("id") == args.proxy_id]
        if len(selected) != 1:
            raise ValueError("代理ID不存在或重复")
        host = f"http://{config['vmos']['host_ip']}:{int(config['vmos']['port'])}"
        result = ProxySetup(host).run(args.db_id, selected[0], config["proxy"], args.start_stopped,
                                     args.keep_running, args.verify_only, sync_system=True)
        save_json(Path(__file__).parent / ("system_verify.json" if args.verify_only else "system_status.json"), result)
        return 0 if result["success"] else 1
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(str(error) if type(error) is ValueError else type(error).__name__)


if __name__ == "__main__":
    raise SystemExit(main())
