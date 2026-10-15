# 状态检查器
import time
import requests
from typing import List, Dict, Optional
from device_config import POLL_CONFIG


def parse_instances(result) -> List[Dict]:
    """兼容实例列表的data数组和data.list两种结构。"""
    if not isinstance(result, dict) or str(result.get("code")) != "200":
        raise ValueError(f"实例列表查询失败: {result}")
    instances = result.get("data")
    if isinstance(instances, dict):
        instances = instances.get("list")
    if not isinstance(instances, list) or any(
        not isinstance(item, dict) or not isinstance(item.get("db_id"), str)
        for item in instances
    ):
        raise ValueError("实例列表格式无法识别")
    return instances


class StatusChecker:
    """状态检查器"""

    def __init__(self, base_url: str):
        self.base_url = base_url
        self.interval = POLL_CONFIG["interval"]
        self.max_attempts = POLL_CONFIG["max_attempts"]

    def _get_instances(self) -> Optional[List[Dict]]:
        try:
            response = requests.post(
                f"{self.base_url}/container_api/v1/get_db", json={}, timeout=10
            )
            response.raise_for_status()
            return parse_instances(response.json())
        except (requests.RequestException, ValueError):
            return None

    def wait_for_running(self, db_ids: List[str]) -> Dict[str, bool]:
        """轮询等待实例启动完成"""
        results = {db_id: False for db_id in db_ids}

        for _ in range(self.max_attempts):
            instances = self._get_instances()
            if instances is not None:
                for instance in instances:
                    db_id = instance["db_id"]
                    if db_id in results and (instance.get("state") or instance.get("status")) == "running":
                        results[db_id] = True
            if all(results.values()):
                return results
            time.sleep(self.interval)

        return results

    def wait_for_rom_ready(self, db_ids: List[str]) -> Dict[str, bool]:
        """轮询等待ROM就绪"""
        results = {db_id: False for db_id in db_ids}

        for _ in range(self.max_attempts):
            for db_id in db_ids:
                if results[db_id]:
                    continue

                try:
                    resp = requests.get(f"{self.base_url}/container_api/v1/rom_status/{db_id}", timeout=10)
                    if resp.status_code == 200:
                        data = resp.json()
                        if str(data.get("code")) == "200":
                            results[db_id] = True
                except (requests.RequestException, ValueError):
                    pass

            if all(results.values()):
                return results

            time.sleep(self.interval)

        return results

    def check_instance_state(self, db_id: str) -> str:
        """查询单个实例状态"""
        instances = self._get_instances()
        for instance in instances or []:
            if instance["db_id"] == db_id:
                return instance.get("state") or instance.get("status", "unknown")
        return "unknown"
