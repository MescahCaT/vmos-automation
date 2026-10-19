"""按清单创建并初始化云机：代理及中转、地区、联网验证、TikTok安装；已有云机用 --configure-only。"""
import argparse
import copy
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading

import requests


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "阶段2-云机批量创建与初始化"))
sys.path.insert(0, str(ROOT / "阶段9-脚本开发与集成"))
sys.path.insert(0, str(ROOT / "阶段3-网络代理配置"))
from create_devices import DeviceCreator
from setup_proxy import MAPPING_LOCK, ProxySetup, proxy_body
from setup_system import SystemSetup, region_settings
from install_tiktok import TikTokInstaller
from text_import_common import ensure_transit, proxy_index, read_json, write_json_files
from check_vmos_connection import check_apk, check_proxy, save_json


def load_resources(config, names, bindings_path, operation="initialize"):
    main = read_json(ROOT / "配置文件" / "config.json")
    if main["vmos"]["host_ip"] != config["host_ip"] or main["vmos"]["port"] != 18182:
        raise ValueError("创建清单与主配置中的 VMOS 地址或端口不一致")
    bindings = read_json(bindings_path)
    proxy_path = ROOT / main["proxy"]["config_file"]
    document = read_json(proxy_path)
    before = copy.deepcopy(document)
    indexed = proxy_index(document)
    selected = {}
    for name in names:
        proxy_id = bindings.get(name) if isinstance(bindings, dict) else None
        if not isinstance(proxy_id, str) or proxy_id not in indexed or indexed[proxy_id].get("enabled", True) is not True:
            raise ValueError(f"云机 {name} 缺少有效的代理对应关系")
        selected[proxy_id] = indexed[proxy_id]
    if operation in ("initialize", "proxy"):
        ensure_transit(document, list(selected.values()))
    for proxy in selected.values():
        if operation in ("initialize", "proxy"):
            proxy_body(proxy)
        if operation != "proxy":
            try:
                region_settings(proxy)
            except ValueError as error:
                raise ValueError(f"代理组 {proxy['id']} 地区参数无效：{error}；必须补齐，不能跳过地区初始化") from None
    if document != before:
        for backup in write_json_files({proxy_path: document}):
            print(f"已补齐本批中转，代理配置原件备份：{backup}", flush=True)
    return main, bindings, selected


def configure_region(setup, db_id, proxy, main, report):
    """仅设置地区；GPS未指定时，从已有代理的实际出口解析。"""
    instance = setup.device.list_instances().get(db_id, {})
    state = instance.get("state") or instance.get("status")
    report.update(success=False, original_state=state, started_by_script=False, system={})
    try:
        if state == "stopped":
            report["started_by_script"] = True
            try:
                response = setup.device.post_action("run", [db_id])
                if str(response.get("code")) != "200":
                    raise ValueError("VMOS拒绝启动目标云机")
            except requests.RequestException:
                setup.log("启动响应不明确，只回查ROM状态；未重发")
        elif state != "running":
            raise ValueError("云机未处于运行或停机状态，未设置地区")
        if setup.device.wait_ready([db_id]) != [db_id]:
            raise ValueError("ROM未就绪，未设置地区")
        setup.wait_android_ready(db_id)
        system = SystemSetup(setup)
        expected = region_settings(proxy)
        if "latitude" not in expected:
            local = check_proxy(proxy, main["proxy"])
            actual = setup.cloud_json(db_id, main["proxy"]["probe_url"], main["proxy"]["timeout"])
            if actual.get("ip") != local["exit_ip"]:
                raise ValueError("GPS未指定且云机出口与配置代理不一致，请先设置代理")
            expected = system.resolve(db_id, proxy, local["exit_ip"])
        with MAPPING_LOCK:
            previous = read_json(setup.mapping_path)["devices"][db_id].get("system_config")
        system.configure(db_id, expected, report["system"], previous=previous)
        if report["system"].get("success") is not True:
            raise ValueError("国家、时区、语言或GPS未确认")
        with MAPPING_LOCK:
            mapping = read_json(setup.mapping_path)
            mapping["devices"][db_id]["system_config"] = {
                "expected": expected, "country_api_confirmed": report["system"].get("country_api_confirmed") is True,
                "gps_persisted": report["system"].get("gps_persisted") is True,
                "verified_at": datetime.now().astimezone().isoformat(timespec="seconds")}
            save_json(setup.mapping_path, mapping)
        report["success"] = True
    except Exception as error:
        report["error"] = str(error) if isinstance(error, ValueError) else type(error).__name__
        raise
    finally:
        save_json(setup.output_dir / "system_status.json", report)


def configure(devices, main, bindings, proxies, job, instances, apk, workers=1, operation="initialize"):
    host = f"http://{main['vmos']['host_ip']}:{main['vmos']['port']}"
    mapping_path = ROOT / "数据" / "device_mapping.json"
    by_id = {item["db_id"]: item for item in instances}
    with MAPPING_LOCK:
        mapping = read_json(mapping_path) if mapping_path.exists() else {"host": host, "devices": {}}
        if mapping.get("host") != host or not isinstance(mapping.get("devices"), dict):
            raise ValueError("设备映射的主机或格式不匹配，未绑定代理")
        for device in devices:
            db_id = device["db_id"]
            instance = by_id.get(db_id)
            if instance is None or (instance.get("user_name") or instance.get("name")) != device["user_name"]:
                raise ValueError("实际云机ID与清单名称不匹配，未绑定代理")
            record = mapping["devices"].setdefault(db_id, {"proxy_id": None, "account_email": None})
            record.update(name=device["user_name"], state=instance.get("state") or instance.get("status"), present=True)
        save_json(mapping_path, mapping)
    if bindings:
        save_json(job / "bindings.json", {device["user_name"]: bindings[device["user_name"]] for device in devices})
    workers = min(workers, len(devices))
    label = {"initialize": "完整初始化", "proxy": "代理设置与联网验证", "region": "地区设置", "install": "TikTok安装"}[operation]
    summary = {"success": False, "operation": operation, "total": len(devices), "workers": workers, "devices": []}
    save_json(job / "batch_status.json", summary)
    print(f"{label}并发数：{workers}", flush=True)
    status_lock = threading.Lock()
    stop_dispatch = threading.Event()
    records = [None] * len(devices)

    def publish(index, record):
        with status_lock:
            records[index] = dict(record)
            summary["devices"] = [item for item in records if item is not None]
            save_json(job / "batch_status.json", summary)

    def configure_one(index, device):
        db_id, name = device["db_id"], device["user_name"]
        proxy = proxies[bindings[name]] if bindings else None
        print(f"{label}：{name}", flush=True)
        record = {"name": name, "db_id": db_id, "proxy_id": proxy["id"] if proxy else None,
                  "success": False, "system_requested": operation in ("initialize", "region"),
                  "system_configured": False, "network_verified": False,
                  "installation_requested": operation in ("initialize", "install"), "installation_attempted": False,
                  "tiktok_installed": False, "install_status": "not_started",
                  "report": str(job / db_id / {"region": "system_status.json", "install": "install_status.json"}.get(operation, "proxy_status.json"))}
        publish(index, record)
        setup = ProxySetup(host, output_dir=job / db_id, mapping_path=mapping_path)
        proxy_log = setup.log
        setup.log = lambda message: proxy_log(f"{name}：{message}")
        setup.device.log = setup.log
        result = {}
        original_state = by_id[db_id].get("state") or by_id[db_id].get("status")
        restore_required = original_state == "stopped"
        try:
            # 初始化入口统一管理停机，避免代理配置后停机、安装时再次启动。
            if operation in ("initialize", "proxy"):
                result = setup.run(db_id, proxy, main["proxy"], start_stopped=True,
                                   wait_time=60, sync_system=record["system_requested"], restore_state=False)
            elif operation == "region":
                configure_region(setup, db_id, proxy, main, result)
            else:
                result = {"success": True}
            original_state = result.get("original_state", original_state)
            restore_required = result.get("started_by_script", restore_required)
            record.update(system_configured=result.get("system", {}).get("success") is True,
                          network_verified=result.get("verification", {}).get("verified") is True)
            if result.get("success") is not True:
                stop_dispatch.set()
            if operation == "install" or (operation == "initialize" and record["system_configured"]):
                # 本地APK不依赖外网；网页失败也安装，但不将联网结果改为成功。
                record.update(installation_attempted=True, install_report=str(job / db_id / "install_status.json"))
                publish(index, record)
                print(f"安装TikTok：{name}，使用 {Path(apk['path']).name}", flush=True)
                installer = TikTokInstaller(host, output_dir=job / db_id, package_name=main["tiktok"]["package_name"])
                install_log = installer.log
                installer.log = lambda message: install_log(f"{name}：{message}")
                installed = installer.install_from_local(apk["path"], [db_id], verify=True,
                                                         start_stopped=operation == "install", restore_state=False)
                target = installed.get("devices", {}).get(db_id, {})
                record.update(install_status=target.get("status", "unconfirmed"),
                              tiktok_installed=(installed.get("success") is True
                                  and target.get("status") in ("installed", "already_installed")
                                  and target.get("package_verified") is True))
                if not record["tiktok_installed"]:
                    record["install_error"] = target.get("error") or "请查看安装报告"
            if result.get("success") is not True:
                record["error"] = result.get("error") or f"{label}未确认"
            elif record["system_requested"] and not record["system_configured"]:
                record["error"] = "国家、时区、语言或GPS未确认"
            if record["installation_attempted"] and not record["tiktok_installed"]:
                stop_dispatch.set()
                record["error"] = (((record["error"] + "；") if record.get("error") else "")
                                   + "TikTok安装未确认：" + record.get("install_error", "请查看安装报告"))
        except Exception as error:
            stop_dispatch.set()
            record["error"] = str(error)
            if record["installation_attempted"] and not record["tiktok_installed"]:
                record["install_status"] = "unconfirmed"
        finally:
            try:
                if restore_required:
                    record["restore_state"] = setup.device.restore_stopped([db_id])
                record["state_restored"] = not restore_required or record["restore_state"].get("success") is True
                final = setup.device.list_instances().get(db_id, {})
                record["final_state"] = final.get("state") or final.get("status") or "unknown"
                record["state_restored"] = record["state_restored"] and record["final_state"] == original_state
                if not record["state_restored"]:
                    record["restore_error"] = record.get("restore_state", {}).get("error") or "恢复原运行/停机状态未确认"
                    record["error"] = (((record["error"] + "；") if record.get("error") else "")
                                       + "恢复原运行/停机状态未确认：" + record["restore_error"])
                with MAPPING_LOCK:
                    latest = read_json(mapping_path)
                    if latest.get("host") != host or db_id not in latest.get("devices", {}):
                        raise ValueError("设备映射主机或目标不匹配，未更新最终状态")
                    latest["devices"][db_id]["state"] = record["final_state"]
                    save_json(mapping_path, latest)
            except Exception as error:
                record["state_restored"] = False
                record["error"] = (((record["error"] + "；") if record.get("error") else "")
                                   + "最终状态更新失败: " + str(error))
            record["success"] = (result.get("success") is True
                                 and (not record["system_requested"] or record["system_configured"])
                                 and (operation not in ("initialize", "proxy") or record["network_verified"])
                                 and (not record["installation_requested"] or record["tiktok_installed"])
                                 and record["state_restored"] and not record.get("error"))
            if not record["success"]:
                stop_dispatch.set()
            publish(index, record)
        if operation == "initialize":
            print(f"{name}：地区初始化{'通过' if record['system_configured'] else '未确认'}，"
                  f"联网验证{'通过' if record['network_verified'] else '未通过'}，"
                  f"TikTok安装{'已确认' if record['tiktok_installed'] else '未确认'}", flush=True)
        else:
            print(f"{name}：{label}{'通过' if record['success'] else '未确认'}", flush=True)
        return record

    # 只保留最多workers个在途任务，失败后不再向线程池排入新设备。
    next_index = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = set()
        while next_index < len(devices) or pending:
            while not stop_dispatch.is_set() and next_index < len(devices) and len(pending) < workers:
                pending.add(pool.submit(configure_one, next_index, devices[next_index]))
                next_index += 1
            if not pending:
                break
            completed, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in completed:
                try:
                    future.result()
                except Exception as error:
                    stop_dispatch.set()
                    with status_lock:
                        summary.setdefault("errors", []).append(str(error))
    summary["not_started"] = [device["user_name"] for index, device in enumerate(devices) if records[index] is None]
    summary["success"] = (not stop_dispatch.is_set() and not summary["not_started"]
                          and all(record["success"] for record in summary["devices"]))
    save_json(job / "batch_status.json", summary)
    if not summary["success"]:
        failures = [record for record in summary["devices"] if not record["success"]]
        reason = "；".join(f"{record['name']}：{record.get('error') or '初始化或最终状态未确认'}" for record in failures)
        reason = reason or "；".join(summary.get("errors", [])) or "初始化未全部确认"
        raise ValueError(f"{reason}；已停止后续配置，已启动任务均已收尾；查看 {job}，请勿重复创建")
    print(f"{label}全部确认：{len(devices)}/{len(devices)} 成功。结果目录：{job}")


def create(config_path, configure_only=False, name=None, bindings_path=None, workers=1,
           selected_names=None, create_only=False, operation="initialize"):
    if type(workers) is not int or workers < 1:
        raise ValueError("并发数 --workers 必须为大于等于1的整数")
    print(f"读取创建配置：{config_path}", flush=True)
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    devices = config.get("devices") if isinstance(config, dict) else None
    if not isinstance(devices, list) or not devices:
        raise ValueError("创建清单必须包含非空 devices 数组，请先生成清单")
    names = [device.get("user_name") if isinstance(device, dict) else None for device in devices]
    if any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{3,}", name) for name in names):
        raise ValueError("云机名称必须至少3位，只含字母、数字、点、下划线或横线")
    if len({name.casefold() for name in names}) != len(names):
        raise ValueError("创建清单有重复名称，请先修改")
    if not config.get("host_ip") or not isinstance(config.get("default_config"), dict) or not config["default_config"]:
        raise ValueError("创建清单缺少主机地址或公共参数")
    requested_names = [name] if name is not None else selected_names
    # 指定名称必须全部存在，不能悄悄忽略。
    if requested_names is not None:
        if len({item.casefold() for item in requested_names}) != len(requested_names):
            raise ValueError("指定的云机名称重复")
        missing = [item for item in requested_names if item not in names]
        if missing:
            raise ValueError("指定名称不在本批清单中：" + "、".join(missing))
        config["devices"] = [device for device in devices if device["user_name"] in requested_names]
    names = [device["user_name"] for device in config["devices"]]
    config["count"] = len(names)
    bindings_path = bindings_path or ROOT / "阶段9-脚本开发与集成" / "batch10_bindings.example.json"
    if not create_only and operation != "install":
        main, bindings, proxies = load_resources(config, names, bindings_path, operation)
    else:
        main = read_json(ROOT / "配置文件" / "config.json")
        if main["vmos"]["host_ip"] != config["host_ip"] or main["vmos"]["port"] != 18182:
            raise ValueError("创建清单与主配置中的 VMOS 地址或端口不一致")
        bindings, proxies = {}, {}
    apk = None
    if not create_only and operation in ("initialize", "install"):
        apk = check_apk(main["tiktok"], ROOT)
        print(f"TikTok安装包：{apk['path']}（版本 {apk['version_name']}）", flush=True)

    logs = ROOT / "日志"
    logs.mkdir(exist_ok=True)
    prefix = {"initialize": "create", "proxy": "proxy", "region": "region", "install": "install"}[operation]
    job = Path(tempfile.mkdtemp(prefix=prefix + "-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-", dir=logs))
    snapshot = job / "devices_input.json"
    snapshot.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if apk is not None:
        save_json(job / "apk_input.json", apk)
    print(f"本批清单：{len(names)} 台，主机：{config['host_ip']}", flush=True)
    print(f"结果目录：{job}", flush=True)
    creator = DeviceCreator(str(snapshot))
    instances = creator._list_instances()
    if configure_only:
        selected = []
        for target in names:
            matches = [item for item in instances if (item.get("user_name") or item.get("name")) == target]
            if len(matches) != 1:
                raise ValueError(f"云机 {target} 不存在或同名不唯一，未执行补配置")
            selected.append({"user_name": target, "db_id": matches[0]["db_id"]})
        configure(selected, main, bindings, proxies, job, instances, apk, workers, operation)
        return 0
    requested = {name.casefold() for name in names}
    existing = sorted({name for instance in instances
                       if isinstance(name := instance.get("user_name") or instance.get("name"), str)
                       and name.casefold() in requested})
    if existing:
        raise ValueError("主机已有同名云机，已停止创建：" + "、".join(existing) + "；已有云机补代理请加 --configure-only")

    previous_directory = Path.cwd()
    try:
        os.chdir(job)
        try:
            creator.create_batch()
        finally:
            creator.save_results()
    finally:
        os.chdir(previous_directory)
    results = creator.creation_results
    created = creator.created_devices
    if (len(results) != len(names) or any(item.get("status") != "success" for item in results)
            or len(created) != len(names) or {item.get("user_name") for item in created} != set(names)
            or any(not item.get("db_id") for item in created)
            or len({item["db_id"] for item in created}) != len(names)):
        raise ValueError(f"本批未全部确认成功，请查看 {job}；先核对结果，勿整批重跑")
    if create_only:
        save_json(job / "batch_status.json", {"success": True, "operation": "create", "total": len(names),
                                            "devices": created, "not_started": []})
        print(f"仅创建全部确认：{len(names)}/{len(names)} 成功。结果目录：{job}")
        return 0
    print(f"全部创建确认：{len(names)}/{len(names)} 成功，开始绑定代理及中转。")
    configure(created, main, bindings, proxies, job, creator._list_instances(), apk, workers, operation)
    return 0


def main(argv=None, operation="initialize", create_only=False):
    descriptions = {"initialize": __doc__, "proxy": "仅设置代理及中转并验证联网，支持按名称选择一台或多台。",
                    "region": "仅设置国家、时区、语言、GPS及重启核验，支持按名称选择一台或多台。",
                    "install": "仅安装TikTok并核验包名，支持按名称选择一台或多台。"}
    description = "仅创建并确认云机，不执行代理、地区设置或TikTok安装。" if create_only else descriptions[operation]
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("config", nargs="?", type=Path,
                        default=ROOT / "阶段9-脚本开发与集成" / "batch10_create.example.json",
                        help="可选创建 JSON 路径，默认读取已生成的清单")
    parser.add_argument("--bindings", type=Path, help="可选的云机名称→代理组对应表")
    if operation == "initialize" and not create_only:
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--configure-only", action="store_true", help="给本批已有云机补代理、中转、地区和TikTok安装，不创建")
        mode.add_argument("--create-only", action="store_true", help="仅创建并确认结果，不执行代理、地区或安装")
    targets = parser.add_mutually_exclusive_group()
    targets.add_argument("--name", help="仅处理清单中的一台，例如 --name 145.2")
    targets.add_argument("--names", nargs="+", help="仅处理清单中的一台或多台，空格分隔，例如 --names 145.2 146.1")
    worker_help = ("仅创建阶段仍逐台提交，此参数保留兼容，默认1"
                   if create_only else "配置或安装的并发数，默认1；创建阶段仍逐台提交，例如 --workers 3")
    parser.add_argument("--workers", type=int, default=1, help=worker_help)
    args = parser.parse_args(argv)
    try:
        return create(args.config.resolve(), getattr(args, "configure_only", operation != "initialize"), args.name,
                      args.bindings.resolve() if args.bindings else None, args.workers,
                      args.names, create_only or getattr(args, "create_only", False), operation)
    except Exception as error:
        print(f"任务未完成：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
