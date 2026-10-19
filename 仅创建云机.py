"""仅创建并确认云机，不执行代理、地区设置或TikTok安装。"""
from 创建云机 import main


if __name__ == "__main__":
    raise SystemExit(main(create_only=True))
