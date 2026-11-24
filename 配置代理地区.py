"""启动已有云机，仅提交代理及国家、时区、语言设置请求，不做结果检测或TikTok安装。"""
from 创建云机 import main


if __name__ == "__main__":
    raise SystemExit(main(operation="configure"))
