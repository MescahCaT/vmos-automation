import requests
import json
import time

BASE_URL = "http://192.168.100.24:18182"

# 从 available_devices.json 读取 db_id
with open('available_devices.json', 'r', encoding='utf-8') as f:
    device_data = json.load(f)
    DB_IDS = device_data['db_ids']

def start_device(db_id):
    """启动单个云机"""
    try:
        resp = requests.post(
            f"{BASE_URL}/container_api/v1/start/{db_id}",
            timeout=30
        )
        result = resp.json()
        return result.get('code') == 200
    except:
        return False

print(f"准备启动 {len(DB_IDS)} 台云机...")
print("=" * 60)

success_count = 0
failed_devices = []

for i, db_id in enumerate(DB_IDS, 1):
    print(f"[{i}/{len(DB_IDS)}] 启动 {db_id}...", end=" ")

    if start_device(db_id):
        success_count += 1
        print("✓")
    else:
        failed_devices.append(db_id)
        print("✗")

    time.sleep(0.5)

print("=" * 60)
print(f"\n启动完成: {success_count} 台")
print(f"启动失败: {len(failed_devices)} 台")

if failed_devices:
    print(f"\n失败设备:\n{', '.join(failed_devices)}")

print("\n等待10秒让云机完全启动...")
time.sleep(10)
print("可以开始安装 TikTok 了")
