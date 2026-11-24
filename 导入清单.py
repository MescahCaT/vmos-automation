"""将逐行云机名称TXT导入指定主机的清单和代理绑定表，不操作云机。"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "阶段9-脚本开发与集成"))
from generate_devices import main


if __name__ == "__main__":
    raise SystemExit(main(require_host=True))
