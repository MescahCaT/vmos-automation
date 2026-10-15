import requests
import json
import time
import os
from datetime import datetime

BASE_URL = "http://192.168.100.24:18182"
TIKTOK_APK = r"D:\000aaa_vmos自动化\阶段5-TikTok应用安装\tiktokdowmload\tiktok-43-7-2.apk"

with open('available_devices.json', 'r', encoding='utf-8') as f:
    ALL_DB_IDS = json.load(f)['db_ids']


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def find_running_devices():
    """找出真正运行中的云机（通过尝试安装来判断）"""
    log("正在识别运行中的云机...")
    running = []

    for db_id in ALL_DB_IDS:
        try:
            # 尝试获取应用列表，只有运行中的云机才能成功
            resp = requests.get(f"{BASE_URL}/android_api/v1/app_get/{db_id}", timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                if data.get('code') == 200:
                    running.append(db_id)
                    log(f"  OK {db_id} 运行中")
        except:
            pass

    log(f"找到 {len(running)} 台运行中的云机")
    return running


def install_to_devices(db_ids):
    """批量安装TikTok"""
    if not db_ids:
        return []

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
                    code = item.get('code')
                    db_id = item.get('db_id')
                    msg = item.get('msg')

                    # 安装成功的标志：msg包含"成功"或code为'1'
                    if code == '1' or '成功' in str(msg):
                        success_ids.append(db_id)
                        log(f"  OK {db_id}: {msg}")
                    else:
                        log(f"  FAIL {db_id}: {msg}")

            log(f"上传成功: {len(success_ids)}/{len(db_ids)} 台")
            return success_ids

        except Exception as e:
            log(f"上传异常: {e}")
            return []


def wait_for_install(db_ids, max_wait=300, check_interval=15):
    """轮询等待安装完成"""
    log(f"\n轮询检测安装进度（最多等待{max_wait}秒）...")

    remaining = set(db_ids)
    elapsed = 0

    while remaining and elapsed < max_wait:
        time.sleep(check_interval)
        elapsed += check_interval

        newly_installed = []
        for db_id in list(remaining):
            try:
                resp = requests.get(f"{BASE_URL}/android_api/v1/app_get/{db_id}", timeout=10)
                if resp.status_code == 200:
                    apps = resp.json().get('data', {}).get('apps', [])
                    if any(app.get('package_name') == 'com.zhiliaoapp.musically' for app in apps):
                        newly_installed.append(db_id)
                        remaining.remove(db_id)
                        log(f"  [{elapsed}s] OK {db_id} 安装完成")
            except:
                pass

        if not remaining:
            log(f"  全部安装完成！总耗时 {elapsed} 秒")
            return db_ids

        log(f"  [{elapsed}s] {len(db_ids) - len(remaining)}/{len(db_ids)} 已完成")

    installed = list(set(db_ids) - remaining)
    log(f"  超时，{len(installed)}/{len(db_ids)} 台已完成")
    return installed


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


log("开始批量安装 TikTok")
log("=" * 80)

# 1. 找出运行中的云机
running_devices = find_running_devices()

if not running_devices:
    log("错误: 未找到运行中的云机")
    log("请在客户端手动启动12台云机后重新运行此脚本")
    exit(1)

log(f"\n将对 {len(running_devices)} 台运行中的云机安装 TikTok")
log("=" * 80)

# 2. 批量安装
installed = install_to_devices(running_devices)

if not installed:
    log("安装失败")
    exit(1)

# 3. 轮询等待安装完成
verified = wait_for_install(installed)

# 4. 授权
if verified:
    grant_permissions(verified)

# 5. 保存结果
failed = list(set(running_devices) - set(verified))

log("\n" + "=" * 80)
log(f"[OK] 安装成功: {len(verified)} 台")
log(f"[FAIL] 安装失败: {len(failed)} 台")

with open('../日志/running_install_result.json', 'w', encoding='utf-8') as f:
    json.dump({
        'timestamp': datetime.now().isoformat(),
        'total_running': len(running_devices),
        'success': verified,
        'failed': failed
    }, f, ensure_ascii=False, indent=2)

log("\n结果已保存: ../日志/running_install_result.json")

if verified:
    log(f"\n成功安装的云机:")
    for db_id in verified:
        log(f"  OK {db_id}")
