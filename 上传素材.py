"""运行阶段7素材上传入口。"""
from pathlib import Path
import runpy


if __name__ == "__main__":
    script = Path(__file__).resolve().parent / "阶段7-素材上传与内容发布" / "upload_materials.py"
    runpy.run_path(str(script), run_name="__main__")
