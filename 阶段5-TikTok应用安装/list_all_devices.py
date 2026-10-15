import requests
import json

BASE_URL = "http://192.168.100.24:18182"

def get_all_devices():
    """获取所有云机列表"""
    try:
        resp = requests.get(f"{BASE_URL}/container_api/v1/list_names", timeout=10)
        resp.raise_for_status()
        result = resp.json()

        if result.get('code') == 200:
            devices = result.get('data', {}).get('list', [])
            return devices
        return []
    except Exception as e:
        print(f"查询失败: {e}")
        return []

print("查询所有云机...")
print("=" * 80)

devices = get_all_devices()

if not devices:
    print("未找到任何云机")
else:
    print(f"找到 {len(devices)} 台云机:\n")

    target_devices = []

    for device in devices:
        db_id = device.get('db_id')
        user_name = device.get('user_name')
        status = device.get('status', 'unknown')

        # 筛选 s138-s161 范围的云机
        if user_name and user_name.startswith('s1') and '.' in user_name:
            num = int(user_name.split('.')[0][1:])
            if 138 <= num <= 161:
                target_devices.append(device)
                print(f"db_id: {db_id:20s} | user_name: {user_name:10s} | 状态: {status}")

    print("=" * 80)
    print(f"\n目标范围内云机 ({len(target_devices)} 台):")

    db_ids = [d.get('db_id') for d in target_devices]
    print(','.join(db_ids))

    # 保存到文件
    with open('available_devices.json', 'w', encoding='utf-8') as f:
        json.dump({
            'count': len(target_devices),
            'db_ids': db_ids,
            'devices': target_devices
        }, f, ensure_ascii=False, indent=2)

    print(f"\n详细信息已保存到: available_devices.json")
