"""为完整主机IP准备独立的本地配置，不连接或操作云机，不覆盖已有主机目录。"""
import argparse
import copy
from pathlib import Path
import sys

from 创建云机 import ROOT, host_directory, proxy_index, read_json, write_json_files


def prepare(host, config_path=None, bindings_path=None, main_path=None, proxy_path=None):
    profile = host_directory(host)
    if profile.exists():
        raise ValueError(f"主机目录已存在，未覆盖；请直接修改其中配置：{profile}")
    main = read_json(main_path or ROOT / "配置文件" / "config.json")
    if main["vmos"]["port"] != 18182:
        raise ValueError("VMOS端口必须为18182")
    proxy_path = proxy_path or ROOT / main["proxy"]["config_file"]
    proxies = read_json(proxy_path)
    indexed = proxy_index(proxies)
    source = config_path or ROOT / "阶段9-脚本开发与集成" / "batch10_create.example.json"
    config = read_json(source)
    if not isinstance(config, dict) or not isinstance(config.get("default_config"), dict) or not config["default_config"]:
        raise ValueError("创建清单必须包含 default_config 公共参数")
    if config_path is not None and config.get("host_ip") != host:
        raise ValueError("--host 与导入清单的 host_ip 不一致，未准备主机配置")
    if config.get("host_ip") != host:
        # 另一台主机的任务不能自动迁入，只准备空清单供后续生成。
        config = dict(config, host_ip=host, devices=[])
    devices = config.get("devices")
    if not isinstance(devices, list):
        raise ValueError("创建清单必须包含 devices 数组")
    names = [item.get("user_name") if isinstance(item, dict) else None for item in devices]
    if any(not isinstance(name, str) or not name for name in names) or len({name.casefold() for name in names}) != len(names):
        raise ValueError("清单中的云机名称无效或重复")
    bindings = {}
    if names:
        if bindings_path is None:
            # 包括同一时间戳的.bak；无法推导时要求明确指定，避免串用当前绑定。
            if "_create." not in source.name:
                raise ValueError("请通过 --bindings 指定与清单配套的绑定表")
            bindings_path = source.with_name(source.name.replace("_create.", "_bindings.", 1))
        bindings = read_json(bindings_path)
        if (not isinstance(bindings, dict) or any(not isinstance(bindings.get(name), str)
                or bindings[name] not in indexed or indexed[bindings[name]].get("enabled", True) is not True
                for name in names)):
            raise ValueError("清单缺少有效代理绑定，请选择配套绑定表和代理配置")
        bindings = {name: bindings[name] for name in names}
    main["vmos"]["host_ip"] = host
    main["proxy"]["config_file"] = (profile / "proxy_list.json").relative_to(ROOT).as_posix()
    config["count"] = len(names)
    documents = {profile / "config.json": main, profile / "batch_create.json": config,
                 profile / "bindings.json": bindings, profile / "proxy_list.json": proxies}
    mapping_path = ROOT / "数据" / host / "device_mapping.json"
    if not mapping_path.exists():
        previous = ROOT / "数据" / "device_mapping.json"
        mapping = read_json(previous) if previous.exists() else {}
        if mapping.get("host") != f"http://{host}:18182":
            mapping = {"host": f"http://{host}:18182", "devices": {}}
        documents[mapping_path] = copy.deepcopy(mapping)
    write_json_files(documents)
    print(f"主机配置已准备：{profile}")
    print(f"任务清单：{len(names)} 台；数据目录：{mapping_path.parent}")
    if not names:
        print("清单为空：未导入其他主机的任务，请先填写或生成本主机清单与绑定表。")
    print("只处理本地文件，未创建、启动或配置云机。")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", nargs="?", type=Path, help="导入的创建清单；省略时仅在当前清单主机匹配时复制任务")
    parser.add_argument("--host", required=True, help="完整IPv4地址，例如192.168.110.140")
    parser.add_argument("--bindings", type=Path, help="配套绑定表；标准清单文件名可自动匹配同时间戳备份")
    parser.add_argument("--main-config", type=Path, help="主配置来源，默认配置文件/config.json，复制后设置所选主机IP")
    parser.add_argument("--proxies", type=Path, help="代理配置来源，默认使用主配置中的代理文件")
    args = parser.parse_args(argv)
    try:
        return prepare(args.host, args.config, args.bindings, args.main_config, args.proxies)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"准备失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
