# 设备配置管理
import json
import os
from typing import Dict, Any, List, Optional

# 默认配置
DEFAULT_BASE_CONFIG = {
    "host_ip": "192.168.110.35",
    "image_repository": "vcloud_android13_edge_20250925011125",
    "adi_id": 1039,
    "resolution": {
        "dpi": 560,
        "fps": 60,
        "height": 3040,
        "width": 1440
    },
    "selinux": False,
    "dns": ["8.8.8.8", "1.1.1.1"],
    "bool_gms_disabled": False,
    "bool_gms_upgrade_enable": False
}

# 输出文件
OUTPUT_FILES = {
    "devices": "devices_created.json",
    "ports": "port_mapping.json",
    "results": "creation_results.json",
    "log": "creation_log.txt"
}

# 轮询配置
POLL_CONFIG = {
    "interval": 3,
    "max_attempts": 200,
    "create_check_attempts": 20,
    "batch_size": 10
}


class ConfigLoader:
    """配置加载器"""

    def __init__(self, config_file: str = "devices_input.json"):
        self.config_file = config_file
        self.config = self._load_config()

    def _load_config(self) -> Dict:
        """加载配置文件"""
        if not os.path.exists(self.config_file):
            return self._get_default_config()

        with open(self.config_file, "r", encoding="utf-8") as f:
            return json.load(f)

    def _get_default_config(self) -> Dict:
        """获取默认配置"""
        return {
            "host_ip": DEFAULT_BASE_CONFIG["host_ip"],
            "count": 1,
            "devices": [],
            "default_config": DEFAULT_BASE_CONFIG,
            "port_config": {
                "auto_allocate": True,
                "video_start": 25000,
                "audio_start": 27000,
                "control_start": 29000,
                "adb_start": 31000
            }
        }

    def get_host_ip(self) -> str:
        """获取主机IP"""
        return self.config.get("host_ip", DEFAULT_BASE_CONFIG["host_ip"])

    def get_base_url(self) -> str:
        """获取基础URL"""
        return f"http://{self.get_host_ip()}:18182"

    def get_count(self) -> int:
        """获取创建数量"""
        return self.config.get("count", 1)

    def get_devices(self) -> List[Dict]:
        """获取设备列表"""
        return self.config.get("devices", [])

    def get_default_config(self) -> Dict:
        """获取默认设备配置"""
        return self.config.get("default_config", DEFAULT_BASE_CONFIG)

    def get_port_config(self) -> Dict:
        """获取端口配置"""
        return self.config.get("port_config", {
            "auto_allocate": True,
            "video_start": 25000,
            "audio_start": 27000,
            "control_start": 29000,
            "adb_start": 31000
        })

    def get_device_at_index(self, index: int) -> Optional[Dict]:
        """获取指定索引的设备配置"""
        devices = self.get_devices()
        if 0 <= index < len(devices):
            return devices[index]
        return None


def get_create_payload(user_name: str, remark: str, device_config: Dict) -> Dict[str, Any]:
    """生成创建实例请求体"""
    payload = {
        "user_name": user_name,
        "bool_start": False,
        "remark": remark
    }

    cfg = device_config.get("config", device_config)
    payload["image_repository"] = cfg.get("image_repository", DEFAULT_BASE_CONFIG["image_repository"])
    payload["adiID"] = cfg.get("adiID", DEFAULT_BASE_CONFIG["adi_id"])
    payload["resolution"] = cfg.get("resolution", DEFAULT_BASE_CONFIG["resolution"])
    payload["selinux"] = cfg.get("selinux", DEFAULT_BASE_CONFIG["selinux"])
    payload["dns"] = cfg.get("dns", DEFAULT_BASE_CONFIG["dns"])
    payload["bool_gms_disabled"] = cfg.get("bool_gms_disabled", DEFAULT_BASE_CONFIG["bool_gms_disabled"])
    payload["bool_gms_upgrade_enable"] = cfg.get("bool_gms_upgrade_enable", DEFAULT_BASE_CONFIG["bool_gms_upgrade_enable"])

    return payload
