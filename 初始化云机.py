"""已有云机一次开关机内并行申请TK安装与代理，最后提交地区、时区、语言和已配置GPS。"""
from 创建云机 import main


if __name__ == "__main__":
    raise SystemExit(main(operation="initialize_existing"))
