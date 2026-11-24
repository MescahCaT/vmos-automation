"""按逐行云机名称生成创建清单和代理对应表；提取首个点或横线前的数字作为代理 ID。"""
import argparse
import ipaddress
import re
import sys
from pathlib import Path

from text_import_common import ROOT, distinct_paths, input_path, proxy_index, read_json, read_text, write_json_files


def parse_devices(text, proxies):
    devices, bindings, seen = [], {}, set()
    for line_number, line in enumerate(text.splitlines(), 1):
        name = line.strip()
        if not name or name.startswith("#"):
            continue
        if not re.fullmatch(r"[A-Za-z0-9_-]+[.-][A-Za-z0-9_.-]+", name):
            raise ValueError(f"第 {line_number} 行：每行一个云机名称，格式如 abc123.2（首个点或横线前的数字为代理组名）")
        if name.casefold() in seen:
            raise ValueError(f"第 {line_number} 行：云机名称重复：{name}")
        prefix = re.split(r"[.-]", name, maxsplit=1)[0]
        group = re.sub(r"[^0-9]", "", prefix)
        if not group:
            raise ValueError(f"第 {line_number} 行：云机名称首个点或横线前必须包含数字，无法匹配代理组")
        if group not in proxies or proxies[group].get("enabled", True) is not True:
            raise ValueError(f"第 {line_number} 行：代理组 {group} 不存在或未启用，请先导入代理")
        devices.append({"user_name": name, "remark": name})
        bindings[name] = group
        seen.add(name.casefold())
    if not devices:
        raise ValueError("文本中没有云机名称")
    return devices, bindings


def main(argv=None, require_host=False):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", nargs="?", help="云机名称文本路径；省略时提示输入")
    parser.add_argument("--host", required=require_host, help="完整IPv4地址；自动选择主机配置/IP下的清单、绑定和代理列表")
    parser.add_argument("--config", type=Path,
                        help="读取并更新此创建 JSON，保留公共参数")
    parser.add_argument("--bindings", type=Path,
                        help="生成的云机名称→代理 ID 对应表")
    parser.add_argument("--proxies", type=Path, help="用于校验代理组的 JSON")
    args = parser.parse_args(argv)
    try:
        profile = None
        if args.host is not None:
            try:
                host = str(ipaddress.IPv4Address(args.host))
            except ipaddress.AddressValueError:
                raise ValueError("--host 必须是完整IPv4地址，例如192.168.110.35，不带协议或端口") from None
            profile = ROOT / "主机配置" / host
            if not profile.is_dir():
                raise ValueError(f"主机配置尚未准备，请先执行：python -X utf8 .\\准备主机.py --host {host}")
        defaults = profile or Path(__file__).resolve().parent
        config_path = (args.config or defaults / ("batch_create.json" if profile else "batch10_create.example.json")).resolve()
        bindings_path = (args.bindings or defaults / ("bindings.json" if profile else "batch10_bindings.example.json")).resolve()
        proxy_path = (args.proxies or (profile / "proxy_list.json" if profile else ROOT / "配置文件" / "proxy_list.json")).resolve()
        source = input_path(args.file, "请输入云机名称文本文件路径：")
        distinct_paths(source, config_path, bindings_path, proxy_path)
        proxies = proxy_index(read_json(proxy_path))
        devices, bindings = parse_devices(read_text(source), proxies)
        config = read_json(config_path)
        if (not isinstance(config, dict) or not config.get("host_ip")
                or not isinstance(config.get("default_config"), dict) or not config["default_config"]):
            raise ValueError("创建 JSON 必须已有 host_ip 和 default_config，请先填写公共创建参数")
        if profile and config["host_ip"] != host:
            raise ValueError("--host 与清单中的 host_ip 不一致，未更新清单或绑定表")
        config["devices"] = devices
        config["count"] = len(devices)
        backups = write_json_files({config_path: config, bindings_path: bindings})
        print(f"创建清单已生成：{len(devices)} 台云机，共用 {len(set(bindings.values()))} 组代理")
        print(f"创建配置：{config_path}")
        print(f"代理对应表：{bindings_path}")
        for backup in backups:
            print(f"原文件备份：{backup}")
        print("user_name 与 remark 已设为相同名称，count 已同步；公共参数保留。")
        host_option = f" --host {host}" if profile else ""
        print(f"清单已保存。创建这批云机，请在项目根目录执行：python -X utf8 .\\创建云机.py{host_option}")
        return 0
    except (OSError, ValueError, EOFError) as error:
        print(f"生成失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
