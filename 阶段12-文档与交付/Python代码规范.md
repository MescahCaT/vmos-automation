# Python 代码规范 - VMOS 自动化项目

## 1. 核心原则

### 1.1 高内聚低耦合
- 每个模块只负责一个功能域
- 模块间通过明确接口交互，不直接访问内部实现
- 配置、工具、业务逻辑严格分离

### 1.2 快速定位原则
- 函数名直接反映功能（`create_device` 优于 `process`）
- 异常必须包含上下文信息（设备ID、操作类型）
- 日志记录关键节点和参数

### 1.3 方便修改原则
- 硬编码值统一到配置文件
- 重复逻辑抽取为函数
- 接口参数使用字典，便于扩展

## 2. 项目结构规范

### 2.1 目录结构
```
阶段X-功能模块/
├── README.md           # 模块说明
├── config.py           # 模块配置（可选）
├── core.py             # 核心业务逻辑
├── utils.py            # 工具函数
└── main.py             # 入口脚本
```

### 2.2 公共模块
```
common/
├── api_client.py       # VMOS API 统一封装
├── logger.py           # 日志配置
├── config_loader.py    # 配置加载器
└── constants.py        # 全局常量
```

## 3. 代码风格

### 3.1 命名规范
```python
# 模块/文件：小写+下划线
device_manager.py

# 类：大驼峰
class DeviceManager:

# 函数/变量：小写+下划线
def create_device(device_config):
    db_id = device_config.get('db_id')

# 常量：大写+下划线
API_TIMEOUT = 30
DEFAULT_IMAGE = "vcloud_android13_edge"

# 私有方法：单下划线前缀
def _validate_response(self, response):
```

### 3.2 函数设计
```python
# ✓ 好：单一职责，参数清晰
def create_device(name: str, image: str, proxy: dict) -> dict:
    """创建云机实例
    
    Args:
        name: 设备名称
        image: 镜像ID
        proxy: 代理配置 {"proxyUrl": "socks5://..."}
    
    Returns:
        {"db_id": "EDGE001", "status": "created"}
    
    Raises:
        DeviceCreateError: 创建失败时抛出
    """
    config = _build_config(name, image, proxy)
    response = api_client.post('/container_api/v1/create', json=config)
    return _parse_response(response)


# ✗ 差：职责混乱，参数不清晰
def process(data):
    # 创建设备、设置代理、安装应用...混在一起
    pass
```

### 3.3 错误处理
```python
# ✓ 好：明确异常类型，包含上下文
class VMOSAPIError(Exception):
    """VMOS API 调用异常"""
    def __init__(self, endpoint: str, status_code: int, message: str):
        self.endpoint = endpoint
        self.status_code = status_code
        super().__init__(f"{endpoint} failed ({status_code}): {message}")


def set_proxy(db_id: str, proxy_url: str):
    try:
        response = requests.post(f'{API_BASE}/android_api/v1/proxy_set/{db_id}', 
                                json={'proxyUrl': proxy_url}, timeout=10)
        response.raise_for_status()
    except requests.Timeout:
        raise VMOSAPIError(f'/proxy_set/{db_id}', 0, 'Request timeout')
    except requests.HTTPError as e:
        raise VMOSAPIError(f'/proxy_set/{db_id}', e.response.status_code, str(e))


# ✗ 差：吞掉异常，无法定位问题
def set_proxy(db_id, url):
    try:
        requests.post(API + db_id, json={'url': url})
    except:
        pass
```

### 3.4 日志规范
```python
import logging

logger = logging.getLogger(__name__)

# ✓ 好：关键参数记录，便于追踪
def install_apk(db_id: str, apk_path: str):
    logger.info(f"[{db_id}] 开始安装APK: {apk_path}")
    try:
        result = _upload_and_install(db_id, apk_path)
        logger.info(f"[{db_id}] 安装成功")
        return result
    except Exception as e:
        logger.error(f"[{db_id}] 安装失败: {e}", exc_info=True)
        raise

# ✗ 差：日志无上下文
def install_apk(db_id, path):
    logger.info("开始安装")
    # 出错时不知道是哪个设备
```

## 4. 模块化设计

### 4.1 API 客户端封装
```python
# common/api_client.py
class VMOSClient:
    def __init__(self, host: str, port: int):
        self.base_url = f"http://{host}:{port}"
        self.session = requests.Session()
        self.session.headers.update({'Content-Type': 'application/json'})
    
    def create_device(self, config: dict) -> dict:
        """创建云机"""
        return self._post('/container_api/v1/create', config)
    
    def set_proxy(self, db_id: str, proxy: dict) -> bool:
        """设置代理"""
        return self._post(f'/android_api/v1/proxy_set/{db_id}', proxy)
    
    def _post(self, endpoint: str, data: dict) -> dict:
        """统一POST请求处理"""
        url = self.base_url + endpoint
        try:
            resp = self.session.post(url, json=data, timeout=30)
            resp.raise_for_status()
            result = resp.json()
            if result.get('code') != 0:
                raise VMOSAPIError(endpoint, result.get('code'), result.get('msg'))
            return result.get('data', {})
        except requests.RequestException as e:
            raise VMOSAPIError(endpoint, 0, str(e))
```

### 4.2 配置管理
```python
# common/config_loader.py
import yaml
from pathlib import Path

class Config:
    def __init__(self, config_path: str):
        self._data = self._load(config_path)
    
    def _load(self, path: str) -> dict:
        with open(path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    
    @property
    def vmos_host(self) -> str:
        return self._data['vmos']['host_ip']
    
    @property
    def vmos_port(self) -> int:
        return self._data['vmos']['port']
    
    def get_proxy_list(self) -> list:
        return self._data.get('proxies', [])


# 使用示例
config = Config('配置文件/config.yaml')
client = VMOSClient(config.vmos_host, config.vmos_port)
```

### 4.3 批量操作器
```python
# common/batch_executor.py
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, List, Any
import time

class BatchExecutor:
    def __init__(self, max_workers: int = 10, delay: float = 0.5):
        self.max_workers = max_workers
        self.delay = delay
    
    def execute(self, func: Callable, items: List[Any]) -> dict:
        """批量执行任务
        
        Returns:
            {"success": [...], "failed": [...]}
        """
        results = {"success": [], "failed": []}
        
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {executor.submit(func, item): item for item in items}
            
            for future in as_completed(futures):
                item = futures[future]
                try:
                    result = future.result()
                    results["success"].append({"item": item, "result": result})
                    logger.info(f"[{item}] 执行成功")
                except Exception as e:
                    results["failed"].append({"item": item, "error": str(e)})
                    logger.error(f"[{item}] 执行失败: {e}")
                
                time.sleep(self.delay)
        
        return results


# 使用示例
executor = BatchExecutor(max_workers=5, delay=1)
results = executor.execute(
    lambda db_id: client.set_proxy(db_id, proxies[db_id]),
    device_ids
)
```

## 5. 禁止事项

### 5.1 禁止大段耦合代码
```python
# ✗ 禁止：一个函数做所有事情
def setup_device(device_id):
    # 创建设备
    requests.post(...)
    # 设置代理
    requests.post(...)
    # 安装应用
    requests.post(...)
    # 启动应用
    requests.post(...)


# ✓ 正确：拆分为独立步骤
def setup_device_pipeline(device_id):
    device = create_device(device_id)
    set_proxy(device['db_id'], get_proxy(device_id))
    install_app(device['db_id'], APK_PATH)
    start_app(device['db_id'], PACKAGE_NAME)
```

### 5.2 禁止硬编码
```python
# ✗ 禁止
response = requests.post('http://192.168.100.24:18182/container_api/v1/create')

# ✓ 正确
API_BASE = config.get_api_base()
response = requests.post(f'{API_BASE}/container_api/v1/create')
```

### 5.3 禁止调试代码残留
```python
# ✗ 禁止提交
def process(data):
    print("调试信息:", data)  # 删除
    import pdb; pdb.set_trace()  # 删除
    # result = old_method()  # 删除注释代码
    result = new_method()
    return result
```

## 6. 文件头模板

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块名称: 云机批量创建

功能描述:
    批量创建 VMOS 云机实例，分配端口和代理

主要函数:
    - create_devices: 批量创建设备
    - allocate_ports: 分配端口范围

依赖模块:
    - common.api_client
    - common.config_loader

作者: VMOS自动化团队
创建时间: 2025-01-15
"""

import logging
from typing import List, Dict

logger = logging.getLogger(__name__)
```

## 7. 单元测试规范

```python
# 测试文件命名: test_模块名.py
# 测试目录: 各阶段目录下 tests/

import unittest
from unittest.mock import patch, MagicMock

class TestDeviceManager(unittest.TestCase):
    def setUp(self):
        self.client = VMOSClient('192.168.100.24', 18182)
    
    @patch('requests.Session.post')
    def test_create_device_success(self, mock_post):
        mock_post.return_value.json.return_value = {
            'code': 0,
            'data': {'db_id': 'EDGE001'}
        }
        
        result = self.client.create_device({'user_name': 'TEST'})
        self.assertEqual(result['db_id'], 'EDGE001')
    
    def test_create_device_missing_name(self):
        with self.assertRaises(ValueError):
            self.client.create_device({})
```

## 8. 变更记录格式

每次代码提交必须附带变更说明：

```
【阶段X-模块/文件名: 修改内容; 目的】

示例：
【阶段2-云机创建/create_devices.py: 增加端口冲突检测; 避免创建失败】
【common/api_client.py: 统一异常处理; 便于上层捕获】
```

## 9. 代码审查检查点

提交前自查：
- [ ] 函数职责单一，不超过50行
- [ ] 无硬编码IP、端口、路径
- [ ] 异常处理完整，包含上下文
- [ ] 日志记录关键操作和参数
- [ ] 无 print、pdb 等调试代码
- [ ] 无注释代码块
- [ ] 变量、函数命名清晰
- [ ] 复杂逻辑有注释说明

## 10. 性能规范

### 10.1 批量操作
```python
# ✓ 好：使用批量API
api_client.batch_install_apk(device_ids, apk_path)

# ✗ 差：循环调用单个API
for db_id in device_ids:
    api_client.install_apk(db_id, apk_path)
```

### 10.2 并发控制
```python
# 并发数不超过 10，避免主机过载
executor = BatchExecutor(max_workers=10)

# 操作间隔 0.5-2 秒，避免风控
executor = BatchExecutor(delay=1.0)
```

### 10.3 资源清理
```python
# ✓ 好：使用上下文管理器
with open(file_path, 'r') as f:
    data = f.read()

# ✓ 好：手动清理
session = requests.Session()
try:
    result = session.post(...)
finally:
    session.close()
```

## 11. 配置文件规范

### 11.1 YAML 格式
```yaml
# 配置文件/config.yaml
vmos:
  host_ip: "192.168.100.24"
  port: 18182
  timeout: 30

devices:
  batch_size: 10          # 每批处理数量
  retry_times: 3          # 失败重试次数
  image: "vcloud_android13_edge_20250925011125"

logging:
  level: "INFO"
  format: "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
  file: "日志/app.log"
```

### 11.2 敏感信息处理
```python
# 敏感信息不入代码库，使用环境变量或单独文件
import os

PROXY_PASSWORD = os.getenv('PROXY_PASSWORD')
# 或从 .env 文件加载（不提交到 git）
```

---

**本规范强制执行，所有提交代码必须符合以上要求**
