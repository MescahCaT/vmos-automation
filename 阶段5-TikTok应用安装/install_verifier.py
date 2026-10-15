import requests
import time
from typing import List, Dict, Optional


def parse_app_list(result):
    """兼容真实data.app字符串数组、旧data.apps及包名对象列表。"""
    if not isinstance(result, dict) or str(result.get("code")) != "200":
        raise ValueError("应用列表业务状态码不是200")
    data = result.get("data")
    apps = data.get("app", data.get("apps")) if isinstance(data, dict) else data
    if not isinstance(apps, list):
        raise ValueError("应用列表格式无法识别")
    packages = []
    for app in apps:
        package = app.get("package_name") if isinstance(app, dict) else app
        if not isinstance(package, str) or not package:
            raise ValueError("应用列表缺少包名")
        packages.append(package)
    return packages


class InstallVerifier:
    """安装验证器"""

    def __init__(self, base_url: str, session=None):
        self.base_url = base_url
        self.session = session or requests.Session()
        self.session.trust_env = False

    def get_app_list(self, db_id: str) -> Optional[List[str]]:
        """获取设备已安装应用列表"""
        try:
            resp = self.session.get(
                f"{self.base_url}/android_api/v1/app_get/{db_id}",
                timeout=(5, 10)
            )
            resp.raise_for_status()
            result = resp.json()
            return parse_app_list(result)
        except (requests.RequestException, ValueError):
            return None

    def verify_installed(self, db_id: str, package_name: str = "com.zhiliaoapp.musically") -> bool:
        """验证TikTok是否已安装"""
        apps = self.get_app_list(db_id)
        if not apps:
            return False

        return package_name in apps

    def batch_verify(self, db_ids: List[str], package_name: str = "com.zhiliaoapp.musically") -> Dict:
        """批量验证安装状态"""
        result = {
            'success': [],
            'failed': [],
            'unconfirmed': []
        }

        for db_id in db_ids:
            apps = self.get_app_list(db_id)
            if apps is None:
                result['unconfirmed'].append(db_id)
            elif package_name in apps:
                result['success'].append(db_id)
            else:
                result['failed'].append(db_id)

        return result

    def wait_for_install(self, db_ids, timeout=300, package_name="com.zhiliaoapp.musically", log=print):
        deadline = time.monotonic() + timeout
        remaining = list(db_ids)
        success = []
        last_query = {}
        while remaining:
            for db_id in remaining[:]:
                apps = self.get_app_list(db_id)
                last_query[db_id] = "unavailable" if apps is None else "package_absent"
                if apps is not None and package_name in apps:
                    last_query[db_id] = "package_present"
                    success.append(db_id)
                    remaining.remove(db_id)
                    log(f"安装已确认: {db_id}")
            if not remaining or time.monotonic() >= deadline:
                break
            log(f"等待安装: {len(success)}/{len(db_ids)} 已确认")
            time.sleep(min(5, max(0, deadline - time.monotonic())))
        return {"success": success, "unconfirmed": remaining, "last_query": last_query}
