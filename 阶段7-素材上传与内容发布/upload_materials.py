"""Upload local MP4/PNG/JPG/JPEG files to existing VMOS devices."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from urllib.parse import urlsplit

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "阶段1-环境准备与配置"))
sys.path.insert(0, str(ROOT / "阶段3-网络代理配置"))
from check_vmos_connection import poll_readback, read_json, save_json
from setup_proxy import ProxySetup

ALLOWED_EXTENSIONS = {".mp4", ".png", ".jpg", ".jpeg"}
DEFAULT_DESTINATION = "/sdcard/DCIM/Codex/"
DEVICE_NAME = re.compile(r"[A-Za-z0-9_.-]{3,}")


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def eligible_files(folder):
    return sorted((path.resolve() for path in folder.iterdir()
                   if path.is_file() and not path.is_symlink()
                   and path.suffix.lower() in ALLOWED_EXTENSIONS), key=lambda path: path.name.casefold())


def checked_asset(path, asset_id, device, task_line=None, hashes=None):
    stat = path.stat()
    if not stat.st_size:
        raise ValueError(f"素材为空文件：{path}")
    if re.search(r"[\r\n\0]", path.name):
        raise ValueError(f"素材文件名包含不支持的字符：{path}")
    hashes = hashes if hashes is not None else {}
    digest = hashes.get(str(path))
    if digest is None:
        digest = hashes[str(path)] = file_sha256(path)
    return {"asset_id": asset_id, "path": path, "device": device, "task_line": task_line,
            "size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha256": digest,
            "mime_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream"}


def parse_device_list(path, manifest_names):
    names, seen = [], set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        name = line.strip()
        if not name or name.startswith("#"):
            continue
        if not DEVICE_NAME.fullmatch(name):
            raise ValueError(f"云机列表第 {line_number} 行名称格式无效：{name}")
        if name.casefold() in seen:
            raise ValueError(f"云机列表有重复名称：{name}")
        if name not in manifest_names:
            raise ValueError(f"云机名称不在当前创建清单中：{name}")
        names.append(name)
        seen.add(name.casefold())
    if not names:
        raise ValueError("云机列表中没有有效名称")
    return names


def plan_pool_uploads(folder, names, count):
    if type(count) is not int or count < 1:
        raise ValueError("每台云机的素材数量必须是正整数")
    folder = folder.resolve(strict=True)
    if not folder.is_dir():
        raise ValueError(f"素材源路径不是文件夹：{folder}")
    files = eligible_files(folder)
    needed = len(names) * count
    if len(files) < needed:
        raise ValueError(f"素材不足：{len(files)} 个可用文件，{len(names)} 台云机各需 {count} 个，共需 {needed} 个")
    assets, hashes = [], {}
    cursor = 0
    for name in names:
        for path in files[cursor:cursor + count]:
            assets.append(checked_asset(path, len(assets), name, hashes=hashes))
        cursor += count
    return assets, len(list(folder.iterdir())) - len(files)


def task_files(path):
    path = path.expanduser().resolve(strict=True)
    if path.is_file():
        if path.suffix.lower() not in ALLOWED_EXTENSIONS:
            raise ValueError(f"不支持的素材格式：{path}")
        return [path], []
    if path.is_dir():
        files = eligible_files(path)
        if not files:
            raise ValueError(f"文件夹首层没有 MP4、PNG、JPG 或 JPEG 素材：{path}")
        skipped = [item.name for item in path.iterdir() if item.is_file() and item not in files]
        return files, skipped
    raise ValueError(f"任务路径不是文件或文件夹：{path}")


def parse_task_list(path, manifest_names):
    assets, skipped, seen_pairs, hashes = [], [], set(), {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 2:
            raise ValueError(f"任务清单第 {line_number} 行须为两列 Tab 分隔：云机名<TAB>绝对路径")
        name, source = fields[0].strip(), fields[1].strip()
        if name not in manifest_names:
            raise ValueError(f"任务清单第 {line_number} 行云机不在当前创建清单中：{name}")
        source_path = Path(source).expanduser()
        if not source_path.is_absolute():
            raise ValueError(f"任务清单第 {line_number} 行素材路径必须是绝对路径")
        files, ignored = task_files(source_path)
        skipped.extend({"line": line_number, "path": str(source_path), "file": item} for item in ignored)
        for file_path in files:
            key = (name.casefold(), os.path.normcase(str(file_path)))
            if key in seen_pairs:
                raise ValueError(f"任务清单重复指定同一云机和素材：{name} / {file_path}")
            seen_pairs.add(key)
            assets.append(checked_asset(file_path, len(assets), name, line_number, hashes))
    if not assets:
        raise ValueError("任务清单中没有可上传的素材")
    validate_remote_names(assets)
    return assets, skipped


def validate_remote_names(assets):
    seen = set()
    for asset in assets:
        key = (asset["device"].casefold(), asset["path"].name.casefold())
        if key in seen:
            raise ValueError(f"同一云机有重名素材，避免覆盖，已停止：{asset['device']} / {asset['path'].name}")
        seen.add(key)


def normalize_destination(value):
    parts = value.split("/")
    if not value.startswith("/") or any(part in (".", "..") for part in parts) or "\0" in value:
        raise ValueError("云机目标目录必须是绝对路径且不能包含 . 或 ..")
    normalized = "/" + "/".join(part for part in parts if part)
    if normalized == "/":
        raise ValueError("不能把素材上传到云机根目录")
    return normalized.rstrip("/") + "/"


def upload_file(curl, control_base, asset, destination, timeout):
    filename = asset["path"].name.replace("\\", "\\\\").replace('"', '\\"')
    file_form = f"file=@-;filename=\"{filename}\";type={asset['mime_type']}"
    try:
        with asset["path"].open("rb") as source:
            result = subprocess.run(
                [curl, "--disable", "--noproxy", "*", "--silent", "--show-error",
                 "--connect-timeout", "10", "--max-time", str(timeout),
                 "--form-string", "dir_path=" + destination,
                 "--form-string", "scan=false", "--form", file_form,
                 "--output", "-", "--write-out", "\n%{http_code}",
                 control_base.rstrip("/") + "/file/import"],
                stdin=source, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=False, timeout=timeout + 15, check=False)
        if result.returncode:
            return {"success": False, "status": "unconfirmed", "error": f"curl退出码 {result.returncode}"}
        try:
            body, status_text = result.stdout.rsplit(b"\n", 1)
            http_status = int(status_text.strip())
            payload = json.loads(body.decode("utf-8-sig"))
        except (ValueError, UnicodeError):
            return {"success": False, "status": "unconfirmed", "error": "上传响应无法解析"}
        if not isinstance(payload, dict) or not 200 <= http_status < 300 or str(payload.get("code")) != "200":
            return {"success": False, "status": "failed", "http_status": http_status,
                    "api_code": payload.get("code") if isinstance(payload, dict) else None,
                    "error": "云机文件导入接口未确认成功"}
        data = payload.get("data")
        files = data.get("files") if isinstance(data, dict) else None
        if not isinstance(files, list) or len(files) != 1 or not isinstance(files[0], dict):
            return {"success": False, "status": "unconfirmed", "http_status": http_status,
                    "error": "接口成功响应未返回唯一文件记录"}
        record = files[0]
        remote_path = record.get("file_path")
        expected_path = destination + asset["path"].name
        if not isinstance(remote_path, str) or remote_path != expected_path:
            return {"success": False, "status": "unconfirmed", "http_status": http_status,
                    "remote_path": remote_path, "error": "云机返回路径与指定目标不一致或缺失"}
        remote_size = record.get("file_size")
        if isinstance(remote_size, bool) or not isinstance(remote_size, int) or remote_size != asset["size_bytes"]:
            return {"success": False, "status": "unconfirmed", "http_status": http_status,
                    "remote_path": remote_path, "remote_size": remote_size,
                    "error": "云机返回的文件大小与本地文件不一致或未提供"}
        return {"success": True, "status": "uploaded", "http_status": http_status,
                "remote_path": remote_path, "remote_size": remote_size,
                "scanned": record.get("scanned", False)}
    except (OSError, subprocess.SubprocessError) as error:
        return {"success": False, "status": "unconfirmed", "error": type(error).__name__}


def archive_file(path):
    if path.parent.name == "已上传":
        return {"success": True, "path": str(path), "already_archived": True}
    directory = path.parent / "已上传"
    directory.mkdir(exist_ok=True)
    target = directory / path.name
    index = 1
    while target.exists():
        target = directory / f"{path.stem} ({index}){path.suffix}"
        index += 1
    shutil.move(str(path), str(target))
    return {"success": True, "path": str(target), "already_archived": False}


def resolve_instances(host, names):
    setup = ProxySetup(host)
    instances = setup.device.list_instances()
    selected = {}
    for name in names:
        matches = [item for item in instances.values()
                   if (item.get("user_name") or item.get("name")) == name]
        if len(matches) != 1:
            raise ValueError(f"云机 {name} 在 VMOS 中不存在或名称重复，未上传")
        instance = matches[0]
        db_id = instance.get("db_id")
        if not isinstance(db_id, str) or not re.fullmatch(r"EDGE[A-Za-z0-9]+", db_id):
            raise ValueError(f"云机 {name} 的实例ID无效，未上传")
        state = instance.get("state") or instance.get("status")
        if state not in ("running", "stopped"):
            raise ValueError(f"云机 {name} 当前状态为 {state or 'unknown'}，未上传")
        port = instance.get("http_api_port")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError(f"云机 {name} 的独立控制端口无效，未上传")
        selected[name] = {"db_id": db_id, "state": state}
    all_ports = [item.get("http_api_port") for item in instances.values()]
    for name, item in selected.items():
        if all_ports.count(next(instance.get("http_api_port") for instance in instances.values()
                                if instance.get("db_id") == item["db_id"])) != 1:
            raise ValueError(f"云机 {name} 的独立控制端口重复，未上传")
    return selected


def upload_device(host, curl, name, target, assets, destination, timeout, report_path):
    db_id, original_state = target["db_id"], target["state"]
    setup = ProxySetup(host, output_dir=report_path.parent)
    setup.log = lambda message: print(f"{name}：{message}", flush=True)
    setup.device.log = setup.log
    report = {"name": name, "db_id": db_id, "original_state": original_state,
              "uploads": [], "success": False, "state_restored": False}
    for asset in assets:
        report["uploads"].append({"asset_id": asset["asset_id"], "file": asset["path"].name,
                                  "source_path": str(asset["path"]), "size_bytes": asset["size_bytes"],
                                  "sha256": asset["sha256"], "task_line": asset["task_line"],
                                  "status": "not_started", "success": False})
    save_json(report_path, report)
    start_requested = original_state == "stopped"
    try:
        def check_identity():
            current = setup.device.list_instances()
            matches = [item for item in current.values()
                       if item.get("db_id") == db_id or (item.get("user_name") or item.get("name")) == name]
            if (len(matches) != 1 or matches[0].get("db_id") != db_id
                    or (matches[0].get("user_name") or matches[0].get("name")) != name):
                raise ValueError("云机名称与实例ID未唯一对应")
            return matches[0]

        check_identity()
        if start_requested:
            try:
                response = setup.device.post_action("run", [db_id])
                report["start_response_code"] = response.get("code")
            except (requests.RequestException, ValueError) as error:
                report["start_response_uncertain"] = type(error).__name__

            def check_running():
                instance = check_identity()
                if (instance.get("state") or instance.get("status")) != "running":
                    raise ValueError("云机仍未运行")
                return instance

            poll_readback(check_running, setup.log, "启动状态回查", timeout=120, interval=2)
        if setup.device.wait_ready([db_id], timeout=120) != [db_id]:
            raise ValueError("云机ROM未就绪")
        setup.wait_android_ready(db_id, wait_time=120)
        check_identity()
        control_base = setup.control_base(db_id)
        for index, asset in enumerate(assets):
            stat = asset["path"].stat()
            if (stat.st_size != asset["size_bytes"] or stat.st_mtime_ns != asset["mtime_ns"]
                    or file_sha256(asset["path"]) != asset["sha256"]):
                result = {"success": False, "status": "failed", "error": "上传前素材内容已变化"}
            else:
                print(f"上传素材：{name} / {asset['path'].name}", flush=True)
                result = upload_file(curl, control_base, asset, destination, timeout)
            report["uploads"][index].update(result)
            save_json(report_path, report)
    except Exception as error:
        report["error"] = str(error) if isinstance(error, ValueError) else type(error).__name__
        for entry in report["uploads"]:
            if entry["status"] == "not_started":
                entry.update(status="not_started", error=report["error"])
    finally:
        try:
            if original_state == "stopped":
                report["restore_state"] = setup.device.restore_stopped([db_id], timeout=120)
                report["final_state"] = report["restore_state"].get("states", {}).get(db_id, "unknown")
                report["state_restored"] = (report["restore_state"].get("success") is True
                                             and report["final_state"] == "stopped")
            else:
                current = setup.device.list_instances().get(db_id, {})
                if (current.get("user_name") or current.get("name")) != name:
                    raise ValueError("最终云机身份未确认")
                report["final_state"] = current.get("state") or current.get("status") or "unknown"
                report["state_restored"] = report["final_state"] == original_state
        except Exception as error:
            report["restore_error"] = str(error) if isinstance(error, ValueError) else type(error).__name__
            report["state_restored"] = False
        report["success"] = (report["state_restored"] is True
                             and all(item["success"] for item in report["uploads"]))
        save_json(report_path, report)
    return report


def load_target_names(config_path):
    config = read_json(config_path)
    if not isinstance(config, dict) or not isinstance(config.get("devices"), list) or not config["devices"]:
        raise ValueError("创建清单缺少非空 devices 数组")
    names = [item.get("user_name") if isinstance(item, dict) else None for item in config["devices"]]
    if any(not isinstance(name, str) or not DEVICE_NAME.fullmatch(name) for name in names):
        raise ValueError("创建清单中的云机名称无效")
    if len({name.casefold() for name in names}) != len(names):
        raise ValueError("创建清单存在重复云机名称")
    main = read_json(ROOT / "配置文件" / "config.json")
    if (main.get("vmos", {}).get("host_ip") != config.get("host_ip")
            or main.get("vmos", {}).get("port") != 18182):
        raise ValueError("创建清单与主配置中的 VMOS 地址或端口不一致")
    return config["host_ip"], set(names)


def run(args):
    if args.workers < 1 or args.timeout < 10:
        raise ValueError("--workers 必须至少为1，--timeout 必须至少为10秒")
    destination = normalize_destination(args.destination)
    config_path = args.config.resolve()
    host_ip, manifest_names = load_target_names(config_path)
    if args.pool:
        if args.devices is None:
            raise ValueError("模式1必须指定 --devices 云机名称TXT")
        names = parse_device_list(args.devices.resolve(strict=True), manifest_names)
        assets, skipped_count = plan_pool_uploads(args.pool, names, args.count)
        mode = "pool"
        skipped = [{"unsupported_or_subfolder_entries": skipped_count}]
    else:
        assets, skipped = parse_task_list(args.tasks.resolve(strict=True), manifest_names)
        names = list(dict.fromkeys(asset["device"] for asset in assets))
        mode = "tasks"
    validate_remote_names(assets)
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not curl:
        raise ValueError("找不到 curl.exe，未连接云机")
    targets = resolve_instances(f"http://{host_ip}:18182", names)

    log_root = ROOT / "日志"
    log_root.mkdir(exist_ok=True)
    job = Path(tempfile.mkdtemp(prefix="upload-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-",
                                dir=log_root))
    grouped = {name: [asset for asset in assets if asset["device"] == name] for name in names}
    report_paths = {name: job / "instances" / targets[name]["db_id"] / "upload_status.json" for name in names}
    summary = {"success": False, "mode": mode, "destination": destination, "device_count": len(names),
               "asset_count": len(assets), "workers": min(args.workers, len(names)), "skipped": skipped,
               "devices": [], "failed_names": []}
    save_json(job / "batch_status.json", summary)
    print(f"素材上传：{len(assets)} 个文件，{len(names)} 台云机；结果目录：{job}", flush=True)

    reports = {}
    with ThreadPoolExecutor(max_workers=summary["workers"]) as executor:
        futures = {executor.submit(upload_device, f"http://{host_ip}:18182", curl, name, targets[name],
                                   grouped[name], destination, args.timeout, report_paths[name]): name
                   for name in names}
        for future in as_completed(futures):
            name = futures[future]
            try:
                reports[name] = future.result()
            except Exception as error:
                reports[name] = {"name": name, "db_id": targets[name]["db_id"], "success": False,
                                 "state_restored": False, "error": type(error).__name__, "uploads": []}
            summary["devices"] = [reports[key] for key in names if key in reports]
            save_json(job / "batch_status.json", summary)

    by_source = {}
    for asset in assets:
        by_source.setdefault(os.path.normcase(str(asset["path"])), {"path": asset["path"], "assets": []})["assets"].append(asset)
    for source in by_source.values():
        related = source["assets"]
        entries = [next(entry for entry in reports[asset["device"]]["uploads"]
                        if entry["asset_id"] == asset["asset_id"])
                   for asset in related]
        if all(entry["success"] for entry in entries):
            try:
                archive = archive_file(source["path"])
                for entry in entries:
                    entry["archive"] = archive
            except OSError as error:
                archive = {"success": False, "error": type(error).__name__}
                for entry in entries:
                    entry["archive"] = archive
        else:
            archive = {"success": False, "error": "至少一个目标上传未确认，源文件保留"}
            for entry in entries:
                entry["archive"] = archive

    for name, report in reports.items():
        report["success"] = (report.get("state_restored") is True
                             and all(item.get("success") is True and item.get("archive", {}).get("success") is True
                                     for item in report.get("uploads", [])))
        save_json(report_paths[name], report)
    summary["devices"] = [reports[name] for name in names]
    summary["success_count"] = sum(report["success"] for report in summary["devices"])
    summary["failed_names"] = [report["name"] for report in summary["devices"] if not report["success"]]
    summary["success"] = not summary["failed_names"]
    save_json(job / "batch_status.json", summary)
    print(f"素材上传结束：{summary['success_count']}/{len(names)} 台完成；结果：{job}", flush=True)
    return 0 if summary["success"] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--pool", type=Path, help="模式1：本地素材文件夹，只读取首层")
    modes.add_argument("--tasks", type=Path, help="模式2：Tab分隔的云机名与绝对路径任务清单")
    parser.add_argument("--devices", type=Path, help="模式1：一行一个云机名的TXT")
    parser.add_argument("--count", type=int, default=1, help="模式1：每台云机上传数量，默认1")
    parser.add_argument("--config", type=Path,
                        default=ROOT / "阶段9-脚本开发与集成" / "batch10_create.example.json",
                        help="云机名称清单JSON")
    parser.add_argument("--destination", default=DEFAULT_DESTINATION, help="云机内目标目录")
    parser.add_argument("--workers", type=int, default=1, help="同时处理的云机数，默认1")
    parser.add_argument("--timeout", type=int, default=600, help="单文件上传超时秒数，默认600")
    args = parser.parse_args(argv)
    try:
        return run(args)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"素材上传未完成：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
