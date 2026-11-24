"""导入「代理组名 IP 端口 账户 密码」，并核验出口、补齐地区配置。"""
import argparse
import copy
from concurrent.futures import ThreadPoolExecutor, as_completed
import ipaddress
import re
import sys
from pathlib import Path

import requests

from text_import_common import ROOT, distinct_paths, ensure_transit, input_path, proxy_index, read_json, read_text, write_json_files

sys.path.insert(0, str(ROOT / "阶段4-系统设置与环境配置"))
from setup_system import region_settings
from check_vmos_connection import check_proxy


def discover_region(proxy, language):
    """通过既有中转探测真实出口；定位失败最多查询3轮，不按代理入口IP猜地区。"""
    exit_ip = check_proxy(proxy, {"probe_url": "https://api.ipify.org?format=json", "timeout": 15})["exit_ip"]
    for attempt in range(3):
        try:
            response = requests.get("https://ipwho.is/" + exit_ip, timeout=(5, 10))
            response.raise_for_status()
            geo = response.json()
            if (not isinstance(geo, dict) or geo.get("success") is not True
                    or ipaddress.ip_address(geo.get("ip")) != ipaddress.ip_address(exit_ip)):
                raise ValueError("定位结果未确认实际出口IP")
            timezone = geo.get("timezone")
            region = {"country": geo.get("country_code"), "language": language,
                      "timezone": timezone.get("id") if isinstance(timezone, dict) else None}
            return region_settings({"region": region})
        except (requests.RequestException, ValueError, TypeError):
            if attempt == 2:
                raise ValueError("出口地区查询3次未确认有效国家/时区") from None


def parse_proxies(text):
    proxies, seen = [], set()
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split()
        if len(fields) != 5:
            raise ValueError(f"第 {line_number} 行：必须是 5 列「代理组名 IP 端口 账户 密码」，用空格或 Tab 分隔")
        group, host, port, username, password = fields
        if not re.fullmatch(r"[0-9]+", group):
            raise ValueError(f"第 {line_number} 行：代理组名只能包含数字0～9")
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
    parser.add_argument("--language", help="本次导入组的语言代码，例如 en；省略时保留原语言，新组默认 en")
    parser.add_argument("--workers", type=int, default=3, help="出口及地区查询并发数，默认3")
    args = parser.parse_args(argv)
    try:
        if args.workers < 1:
            raise ValueError("--workers 必须为正整数")
        if args.language is not None and not re.fullmatch(r"[a-z]{2,3}", args.language):
            raise ValueError("--language 必须为2～3位小写语言代码，例如 en")
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
        lookup = []
        for entry in incoming:
            group = entry["id"]
            if group in existing:
                target = existing[group]
            else:
                target = {}
                document["proxies"].append(target)
                existing[group] = target
                added += 1
            previous = copy.deepcopy(target)
            target.update(entry)
            target.update(copy.deepcopy(inherited))
            region = target.get("region")
            language = args.language or (region.get("language") if isinstance(region, dict) else None) or "en"
            if isinstance(region, dict):
                region["language"] = language
            changed = bool(previous) and any(previous.get(key) != target.get(key)
                                            for key in ("protocol", "host", "port", "username", "password", "transit"))
            try:
                region_settings(target)
                refresh = changed and "region" not in inherited
            except ValueError:
                refresh = True
            if refresh:
                lookup.append((target, language))
        ensure_transit(document, [existing[entry["id"]] for entry in incoming])
        failures = []
        if lookup:
            print(f"核验 {len(lookup)} 组实际出口并补齐地区，查询并发：{min(args.workers, len(lookup))}", flush=True)
            with ThreadPoolExecutor(max_workers=min(args.workers, len(lookup))) as pool:
                futures = {pool.submit(discover_region, proxy, language): proxy for proxy, language in lookup}
                for future in as_completed(futures):
                    proxy = futures[future]
                    try:
                        proxy["region"] = future.result()
                        region_settings(proxy)
                        region = proxy["region"]
                        print(f"代理组 {proxy['id']}：{region['country']} / {region['language']} / {region['timezone']}", flush=True)
                    except Exception as error:
                        detail = str(error) if type(error) is ValueError else type(error).__name__
                        failures.append(f"代理组 {proxy['id']}：{detail}")
                        print(f"地区核验失败：{failures[-1]}；继续核查其余组", flush=True)
            if failures:
                raise ValueError("；".join(failures) + "；本次未写入配置，请检查后重新导入")
        backups = write_json_files({output: document})
        print(f"代理导入完成：新增 {added} 组，更新 {len(incoming) - added} 组（SOCKS5）")
        print(f"保存到：{output}")
        print(f"本次 {len(incoming)} 组均已携带有效中转；未指定中转时自动沿用上次配置。")
        for backup in backups:
            print(f"原文件备份：{backup}")
        print(f"本次 {len(incoming)} 组地区配置均已补齐；GPS仍在云机初始化时按实际出口解析。")
        print("未连接VMOS或创建云机；云机代理绑定由阶段3执行。")
        return 0
    except (OSError, ValueError, EOFError) as error:
        print(f"导入失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
