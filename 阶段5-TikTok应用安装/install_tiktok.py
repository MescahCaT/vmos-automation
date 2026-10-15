"""指定云机安装TikTok：启动、ROM检查、上传、回查；不自动扫描全部云机。"""
import argparse
import json
import re
import time
from datetime import datetime
from pathlib import Path

import requests
from apk_uploader import ApkUploader
from install_verifier import InstallVerifier
from permission_manager import PermissionManager


ROOT = Path(__file__).resolve().parents[1]


def parse_instances(payload):
    if not isinstance(payload, dict) or str(payload.get("code")) != "200":
        raise ValueError("实例列表业务状态码不是200")
    data = payload.get("data")
    items = data.get("list") if isinstance(data, dict) else data
    if not isinstance(items, list) or any(not isinstance(item, dict) or not item.get("db_id") for item in items):
        raise ValueError("实例列表格式无法识别")
    return {item["db_id"]: item for item in items}


def device_ids(value):
    """接受逗号分隔、db_ids对象或阶段2的devices_created数组。"""
    if isinstance(value, dict):
        value = value.get("db_ids", value.get("devices"))
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list):
        raise ValueError("设备清单必须是db_ids数组或含db_id的设备数组")
    ids = []
    for item in value:
        db_id = item.get("db_id") if isinstance(item, dict) else item
        if not isinstance(db_id, str) or not re.fullmatch(r"EDGE[A-Za-z0-9]+", db_id.strip()):
            raise ValueError("设备ID无效")
        if db_id.strip() not in ids:
            ids.append(db_id.strip())
    if not ids:
        raise ValueError("未指定目标云机")
    return ids


class TikTokInstaller:
    def __init__(self, base_url, output_dir=None, package_name="com.zhiliaoapp.musically"):
        self.base_url = base_url.rstrip("/")
        self.output_dir = Path(output_dir) if output_dir else ROOT / "日志"
        self.package_name = package_name
        self.session = requests.Session()
        self.session.trust_env = False
        self.uploader = ApkUploader(self.base_url, self.session)
        self.verifier = InstallVerifier(self.base_url, self.session)
        self.permission_mgr = PermissionManager(self.base_url, self.session)
        self.logs = []

    def log(self, message):
        line = f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] {message}"
        self.logs.append(line)
        print(line, flush=True)

    def list_instances(self):
        response = self.session.get(self.base_url + "/container_api/v1/list_names", timeout=(5, 10))
        response.raise_for_status()
        return parse_instances(response.json())

    def post_action(self, action, db_ids):
        response = self.session.post(self.base_url + "/container_api/v1/" + action,
                                     json={"db_ids": db_ids}, timeout=(5, 30))
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("实例操作响应不是对象")
        return payload

    def wait_ready(self, db_ids, timeout=180):
        deadline = time.monotonic() + timeout
        remaining = list(db_ids)
        while remaining:
            for db_id in remaining[:]:
                try:
                    response = self.session.get(self.base_url + "/container_api/v1/rom_status/" + db_id,
                                                timeout=(5, 10))
                    response.raise_for_status()
                    if str(response.json().get("code")) == "200":
                        remaining.remove(db_id)
                        self.log(f"ROM已就绪: {db_id}")
                except (requests.RequestException, ValueError, AttributeError):
                    pass
            if not remaining or time.monotonic() >= deadline:
                return [db_id for db_id in db_ids if db_id not in remaining]
            self.log(f"等待ROM就绪: {len(db_ids) - len(remaining)}/{len(db_ids)}")
            time.sleep(min(5, max(0, deadline - time.monotonic())))

    def restore_stopped(self, db_ids):
        if not db_ids:
            return {"success": True, "devices": []}
        self.log("恢复原停机状态: " + ", ".join(db_ids))
        try:
            current = self.list_instances()
            pending = [db_id for db_id in db_ids if current.get(db_id, {}).get("state") != "stopped"]
            if pending:
                response = self.post_action("stop", pending)
                if str(response.get("code")) != "200":
                    return {"success": False, "devices": pending, "response": response}
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    current = self.list_instances()
                    pending = [db_id for db_id in pending if current.get(db_id, {}).get("state") != "stopped"]
                    if not pending:
                        break
                    time.sleep(3)
            return {"success": not pending, "devices": db_ids, "unconfirmed": pending}
        except (requests.RequestException, ValueError) as error:
            return {"success": False, "devices": db_ids, "error": str(error)}

    def install_from_local(self, apk_path, db_ids, wait_time=300, verify=True, **options):
        apk = Path(apk_path).resolve()
        if not apk.is_file() or not apk.stat().st_size:
            raise ValueError("APK不存在或为空")
        return self._install("local", str(apk), db_ids, wait_time, verify, **options)

    def install_from_url(self, url, db_ids, wait_time=300, verify=True, **options):
        if not url.startswith(("http://", "https://")):
            raise ValueError("APK地址必须是HTTP或HTTPS")
        return self._install("url", url, db_ids, wait_time, verify, **options)

    def _install(self, mode, source, db_ids, wait_time, verify, batch_size=1,
                 start_stopped=False, restore_state=False, overlay=False):
        db_ids = device_ids(db_ids)
        if not 1 <= batch_size <= 12 or wait_time < 0:
            raise ValueError("批次大小必须为1至12，安装等待时间不能为负数")
        result = {"timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
                  "host": self.base_url, "source": source, "mode": mode,
                  "package_name": self.package_name, "devices": {}, "batches": []}
        self.log(f"目标云机 {len(db_ids)} 台，批次大小 {batch_size}")
        for offset in range(0, len(db_ids), batch_size):
            batch = db_ids[offset:offset + batch_size]
            record = {"db_ids": batch}
            result["batches"].append(record)
            started = []
            ready_candidates = []
            self.log(f"处理批次 {offset // batch_size + 1}: {', '.join(batch)}")
            for db_id in batch:
                result["devices"][db_id] = {"status": "unconfirmed"}
            try:
                instances = self.list_instances()
                for db_id in batch:
                    device = result["devices"][db_id]
                    instance = instances.get(db_id)
                    if instance is None:
                        device.update(status="failed", error="目标云机不存在")
                        continue
                    state = instance.get("state") or instance.get("status")
                    device.update(name=instance.get("user_name") or instance.get("name"), original_state=state)
                    if state == "running":
                        ready_candidates.append(db_id)
                    elif state == "stopped" and start_stopped:
                        # 在请求前记录，以便请求超时但实际启动成功时也能恢复原状态。
                        started.append(db_id)
                        ready_candidates.append(db_id)
                    else:
                        device.update(status="failed", error="云机未运行，需指定--start-stopped或手动启动")
                if started:
                    self.log("启动目标云机: " + ", ".join(started))
                    try:
                        response = self.post_action("run", started)
                        record["start_response"] = response
                        if str(response.get("code")) != "200":
                            for db_id in started:
                                result["devices"][db_id].update(status="failed", error="VMOS拒绝启动", start_response=response)
                            ready_candidates = [db_id for db_id in ready_candidates if db_id not in started]
                            started = []
                    except (requests.RequestException, ValueError) as error:
                        record["start_error"] = str(error)
                        self.log("启动请求结果不明确，回查ROM状态")
                ready = self.wait_ready(ready_candidates) if ready_candidates else []
                for db_id in set(ready_candidates) - set(ready):
                    result["devices"][db_id].update(status="unconfirmed", error="ROM就绪超时，未上传APK")
                upload = []
                for db_id in ready:
                    apps = self.verifier.get_app_list(db_id)
                    if apps is None:
                        result["devices"][db_id].update(status="unconfirmed", error="无法查询安装前应用列表，未上传APK")
                    elif self.package_name in apps:
                        result["devices"][db_id].update(status="already_installed", package_verified=True)
                        self.log(f"已安装，跳过上传: {db_id}")
                    else:
                        upload.append(db_id)
                if upload:
                    self.log(f"上传安装包到 {len(upload)} 台，等待接口返回；不重发安装请求")
                    response = self.uploader.upload_local(source, upload) if mode == "local" else self.uploader.install_from_url(source, upload)
                    record["upload_response"] = response
                    self.log("安装请求已返回，回查实际包名")
                    # 请求失败/超时也回查所有目标，避免服务端已安装却被判失败。
                    verified = self.verifier.wait_for_install(upload, wait_time, self.package_name, self.log) if verify else {"success": [], "unconfirmed": upload}
                    record["verification"] = verified
                    for db_id in verified["success"]:
                        result["devices"][db_id].update(status="installed", package_verified=True)
                    for db_id in verified["unconfirmed"]:
                        result["devices"][db_id].update(status="unconfirmed", error="安装尚未确认；请先使用--verify-only回查，未重发上传")
                if overlay:
                    installed = [db_id for db_id in batch if result["devices"][db_id]["status"] in ("installed", "already_installed")]
                    record["permissions"] = self.permission_mgr.batch_grant_permissions(installed)
            except (requests.RequestException, OSError, ValueError, TypeError) as error:
                record["error"] = str(error)
                self.log(f"本批异常: {error}")
            finally:
                if restore_state:
                    record["restore_state"] = self.restore_stopped(started)
                self._summarize(result)
                self.save_results(result)
        self._summarize(result)
        self.log(f"安装完成: {len(result['install_success'])}/{len(db_ids)} 已确认，{len(result['install_failed'])} 失败，{len(result['install_pending'])} 待确认")
        self.save_results(result)
        return result

    def _summarize(self, result):
        devices = result["devices"]
        result["install_success"] = [db_id for db_id, item in devices.items() if item["status"] in ("installed", "already_installed")]
        result["install_failed"] = [db_id for db_id, item in devices.items() if item["status"] == "failed"]
        result["install_pending"] = [db_id for db_id, item in devices.items() if item["status"] == "unconfirmed"]
        result["cleanup_failed"] = any(not item.get("restore_state", {}).get("success", True) for item in result["batches"])
        result["success"] = not result["install_failed"] and not result["install_pending"] and not result["cleanup_failed"]

    def save_results(self, result, filename="install_status.json"):
        self.output_dir.mkdir(parents=True, exist_ok=True)
        path = self.output_dir / filename
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(path)
        (self.output_dir / "failed_devices.txt").write_text("\n".join(result.get("install_failed", [])), encoding="utf-8")
        (self.output_dir / "pending_devices.txt").write_text("\n".join(result.get("install_pending", [])), encoding="utf-8")
        (self.output_dir / "install_log.txt").write_text("\n".join(self.logs), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", nargs="?", choices=("local", "url"), default="local")
    parser.add_argument("source", nargs="?", help="可选APK路径或URL；local默认读取主配置")
    parser.add_argument("legacy_ids", nargs="?", help="兼容旧命令的逗号分隔ID")
    parser.add_argument("--config", type=Path, default=ROOT / "配置文件" / "config.json")
    targets = parser.add_mutually_exclusive_group()
    targets.add_argument("--db-ids", help="逗号分隔的目标云机ID")
    targets.add_argument("--devices-file", type=Path, help="目标JSON，支持阶段2 devices_created.json")
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--wait-time", type=int, default=300)
    parser.add_argument("--start-stopped", action="store_true")
    parser.add_argument("--restore-state", action="store_true", help="结束后停止本脚本启动的云机")
    parser.add_argument("--overlay", action="store_true", help="安装确认后额外授予悬浮窗；TikTok安装无需此权限")
    parser.add_argument("--verify-only", action="store_true", help="只查询应用列表，不启动/上传/授权/停机")
    args = parser.parse_args()
    if args.legacy_ids and (args.db_ids or args.devices_file):
        parser.error("只能使用一种目标来源")
    try:
        config = json.loads(args.config.read_text(encoding="utf-8-sig"))
        raw_ids = json.loads(args.devices_file.read_text(encoding="utf-8-sig")) if args.devices_file else args.db_ids or args.legacy_ids
        db_ids = device_ids(raw_ids)
        host = f"http://{config['vmos']['host_ip']}:{int(config['vmos']['port'])}"
        installer = TikTokInstaller(host, package_name=config["tiktok"]["package_name"])
        if args.verify_only:
            result = installer.verifier.batch_verify(db_ids, installer.package_name)
            # 不覆盖实际安装报告；停机设备无法查应用列表，记为待确认。
            installer.output_dir.mkdir(parents=True, exist_ok=True)
            (installer.output_dir / "install_verify.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if len(result["success"]) == len(db_ids) else 2
        source = args.source or (str(ROOT / config["tiktok"]["apk_path"]) if args.mode == "local" else "")
        method = installer.install_from_local if args.mode == "local" else installer.install_from_url
        result = method(source, db_ids, wait_time=args.wait_time, batch_size=args.batch_size,
                        start_stopped=args.start_stopped, restore_state=args.restore_state, overlay=args.overlay)
        return 0 if result["success"] else 1 if result["install_failed"] or result["cleanup_failed"] else 2
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
