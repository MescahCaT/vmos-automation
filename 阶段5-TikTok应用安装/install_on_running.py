import requests
import json
import time
import os
from datetime import datetime

BASE_URL = "http://192.168.100.24:18182"
TIKTOK_APK = r"D:\000aaa_vmos自动化\阶段5-TikTok应用安装\tiktokdowmload\tiktok-43-7-2.apk"
BATCH_SIZE = 10
INSTALL_WAIT = 240

with open('available_devices.json', 'r', encoding='utf-8') as f:
    ALL_DB_IDS = json.load(f)['db_ids']


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def check_running(db_ids):
    """检查哪些云机正在运行"""
    running = []
    for db_id in db_ids:
        try:
            resp = requests.get(f"{BASE_URL}/container_api/v1/detail/{db_id}", timeout=5)
            if resp.status_code == 200:
                status = resp.json().get('data', {}).get('status')
                if status == 'running':
                    running.append(db_id)
        except:
            pass
    return running


def start_stopped_devices(db_ids):
    """只启动已停止的云机"""
    if not db_ids:
        return []

    log(f"  尝试启动 {len(db_ids)} 台云机...")
    try:
        resp = requests.post(f"{BASE_URL}/container_api/v1/run", json={"db_ids": db_ids}, timeout=60)
        result = resp.json()

        log(f"  启动API响应: code={result.get('code')}, msg={result.get('msg')}")

        if result.get('code') == 2:
            log(f"  详细信息: {result.get('data')}")
            return []

        if result.get('code') == 200:
            return db_ids

        return []
    except Exception as e:
        log(f"  启动异常: {e}")
        return []


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
            success_ids = []

            if result.get('code') == 200:
                for item in result.get('data', {}).get('list', []):
                    if item.get('code') == '1':
                        success_ids.append(item.get('db_id'))
                    else:
                        log(f"  {item.get('db_id')}: {item.get('msg')}")

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


def grant_permissions(db_ids):
    """批量授权"""
    log(f"配置权限...")
    for db_id in db_ids:
        try:
            requests.post(
                f"{BASE_URL}/android_api/v1/add_show_other_app_over_permission",
                json={'db_id': db_id, 'package_name': 'com.zhiliaoapp.musically'},
                timeout=10
            )
        except:
            pass


log(f"开始批量安装 TikTok（直接对运行中云机操作）")
log(f"总云机数: {len(ALL_DB_IDS)}")
log("=" * 80)

all_success = []
all_failed = []
batches = [ALL_DB_IDS[i:i+BATCH_SIZE] for i in range(0, len(ALL_DB_IDS), BATCH_SIZE)]

for batch_num, batch in enumerate(batches, 1):
    log(f"\n【批次 {batch_num}/{len(batches)}】- {len(batch)} 台云机")
    log("-" * 80)

    # 检查运行状态
    log("检查云机状态...")
    running = check_running(batch)
    stopped = list(set(batch) - set(running))

    log(f"  运行中: {len(running)} 台")
    log(f"  已停止: {len(stopped)} 台")

    # 启动已停止的
    target_devices = running[:]  # 先加入已运行的

    if stopped:
        started = start_stopped_devices(stopped)
        if started:
            log(f"  已启动 {len(started)} 台，等待60秒...")
            time.sleep(60)
            target_devices.extend(started)
        else:
            log(f"  启动失败，跳过这 {len(stopped)} 台")

    if not target_devices:
        log("  无可用云机，跳过")
        all_failed.extend(batch)
        continue

    # 批量安装
    log(f"批量安装 TikTok 到 {len(target_devices)} 台云机...")
    installed = install_batch(target_devices)

    if not installed:
        log("安装失败，跳过")
        all_failed.extend(batch)
        continue

    log(f"等待 {INSTALL_WAIT} 秒完成安装...")
    time.sleep(INSTALL_WAIT)

    # 验证
    verified = verify_batch(installed)
    failed = list(set(batch) - set(verified))

    all_success.extend(verified)
    all_failed.extend(failed)

    # 授权
    if verified:
        grant_permissions(verified)

    log(f"批次完成 - 成功: {len(verified)}, 失败: {len(failed)}")

    if batch_num < len(batches):
        log("等待10秒...")
        time.sleep(10)

log("\n" + "=" * 80)
log(f"✓ 成功: {len(all_success)} 台")
log(f"✗ 失败: {len(all_failed)} 台")

with open('../日志/auto_install_result.json', 'w', encoding='utf-8') as f:
    json.dump({
        'timestamp': datetime.now().isoformat(),
        'success': all_success,
        'failed': all_failed
    }, f, ensure_ascii=False, indent=2)

log("结果已保存")
