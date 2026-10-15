import requests

BASE_URL = "http://192.168.100.24:18182"
test_id = "EDGE16WGIVWHLPZN"

print(f"检查 {test_id} 的应用列表...")

resp = requests.get(f"{BASE_URL}/android_api/v1/app_get/{test_id}", timeout=10)
print(f"HTTP状态: {resp.status_code}")

if resp.status_code == 200:
    result = resp.json()
    print(f"响应code: {result.get('code')}")

    apps = result.get('data', {}).get('apps', [])
    print(f"\n共 {len(apps)} 个应用:")

    for app in apps[:10]:  # 只显示前10个
        print(f"  - {app.get('package_name')} ({app.get('app_name')})")

    # 查找TikTok
    tiktok = [app for app in apps if 'tiktok' in app.get('package_name', '').lower() or 'musically' in app.get('package_name', '').lower()]

    if tiktok:
        print(f"\n找到TikTok相关应用:")
        for app in tiktok:
            print(f"  - {app.get('package_name')}")
    else:
        print("\n未找到TikTok")
