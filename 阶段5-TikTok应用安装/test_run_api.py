import requests
import json
import time

BASE_URL = "http://192.168.100.24:18182"

# 测试启动少量云机
test_ids = ["EDGE16WGIVWHLPZN", "EDGE1IG3D7XBSATY"]

print(f"测试启动 {len(test_ids)} 台云机")
print("=" * 60)

# 1. 启动前检查状态
print("\n启动前状态:")
for db_id in test_ids:
    resp = requests.get(f"{BASE_URL}/container_api/v1/detail/{db_id}", timeout=5)
    if resp.status_code == 200:
        data = resp.json().get('data', {})
        print(f"  {db_id}: {data.get('status', 'unknown')}")

# 2. 调用启动API
print("\n调用启动API...")
resp = requests.post(
    f"{BASE_URL}/container_api/v1/run",
    json={"db_ids": test_ids},
    timeout=30
)
print(f"HTTP状态: {resp.status_code}")
result = resp.json()
print(f"响应: {json.dumps(result, ensure_ascii=False, indent=2)}")

# 3. 等待后检查状态
print("\n等待30秒...")
time.sleep(30)

print("\n启动后状态:")
for db_id in test_ids:
    resp = requests.get(f"{BASE_URL}/container_api/v1/detail/{db_id}", timeout=5)
    if resp.status_code == 200:
        data = resp.json().get('data', {})
        print(f"  {db_id}: {data.get('status', 'unknown')}")
