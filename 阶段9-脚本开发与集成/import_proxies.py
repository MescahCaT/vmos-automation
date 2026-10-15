"""将「代理组名 IP 端口 账户 密码」文本合并到代理列表。"""
import argparse
import copy
import ipaddress
import re
import sys
from pathlib import Path

from text_import_common import ROOT, distinct_paths, ensure_transit, input_path, proxy_index, read_json, read_text, write_json_files


def parse_proxies(text):
    proxies, seen = [], set()
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split()
        if len(fields) != 5:
            raise ValueError(f"第 {line_number} 行：必须是 5 列「代理组名 IP 端口 账户 密码」，用空格或 Tab 分隔")
        group, host, port, username, password = fields
        if not re.fullmatch(r"[A-Za-z0-9_-]+", group):
            raise ValueError(f"第 {line_number} 行：代理组名只能包含字母、数字、下划线或横线")
        if group in seen:
            raise ValueError(f"第 {line_number} 行：代理组名重复：{group}")
        try:
            ipaddress.ip_address(host)
            if not re.fullmatch(r"[0-9]+", port) or not 1 <= int(port) <= 65535:
                raise ValueError()
        except ValueError:
            raise ValueError(f"第 {line_number} 行：IP 地址或端口无效（端口应为 1～65535）") from None
        proxies.append(dict(id=group, protocol="socks5", host=host, port=int(port),
                            username=username, password=password, enabled=True))
        seen.add(group)
    if not proxies:
        raise ValueError("文本中没有可导入的代理")
    return proxies


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", nargs="?", help="代理文本路径；省略时提示输入")
    parser.add_argument("--output", type=Path, default=ROOT / "配置文件" / "proxy_list.json", help="代理列表 JSON 路径")
    parser.add_argument("--template-proxy", help="显式沿用该代理的中转、引擎及国家/语言/时区；GPS 仍按各自出口解析")
    args = parser.parse_args(argv)
    try:
        source = input_path(args.file, "请输入代理文本文件路径：")
        output = args.output.resolve()
        distinct_paths(source, output)
        incoming = parse_proxies(read_text(source))
        document = read_json(output) if output.exists() else {"proxies": []}
        existing = proxy_index(document)
        inherited = {}
        if args.template_proxy:
            template = existing.get(args.template_proxy)
            if template is None or template.get("enabled", True) is not True:
                raise ValueError("模板代理不存在或未启用")
            if not isinstance(template.get("transit"), dict):
                raise ValueError("模板代理必须携带有效中转")
            inherited = {key: copy.deepcopy(template[key]) for key in ("transit", "engine_type") if key in template}
            if isinstance(template.get("region"), dict):
                inherited["region"] = {key: template["region"][key] for key in ("country", "language", "timezone") if key in template["region"]}
            document["default_transit"] = copy.deepcopy(template["transit"])
        added = 0
        missing_region = 0
        for entry in incoming:
            group = entry["id"]
            if group in existing:
                target = existing[group]
            else:
                target = {}
                document["proxies"].append(target)
                existing[group] = target
                added += 1
            target.update(entry)
            target.update(copy.deepcopy(inherited))
            region = target.get("region")
            if not isinstance(region, dict) or not all(region.get(key) for key in ("country", "language", "timezone")):
                missing_region += 1
        ensure_transit(document, [existing[entry["id"]] for entry in incoming])
        backups = write_json_files({output: document})
        print(f"代理导入完成：新增 {added} 组，更新 {len(incoming) - added} 组（SOCKS5）")
        print(f"保存到：{output}")
        print(f"本次 {len(incoming)} 组均已携带有效中转；未指定中转时自动沿用上次配置。")
        for backup in backups:
            print(f"原文件备份：{backup}")
        if missing_region:
            print(f"其中 {missing_region} 组未配置完整地区；执行阶段3/4前请补 country/language/timezone。")
        print("本次完成本地配置导入；代理联网及云机绑定由阶段3执行。")
        return 0
    except (OSError, ValueError, EOFError) as error:
        print(f"导入失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
