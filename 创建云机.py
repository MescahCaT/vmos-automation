"""按清单创建并初始化云机：代理及地区只发送设置请求；状态回查单次最多120秒，安装队列续等，业务码201先回查包名再重试。已有云机用 --configure-only。"""
import argparse
import copy
from contextlib import nullcontext
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
import json
import ipaddress
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time

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
from check_vmos_connection import STARTUP_TIMEOUT, check_apk, check_proxy, poll_readback, save_json, time_only_waits


def host_directory(host):
    """完整 IPv4 地址同时作为本地主机配置目录名。"""
    try:
        address = str(ipaddress.IPv4Address(host))
    except ipaddress.AddressValueError:
        raise ValueError("--host 必须是完整 IPv4 地址，例如 192.168.110.140（不带协议或端口）") from None
    return ROOT / "主机配置" / address


def device_name(instance):
    return instance.get("user_name") or instance.get("name") if isinstance(instance, dict) else None


def proxy_group_from_name(name):
    prefix = re.split(r"[.-]", name, maxsplit=1)[0]
    group = re.sub(r"[^0-9]", "", prefix)
    if not group:
        raise ValueError(f"云机名称无法推导代理组：{name}")
    return group


def select_live_devices(instances, requested_names=None):
    by_name = {}
    for instance in instances:
        name = device_name(instance)
        if isinstance(name, str) and name:
            if not re.fullmatch(r"[A-Za-z0-9_.-]{3,}", name):
                raise ValueError(f"主机实时列表中的云机名称格式无效：{name}")
            key = name.casefold()
            if key in by_name:
                raise ValueError(f"主机实时列表存在重复云机名称：{name}")
            by_name[key] = (name, instance)
    if requested_names is None:
        if not by_name:
            raise ValueError("主机实时列表中没有可操作的云机")
        return [(name, instance) for name, instance in by_name.values()]
    if len({name.casefold() for name in requested_names}) != len(requested_names):
        raise ValueError("指定的云机名称重复")
    missing = [name for name in requested_names if name.casefold() not in by_name]
    if missing:
        raise ValueError("指定云机不在主机实时列表中：" + "、".join(missing))
    wrong_case = [name for name in requested_names if by_name[name.casefold()][0] != name]
    if wrong_case:
        raise ValueError("云机名称大小写与主机实时列表不一致：" + "、".join(wrong_case))
    return [by_name[name.casefold()] for name in requested_names]


def save_live_inventory(host_ip, instances):
    devices = []
    for instance in instances:
        name = device_name(instance)
        if isinstance(name, str) and name:
            devices.append({"user_name": name, "db_id": instance["db_id"],
                            "state": instance.get("state") or instance.get("status")})
    path = ROOT / "数据" / host_ip / "device_inventory.json"
    save_json(path, {"host_ip": host_ip, "updated_at": datetime.now().astimezone().isoformat(),
                     "devices": devices})
    return path


def runtime_bindings(names, bindings_path=None):
    configured = read_json(bindings_path) if bindings_path is not None else {}
    if not isinstance(configured, dict):
        raise ValueError("代理绑定文件格式无效")
    bindings = {}
    for name in names:
        group = proxy_group_from_name(name)
        if name in configured and configured[name] != group:
            raise ValueError(f"云机 {name} 的代理绑定为 {configured[name]}，按名称规则应为 {group}")
        bindings[name] = group
    return bindings


def load_resources(config, names, bindings_path, operation="initialize", main_path=None, proxy_path=None,
                   bindings_override=None):
    main = read_json(main_path or ROOT / "配置文件" / "config.json")
    if main["vmos"]["host_ip"] != config["host_ip"] or main["vmos"]["port"] != 18182:
        raise ValueError("创建清单与主配置中的 VMOS 地址或端口不一致")
    bindings = bindings_override if bindings_override is not None else read_json(bindings_path)
    proxy_path = proxy_path or ROOT / main["proxy"]["config_file"]
    document = read_json(proxy_path)
    before = copy.deepcopy(document)
    indexed = proxy_index(document)
    selected = {}
    for name in names:
        proxy_id = bindings.get(name) if isinstance(bindings, dict) else None
        if not isinstance(proxy_id, str) or proxy_id not in indexed or indexed[proxy_id].get("enabled", True) is not True:
            raise ValueError(f"云机 {name} 缺少有效的代理对应关系")
        selected[proxy_id] = indexed[proxy_id]
    if operation in ("initialize", "proxy", "configure", "initialize_existing"):
        ensure_transit(document, list(selected.values()))
    for proxy in selected.values():
        if operation in ("initialize", "proxy", "configure", "initialize_existing"):
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


def unique_instance(instances, db_id, name):
    """名称和ID都只能对应列表中的同一条记录。"""
    matches = [item for item in instances
               if item.get("db_id") == db_id or (item.get("user_name") or item.get("name")) == name]
    if (isinstance(db_id, str) and db_id and len(matches) == 1 and matches[0].get("db_id") == db_id
            and (matches[0].get("user_name") or matches[0].get("name")) == name):
        return matches[0]
    return None


def configure_region(setup, db_id, proxy, state, report):
    """按原状态启动目标，发送地区请求并等待短暂间隔。"""
    report.update(success=False, original_state=state, started_by_script=False, system={})
    try:
        if state == "stopped":
            report["started_by_script"] = True
            response = setup.device.post_action("run", [db_id])
            if str(response.get("code")) != "200":
                raise ValueError("VMOS拒绝启动目标云机")
            if setup.device.wait_ready([db_id]) != [db_id]:
                raise ValueError("ROM未就绪，未提交地区设置")
            setup.wait_android_ready(db_id)
        elif state != "running":
            raise ValueError("云机状态不是运行或停机，未提交地区设置")
        SystemSetup(setup).configure(db_id, region_settings(proxy), report["system"], verify_result=False)
        report["requests_accepted"] = report["system"].get("requests_accepted")
        report["success"] = (report["system"].get("settings_attempted") is True
                              and report["requests_accepted"] is True)
        time.sleep(0.5)
    except Exception as error:
        report["error"] = str(error) if isinstance(error, ValueError) else type(error).__name__
        raise
    finally:
        save_json(setup.output_dir / "system_status.json", report)
        logs = getattr(setup, "logs", [])
        if isinstance(logs, list):
            (setup.output_dir / "system_log.txt").write_text("\n".join(logs) + "\n", encoding="utf-8")


def initialize_existing(setup, installer, db_id, proxy, main, apk, state):
    """一次启动内并行申请安装和代理，响应返回后提交地区，最后等待安装检查。"""
    result = {"success": False, "original_state": state, "started_by_script": state == "stopped",
              "rom_ready": False, "installation_attempted": False, "proxy_attempted": False,
              "system": {}, "errors": {}}
    try:
        if state == "stopped":
            try:
                response = setup.device.post_action("run", [db_id])
                if str(response.get("code")) != "200":
                    raise ValueError("启动响应未确认")
            except (requests.RequestException, ValueError) as error:
                result["startup_response_uncertain"] = str(error)
                setup.log("启动响应未确认，只检查实际就绪状态，不重发启动请求")
        elif state != "running":
            raise ValueError("云机状态不是运行或停机，未提交初始化请求")
        if setup.device.wait_ready([db_id]) != [db_id]:
            raise ValueError("ROM未就绪，未提交初始化请求")
        setup.wait_android_ready(db_id)
    except Exception as error:
        result["errors"]["startup"] = result["error"] = str(error)
        return result
    result["rom_ready"] = True
    submitted = threading.Event()

    def install():
        try:
            return installer.install_from_local(apk["path"], [db_id], verify=True, wait_time=120,
                                                start_stopped=False, restore_state=False,
                                                on_submitted=submitted.set)
        finally:
            # 已安装或提交异常时没有上传回调，同样允许后续地区任务继续。
            submitted.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        result["installation_attempted"] = True
        installation = pool.submit(install)
        proxy_task = pool.submit(setup.run, db_id, proxy, main["proxy"], start_stopped=False,
                                 sync_system=False, restore_state=False, check_network=False, verify_system=False)
        try:
            proxy_result = proxy_task.result()
            result["proxy_attempted"] = proxy_result.get("proxy_attempted") is True
            result["proxy_requests_accepted"] = proxy_result.get("set_api", {}).get("accepted") is True
            if proxy_result.get("success") is not True or not result["proxy_requests_accepted"]:
                result["errors"]["proxy"] = proxy_result.get("error") or "代理设置请求未确认受理"
        except Exception as error:
            result["errors"]["proxy"] = str(error)
        submitted.wait()
        try:
            SystemSetup(setup).configure(db_id, region_settings(proxy), result["system"], verify_result=False)
            if result["system"].get("requests_accepted") is not True:
                result["errors"]["region"] = "地区设置请求未全部确认受理"
        except Exception as error:
            result["errors"]["region"] = str(error)
        finally:
            save_json(setup.output_dir / "system_status.json", result["system"])
        try:
            installed = installation.result()
            target = installed.get("devices", {}).get(db_id, {})
            result["install_status"] = target.get("status", "unconfirmed")
            result["tiktok_installed"] = (installed.get("success") is True
                and target.get("status") in ("installed", "already_installed")
                and target.get("package_verified") is True)
            if not result["tiktok_installed"]:
                result["errors"]["install"] = target.get("error") or "TikTok安装包名未确认"
        except Exception as error:
            result["install_status"] = "unconfirmed"
            result["errors"]["install"] = str(error)
    if result["system"].get("settings_attempted"):
        with MAPPING_LOCK:
            mapping = read_json(setup.mapping_path)
            record = mapping["devices"][db_id]
            record.pop("system_config", None)
            record["system_settings_submission"] = {
                "expected": region_settings(proxy), "attempted_at": datetime.now().astimezone().isoformat(),
                "requests_accepted": result["system"].get("requests_accepted") is True}
            save_json(setup.mapping_path, mapping)
    result["success"] = not result["errors"]
    if result["errors"]:
        result["error"] = "；".join(f"{stage}: {error}" for stage, error in result["errors"].items())
    return result


def configure(devices, main, bindings, proxies, job, instances, apk, workers=1, operation="initialize",
              initial_failures=None, requested_total=None, retry_failed=False, mapping_path=None):
    host = f"http://{main['vmos']['host_ip']}:{main['vmos']['port']}"
    # 独立安装只使用本次任务的状态映射，不依赖其他主机的代理和账号记录。
    mapping_path = (job / "device_mapping.json" if operation == "install"
                    else mapping_path or ROOT / "数据" / "device_mapping.json")
    by_id = {item["db_id"]: item for item in instances}
    requested_total = len(devices) if requested_total is None else requested_total
    initial_failures = list(initial_failures or [])
    valid_devices = []
    with MAPPING_LOCK:
        mapping = read_json(mapping_path) if mapping_path.exists() else {"host": host, "devices": {}}
        if (not isinstance(mapping, dict) or not isinstance(mapping.get("devices"), dict)
                or any(not isinstance(item, dict) for item in mapping["devices"].values())):
            raise ValueError("设备映射格式无效，已保留原文件，未绑定代理")
        if mapping.get("host") != host:
            backup = job / "previous_device_mapping.json"
            backup.write_bytes(mapping_path.read_bytes())
            print(f"设备映射切换主机：{mapping.get('host')} → {host}；原映射已备份：{backup}", flush=True)
            mapping = {"host": host, "devices": {}}
        for device in devices:
            db_id = device["db_id"]
            instance = unique_instance(instances, db_id, device["user_name"])
            if instance is None:
                initial_failures.append({"name": device["user_name"],
                                         "error": "实际云机名称或ID不唯一，已跳过且未写入"})
                continue
            valid_devices.append(device)
            record = mapping["devices"].setdefault(db_id, {"proxy_id": None, "account_email": None})
            record.update(name=device["user_name"], state=instance.get("state") or instance.get("status"), present=True)
        save_json(mapping_path, mapping)
    devices = valid_devices
    if bindings:
        save_json(job / "bindings.json", {device["user_name"]: bindings[device["user_name"]] for device in devices})
    workers = min(workers, len(devices))
    label = {"initialize": "完整初始化", "proxy": "代理设置", "configure": "代理及地区请求",
             "region": "地区设置", "install": "TikTok安装", "initialize_existing": "已有云机初始化"}[operation]
    summary = {"success": False, "operation": operation,
               "total": requested_total if requested_total is not None else len(devices),
               "workers": workers, "devices": [], "creation_failures": initial_failures,
               "retry_failed": retry_failed, "retry_rounds": 0, "retried_names": []}
    save_json(job / "batch_status.json", summary)
    print(f"{label}并发数：{workers}", flush=True)
    status_lock = threading.Lock()
    records = [None] * len(devices)

    def publish(index, record):
        with status_lock:
            records[index] = dict(record)
            summary["devices"] = [item for item in records if item is not None]
            save_json(job / "batch_status.json", summary)

    def configure_device(index, device, attempt):
        db_id, name = device["db_id"], device["user_name"]
        proxy = proxies[bindings[name]] if bindings else None
        attempt_dir = job if attempt == 1 else job / "retry-1"
        original_state = ((records[index] or {}).get("original_state") if attempt > 1 else None)
        original_state = original_state or by_id[db_id].get("state") or by_id[db_id].get("status")
        print(f"{label}：{name}", flush=True)
        record = {"name": name, "db_id": db_id, "proxy_id": proxy["id"] if proxy else None,
                  "attempt": attempt, "original_state": original_state,
                  "success": False, "system_requested": operation in ("initialize", "configure", "region", "initialize_existing"),
                  "system_configured": False, "proxy_configured": False, "proxy_attempted": False, "rom_ready": False,
                  "system_attempted": False, "system_verification_requested": False,
                  "network_check_requested": False, "network_verified": False,
                  "installation_requested": operation in ("initialize", "install", "initialize_existing"), "installation_attempted": False,
                  "tiktok_installed": False, "install_status": "not_started",
                  "report": str(attempt_dir / db_id / {"region": "system_status.json", "install": "install_status.json",
                      "initialize_existing": "initialization_status.json"}.get(operation, "proxy_status.json"))}
        publish(index, record)
        setup = ProxySetup(host, output_dir=attempt_dir / db_id, mapping_path=mapping_path)
        proxy_log = setup.log
        setup.log = lambda message: proxy_log(f"{name}：{message}")
        setup.device.log = setup.log
        result = {}
        identity_confirmed = attempt == 1
        restore_required = identity_confirmed and original_state == "stopped"
        try:
            if attempt > 1:
                def confirm_retry_identity():
                    current = list(setup.device.list_instances().values())
                    instance = unique_instance(current, db_id, name)
                    if instance is None:
                        raise ValueError("候补前原云机名称或ID不存在或不唯一，未操作云机")
                    if (instance.get("state") or instance.get("status")) not in ("running", "stopped"):
                        raise ValueError("候补前云机仍在启动或重启，未操作云机")
                    return instance
                poll_readback(confirm_retry_identity, setup.log, "候补前身份与状态回查", timeout=60, interval=2)
                identity_confirmed = True
                restore_required = original_state == "stopped"
            # 初始化入口统一管理停机，避免代理配置后停机、安装时再次启动。
            if operation in ("initialize", "proxy", "configure"):
                result = setup.run(db_id, proxy, main["proxy"], start_stopped=True,
                                   wait_time=120 if operation in ("initialize", "configure") else STARTUP_TIMEOUT,
                                   sync_system=record["system_requested"], restore_state=False,
                                   check_network=False, verify_system=operation not in ("initialize", "configure"))
            elif operation == "region":
                configure_region(setup, db_id, proxy, original_state, result)
            elif operation == "initialize_existing":
                installer = TikTokInstaller(host, output_dir=attempt_dir / db_id, package_name=main["tiktok"]["package_name"])
                install_log = installer.log
                installer.log = lambda message: install_log(f"{name}：{message}")
                result = initialize_existing(setup, installer, db_id, proxy, main, apk, original_state)
                record.update(installation_attempted=result["installation_attempted"],
                              tiktok_installed=result.get("tiktok_installed") is True,
                              install_status=result.get("install_status", "not_started"),
                              proxy_requests_accepted=result.get("proxy_requests_accepted") is True,
                              system_requests_accepted=result["system"].get("requests_accepted") is True,
                              errors=result["errors"], install_error=result["errors"].get("install"),
                              install_report=str(attempt_dir / db_id / "install_status.json"))
            else:
                result = {"success": True}
            if attempt == 1:
                original_state = result.get("original_state", original_state)
                restore_required = result.get("started_by_script", restore_required)
            record["original_state"] = original_state
            record.update(system_configured=result.get("system", {}).get("success") is True,
                          system_attempted=result.get("system", {}).get("settings_attempted") is True,
                          proxy_configured=result.get("proxy_configured") is True,
                          proxy_attempted=result.get("proxy_attempted") is True,
                          rom_ready=result.get("rom_ready") is True)
            if operation == "install" or (operation == "initialize" and record["rom_ready"]):
                # 安装完成只根据实际包名确认。
                if operation == "initialize" and result.get("success") is not True:
                    print(f"{name}：代理或地区设置未全部完成，原因保留在报告；ROM已就绪，继续安装TikTok", flush=True)
                record.update(installation_attempted=True, install_report=str(attempt_dir / db_id / "install_status.json"))
                publish(index, record)
                print(f"安装TikTok：{name}，使用 {Path(apk['path']).name}", flush=True)
                installer = TikTokInstaller(host, output_dir=attempt_dir / db_id, package_name=main["tiktok"]["package_name"])
                install_log = installer.log
                installer.log = lambda message: install_log(f"{name}：{message}")
                installed = installer.install_from_local(apk["path"], [db_id], verify=True,
                                                         wait_time=120 if operation == "initialize" else 300,
                                                         request_wait=120 if operation == "initialize" else None,
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
            elif operation == "proxy" and not record["proxy_configured"]:
                record["error"] = "代理配置未确认"
            elif operation in ("initialize", "configure") and not record["proxy_attempted"]:
                record["error"] = "代理设置请求流程未结束"
            elif record["system_verification_requested"] and not record["system_configured"]:
                record["error"] = "国家、时区、语言或GPS未确认"
            elif operation in ("initialize", "configure") and not record["system_attempted"]:
                record["error"] = "地区设置请求流程未结束"
            if record["installation_attempted"] and not record["tiktok_installed"]:
                record["error"] = (((record["error"] + "；") if record.get("error") else "")
                                   + "TikTok安装未确认：" + record.get("install_error", "请查看安装报告"))
        except Exception as error:
            record["error"] = str(error)
            if record["installation_attempted"] and not record["tiktok_installed"]:
                record["install_status"] = "unconfirmed"
        finally:
            try:
                if not identity_confirmed:
                    raise ValueError("候补前原云机身份或状态未确认，未执行恢复操作")
                if restore_required:
                    record["restore_state"] = setup.device.restore_stopped([db_id])
                record["state_restored"] = not restore_required or record["restore_state"].get("success") is True
                def confirm_final_state():
                    final = setup.device.list_instances().get(db_id, {})
                    record["final_state"] = final.get("state") or final.get("status") or "unknown"
                    if record["final_state"] != original_state:
                        raise ValueError("最终运行状态尚未恢复")
                    return final
                if restore_required and db_id in record["restore_state"].get("states", {}):
                    # 恢复函数已经查询实际状态，直接使用证据，避免重复等待。
                    record["final_state"] = record["restore_state"]["states"][db_id]
                    record["state_restored"] = record["state_restored"] and record["final_state"] == original_state
                elif restore_required and not record["state_restored"]:
                    record["final_state"] = "unknown"
                else:
                    try:
                        poll_readback(confirm_final_state, setup.log, "最终运行状态回查", timeout=60, interval=2)
                    except ValueError:
                        record["state_restored"] = False
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
                                 and (not record["system_verification_requested"] or record["system_configured"])
                                 and (operation not in ("initialize", "configure") or record["system_attempted"])
                                 and (operation not in ("initialize", "configure") or record["proxy_attempted"])
                                 and (operation != "proxy" or record["proxy_configured"])
                                 and (not record["installation_requested"] or record["tiktok_installed"])
                                 and record["state_restored"] and not record.get("error"))
            if operation == "initialize_existing":
                save_json(Path(record["report"]), record)
            publish(index, record)
        if not record["success"]:
            print(f"{name}：本台未完成，原因已写入报告；继续后续云机", flush=True)
        if operation == "initialize":
            print(f"{name}：地区设置请求{'已结束（未核验）' if record['system_attempted'] else '未结束'}，"
                  f"代理设置请求{'已结束（未核验）' if record['proxy_attempted'] else '未结束'}，"
                  f"TikTok安装{'已确认' if record['tiktok_installed'] else '未确认'}", flush=True)
        elif operation == "region":
            print(f"{name}：地区设置请求已提交（未核验）" if record["success"]
                  else f"{name}：地区设置请求未完成", flush=True)
        elif operation == "configure":
            print(f"{name}：代理及地区请求已提交（未核验）" if record["success"]
                  else f"{name}：代理及地区请求未完成", flush=True)
        else:
            print(f"{name}：{label}{'通过' if record['success'] else '未确认'}", flush=True)
        return record

    def configure_one(index, device, attempt):
        with time_only_waits() if operation == "initialize" else nullcontext():
            return configure_device(index, device, attempt)

    def run_round(indices, attempt):
        # 最多workers个在途任务；本轮全部收尾后才返回，不立即重试单台。
        next_index = 0
        if not indices:
            return
        with ThreadPoolExecutor(max_workers=min(workers, len(indices))) as pool:
            pending = {}
            while next_index < len(indices) or pending:
                while next_index < len(indices) and len(pending) < workers:
                    index = indices[next_index]
                    pending[pool.submit(configure_one, index, devices[index], attempt)] = index
                    next_index += 1
                completed, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in completed:
                    index = pending.pop(future)
                    try:
                        future.result()
                    except Exception as error:
                        record = dict(records[index] or {"name": devices[index]["user_name"],
                                                        "db_id": devices[index]["db_id"]})
                        record.update(success=False, error=str(error), attempt=attempt)
                        publish(index, record)

    def save_summary():
        summary["not_started"] = [device["user_name"] for index, device in enumerate(devices) if records[index] is None]
        failures = [record for record in summary["devices"] if not record["success"]]
        summary["failed_names"] = [item["name"] for item in initial_failures] + [record["name"] for record in failures]
        summary["success_count"] = sum(record["success"] for record in summary["devices"])
        summary["failed_count"] = len(initial_failures) + len(failures)
        summary["success"] = (not initial_failures and not summary["not_started"]
                              and all(record["success"] for record in summary["devices"]))
        save_json(job / "batch_status.json", summary)
        (job / "failed_names.txt").write_text("\n".join(summary["failed_names"])
                                              + ("\n" if summary["failed_names"] else ""), encoding="utf-8")

    run_round(list(range(len(devices))), 1)
    save_summary()
    if retry_failed:
        summary["first_failed_names"] = list(summary["failed_names"])
        save_json(job / "first_pass_status.json", summary)
        (job / "first_failed_names.txt").write_text((job / "failed_names.txt").read_text(encoding="utf-8"), encoding="utf-8")
        indices = [index for index, record in enumerate(records) if record is not None and not record["success"]]
        summary["retry_skipped_names"] = [item["name"] for item in initial_failures]
        if initial_failures:
            print("以下云机身份未确认，保留失败记录，候补不操作：" + "、".join(summary["retry_skipped_names"]), flush=True)
        if indices:
            summary.update(retry_rounds=1, retried_names=[devices[index]["user_name"] for index in indices])
            save_json(job / "batch_status.json", summary)
            print(f"首轮全部收尾；候补 {len(indices)} 台，仅重试一轮，并发数：{min(workers, len(indices))}", flush=True)
            run_round(indices, 2)
        else:
            print("首轮全部收尾；无可重试的失败云机。", flush=True)
        save_summary()
    failures = [record for record in summary["devices"] if not record["success"]]
    if not summary["success"]:
        reason = "；".join(f"{item['name']}：{item['error']}" for item in initial_failures)
        device_reason = "；".join(f"{record['name']}：{record.get('error') or '初始化或最终状态未确认'}" for record in failures)
        reason = "；".join(item for item in (reason, device_reason) if item)
        reason = reason or "；".join(summary.get("errors", [])) or "初始化未全部确认"
        raise ValueError(f"{reason}；本批处理已结束，{summary['success_count']}台成功、{summary['failed_count']}台未完成；"
                         f"单台失败未中止后续配置；查看 {job}，请勿重复创建")
    print(f"{label}全部确认：{len(devices)}/{len(devices)} 成功。结果目录：{job}")


def create(config_path, configure_only=False, name=None, bindings_path=None, workers=1,
           selected_names=None, create_only=False, operation="initialize", retry_failed=False, host=None):
    if type(workers) is not int or workers < 1:
        raise ValueError("并发数 --workers 必须为大于等于1的整数")
    if retry_failed and (not configure_only or create_only or operation != "initialize"):
        raise ValueError("--retry-failed 仅支持 创建云机.py --configure-only，不重试创建请求")
    profile = host_directory(host) if host is not None else None
    if profile is not None and not (profile / "config.json").is_file():
        raise ValueError(f"主机配置尚未准备，请先执行：python -X utf8 .\\准备主机.py --host {host}")
    config_path_supplied = config_path is not None
    config_path = config_path or (profile / "batch_create.json" if profile else
                                  ROOT / "阶段9-脚本开发与集成" / "batch10_create.example.json")
    main_path = profile / "config.json" if profile else ROOT / "配置文件" / "config.json"
    if config_path_supplied and not config_path.is_file():
        raise FileNotFoundError(f"指定清单不存在：{config_path}")
    runtime_mode = operation != "initialize" or configure_only
    print(f"读取{'主机操作配置' if runtime_mode else '创建清单'}：{main_path if runtime_mode else config_path}", flush=True)
    config = (json.loads(config_path.read_text(encoding="utf-8-sig"))
              if config_path.is_file() and (not runtime_mode or config_path_supplied) else {})
    if not config and not runtime_mode:
        raise ValueError("创建清单不存在，请先生成清单")
    main = read_json(main_path)
    if runtime_mode:
        if not isinstance(config, dict):
            config = {}
        config["host_ip"] = config.get("host_ip") or main.get("vmos", {}).get("host_ip")
        if not config.get("host_ip") or main.get("vmos", {}).get("port") != 18182:
            raise ValueError("主配置缺少有效 VMOS 地址或端口")
        if config["host_ip"] != main.get("vmos", {}).get("host_ip"):
            raise ValueError("所选清单与主机配置中的 VMOS 地址不一致，未操作云机")
        if host is not None and config["host_ip"] != host:
            raise ValueError("--host 与主机配置中的 host_ip 不一致，未操作云机")
        requested_names = [name] if name is not None else selected_names
        if requested_names is not None and len({item.casefold() for item in requested_names}) != len(requested_names):
            raise ValueError("指定的云机名称重复")
        if config_path.is_file():
            inventory_reader = DeviceCreator(str(config_path))
            inventory_reader.base_url = f"http://{config['host_ip']}:18182"
            list_instances = inventory_reader._list_instances
        else:
            inventory_reader = TikTokInstaller(f"http://{config['host_ip']}:18182")
            list_instances = lambda: list(inventory_reader.list_instances().values())
        instances = poll_readback(list_instances, print, "主机实时云机列表回查", timeout=30, interval=2)
        inventory_path = save_live_inventory(config["host_ip"], instances)
        print(f"主机实时云机清单：{inventory_path}", flush=True)
        selected = select_live_devices(instances, requested_names)
        names = [name for name, _ in selected]
        config["devices"] = [{"user_name": name, "remark": name} for name in names]
        config["count"] = len(names)
    else:
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
        if host is not None and config["host_ip"] != host:
            raise ValueError("--host 与所选清单的 host_ip 不一致，未操作云机")
        requested_names = [name] if name is not None else selected_names
        if requested_names is not None:
            if len({item.casefold() for item in requested_names}) != len(requested_names):
                raise ValueError("指定的云机名称重复")
            missing = [item for item in requested_names if item not in names]
            if missing:
                raise ValueError("指定名称不在本批清单中：" + "、".join(missing))
            config["devices"] = [device for device in devices if device["user_name"] in requested_names]
        names = [device["user_name"] for device in config["devices"]]
        config["count"] = len(names)
    bindings_path_supplied = bindings_path is not None
    bindings_path = bindings_path or (profile / "bindings.json" if profile else
                                      ROOT / "阶段9-脚本开发与集成" / "batch10_bindings.example.json")
    if runtime_mode and operation != "install":
        bindings = runtime_bindings(names, bindings_path if bindings_path_supplied else None)
        main, bindings, proxies = load_resources(config, names, bindings_path, operation, main_path,
                                                 profile / "proxy_list.json" if profile else None,
                                                 bindings_override=bindings)
    elif not create_only and operation != "install":
        main, bindings, proxies = load_resources(config, names, bindings_path, operation, main_path,
                                                 profile / "proxy_list.json" if profile else None)
    else:
        if main["vmos"]["host_ip"] != config["host_ip"] or main["vmos"]["port"] != 18182:
            raise ValueError("创建清单与主配置中的 VMOS 地址或端口不一致")
        bindings, proxies = {}, {}
    apk = None
    if not create_only and operation in ("initialize", "install", "initialize_existing"):
        apk = check_apk(main["tiktok"], ROOT)
        print(f"TikTok安装包：{apk['path']}（版本 {apk['version_name']}）", flush=True)

    logs = ROOT / "日志" / host if profile else ROOT / "日志"
    mapping_path = ROOT / "数据" / host / "device_mapping.json" if profile else None
    logs.mkdir(parents=True, exist_ok=True)
    prefix = {"initialize": "create", "proxy": "proxy", "configure": "configure",
              "region": "region", "install": "install", "initialize_existing": "initialize"}[operation]
    job = Path(tempfile.mkdtemp(prefix=prefix + "-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-", dir=logs))
    snapshot = job / "devices_input.json"
    snapshot.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if apk is not None:
        save_json(job / "apk_input.json", apk)
    print(f"本批清单：{len(names)} 台，主机：{config['host_ip']}", flush=True)
    print(f"结果目录：{job}", flush=True)
    if not runtime_mode:
        creator = DeviceCreator(str(snapshot))
        instances = poll_readback(creator._list_instances, print, "创建前实例基线回查", timeout=30, interval=2)
    if configure_only:
        selected = []
        identity_failures = []
        for target in names:
            matches = [item for item in instances if (item.get("user_name") or item.get("name")) == target]
            db_id = matches[0].get("db_id") if len(matches) == 1 else None
            if unique_instance(instances, db_id, target) is None:
                identity_failures.append({"name": target, "error": "云机名称或ID不存在或不唯一，已跳过且未写入"})
                continue
            selected.append({"user_name": target, "db_id": db_id})
        configure(selected, main, bindings, proxies, job, instances, apk, workers, operation,
                  initial_failures=identity_failures, requested_total=len(names), retry_failed=retry_failed,
                  mapping_path=mapping_path)
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
    initial_failures = []
    confirmed_created = []
    for target in names:
        matches = [item for item in results if item.get("user_name") == target]
        devices_for_name = [item for item in created if item.get("user_name") == target]
        if (len(matches) == 1 and matches[0].get("status") == "success" and len(devices_for_name) == 1
                and matches[0].get("db_id") and matches[0]["db_id"] == devices_for_name[0].get("db_id")
                and sum(item.get("db_id") == matches[0]["db_id"] for item in created) == 1):
            confirmed_created.append(devices_for_name[0])
        else:
            initial_failures.append({"name": target, "error": "创建结果或名称与唯一ID未确认；未重发创建请求"})
    if create_only:
        summary = {"success": not initial_failures, "operation": "create",
                   "total": len(names), "success_count": len(confirmed_created), "failed_count": len(initial_failures),
                   "failed_names": [item["name"] for item in initial_failures], "creation_failures": initial_failures,
                   "devices": confirmed_created, "not_started": [item["name"] for item in initial_failures]}
        save_json(job / "batch_status.json", summary)
        (job / "failed_names.txt").write_text("\n".join(summary["failed_names"]), encoding="utf-8")
        if not summary["success"]:
            raise ValueError(f"创建部分完成：{summary['success_count']}/{len(names)} 台确认，失败 {summary['failed_count']} 台；查看 {job}，勿重发未确认创建")
        print(f"仅创建全部确认：{len(names)}/{len(names)} 成功。结果目录：{job}")
        return 0
    if initial_failures:
        print(f"创建阶段 {len(initial_failures)} 台未确认；继续初始化已确认的云机。")
    latest_instances = []
    def query_created_identities():
        nonlocal latest_instances
        latest_instances = creator._list_instances()
        for item in confirmed_created:
            if unique_instance(latest_instances, item["db_id"], item["user_name"]) is None:
                raise ValueError("创建后实例列表尚未唯一确认名称与ID")
        return latest_instances
    try:
        instances = poll_readback(query_created_identities, print, "创建后实例身份回查", timeout=30, interval=2)
    except ValueError:
        # 继续处理已经出现在最近一次列表中的设备；缺失设备仅记为失败。
        instances = latest_instances
    verified_created = []
    for item in confirmed_created:
        if unique_instance(instances, item["db_id"], item["user_name"]) is not None:
            verified_created.append(item)
        else:
            initial_failures.append({"name": item["user_name"], "error": "创建后实例列表未唯一确认名称与ID"})
    if not verified_created:
        summary = {"success": False, "operation": operation, "total": len(names), "workers": 0,
                   "success_count": 0, "failed_count": len(initial_failures),
                   "failed_names": [item["name"] for item in initial_failures],
                   "creation_failures": initial_failures, "devices": [], "not_started": []}
        save_json(job / "batch_status.json", summary)
        (job / "failed_names.txt").write_text("\n".join(summary["failed_names"]), encoding="utf-8")
        raise ValueError(f"没有可安全初始化的已确认云机；查看 {job}，勿重复创建")
    print(f"创建结果：{len(verified_created)}/{len(names)} 台身份确认，开始配置已确认实例。")
    configure(verified_created, main, bindings, proxies, job, instances, apk, workers, operation,
              initial_failures=initial_failures, requested_total=len(names), mapping_path=mapping_path)
    return 0


def _main(argv=None, operation="initialize", create_only=False):
    descriptions = {"initialize": __doc__, "proxy": "仅设置代理及中转并回查配置，支持按名称选择一台或多台。",
                    "configure": "启动云机后仅提交代理及地区请求，不检测、不安装TikTok，完成后恢复原运行状态。",
                    "region": "停机目标自动启动，提交国家、时区、语言及已配置GPS请求后恢复原状态；不回查地区、不重启。",
                    "install": "仅安装TikTok并核验包名，支持按名称选择一台或多台。",
                    "initialize_existing": "初始化已有云机：并行申请TK安装和代理，最后提交地区设置，不创建、不重启，完成后恢复原状态。"}
    description = "仅创建并确认云机，不执行代理、地区设置或TikTok安装。" if create_only else descriptions[operation]
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("config", nargs="?", type=Path,
                        help="可选创建 JSON 路径；带--host时默认读取该主机清单，否则读取原清单")
    parser.add_argument("--host", help="完整主机IP，例如192.168.110.140；使用该主机独立配置、清单、映射和日志")
    parser.add_argument("--bindings", type=Path, help="可选的云机名称→代理组对应表")
    if operation == "initialize" and not create_only:
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--configure-only", action="store_true", help="给本批已有云机补代理、中转、地区和TikTok安装，不创建")
        mode.add_argument("--create-only", action="store_true", help="仅创建并确认结果，不执行代理、地区或安装")
        parser.add_argument("--retry-failed", action="store_true", help="仅配合--configure-only：首轮全部收尾后，自动重试失败云机一轮")
    targets = parser.add_mutually_exclusive_group()
    targets.add_argument("--name", help="仅处理清单中的一台，例如 --name 145.2")
    targets.add_argument("--names", nargs="+", help="仅处理清单中的一台或多台，空格分隔，例如 --names 145.2 146.1")
    worker_help = ("仅创建阶段仍逐台提交，此参数保留兼容，默认1"
                   if create_only else "配置或安装的并发数，默认1；创建阶段仍逐台提交，例如 --workers 3")
    parser.add_argument("--workers", type=int, default=1, help=worker_help)
    args = parser.parse_args(argv)
    try:
        return create(args.config.resolve() if args.config else None,
                      getattr(args, "configure_only", operation != "initialize"), args.name,
                      args.bindings.resolve() if args.bindings else None, args.workers,
                      args.names, create_only or getattr(args, "create_only", False), operation,
                      getattr(args, "retry_failed", False), host=args.host)
    except Exception as error:
        print(f"任务未完成：{error}", file=sys.stderr)
        return 1


def main(argv=None, operation="initialize", create_only=False):
    with time_only_waits() if operation == "initialize" else nullcontext():
        return _main(argv, operation, create_only)


if __name__ == "__main__":
    sys.exit(main())
