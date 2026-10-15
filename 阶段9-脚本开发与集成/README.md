# 阶段9：脚本开发与集成

2026-10-14已提供可用模块的单台使用模板：[使用说明](../使用模板.md)、`use_modules.ps1` 和 `create_single.example.json`。PowerShell模板直接复用阶段3/4、5、6正式入口，支持Check/Prepare/Login/Run和Plan预览，任一步非0立即停止；阶段1检查及阶段2创建另有明确操作步骤。5项隔离测试通过，未触发真实创建或登录。阶段7/8暂缓。下方类设计、批量编排及监控仍为后续规划，不代表已经实现。

按文本批量导入代理与云机名清单可用[文本导入说明](文本导入使用说明.md)、`import_proxies.py` 和 `generate_devices.py`；会生成创建 JSON 与名称→代理ID绑定表，不会自动创建 VMOS 实例。隔离测试：`python -X utf8 .\阶段9-脚本开发与集成\tests\test_text_imports.py`。

实际创建的简洁入口在项目根目录：`python -X utf8 .\创建云机.py`。默认读取阶段9生成的创建清单与绑定表，创建后必须依次绑定SOCKS5＋中转、设置国家、时区、语言、GPS、重启回查、验证联网，再安装TikTok。已有云机加 `--configure-only`，单台可再加 `--name 144.1`。导入和创建要求有效中转，未提供时沿用上次中转。每组必须提供完整region，缺少或无效时在创建前停止。安装包沿用主配置 `tiktok.apk_path`，创建前校验包名；已安装跳过上传。地区成功后，网页探测失败也执行当前云机的本地安装，分别记录地区、网络、安装结果；任一未确认仍返回非0并停止后续云机。初始化入口统一在安装结束后恢复原状态，避免代理阶段停机后又为安装重复启动。

## 📋 任务目标

开发核心功能模块并集成为完整的自动化系统。

## ✅ 任务清单

### 9.1 核心功能模块开发
- [ ] VMOSClient 类 - API 封装
- [ ] DeviceManager 类 - 云机管理
- [ ] ProxyManager 类 - 代理管理
- [ ] TikTokAutomation 类 - TikTok 自动化
- [ ] EmailVerification 类 - 验证码处理
- [ ] DataCollector 类 - 数据采集

### 9.2 配置文件设计
- [ ] config.yaml - 主配置文件
- [ ] device_mapping.json - 设备映射
- [ ] proxy_mapping.json - 代理映射
- [ ] account_mapping.json - 账号映射

### 9.3 日志系统
- [ ] 操作日志记录
- [ ] 错误日志记录
- [ ] 状态变更日志
- [ ] 日志文件轮转

### 9.4 异常处理与重试机制
- [ ] API 调用失败重试
- [ ] 网络超时处理
- [ ] 代理失效自动切换
- [ ] 登录失败重试策略

## 📁 项目结构

```
阶段9-脚本开发与集成/
├── src/                      # 源代码目录
│   ├── __init__.py
│   ├── vmos_client.py        # VMOS API 客户端
│   ├── device_manager.py     # 云机管理器
│   ├── proxy_manager.py      # 代理管理器
│   ├── tiktok_automation.py  # TikTok 自动化
│   ├── email_verifier.py     # 邮箱验证
│   ├── data_collector.py     # 数据采集
│   └── utils.py              # 工具函数
├── main.py                   # 主程序入口
├── requirements.txt          # 依赖包列表
└── README.md                 # 说明文档
```

## 🔧 核心类设计

### VMOSClient 类
```python
class VMOSClient:
    """VMOS API 客户端"""
    
    def __init__(self, host_ip: str, port: int = 18182):
        self.base_url = f"http://{host_ip}:{port}"
        self.session = requests.Session()
    
    def create_device(self, config: dict) -> dict:
        """创建云机"""
        pass
    
    def start_device(self, db_id: str) -> dict:
        """启动云机"""
        pass
    
    def set_proxy(self, db_id: str, proxy_config: dict) -> dict:
        """设置代理"""
        pass
```

### DeviceManager 类
```python
class DeviceManager:
    """云机管理器"""
    
    def __init__(self, client: VMOSClient):
        self.client = client
        self.devices = []
    
    def batch_create(self, count: int, config: dict) -> list:
        """批量创建云机"""
        pass
    
    def wait_for_ready(self, db_ids: list, timeout: int = 300) -> dict:
        """等待云机就绪"""
        pass
    
    def get_status(self, db_ids: list) -> dict:
        """获取云机状态"""
        pass
```

### ProxyManager 类
```python
class ProxyManager:
    """代理管理器"""
    
    def __init__(self, client: VMOSClient):
        self.client = client
        self.proxies = []
    
    def load_proxies(self, config_file: str):
        """加载代理配置"""
        pass
    
    def assign_proxy(self, db_id: str) -> dict:
        """分配代理"""
        pass
    
    def verify_proxy(self, db_id: str) -> bool:
        """验证代理"""
        pass
```

### TikTokAutomation 类
```python
class TikTokAutomation:
    """TikTok 自动化"""
    
    def __init__(self, client: VMOSClient):
        self.client = client
    
    def install(self, db_ids: list, apk_path: str) -> dict:
        """批量安装 TikTok"""
        pass
    
    def login(self, db_id: str, account: dict) -> bool:
        """自动登录"""
        pass
    
    def publish(self, db_id: str, content: dict) -> bool:
        """发布内容"""
        pass
```

## 📝 配置文件示例

### config.yaml
```yaml
vmos:
  host_ip: "192.168.100.24"
  port: 18182

devices:
  count: 100
  image_repository: "vcloud_android13_edge_20250925011125"
  resolution:
    width: 1080
    height: 1920
    dpi: 420
    fps: 60

tiktok:
  apk_path: "/path/to/tiktok.apk"
  package_name: "com.zhiliaoapp.musically"

logging:
  level: "INFO"
  format: "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
  file: "logs/automation.log"
  max_bytes: 10485760  # 10MB
  backup_count: 5

retry:
  max_attempts: 3
  delay: 5
  backoff: 2
```

## 📦 依赖包列表

### requirements.txt
```
requests==2.31.0
pyyaml==6.0.1
pandas==2.1.4
openpyxl==3.1.2
schedule==1.2.0
python-dotenv==1.0.0
```

## 🔄 重试机制

```python
import time
from functools import wraps

def retry(max_attempts=3, delay=5, backoff=2):
    """重试装饰器"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            attempt = 0
            current_delay = delay
            
            while attempt < max_attempts:
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    attempt += 1
                    if attempt >= max_attempts:
                        raise
                    
                    print(f"重试 {attempt}/{max_attempts}: {e}")
                    time.sleep(current_delay)
                    current_delay *= backoff
            
        return wrapper
    return decorator

# 使用示例
@retry(max_attempts=3, delay=5)
def call_api(url):
    response = requests.get(url)
    response.raise_for_status()
    return response.json()
```

## 📊 日志系统

```python
import logging
from logging.handlers import RotatingFileHandler

def setup_logger(name, log_file, level=logging.INFO):
    """配置日志"""
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    handler = RotatingFileHandler(
        log_file, 
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5
    )
    handler.setFormatter(formatter)
    
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.addHandler(handler)
    
    return logger

# 使用示例
logger = setup_logger('vmos_automation', 'logs/automation.log')
logger.info("开始创建云机")
logger.error("创建失败", exc_info=True)
```

## 🎯 主程序入口

### main.py
```python
import yaml
from src.vmos_client import VMOSClient
from src.device_manager import DeviceManager
from src.proxy_manager import ProxyManager
from src.tiktok_automation import TikTokAutomation

def main():
    # 加载配置
    with open('config.yaml', 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
    
    # 初始化客户端
    client = VMOSClient(
        host_ip=config['vmos']['host_ip'],
        port=config['vmos']['port']
    )
    
    # 初始化管理器
    device_mgr = DeviceManager(client)
    proxy_mgr = ProxyManager(client)
    tiktok = TikTokAutomation(client)
    
    # 执行任务
    print("1. 批量创建云机...")
    devices = device_mgr.batch_create(
        count=config['devices']['count'],
        config=config['devices']
    )
    
    print("2. 配置代理...")
    for device in devices:
        proxy_mgr.assign_proxy(device['db_id'])
    
    print("3. 安装 TikTok...")
    db_ids = [d['db_id'] for d in devices]
    tiktok.install(db_ids, config['tiktok']['apk_path'])
    
    print("完成!")

if __name__ == '__main__':
    main()
```

## 📊 输出产物

- `src/` - 源代码模块
- `main.py` - 主程序
- `requirements.txt` - 依赖列表
- `config.yaml` - 配置文件模板
- `README.md` - 开发文档
