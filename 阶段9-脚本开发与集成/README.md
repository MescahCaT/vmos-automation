# 阶段9：脚本开发与集成

2026-10-23：代理文本导入仅接受数字0～9组成的代理组名。生成云机清单时，从名称中首个点或横线前提取全部数字作为代理ID，例如`abc123.2`→`123`、`abc123-2`→`123`；首个分隔符之后的内容不参与匹配，云机名和备注保留原值。前导零保留，缺少数字或代理不存在/未启用时拒绝生成。名称变化后重新运行`generate_devices.py`更新清单和绑定表，创建/初始化命令不变；已有历史代理不自动改名。

2026-10-20：根目录增加 `登录TK.py`，支持按名称单台/多台、`--workers`、`--accounts-file`、`--bindings`、加密断点续跑及单台失败继续。复用阶段6正式登录逻辑，不由创建/安装入口自动触发。用法见 [阶段6说明](../阶段6-TikTok自动登录/README.md)，本批10账号实机尚未全部通过。

2026-10-14已提供可用模块的单台使用模板：[使用说明](../使用模板.md)、`use_modules.ps1` 和 `create_single.example.json`。PowerShell模板直接复用阶段3/4、5、6正式入口，支持Check/Prepare/Login/Run和Plan预览，任一步非0立即停止；阶段1检查及阶段2创建另有明确操作步骤。5项隔离测试通过，未触发真实创建或登录。阶段7/8暂缓。下方类设计、批量编排及监控仍为后续规划，不代表已经实现。

按文本批量导入代理与云机名清单可用[文本导入说明](文本导入使用说明.md)、`import_proxies.py` 和 `generate_devices.py`；会生成创建 JSON 与名称→代理ID绑定表，不会自动创建 VMOS 实例。代理导入会自动为新组、地区不完整或连接信息变更的组查询实际出口，补齐国家/时区；语言保留原设置，新组默认en，可用`--language`指定，查询并发默认3，可用`--workers`调整。查询失败继续检查其他组，全部通过才备份写入；原始文本重新导入即可补齐旧组缺失的地区。隔离测试：`python -X utf8 .\阶段9-脚本开发与集成\tests\test_text_imports.py`。

实际创建的简洁入口在项目根目录：`python -X utf8 .\创建云机.py`。最新规则：创建→发送代理及中转请求→国家/时区/语言写入→安装TK→恢复原状态。代理与地区只发送请求，不回查旧代理、设置结果、Android属性或GPS，不重启、不测试联网；响应不明确记录后继续。GPS有成对坐标才写，缺省跳过且不联网定位。ROM就绪后即使代理或地区异常也继续本地安装，安装只核对包名。状态回查单次最多120秒、不限轮数，约每2秒查询；安装队列按120秒窗口继续排队。主机以业务码201明确拒绝安装时，先回查当前目标包名最多120秒，未出现才重试；上传响应不明确不重发。已有云机加 `--configure-only`，单台加 `--name`、多台加`--names`。身份、端口、配置和APK检查保留；单台失败继续后续目标。

只对清单中的某台已有云机继续初始化，例如145.2：

```powershell
python -X utf8 .\创建云机.py --configure-only --name 145.2
```

此处 `--configure-only` 表示操作已有云机，`--name` 指定一台、`--names` 指定一台或多台。只创建使用 `python -X utf8 .\仅创建云机.py --names 145.2 146.1`；原 `创建云机.py --create-only` 保留兼容。名称须存在于清单，且已有云机名称和ID均唯一。初始化中语言等地区设置返回业务0或超时后仅记录响应，不重发、不回查，随后继续安装。

四项独立操作已封装到根目录：`仅创建云机.py`、`设置代理.py`、`设置地区.py`、`安装TK.py`，均支持 `--names` 和 `--name`。完整命令、配置依赖和执行范围见 [独立操作指令](../独立操作指令.md)。`python -X utf8 .\创建云机.py --workers 3` 仍表示批量创建并完整初始化，创建后最多同时配置3台；创建阶段仍逐台提交。

初始化不发送重启请求。`batch_status.json` 的 `restore_state` 保留停机回查结果；恢复失败追加到原始错误。

初始化支持 `--workers N` 指定同时处理的云机数，N为正整数，默认1。例如已有批次同时处理3台：

```powershell
python -X utf8 .\创建云机.py --configure-only --workers 3
```

每台内部按代理→地区写入→TK安装→恢复状态执行，不同云机同时进行，创建阶段逐台提交。已有TK跳过上传。报告用`proxy_attempted/system_attempted`表示请求流程结束，`proxy_configured/system_configured=false`保留未核验事实，不参与初始化成功条件；`tiktok_installed`仍须包名证据。代理和地区详情保留每项响应与错误，映射写`proxy_settings_submission/system_settings_submission`，不伪造已核验标记。失败名称写到`failed_names.txt`，候补仍仅在首轮收尾后重试失败台一轮。

已有云机完整初始化增加候补开关`--retry-failed`，默认关闭：

```powershell
python -X utf8 .\创建云机.py --configure-only --workers 3 --retry-failed
```

首轮全部任务及状态恢复结束后，汇总失败台并按原并发上限自动重试一轮，成功台不重复执行；仍失败不再循环。只支持`--configure-only`，遵守`--name/--names`所选范围。候补身份及稳定状态核对最多120秒、不限轮次；身份不明确或ID变化不操作。首轮运行/停机目标跨轮保留。首轮报告与失败名单为`first_pass_status.json`、`first_failed_names.txt`，候补详情写入`retry-1/实例ID/`。最终`batch_status.json`合并每台最后结果，`failed_names.txt`保存最终失败台。全部完成退出0，剩余失败返回1。

初始化的ROM、包名、身份和状态回查单次最多120秒、不限轮次，固定约2秒查询；成功即继续，嵌套共用截止时间。超时记录并继续后续云机。不等待Android属性或代理服务核验。安装本地队列每120秒记录进度后继续等待；业务码201是主机明确拒绝，先回查当前目标包名最多120秒，仍未出现才重试；上传结果不明确不重发。全局配置、主机连接、目标身份及独立端口检查保留。独立严格操作沿用原等待规则。

实机已确认主机安装接口会拒绝重叠任务，故TK上传至安装包名确认排队串行处理，代理和地区步骤仍按workers并发。初始化遇到HTTP200/业务码201及“当前有安装任务进行中”时，主机明确表示请求未受理；释放本地安装槽位，先用最多120秒回查当前目标包名，仍未出现才重试该请求。上传响应超时等不明确情况不重发，只在最多120秒的包名回查窗口内确认；仍未出现则记录待确认并继续后续云机。

标准代理和GPS回查使用每台云机独立的控制端口。bridge模式下主机记录的容器IP可能过期，端口异常会停止当前任务。根入口只确认实际配置；底层阶段3的显式网络诊断及阶段6的登录前检查保留。

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
