import requests
import json
import time

BASE_URL = "http://192.168.100.24:18182"

# 从 available_devices.json 读取 db_id
with open('available_devices.json', 'r', encoding='utf-8') as f:
    device_data = json.load(f)
    ALL_DB_IDS = device_data['db_ids']

BATCH_SIZE = 12  # 每批最多启动12台

def start_devices_batch(db_ids):
    """批量启动云机"""
    try:
        resp = requests.post(
            f"{BASE_URL}/container_api/v1/run",
            json={"db_ids": db_ids},
            timeout=60
        )
        result = resp.json()
        if result.get('code') == 200:
            return db_ids  # 返回成功启动的列表
        return []
    except:
        return []

def install_tiktok_batch(db_ids, apk_path):
    """为指定云机批量安装 TikTok"""
    from install_tiktok import TikTokInstaller

    installer = TikTokInstaller(BASE_URL)
    result = installer.install_from_local(
        apk_path=apk_path,
        db_ids=db_ids,
        wait_time=150,  # 增加到150秒
        verify=True
    )
    return result

def stop_devices_batch(db_ids):
    """批量停止云机"""
    try:
        resp = requests.post(
            f"{BASE_URL}/container_api/v1/stop",
            json={"db_ids": db_ids},
            timeout=60
        )
        return resp.json().get('code') == 200
    except:
        return False


print(f"总共 {len(ALL_DB_IDS)} 台云机，分批处理（每批 {BATCH_SIZE} 台）")
print("=" * 80)

TIKTOK_APK = r"D:\000aaa_vmos自动化\阶段5-TikTok应用安装\tiktokdowmload\tiktok-43-7-2.apk"

all_success = []
all_failed = []

batches = [ALL_DB_IDS[i:i+BATCH_SIZE] for i in range(0, len(ALL_DB_IDS), BATCH_SIZE)]

for batch_num, batch in enumerate(batches, 1):
    print(f"\n【批次 {batch_num}/{len(batches)}】处理 {len(batch)} 台云机")
    print("-" * 80)

    # 1. 启动这批云机
    print("步骤1: 启动云机...")
    started = start_devices_batch(batch)

    if not started:
        print("  启动失败，跳过本批次")
        all_failed.extend(batch)
        continue

    print(f"  启动成功 {len(started)} 台，等待60秒让云机完全启动...")
    time.sleep(60)

    # 2. 安装 TikTok
    print("步骤2: 批量安装 TikTok...")
    result = install_tiktok_batch(started, TIKTOK_APK)

    batch_success = result.get('install_success', [])
    batch_failed = result.get('install_failed', [])

    all_success.extend(batch_success)
    all_failed.extend(batch_failed)

    print(f"  本批次安装成功: {len(batch_success)} 台")
    print(f"  本批次安装失败: {len(batch_failed)} 台")

    # 3. 停止这批云机（释放资源）
    print("步骤3: 停止云机释放资源...")
    if stop_devices_batch(started):
        print(f"  ✓ 已停止 {len(started)} 台云机")
    else:
        print(f"  ! 停止失败（忽略）")

    print(f"\n批次 {batch_num} 完成，等待5秒后处理下一批...")
    time.sleep(5)

print("\n" + "=" * 80)
print("【总结】")
print(f"总安装成功: {len(all_success)} 台")
print(f"总安装失败: {len(all_failed)} 台")

# 保存最终结果
with open('../日志/final_install_result.json', 'w', encoding='utf-8') as f:
    json.dump({
        'success_count': len(all_success),
        'failed_count': len(all_failed),
        'success_devices': all_success,
        'failed_devices': all_failed
    }, f, ensure_ascii=False, indent=2)

print(f"\n结果已保存: ../日志/final_install_result.json")
