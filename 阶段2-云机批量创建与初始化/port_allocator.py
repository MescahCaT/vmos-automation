# 端口分配器
from typing import Dict, List, Optional


class PortAllocator:
    """端口分配器"""

    def __init__(self, port_config: Dict = None):
        self.allocated = {"video": [], "audio": [], "control": [], "adb": []}

        if port_config and not port_config.get("auto_allocate", True):
            self.current = {}
        else:
            default_starts = {
                "video": 25000,
                "audio": 27000,
                "control": 29000,
                "adb": 31000
            }
            if port_config:
                self.current = {
                    "video": port_config.get("video_start", 25000),
                    "audio": port_config.get("audio_start", 27000),
                    "control": port_config.get("control_start", 29000),
                    "adb": port_config.get("adb_start", 31000)
                }
            else:
                self.current = default_starts

    def allocate(self, db_id: str, custom_ports: Optional[Dict] = None) -> Dict[str, int]:
        """为设备分配端口"""
        if custom_ports:
            ports = {
                "video": custom_ports.get("video"),
                "audio": custom_ports.get("audio"),
                "control": custom_ports.get("control"),
                "adb": custom_ports.get("adb")
            }
        else:
            ports = {}
            if not self.current:
                return ports
            for port_type in ["video", "audio", "control", "adb"]:
                port = self.current[port_type]
                ports[port_type] = port
                self.current[port_type] += 1

        for port_type, port in ports.items():
            self.allocated[port_type].append({"db_id": db_id, "port": port})

        return ports

    def get_mapping(self) -> Dict[str, List[Dict]]:
        """获取完整映射表"""
        return self.allocated

    def get_device_ports(self, db_id: str) -> Dict[str, int]:
        """查询指定设备的端口"""
        result = {}
        for port_type, allocations in self.allocated.items():
            for item in allocations:
                if item["db_id"] == db_id:
                    result[port_type] = item["port"]
                    break
        return result
