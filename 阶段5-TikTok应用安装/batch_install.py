from install_tiktok import TikTokInstaller

# 从 available_devices.json 读取实际的 db_id
import json

with open('available_devices.json', 'r', encoding='utf-8') as f:
    device_data = json.load(f)
    DB_IDS = device_data['db_ids']

BASE_URL = "http://192.168.100.24:18182"

# TikTok APK 配置（请根据实际情况修改）
TIKTOK_URL = "https://example.com/tiktok.apk"  #FIXME 替换为实际下载链接
# 或使用本地文件
TIKTOK_LOCAL_PATH = r"D:\000aaa_vmos自动化\阶段5-TikTok应用安装\tiktokdowmload\tiktok-43-7-2.apk"


def main():
    installer = TikTokInstaller(BASE_URL)

    print(f"准备为 {len(DB_IDS)} 台云机安装 TikTok")
    print("=" * 60)

    # 方式1：使用URL安装（推荐）
    # result = installer.install_from_url(
    #     url=TIKTOK_URL,
    #     db_ids=DB_IDS,
    #     wait_time=90,
    #     verify=True
    # )

    # 方式2：使用本地APK安装
    result = installer.install_from_local(
        apk_path=TIKTOK_LOCAL_PATH,
        db_ids=DB_IDS,
        wait_time=90,
        verify=True
    )

    installer.save_results(result)

    print("=" * 60)
    print(f"安装完成: {len(result.get('install_success', []))} 台")
    print(f"安装失败: {len(result.get('install_failed', []))} 台")


if __name__ == "__main__":
    main()
