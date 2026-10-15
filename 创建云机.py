"""按清单创建并初始化云机：代理及中转、地区、联网验证、TikTok安装；已有云机用 --configure-only。"""
import argparse
import copy
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sys
import tempfile


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "阶段2-云机批量创建与初始化"))
sys.path.insert(0, str(ROOT / "阶段9-脚本开发与集成"))
sys.path.insert(0, str(ROOT / "阶段3-网络代理配置"))
from create_devices import DeviceCreator
from setup_proxy import ProxySetup, proxy_body
from setup_system import region_settings
from install_tiktok import TikTokInstaller
from text_import_common import ensure_transit, proxy_index, read_json, write_json_files
from check_vmos_connection import check_apk, save_json


def load_resources(config, names, bindings_path):
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
    ensure_transit(document, list(selected.values()))
    for proxy in selected.values():
        proxy_body(proxy)
        try:
            region_settings(proxy)
        except ValueError as error:
            raise ValueError(f"代理组 {proxy['id']} 地区参数无效：{error}；必须补齐，不能跳过地区初始化") from None
    if document != before:
        for backup in write_json_files({proxy_path: document}):
            print(f"已补齐本批中转，代理配置原件备份：{backup}", flush=True)
    return main, bindings, selected


def configure(devices, main, bindings, proxies, job, instances, apk):
    host = f"http://{main['vmos']['host_ip']}:{main['vmos']['port']}"
    mapping_path = ROOT / "数据" / "device_mapping.json"
    mapping = read_json(mapping_path) if mapping_path.exists() else {"host": host, "devices": {}}
    if mapping.get("host") != host or not isinstance(mapping.get("devices"), dict):
        raise ValueError("设备映射的主机或格式不匹配，未绑定代理")
    by_id = {item["db_id"]: item for item in instances}
    for device in devices:
        db_id = device["db_id"]
        instance = by_id.get(db_id)
        if instance is None or (instance.get("user_name") or instance.get("name")) != device["user_name"]:
            raise ValueError("实际云机ID与清单名称不匹配，未绑定代理")
        record = mapping["devices"].setdefault(db_id, {"proxy_id": None, "account_email": None})
        record.update(name=device["user_name"], state=instance.get("state") or instance.get("status"), present=True)
    save_json(mapping_path, mapping)
    save_json(job / "bindings.json", {device["user_name"]: bindings[device["user_name"]] for device in devices})
    summary = {"success": False, "total": len(devices), "devices": []}
    save_json(job / "batch_status.json", summary)
    for device in devices:
        db_id, name = device["db_id"], device["user_name"]
        proxy = proxies[bindings[name]]
        print(f"绑定 {name} → 代理组 {proxy['id']}＋中转", flush=True)
        record = {"name": name, "db_id": db_id, "proxy_id": proxy["id"],
                  "success": False, "system_requested": True,
                  "system_configured": False, "network_verified": False,
                  "installation_requested": True, "installation_attempted": False,
                  "tiktok_installed": False, "install_status": "not_started",
                  "report": str(job / db_id / "proxy_status.json")}
        summary["devices"].append(record)
        save_json(job / "batch_status.json", summary)
        setup = ProxySetup(host, output_dir=job / db_id, mapping_path=mapping_path)
        result = {}
        original_state = by_id[db_id].get("state") or by_id[db_id].get("status")
        restore_required = original_state == "stopped"
        try:
            # 初始化入口统一管理停机，避免代理配置后停机、安装时再次启动。
            result = setup.run(db_id, proxy, main["proxy"], start_stopped=True,
                               wait_time=60, sync_system=True, restore_state=False)
            original_state = result.get("original_state", original_state)
            restore_required = result.get("started_by_script", restore_required)
            record.update(system_configured=result.get("system", {}).get("success") is True,
                          network_verified=result.get("verification", {}).get("verified") is True)
            if record["system_configured"]:
                # 本地APK不依赖外网；网页失败也安装，但不将联网结果改为成功。
                record.update(installation_attempted=True, install_report=str(job / db_id / "install_status.json"))
                save_json(job / "batch_status.json", summary)
                print(f"安装TikTok：{name}，使用 {Path(apk['path']).name}", flush=True)
                installer = TikTokInstaller(host, output_dir=job / db_id, package_name=main["tiktok"]["package_name"])
                installed = installer.install_from_local(apk["path"], [db_id], verify=True,
                                                         start_stopped=False, restore_state=False)
                target = installed.get("devices", {}).get(db_id, {})
                record.update(install_status=target.get("status", "unconfirmed"),
                              tiktok_installed=(installed.get("success") is True
                                  and target.get("status") in ("installed", "already_installed")
                                  and target.get("package_verified") is True))
            if result.get("success") is not True:
                record["error"] = result.get("error") or "代理配置或联网未确认"
            elif not record["system_configured"]:
                record["error"] = "国家、时区、语言或GPS未确认"
            if record["installation_attempted"] and not record["tiktok_installed"]:
                record["error"] = ((record["error"] + "；") if record.get("error") else "") + "TikTok安装未确认，请查看安装报告"
        except Exception as error:
            record["error"] = str(error)
            if record["installation_attempted"] and not record["tiktok_installed"]:
                record["install_status"] = "unconfirmed"
        finally:
            try:
                record["state_restored"] = not restore_required or setup.device.restore_stopped([db_id]).get("success") is True
                final = setup.device.list_instances().get(db_id, {})
                record["final_state"] = final.get("state") or final.get("status") or "unknown"
                record["state_restored"] = record["state_restored"] and record["final_state"] == original_state
                if not record["state_restored"]:
                    record["error"] = "恢复原运行/停机状态未确认"
                latest = read_json(mapping_path)
                if latest.get("host") != host or db_id not in latest.get("devices", {}):
                    raise ValueError("设备映射主机或目标不匹配，未更新最终状态")
                latest["devices"][db_id]["state"] = record["final_state"]
                save_json(mapping_path, latest)
            except Exception as error:
                record.update(state_restored=False, error="最终状态更新失败: " + str(error))
            record["success"] = (result.get("success") is True and record["system_configured"]
                                 and record["network_verified"] and record["tiktok_installed"]
                                 and record["state_restored"] and not record.get("error"))
            save_json(job / "batch_status.json", summary)
        print(f"{name}：地区初始化{'通过' if record['system_configured'] else '未确认'}，"
              f"联网验证{'通过' if record['network_verified'] else '未通过'}，"
              f"TikTok安装{'已确认' if record['tiktok_installed'] else '未确认'}", flush=True)
        if not record["success"]:
            reason = record.get("error") or "初始化或最终状态未确认"
            raise ValueError(f"{name} 初始化未全部通过：{reason}；已停止后续配置；查看 {job}，请勿重复创建")
    summary["success"] = True
    save_json(job / "batch_status.json", summary)
    print(f"云机代理、地区、联网及TikTok安装全部确认：{len(devices)}/{len(devices)} 成功。结果目录：{job}")


def create(config_path, configure_only=False, name=None, bindings_path=None):
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
    if name is not None:
        if not configure_only or name not in names:
            raise ValueError("--name 仅用于 --configure-only，且名称必须在本批清单中")
        config["devices"] = [device for device in devices if device["user_name"] == name]
        names = [name]
    config["count"] = len(names)
    bindings_path = bindings_path or ROOT / "阶段9-脚本开发与集成" / "batch10_bindings.example.json"
    main, bindings, proxies = load_resources(config, names, bindings_path)
    apk = check_apk(main["tiktok"], ROOT)
    print(f"TikTok安装包：{apk['path']}（版本 {apk['version_name']}）", flush=True)

    logs = ROOT / "日志"
    logs.mkdir(exist_ok=True)
    job = Path(tempfile.mkdtemp(prefix="create-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-", dir=logs))
    snapshot = job / "devices_input.json"
    snapshot.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
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
        configure(selected, main, bindings, proxies, job, instances, apk)
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
    print(f"全部创建确认：{len(names)}/{len(names)} 成功，开始绑定代理及中转。")
    configure(created, main, bindings, proxies, job, creator._list_instances(), apk)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", nargs="?", type=Path,
                        default=ROOT / "阶段9-脚本开发与集成" / "batch10_create.example.json",
                        help="可选创建 JSON 路径，默认读取已生成的清单")
    parser.add_argument("--bindings", type=Path, help="可选的云机名称→代理组对应表")
    parser.add_argument("--configure-only", action="store_true", help="给本批已有云机补代理、中转、地区和TikTok安装，不创建")
    parser.add_argument("--name", help="补配置时只处理本批指定名称，例如 144.1")
    args = parser.parse_args(argv)
    try:
        return create(args.config.resolve(), args.configure_only, args.name,
                      args.bindings.resolve() if args.bindings else None)
    except Exception as error:
        print(f"任务未完成：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
