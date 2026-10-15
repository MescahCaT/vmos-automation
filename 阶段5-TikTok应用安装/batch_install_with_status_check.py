import requests
import json
import time

BASE_URL = "http://192.168.100.24:18182"

with open('available_devices.json', 'r', encoding='utf-8') as f:
    device_data = json.load(f)
    ALL_DB_IDS = device_data['db_ids']

BATCH_SIZE = 12
TIKTOK_APK = r"D:\000aaa_vmos自动化\阶段5-TikTok应用安装\tiktokdowmload\tiktok-43-7-2.apk"


def start_devices_batch(db_ids):
    """批量启动云机"""
    try:
        resp = requests.post(f"{BASE_URL}/container_api/v1/run", json={"db_ids": db_ids}, timeout=60)
        return resp.json().get('code') == 200
    except:
        return False


def wait_for_running(db_ids, max_wait=120, check_interval=10):
    """等待云机启动完成"""
    print(f"  等待云机启动（最多{max_wait}秒）...")

    elapsed = 0
    while elapsed < max_wait:
        time.sleep(check_interval)
        elapsed += check_interval

        running_count = 0
        for db_id in db_ids:
            try:
                resp = requests.get(f"{BASE_URL}/container_api/v1/detail/{db_id}", timeout=5)
                if resp.status_code == 200:
                    status = resp.json().get('data', {}).get('status')
                    if status == 'running':
                        running_count += 1
            except:
                pass

        print(f"  [{elapsed}s] {running_count}/{len(db_ids)} 台已运行")

        if running_count >= len(db_ids) * 0.8:  # 80%启动即可
            print(f"  ✓ {running_count} 台已启动，继续安装")
            return True

    print(f"  ! 超时，仅 {running_count} 台启动")
    return running_count > 0


def install_tiktok_batch(db_ids, apk_path):
    """批量安装TikTok"""
    from install_tiktok import TikTokInstaller
    installer = TikTokInstaller(BASE_URL)
    return installer.install_from_local(apk_path, db_ids, wait_time=180, verify=True)


def stop_devices_batch(db_ids):
    """批量停止云机"""
    try:
        resp = requests.post(f"{BASE_URL}/container_api/v1/stop", json={"db_ids": db_ids}, timeout=60)
        return resp.json().get('code') == 200
    except:
        return False


print(f"总共 {len(ALL_DB_IDS)} 台云机，分批处理（每批 {BATCH_SIZE} 台）")
print("=" * 80)

all_success = []
all_failed = []

batches = [ALL_DB_IDS[i:i+BATCH_SIZE] for i in range(0, len(ALL_DB_IDS), BATCH_SIZE)]

for batch_num, batch in enumerate(batches, 1):
    print(f"\n【批次 {batch_num}/{len(batches)}】处理 {len(batch)} 台云机")
    print("-" * 80)

    # 1. 启动云机
    print("步骤1: 启动云机...")
    if not start_devices_batch(batch):
        print("  启动API调用失败")
        all_failed.extend(batch)
        continue

    # 2. 等待启动完成
    if not wait_for_running(batch, max_wait=120, check_interval=10):
        print("  启动超时，跳过本批次")
        all_failed.extend(batch)
        continue

    # 3. 安装TikTok
    print("步骤2: 批量安装 TikTok（上传456MB，预计3-5分钟）...")
    result = install_tiktok_batch(batch, TIKTOK_APK)

    batch_success = result.get('install_success', [])
    batch_failed = result.get('install_failed', [])

    all_success.extend(batch_success)
    all_failed.extend(batch_failed)

    print(f"  本批次安装成功: {len(batch_success)} 台")
    print(f"  本批次安装失败: {len(batch_failed)} 台")

    # 4. 停止云机
    print("步骤3: 停止云机释放资源...")
    if stop_devices_batch(batch):
        print(f"  ✓ 已停止 {len(batch)} 台云机")

    print(f"\n批次 {batch_num} 完成，等待10秒后处理下一批...")
    time.sleep(10)

print("\n" + "=" * 80)
print("【总结】")
print(f"总安装成功: {len(all_success)} 台")
print(f"总安装失败: {len(all_failed)} 台")

with open('../日志/final_install_result.json', 'w', encoding='utf-8') as f:
    json.dump({
        'success_count': len(all_success),
        'failed_count': len(all_failed),
        'success_devices': all_success,
        'failed_devices': all_failed
    }, f, ensure_ascii=False, indent=2)

print(f"\n结果已保存: ../日志/final_install_result.json")
