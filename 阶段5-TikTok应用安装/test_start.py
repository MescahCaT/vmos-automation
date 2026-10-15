import requests
import json

BASE_URL = "http://192.168.100.24:18182"

# 测试启动一台云机
test_db_id = "EDGE16WGIVWHLPZN"

print(f"测试启动云机: {test_db_id}")
print("=" * 60)

try:
    resp = requests.post(
        f"{BASE_URL}/container_api/v1/start/{test_db_id}",
        timeout=30
    )
    print(f"HTTP状态: {resp.status_code}")
    print(f"响应内容:\n{json.dumps(resp.json(), ensure_ascii=False, indent=2)}")
except Exception as e:
    print(f"错误: {e}")
