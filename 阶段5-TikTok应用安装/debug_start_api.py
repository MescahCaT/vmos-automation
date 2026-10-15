import requests
import json

BASE_URL = "http://192.168.100.24:18182"

# 测试启动2台云机
test_ids = ["EDGE16WGIVWHLPZN", "EDGE1IG3D7XBSATY"]

print("测试启动API调用...")
print("=" * 60)

try:
    resp = requests.post(
        f"{BASE_URL}/container_api/v1/run",
        json={"db_ids": test_ids},
        timeout=60
    )

    print(f"HTTP状态码: {resp.status_code}")
    print(f"响应内容:")
    print(json.dumps(resp.json(), ensure_ascii=False, indent=2))

    result = resp.json()
    print(f"\nresult.get('code'): {result.get('code')}")
    print(f"result.get('code') == 200: {result.get('code') == 200}")

except Exception as e:
    print(f"异常: {type(e).__name__}: {e}")
