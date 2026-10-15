import requests
import sys

BASE_URL = "http://192.168.100.24:18182"

# 目标云机列表
DB_IDS = [
    "s138.2", "s139.1", "s139.2", "s140.1", "s140.2", "s141.1", "s141.2",
    "s142.1", "s142.2", "s143.1", "s143.2", "s144.1", "s144.2", "s145.1",
    "s145.2", "s146.1", "s146.2", "s147.1", "s147.2", "s148.1", "s148.2",
    "s149.1", "s149.2", "s150.1", "s150.2", "s151.1", "s151.2", "s152.1",
    "s152.2", "s153.1", "s153.2", "s154.1", "s154.2", "s155.1", "s155.2",
    "s156.1", "s156.2", "s157.1", "s157.2", "s158.1", "s158.2", "s159.1",
    "s159.2", "s160.1", "s160.2", "s161.1", "s161.2"
]

def check_device_exists(db_id):
    """检查云机是否存在"""
    try:
        resp = requests.get(f"{BASE_URL}/container_api/v1/detail/{db_id}", timeout=5)
        if resp.status_code == 200:
            result = resp.json()
            return result.get('code') == 200
        return False
    except:
        return False

print("检查云机存在状态...")
print("=" * 60)

existing = []
not_found = []

for db_id in DB_IDS:
    exists = check_device_exists(db_id)
    if exists:
        existing.append(db_id)
        print(f"✓ {db_id} - 存在")
    else:
        not_found.append(db_id)
        print(f"✗ {db_id} - 不存在")

print("=" * 60)
print(f"\n存在的云机 ({len(existing)} 台):")
print(','.join(existing))
print(f"\n不存在的云机 ({len(not_found)} 台):")
print(','.join(not_found))
