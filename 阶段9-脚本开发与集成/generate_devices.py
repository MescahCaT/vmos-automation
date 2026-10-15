"""按逐行云机名称生成创建清单和代理对应表；名称第一个点前的部分是代理 ID。"""
import argparse
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
        if not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_.-]+", name):
            raise ValueError(f"第 {line_number} 行：每行一个云机名称，格式如 151.1（点前为代理组名）")
        if name.casefold() in seen:
            raise ValueError(f"第 {line_number} 行：云机名称重复：{name}")
        group = name.partition(".")[0]
        if group not in proxies or proxies[group].get("enabled", True) is not True:
            raise ValueError(f"第 {line_number} 行：代理组 {group} 不存在或未启用，请先导入代理")
        devices.append({"user_name": name, "remark": name})
        bindings[name] = group
        seen.add(name.casefold())
    if not devices:
        raise ValueError("文本中没有云机名称")
    return devices, bindings


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", nargs="?", help="云机名称文本路径；省略时提示输入")
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parent / "batch10_create.example.json",
                        help="读取并更新此创建 JSON，保留公共参数")
    parser.add_argument("--bindings", type=Path, default=Path(__file__).resolve().parent / "batch10_bindings.example.json",
                        help="生成的云机名称→代理 ID 对应表")
    parser.add_argument("--proxies", type=Path, default=ROOT / "配置文件" / "proxy_list.json", help="用于校验代理组的 JSON")
    args = parser.parse_args(argv)
    try:
        source = input_path(args.file, "请输入云机名称文本文件路径：")
        config_path, bindings_path, proxy_path = args.config.resolve(), args.bindings.resolve(), args.proxies.resolve()
        distinct_paths(source, config_path, bindings_path, proxy_path)
        proxies = proxy_index(read_json(proxy_path))
        devices, bindings = parse_devices(read_text(source), proxies)
        config = read_json(config_path)
        if (not isinstance(config, dict) or not config.get("host_ip")
                or not isinstance(config.get("default_config"), dict) or not config["default_config"]):
            raise ValueError("创建 JSON 必须已有 host_ip 和 default_config，请先填写公共创建参数")
        config["devices"] = devices
        config["count"] = len(devices)
        backups = write_json_files({config_path: config, bindings_path: bindings})
        print(f"创建清单已生成：{len(devices)} 台云机，共用 {len(set(bindings.values()))} 组代理")
        print(f"创建配置：{config_path}")
        print(f"代理对应表：{bindings_path}")
        for backup in backups:
            print(f"原文件备份：{backup}")
        print("user_name 与 remark 已设为相同名称，count 已同步；公共参数保留。")
        print("清单已保存。创建这批云机，请在项目根目录执行：python -X utf8 .\\创建云机.py")
        return 0
    except (OSError, ValueError, EOFError) as error:
        print(f"生成失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
