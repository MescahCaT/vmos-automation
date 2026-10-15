import requests
import json
import time
from datetime import datetime

BASE_URL = "http://192.168.100.24:18182"
TIKTOK_APK = r"D:\000aaa_vmos自动化\阶段5-TikTok应用安装\tiktokdowmload\tiktok-43-7-2.apk"
BATCH_SIZE = 10  # 降到10台，更保守
STARTUP_WAIT = 180  # 启动后等待3分钟
INSTALL_WAIT = 240  # 安装等待4分钟

with open('available_devices.json', 'r', encoding='utf-8') as f:
    ALL_DB_IDS = json.load(f)['db_ids']


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def start_batch(db_ids):
    """启动一批云机"""
    try:
        resp = requests.post(f"{BASE_URL}/container_api/v1/run", json={"db_ids": db_ids}, timeout=60)
        return resp.json().get('code') == 200
    except Exception as e:
        log(f"启动失败: {e}")
        return False


def install_batch(db_ids):
    """批量安装TikTok"""
    log(f"上传APK到 {len(db_ids)} 台云机...")

    with open(TIKTOK_APK, 'rb') as f:
        files = {'file': (os.path.basename(TIKTOK_APK), f)}
        data = {'db_ids': ','.join(db_ids)}

        try:
            resp = requests.post(
                f"{BASE_URL}/android_api/v1/upload_file_android_batch",
                files=files,
                data=data,
                timeout=600
            )

            if resp.status_code != 200:
                log(f"上传失败: HTTP {resp.status_code}")
                return []

            result = resp.json()

            # 解析成功的设备
            success_ids = []
            if result.get('code') == 200:
                for item in result.get('data', {}).get('list', []):
                    if item.get('code') == '1':  # 成功标志
                        success_ids.append(item.get('db_id'))

            log(f"上传成功: {len(success_ids)}/{len(db_ids)} 台")
            return success_ids

        except Exception as e:
            log(f"上传异常: {e}")
            return []


def verify_batch(db_ids):
    """验证安装"""
    log(f"验证 {len(db_ids)} 台...")
    verified = []

    for db_id in db_ids:
        try:
            resp = requests.get(f"{BASE_URL}/android_api/v1/app_get/{db_id}", timeout=10)
            if resp.status_code == 200:
                apps = resp.json().get('data', {}).get('apps', [])
                if any(app.get('package_name') == 'com.zhiliaoapp.musically' for app in apps):
                    verified.append(db_id)
        except:
            pass

    log(f"验证通过: {len(verified)}/{len(db_ids)} 台")
    return verified


def grant_permissions_batch(db_ids):
    """批量授权"""
    log(f"配置权限...")
    success = 0

    for db_id in db_ids:
        try:
            resp = requests.post(
                f"{BASE_URL}/android_api/v1/add_show_other_app_over_permission",
                json={'db_id': db_id, 'package_name': 'com.zhiliaoapp.musically'},
                timeout=10
            )
            if resp.json().get('success'):
                success += 1
        except:
            pass

    log(f"权限配置完成: {success}/{len(db_ids)} 台")


def stop_batch(db_ids):
    """停止一批云机"""
    try:
        resp = requests.post(f"{BASE_URL}/container_api/v1/stop", json={"db_ids": db_ids}, timeout=60)
        return resp.json().get('code') == 200
    except:
        return False


# 主流程
import os

log(f"开始批量安装 TikTok")
log(f"总云机数: {len(ALL_DB_IDS)}, 分 {(len(ALL_DB_IDS) + BATCH_SIZE - 1) // BATCH_SIZE} 批")
log("=" * 80)

all_success = []
all_failed = []
batches = [ALL_DB_IDS[i:i+BATCH_SIZE] for i in range(0, len(ALL_DB_IDS), BATCH_SIZE)]

for batch_num, batch in enumerate(batches, 1):
    log(f"\n【批次 {batch_num}/{len(batches)}】- {len(batch)} 台云机")
    log("-" * 80)

    # 步骤1: 启动
    log(f"1/4 启动云机...")
    if not start_batch(batch):
        log("启动失败，跳过本批")
        all_failed.extend(batch)
        continue

    log(f"等待 {STARTUP_WAIT} 秒让云机完全启动...")
    time.sleep(STARTUP_WAIT)

    # 步骤2: 安装
    log(f"2/4 批量安装 TikTok (456MB APK)...")
    installed = install_batch(batch)

    if not installed:
        log("安装失败，跳过本批")
        all_failed.extend(batch)
        stop_batch(batch)
        continue

    log(f"等待 {INSTALL_WAIT} 秒完成安装...")
    time.sleep(INSTALL_WAIT)

    # 步骤3: 验证
    log(f"3/4 验证安装...")
    verified = verify_batch(installed)

    failed_this_batch = list(set(batch) - set(verified))

    all_success.extend(verified)
    all_failed.extend(failed_this_batch)

    # 步骤4: 配置权限
    if verified:
        log(f"4/4 配置权限...")
        grant_permissions_batch(verified)

    # 停止云机
    log("停止云机释放资源...")
    stop_batch(batch)

    log(f"批次 {batch_num} 完成 - 成功: {len(verified)}, 失败: {len(failed_this_batch)}")

    if batch_num < len(batches):
        log("等待15秒后处理下一批...")
        time.sleep(15)

# 总结
log("\n" + "=" * 80)
log("【最终结果】")
log(f"✓ 安装成功: {len(all_success)} 台")
log(f"✗ 安装失败: {len(all_failed)} 台")
log(f"成功率: {len(all_success) * 100 // len(ALL_DB_IDS)}%")

# 保存结果
result = {
    'timestamp': datetime.now().isoformat(),
    'total': len(ALL_DB_IDS),
    'success_count': len(all_success),
    'failed_count': len(all_failed),
    'success_devices': all_success,
    'failed_devices': all_failed
}

with open('../日志/auto_install_result.json', 'w', encoding='utf-8') as f:
    json.dump(result, f, ensure_ascii=False, indent=2)

log(f"\n详细结果: ../日志/auto_install_result.json")
