# 批量创建云机主脚本
import argparse
import json
import time
import requests
from datetime import datetime
from typing import List, Dict, Optional
from device_config import ConfigLoader, OUTPUT_FILES, get_create_payload, POLL_CONFIG
from port_allocator import PortAllocator
from status_checker import StatusChecker, parse_instances


def extract_db_id(result) -> Optional[str]:
    """兼容单个ID、ID数组及嵌套创建响应，只接受唯一ID。"""
    if isinstance(result, str):
        return result.strip() if result.strip().startswith("EDGE") else None
    if isinstance(result, list):
        ids = {db_id for item in result if (db_id := extract_db_id(item))}
        return ids.pop() if len(ids) == 1 else None
    if not isinstance(result, dict):
        return None
    if result.get("success") is False:
        return None
    if "code" in result and str(result["code"]) != "200":
        return None
    db_id = result.get("db_id")
    if isinstance(db_id, str) and db_id.strip():
        return db_id.strip()
    for key in ("db_ids", "data", "list"):
        db_id = extract_db_id(result.get(key))
        if db_id:
            return db_id
    return None


class DeviceCreator:
    """云机创建器"""

    def __init__(self, config_file: str = "devices_input.json"):
        self.config_loader = ConfigLoader(config_file)
        self.base_url = self.config_loader.get_base_url()
        self.port_allocator = PortAllocator(self.config_loader.get_port_config())
        self.status_checker = StatusChecker(self.base_url)
        self.created_devices = []
        self.creation_results = []
        self.logs = []

    def log(self, message: str):
        """记录日志"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_entry = f"[{timestamp}] {message}"
        self.logs.append(log_entry)
        print(log_entry)

    def _list_instances(self) -> List[Dict]:
        list_error = None
        try:
            response = requests.get(
                f"{self.base_url}/container_api/v1/list_names", timeout=(5, 10)
            )
            response.raise_for_status()
            return parse_instances(response.json())
        except (requests.RequestException, ValueError) as error:
            list_error = error

        try:
            response = requests.post(
                f"{self.base_url}/container_api/v1/get_db", json={}, timeout=(5, 10)
            )
            response.raise_for_status()
            return parse_instances(response.json())
        except (requests.RequestException, ValueError) as error:
            raise ValueError(f"list_names和get_db均查询失败: {list_error}; {error}") from error

    def _submit_create(self, payload: Dict, result: Dict) -> Optional[str]:
        try:
            response = requests.post(
                f"{self.base_url}/container_api/v1/create", json=payload, timeout=(5, 60)
            )
            result["http_status"] = response.status_code
            try:
                data = response.json()
            except ValueError:
                data = response.text
            result["response"] = data
            if 200 <= response.status_code < 300 and isinstance(data, dict):
                if str(data.get("code")) == "200":
                    db_id = extract_db_id(data)
                    if db_id:
                        return db_id
            self.log(f"创建返回需回查 {payload['user_name']}: HTTP {response.status_code}, 响应={str(data)[:500]}")
        except requests.ConnectTimeout as error:
            result.update(status="connection_failed", request_error=str(error))
            self.log(f"无法连接VMOS主机 {self.base_url}，创建请求未建立连接，请检查主机地址和网络")
        except requests.RequestException as error:
            result["request_error"] = str(error)
            self.log(f"创建请求异常 {payload['user_name']}: {error}，需要核对实际实例")
        return None

    def _recover_created_id(self, user_name: str, previous_ids: set,
                            result: Dict) -> Optional[str]:
        """只回查新出现的同名实例，不重复发送创建请求。"""
        for attempt in range(POLL_CONFIG["create_check_attempts"]):
            try:
                instances = self._list_instances()
                matches = {
                    item["db_id"] for item in instances
                    if item.get("user_name") == user_name
                    and item["db_id"] and item["db_id"] not in previous_ids
                    and (item.get("state") or item.get("status")) not in (
                        "creating", "failed", "error", "deleting"
                    )
                }
                if len(matches) == 1:
                    return matches.pop()
                if len(matches) > 1:
                    result["query_error"] = "发现多个新建同名实例，无法确定本次ID"
                    return None
            except (requests.RequestException, ValueError) as error:
                result["query_error"] = str(error)
            if attempt + 1 < POLL_CONFIG["create_check_attempts"]:
                time.sleep(POLL_CONFIG["interval"])
        return None

    def _record_created(self, result: Dict, db_id: str,
                        custom_ports: Optional[Dict]) -> Dict:
        ports = {}
        try:
            ports = self.port_allocator.allocate(db_id, custom_ports)
        except (KeyError, TypeError, ValueError) as error:
            result["port_error"] = str(error)
            self.log(f"云机已创建 {db_id}，本地端口记录异常: {error}")
        result.update(status="success", db_id=db_id)
        device = {
            "index": result["index"], "user_name": result["user_name"],
            "remark": result["remark"], "db_id": db_id, "ports": ports,
            "created_at": datetime.now().isoformat(),
            "creation_source": result["creation_source"]
        }
        self.created_devices.append(device)
        self.log(f"创建成功 {result['user_name']} -> {db_id}（{result['creation_source']}）")
        return device

    def create_single(self, index: int, device_spec: Optional[Dict] = None) -> Optional[Dict]:
        """创建单个实例"""
        if device_spec:
            user_name = device_spec.get("user_name")
            remark = device_spec.get("remark", user_name)
            custom_ports = device_spec.get("ports")
            device_config = device_spec.get("config", self.config_loader.get_default_config())
        else:
            user_name = f"device-{index:04d}"
            remark = f"设备-{index:04d}"
            custom_ports = None
            device_config = self.config_loader.get_default_config()

        payload = get_create_payload(user_name, remark, device_config)
        result = {"index": index, "user_name": user_name, "remark": remark,
                  "status": "unconfirmed", "host": self.base_url,
                  "timestamp": datetime.now().isoformat()}
        self.creation_results.append(result)
        previous_ids = None
        try:
            previous_ids = {item["db_id"] for item in self._list_instances()}
        except (requests.RequestException, ValueError) as error:
            result["before_query_error"] = str(error)
            self.log(f"创建前列表查询异常 {user_name}: {error}")
        db_id = self._submit_create(payload, result)
        result["creation_source"] = "接口返回" if db_id else "未确认"
        if result["status"] == "connection_failed":
            return None
        if not db_id and previous_ids is not None:
            db_id = self._recover_created_id(user_name, previous_ids, result)
            result["creation_source"] = "实例列表回查"
        if db_id:
            return self._record_created(result, db_id, custom_ports)
        self.log(f"创建结果待确认 {user_name}，请核对客户端和 creation_results.json；未重发创建请求")
        return None

    def create_batch(self) -> List[Dict]:
        """批量创建实例"""
        devices = self.config_loader.get_devices()
        count = self.config_loader.get_count()

        if devices:
            self.log(f"使用配置文件中的 {len(devices)} 个设备定义")
        else:
            self.log(f"开始批量创建 {count} 个实例")
        total = len(devices) if devices else count
        for i in range(1, total + 1):
            self.create_single(i, devices[i - 1] if devices else None)
            if self.creation_results[-1]["status"] == "connection_failed":
                self.log("主机连接超时，停止后续创建，请修正配置后再运行")
                break
            time.sleep(0.5)

        success_count = len(self.created_devices)
        connection_failed = sum(item["status"] == "connection_failed" for item in self.creation_results)
        pending = sum(item["status"] == "unconfirmed" for item in self.creation_results)
        skipped = total - len(self.creation_results)
        self.log(f"创建完成: {success_count}/{total} 成功，{pending} 待确认，{connection_failed} 连接失败，{skipped} 未执行")
        return self.created_devices

    def start_batch(self, db_ids: List[str]) -> Dict[str, bool]:
        """批量启动实例"""
        batch_size = POLL_CONFIG["batch_size"]
        results = {}

        for i in range(0, len(db_ids), batch_size):
            batch = db_ids[i:i + batch_size]
            self.log(f"启动批次 {i//batch_size + 1}: {len(batch)} 个实例")

            try:
                resp = requests.post(f"{self.base_url}/container_api/v1/run", json={"db_ids": batch}, timeout=30)
                if 200 <= resp.status_code < 300:
                    data = resp.json()
                    if str(data.get("code")) == "200":
                        self.log(f"启动请求成功，等待状态变更")
                        batch_results = self.status_checker.wait_for_running(batch)
                        results.update(batch_results)
                    else:
                        self.log(f"启动失败: {data.get('msg')}")
                        for db_id in batch:
                            results[db_id] = False
                else:
                    self.log(f"启动失败: HTTP {resp.status_code}")
                    for db_id in batch:
                        results[db_id] = False
            except Exception as e:
                self.log(f"启动异常: {str(e)}")
                for db_id in batch:
                    results[db_id] = False

            time.sleep(2)

        return results

    def wait_rom_ready(self, db_ids: List[str]) -> Dict[str, bool]:
        """等待ROM就绪"""
        self.log(f"等待 {len(db_ids)} 个实例ROM就绪")
        results = self.status_checker.wait_for_rom_ready(db_ids)

        success = sum(1 for ready in results.values() if ready)
        self.log(f"ROM就绪: {success}/{len(db_ids)}")
        return results

    def save_results(self):
        """保存结果到文件"""
        with open(OUTPUT_FILES["devices"], "w", encoding="utf-8") as f:
            json.dump(self.created_devices, f, ensure_ascii=False, indent=2)
        self.log(f"已保存设备列表: {OUTPUT_FILES['devices']}")

        port_mapping = self.port_allocator.get_mapping()
        with open(OUTPUT_FILES["ports"], "w", encoding="utf-8") as f:
            json.dump(port_mapping, f, ensure_ascii=False, indent=2)
        self.log(f"已保存端口映射: {OUTPUT_FILES['ports']}")

        with open(OUTPUT_FILES["results"], "w", encoding="utf-8") as f:
            json.dump(self.creation_results, f, ensure_ascii=False, indent=2)
        self.log(f"已保存创建响应及确认结果: {OUTPUT_FILES['results']}")

        self.log(f"已保存日志: {OUTPUT_FILES['log']}")
        with open(OUTPUT_FILES["log"], "w", encoding="utf-8") as f:
            f.write("\n".join(self.logs))


def main():
    """主流程"""
    parser = argparse.ArgumentParser(description="批量创建VMOS云机并确认实际创建结果")
    parser.add_argument("config_file", nargs="?", default="devices_input.json", help="设备配置JSON文件")
    parser.add_argument("--create-only", action="store_true", help="仅创建和确认结果，跳过启动及ROM检查")
    args = parser.parse_args()
    creator = DeviceCreator(args.config_file)

    # 批量创建
    devices = creator.create_batch()
    creator.save_results()
    if args.create_only:
        return
    if not devices:
        creator.log("无已确认创建的设备，退出；请查看 creation_results.json")
        creator.save_results()
        return

    # 批量启动
    db_ids = [d["db_id"] for d in devices]
    start_results = creator.start_batch(db_ids)
    creator.log(f"启动结果: {sum(start_results.values())}/{len(db_ids)}；创建结果独立保留")

    # 等待ROM就绪
    running_ids = [db_id for db_id, started in start_results.items() if started]
    if running_ids:
        creator.wait_rom_ready(running_ids)

    # 保存结果
    creator.log("全部完成")
    creator.save_results()


if __name__ == "__main__":
    main()
