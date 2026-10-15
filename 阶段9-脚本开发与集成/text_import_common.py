"""文本导入共用读写；只处理本地文件，不连接 VMOS。"""
import copy
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "阶段1-环境准备与配置"))
from relay_proxy import relay_node


def input_path(value, prompt):
    value = (value if value is not None else input(prompt)).strip().strip('\"\'')
    if not value:
        raise ValueError("未输入文本文件路径")
    return Path(value).expanduser().resolve()


def read_text(path):
    data = path.read_bytes()
    encodings = ("utf-16",) if data.startswith((b"\xff\xfe", b"\xfe\xff")) else ("utf-8-sig", "gb18030")
    for encoding in encodings:
        try:
            text = data.decode(encoding)
        except UnicodeError:
            continue
        if "\0" not in text:
            return text
    raise ValueError(f"文本编码无法读取，请另存为 UTF-8：{path}")


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, UnicodeError):
        raise ValueError(f"JSON 无效，请检查文件：{path}") from None


def proxy_index(document):
    if not isinstance(document, dict) or not isinstance(document.get("proxies"), list):
        raise ValueError("代理 JSON 必须包含 proxies 数组")
    result = {}
    for entry in document["proxies"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry["id"]:
            raise ValueError("已有代理缺少字符串 id")
        if entry["id"] in result:
            raise ValueError(f"已有代理 id 重复：{entry['id']}")
        result[entry["id"]] = entry
    return result


def ensure_transit(document, entries):
    """保留各组已有中转，缺失时沿用上次中转；没有可用中转则拒绝写入。"""
    fallback = document.get("default_transit")
    if fallback is None:
        for proxy in reversed(document["proxies"]):
            if proxy.get("enabled", True) is True and proxy.get("transit") is not None:
                try:
                    relay_node(proxy)
                except ValueError:
                    continue
                fallback = proxy["transit"]
                break
    if fallback is not None:
        relay_node({"transit": fallback})
    for proxy in entries:
        if proxy.get("transit") is None:
            if fallback is None:
                raise ValueError("必须携带中转：未找到上次可用的中转，请先配置中转或指定 --template-proxy")
            proxy["transit"] = copy.deepcopy(fallback)
        relay_node(proxy)
        proxy.setdefault("engine_type", 0)
        fallback = proxy["transit"]
    if fallback is not None:
        document["default_transit"] = copy.deepcopy(fallback)


def distinct_paths(*paths):
    resolved = [path.resolve() for path in paths]
    if len(set(resolved)) != len(resolved):
        raise ValueError("输入文本、创建配置、代理列表和绑定表的路径不能互相重合")


def write_json_files(documents):
    """先验证、暂存全部输出，再备份替换；写入失败时恢复已替换的文件。"""
    staged, backups, committed = {}, {}, []
    try:
        for path, document in documents.items():
            payload = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                             dir=path.parent, prefix=path.name + ".", suffix=".tmp",
                                             delete=False) as stream:
                staged[path] = Path(stream.name)
                stream.write(payload)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        for path in documents:
            if path.exists():
                backup = path.with_name(path.name + "." + stamp + ".bak")
                shutil.copy2(path, backup)
                backups[path] = backup
        for path, temporary in staged.items():
            os.replace(temporary, path)
            committed.append(path)
    except OSError:
        for path in reversed(committed):
            if path in backups:
                shutil.copy2(backups[path], path)
            else:
                path.unlink(missing_ok=True)
        raise
    finally:
        for temporary in staged.values():
            temporary.unlink(missing_ok=True)
    return list(backups.values())
