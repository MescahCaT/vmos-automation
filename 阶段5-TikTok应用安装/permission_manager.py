import requests
from typing import List, Dict


class PermissionManager:
    """权限管理器"""

    def __init__(self, base_url: str, session=None):
        self.base_url = base_url
        self.session = session or requests.Session()
        self.session.trust_env = False

    def grant_overlay_permission(self, db_id: str, package_name: str = "com.zhiliaoapp.musically") -> bool:
        """授予悬浮窗权限"""
        payload = {
            'db_id': db_id,
            'app': package_name
        }

        try:
            resp = self.session.post(
                f"{self.base_url}/android_api/v1/add_show_other_app_over_permission",
                json=payload,
                timeout=30
            )
            resp.raise_for_status()
            return str(resp.json().get('code')) == "200"
        except (requests.RequestException, ValueError):
            return False

    def grant_root_permission(self, db_id: str, package_name: str = "com.zhiliaoapp.musically") -> bool:
        """授予Root权限"""
        payload = {
            'db_ids': db_id,
            'app': package_name
        }

        try:
            resp = self.session.post(
                f"{self.base_url}/android_api/v1/root_app",
                json=payload,
                timeout=30
            )
            resp.raise_for_status()
            return str(resp.json().get('code')) == "200"
        except (requests.RequestException, ValueError):
            return False

    def batch_grant_permissions(self, db_ids: List[str], package_name: str = "com.zhiliaoapp.musically",
                                 enable_root: bool = False) -> Dict:
        """批量授予权限"""
        result = {
            'overlay_success': [],
            'overlay_failed': [],
            'root_success': [],
            'root_failed': []
        }

        for db_id in db_ids:
            if self.grant_overlay_permission(db_id, package_name):
                result['overlay_success'].append(db_id)
            else:
                result['overlay_failed'].append(db_id)

            if enable_root:
                if self.grant_root_permission(db_id, package_name):
                    result['root_success'].append(db_id)
                else:
                    result['root_failed'].append(db_id)

        return result
