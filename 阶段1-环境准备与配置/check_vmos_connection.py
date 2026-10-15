"""阶段1只读检查：VMOS、代理、APK、邮箱取码；不创建或启动云机。"""
import argparse
import ipaddress
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "配置文件" / "config.json"
API_PATHS = {
    "heartbeat": "/v1/heartbeat",
    "host_config": "/v1/get_hardware_cfg",
    "images": "/v1/get_img_list",
    "templates": "/v1/get_adi_list",
    "instances": "/container_api/v1/list_names",
}


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def list_data(data):
    """兼容data数组及data.list；空数组是合法的空列表。"""
    items = data.get("list") if isinstance(data, dict) else data
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise ValueError("接口列表格式无法识别")
    return items


def get_vmos_data(session, host, path, timeout):
    response = session.get(host + path, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or str(payload.get("code")) != "200":
        raise ValueError("VMOS业务状态码不是200")
    data = payload.get("data")
    if path in (API_PATHS["images"], API_PATHS["templates"], API_PATHS["instances"]):
        return list_data(data)
    if not isinstance(data, dict):
        raise ValueError("接口数据不是对象")
    if path == API_PATHS["heartbeat"] and any(
        data.get(key) is not True for key in ("docker_status", "http_status", "ping_status")
    ):
        raise ValueError("主机Docker、HTTP或网络健康检查未通过")
    return data


def check_apk(config, root):
    apk = root / config["apk_path"]
    if not apk.is_file() or apk.stat().st_size == 0:
        raise ValueError("APK不存在或为空")
    # 旧版aapt无法读取中文完整路径；工作目录配合文件名可以正常读取。
    result = subprocess.run(
        [config["aapt_path"], "dump", "badging", apk.name], cwd=apk.parent,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
    )
    match = re.search(
        r"^package: name='([^']+)' versionCode='([^']+)' versionName='([^']+)'",
        result.stdout, re.MULTILINE,
    )
    if result.returncode or not match:
        raise ValueError("aapt无法读取APK元数据")
    package, version_code, version = match.groups()
    if package != config["package_name"]:
        raise ValueError("APK包名与配置不符")
    return {"path": str(apk), "size_bytes": apk.stat().st_size,
            "package_name": package, "version_name": version, "version_code": version_code}


def curl_option(name, value):
    text = str(value)
    if "\r" in text or "\n" in text or "\0" in text:
        raise ValueError("代理参数包含非法控制字符")
    return name + ' = "' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def check_proxy(proxy, config):
    protocol, host, port = proxy["protocol"], proxy["host"], int(proxy["port"])
    if protocol not in ("socks5", "http", "https") or not 1 <= port <= 65535:
        raise ValueError("代理协议或端口无效")
    if not isinstance(host, str) or not host or re.search(r"[\s/@?#]", host):
        raise ValueError("代理主机地址无效")
    if ":" in proxy.get("username", ""):
        raise ValueError("curl认证的用户名不能包含冒号")
    if proxy.get("transit") is not None:
        from relay_proxy import local_proxy
        with local_proxy(proxy) as local:
            result = check_proxy(local, config)
        result.update(host=host, port=port, protocol=protocol,
                      transit={key: proxy["transit"][key] for key in ("protocol", "host", "port")})
        return result
    scheme = "socks5h" if protocol == "socks5" else protocol
    authority = f"[{host}]" if ":" in host else host
    options = ["silent", "show-error", "fail", curl_option("noproxy", ""),
               curl_option("proxy", f"{scheme}://{authority}:{port}"),
               curl_option("url", config["probe_url"]),
               curl_option("connect-timeout", 8), curl_option("max-time", config["timeout"])]
    if proxy.get("username") or proxy.get("password"):
        options.append(curl_option("proxy-user", proxy.get("username", "") + ":" + proxy.get("password", "")))
    # 认证信息通过标准输入传递，不出现在进程参数、控制台或检查报告中。
    last_code = None
    # ponytail: 最多重试两次，覆盖代理瞬时超时；连续失败才报告不可用。
    for attempt in range(1, 4):
        result = subprocess.run(
            ["curl.exe", "--disable", "--config", "-"], input="\n".join(options) + "\n",
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=config["timeout"] + 5,
        )
        if result.returncode == 0:
            payload = json.loads(result.stdout)
            exit_ip = str(ipaddress.ip_address(payload["ip"]))
            return {"id": proxy["id"], "protocol": protocol, "host": host, "port": port,
                    "exit_ip": exit_ip, "attempts": attempt, "cloud_device_verified": False}
        last_code = result.returncode
    raise ValueError(f"代理探测3次失败（curl退出码{last_code}）")


def extract_mail_code(payload):
    """只识别六位纯数字或明确的data字段，不从网页/时间戳中猜测验证码。"""
    if isinstance(payload, dict):
        if "code" in payload and str(payload["code"]) not in ("0", "200"):
            raise ValueError("取码接口返回失败的业务状态码")
        payload = payload.get("data")
    if isinstance(payload, (str, int)) and not isinstance(payload, bool):
        text = str(payload).strip()
        if re.fullmatch(r"[0-9]{6}", text):
            return text
    return None


def check_mail(session, config, email):
    response = session.get(config["endpoint"], params={"username": email}, timeout=config["timeout"])
    response.raise_for_status()
    # 只返回响应类型和是否有验证码，不保存响应正文或验证码。
    payload = response.json()
    return {"email": email, "http_status": response.status_code,
            "response_type": type(payload).__name__, "has_six_digit_code": extract_mail_code(payload) is not None,
            "fresh_login_verified": False}


def enabled_entries(path, key):
    entries = read_json(path)[key]
    if not isinstance(entries, list) or any(not isinstance(item, dict) for item in entries):
        raise ValueError("资源列表格式无效")
    return [item for item in entries if item.get("enabled", True) is True]


def update_mapping(path, instances, host, timestamp):
    """以db_id为键；保留已有代理/账号绑定和已消失实例的记录。"""
    existing = read_json(path) if path.exists() else {}
    if not isinstance(existing, dict) or existing.get("host", host) != host:
        raise ValueError("已有设备映射属于其他主机或格式无效，已保留原文件")
    previous = existing.get("devices", {})
    if not isinstance(previous, dict):
        raise ValueError("已有设备映射格式无效，已保留原文件")
    if any(not isinstance(value, dict) for value in previous.values()):
        raise ValueError("已有设备映射记录格式无效，已保留原文件")
    devices = {key: dict(value, present=False) for key, value in previous.items()}
    seen = set()
    for item in instances:
        db_id = item.get("db_id")
        if not isinstance(db_id, str) or not db_id or db_id in seen:
            raise ValueError("实例db_id缺失或重复，已保留原映射文件")
        seen.add(db_id)
        entry = devices.setdefault(db_id, {"proxy_id": None, "account_email": None})
        entry.update({"name": item.get("user_name") or item.get("name") or db_id,
                      "state": item.get("state") or item.get("status") or "unknown",
                      "present": True, "last_seen": timestamp,
                      "ports": {key: item[key] for key in (
                          "adb", "http_api_port", "tcp_api_port", "tcp_port", "tcp_audio_port", "tcp_control_port"
                      ) if key in item}})
    save_json(path, {"host": host, "timestamp": timestamp, "devices": devices})
    return {"current_count": len(instances), "total_record_count": len(devices)}


def inspect_environment(config, root, vmos_only=False):
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    host = f"http://{config['vmos']['host_ip']}:{int(config['vmos']['port'])}"
    info = {"timestamp": timestamp, "host": host}
    report = {"timestamp": timestamp, "host": host, "scope": "vmos_only" if vmos_only else "environment",
              "target_count": config["device"]["count"], "checks": []}

    def add(name, status, message, details=None):
        item = {"name": name, "status": status, "message": message}
        if details is not None:
            item["details"] = {"count": len(details)} if isinstance(details, list) else details
        report["checks"].append(item)
        print(f"[{status.upper()}] {message}")

    def check(name, message, function):
        try:
            details = function()
        except (requests.RequestException, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            # 不记录外部响应或curl stderr，避免将密码/验证码带入日志。
            reason = str(exc) if type(exc) is ValueError else type(exc).__name__
            add(name, "fail", message + "：" + reason)
            return None
        add(name, "pass", message, details)
        return details

    with requests.Session() as session:
        session.trust_env = False
        for key, path in API_PATHS.items():
            if key != "heartbeat" and "heartbeat" not in info:
                add(key, "pending", f"{key}：主机健康检查失败，未继续查询")
                continue
            data = check(key, f"VMOS查询 {path}", lambda: get_vmos_data(session, host, path, config["vmos"]["timeout"]))
            if data is not None:
                info[key] = [{k: v for k, v in item.items() if k != "adiPass"} for item in data] if key == "templates" else data

        for key, field, value, label in (("images", "repository", config["device"]["image_repository"], "镜像"), ("templates", "adiID", config["device"]["adiID"], "机型模板")):
            if key in info:
                selected = next((item for item in info[key] if str(item.get(field)) == str(value)), None)
                add("selected_" + key, "pass" if selected else "fail", f"检查所选{label}是否存在", selected)
        if "instances" in info:
            check("device_mapping", "更新设备映射", lambda: update_mapping(root / "数据" / "device_mapping.json", info["instances"], host, timestamp))

        if not vmos_only:
            check("apk", "检查TikTok APK包名和版本", lambda: check_apk(config["tiktok"], root))
            proxies = check("proxy_config", "读取实际代理配置", lambda: enabled_entries(root / config["proxy"]["config_file"], "proxies"))
            accounts = check("account_config", "读取实际账号配置", lambda: enabled_entries(root / config["tiktok"]["account_file"], "accounts"))
            report["resources"] = {"proxy_count": len(proxies or []), "account_count": len(accounts or []), "target_count": config["device"]["count"]}
            for name, entries in (("proxy", proxies), ("account", accounts)):
                if entries is not None and len(entries) < config["device"]["count"]:
                    add(name + "_resources", "pending", f"{name}数量未达到当前配置目标")
            proxy_ids = set()
            for proxy in proxies or []:
                proxy_id = proxy.get("id")
                if not isinstance(proxy_id, str) or not proxy_id or proxy_id in proxy_ids:
                    add("proxy", "fail", "代理id缺失或重复")
                    continue
                proxy_ids.add(proxy_id)
                check("proxy", f"代理 {proxy_id} 连通性", lambda: check_proxy(proxy, config["proxy"]))
            emails = set()
            for account in accounts or []:
                email = account.get("email", "")
                if not isinstance(email, str) or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or email.lower() in emails or account.get("login_mode") != "email_code":
                    add("email_code", "fail", "账号邮箱无效、重复或登录方式不是email_code")
                    continue
                emails.add(email.lower())
                details = check("email_code", f"邮箱取码接口 {email}", lambda: check_mail(session, config["tiktok"]["email_code"], email))
                if details is not None and not details["has_six_digit_code"]:
                    report["checks"][-1].update(status="pending", message="取码接口已响应，当前没有六位验证码；需在登录环节发送新验证码后再验证")
                    print("[PENDING] 当前没有六位验证码")

    info["complete"] = all(item["status"] == "pass" for item in report["checks"] if item["name"] in API_PATHS)
    failures = sum(item["status"] == "fail" for item in report["checks"])
    pending = sum(item["status"] == "pending" for item in report["checks"])
    report.update(status="failed" if failures else "pending" if pending else "ready", failures=failures, pending=pending)
    save_json(root / "数据" / "vmos_info.json", info)
    save_json(root / "数据" / "environment_check.json", report)
    print(f"检查结果：{report['status']}；失败 {failures} 项，待确认 {pending} 项")
    print(f"报告：{root / '数据' / 'environment_check.json'}")
    return 1 if failures else 2 if pending else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="主配置JSON路径；资源路径相对项目根目录")
    parser.add_argument("--vmos-only", action="store_true", help="只检查VMOS，不探测代理/APK/取码接口")
    args = parser.parse_args()
    try:
        config = read_json(args.config)
        if not isinstance(config["device"]["count"], int) or isinstance(config["device"]["count"], bool) or config["device"]["count"] < 1:
            raise ValueError("device.count必须是正整数")
        return inspect_environment(config, ROOT, args.vmos_only)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        reason = str(exc) if type(exc) is ValueError else type(exc).__name__
        print(f"[FAIL] 配置读取或报告保存失败：{reason}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
