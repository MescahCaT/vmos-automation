# 阶段10：测试与优化

## 📋 任务目标

全面测试系统功能并进行性能优化。

## ✅ 任务清单

### 10.1 单机测试
- [ ] 测试单个云机创建流程
- [ ] 测试代理设置
- [ ] 测试 TikTok 安装
- [ ] 测试自动登录流程
- [ ] 测试素材上传
- [ ] 测试内容发布

### 10.2 小批量测试（10台）
- [ ] 测试批量创建稳定性
- [ ] 测试并发 API 调用
- [ ] 测试资源占用情况
- [ ] 测试错误处理机制
- [ ] 测试日志记录

### 10.3 大批量测试（100+台）
- [ ] 测试系统性能极限
- [ ] 测试网络带宽占用
- [ ] 测试主机资源瓶颈
- [ ] 优化批量操作速度
- [ ] 压力测试

### 10.4 风控测试
- [ ] 测试 TikTok 账号存活率
- [ ] 测试设备指纹识别
- [ ] 测试代理 IP 检测
- [ ] 测试人机验证触发频率
- [ ] 测试批量操作风险

## 📁 核心文件

- `test_single_device.py` - 单机测试脚本
- `test_batch_10.py` - 小批量测试脚本
- `test_batch_100.py` - 大批量测试脚本
- `stress_test.py` - 压力测试脚本
- `performance_monitor.py` - 性能监控脚本

## 🧪 测试用例

### 单机测试用例
```python
def test_create_device():
    """测试创建单个云机"""
    client = VMOSClient("192.168.100.24")
    result = client.create_device({
        "user_name": "TEST-001",
        "bool_start": True
    })
    assert result['code'] == 200
    assert 'db_id' in result['data']

def test_set_proxy():
    """测试设置代理"""
    client = VMOSClient("192.168.100.24")
    result = client.set_proxy(
        db_id="EDGE001",
        proxy_config={"proxyUrl": "socks5://test:pass@1.2.3.4:1080"}
    )
    assert result['code'] == 200

def test_install_tiktok():
    """测试安装 TikTok"""
    client = VMOSClient("192.168.100.24")
    result = client.install_apk(
        db_ids=["EDGE001"],
        apk_path="/path/to/tiktok.apk"
    )
    assert result['code'] == 200
```

### 批量测试用例
```python
def test_batch_create_10():
    """测试批量创建 10 台云机"""
    client = VMOSClient("192.168.100.24")
    device_mgr = DeviceManager(client)
    
    start_time = time.time()
    devices = device_mgr.batch_create(count=10, config={})
    end_time = time.time()
    
    assert len(devices) == 10
    print(f"创建 10 台云机耗时: {end_time - start_time:.2f}秒")
```

## 📊 性能指标

### 时间性能
- 单机创建时间：< 30 秒
- 单机启动时间：< 60 秒
- 批量创建 10 台：< 5 分钟
- 批量创建 100 台：< 30 分钟
- APK 安装时间：< 20 秒/台
- 登录流程时间：< 2 分钟/台

### 资源性能
- 单台云机内存占用：< 2GB
- 单台云机 CPU 占用：< 50%
- 100 台云机总内存：< 200GB
- 网络带宽占用：< 100Mbps

### 成功率指标
- 云机创建成功率：> 99%
- 代理配置成功率：> 95%
- TikTok 安装成功率：> 98%
- 自动登录成功率：> 80%（含人机验证）
- 内容发布成功率：> 90%

## 🔍 性能监控

### 系统资源监控
```python
import psutil
import time

def monitor_system_resources(duration=60):
    """监控系统资源"""
    print("开始监控系统资源...")
    
    for i in range(duration):
        cpu_percent = psutil.cpu_percent(interval=1)
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        net = psutil.net_io_counters()
        
        print(f"[{i+1}/{duration}]")
        print(f"  CPU: {cpu_percent}%")
        print(f"  内存: {memory.percent}% ({memory.used/1024/1024/1024:.2f}GB/{memory.total/1024/1024/1024:.2f}GB)")
        print(f"  磁盘: {disk.percent}%")
        print(f"  网络: ↑{net.bytes_sent/1024/1024:.2f}MB ↓{net.bytes_recv/1024/1024:.2f}MB")
        
        time.sleep(1)
```

### API 性能测试
```python
import time

def benchmark_api(func, *args, iterations=10):
    """API 性能基准测试"""
    times = []
    
    for i in range(iterations):
        start = time.time()
        func(*args)
        end = time.time()
        times.append(end - start)
    
    avg_time = sum(times) / len(times)
    min_time = min(times)
    max_time = max(times)
    
    print(f"平均耗时: {avg_time:.2f}秒")
    print(f"最快: {min_time:.2f}秒")
    print(f"最慢: {max_time:.2f}秒")
```

## 🐛 问题记录与修复

### 问题跟踪表
```markdown
| 问题ID | 问题描述 | 严重程度 | 状态 | 修复方案 |
|--------|---------|---------|------|---------|
| BUG-001 | 云机创建失败率 5% | 高 | 已修复 | 增加重试机制 |
| BUG-002 | 代理设置超时 | 中 | 已修复 | 延长超时时间 |
| BUG-003 | 登录验证码识别失败 | 高 | 进行中 | 优化 OCR 算法 |
```

## ⚡ 性能优化

### 并发优化
```python
from concurrent.futures import ThreadPoolExecutor, as_completed

def batch_operation_concurrent(device_ids, operation, max_workers=10):
    """并发执行批量操作"""
    results = []
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(operation, device_id): device_id 
            for device_id in device_ids
        }
        
        for future in as_completed(futures):
            device_id = futures[future]
            try:
                result = future.result()
                results.append(result)
            except Exception as e:
                print(f"设备 {device_id} 操作失败: {e}")
    
    return results
```

### 缓存优化
```python
from functools import lru_cache
import time

@lru_cache(maxsize=100)
def get_device_info(db_id):
    """获取设备信息（带缓存）"""
    # API 调用
    pass

# 定时清理缓存
def clear_cache_periodically():
    while True:
        time.sleep(3600)  # 每小时清理一次
        get_device_info.cache_clear()
```

### 批量操作优化
```python
def batch_operation_optimized(device_ids, operation, batch_size=10):
    """分批执行操作"""
    results = []
    
    for i in range(0, len(device_ids), batch_size):
        batch = device_ids[i:i+batch_size]
        print(f"处理批次 {i//batch_size + 1}: {len(batch)} 台设备")
        
        batch_results = operation(batch)
        results.extend(batch_results)
        
        # 批次间隔，避免 API 限流
        time.sleep(2)
    
    return results
```

## 📊 测试报告模板

### 测试报告结构
```markdown
# VMOS 自动化系统测试报告

## 测试概述
- 测试日期: 2025-01-15
- 测试环境: VMOS Edge CBS 1.1.1.10
- 测试人员: XXX

## 测试结果

### 功能测试
- 云机创建: ✅ 通过
- 代理配置: ✅ 通过
- TikTok 安装: ✅ 通过
- 自动登录: ⚠️ 部分通过（80%成功率）
- 内容发布: ✅ 通过

### 性能测试
- 单机创建时间: 25秒 ✅
- 批量创建 100 台: 28分钟 ✅
- 内存占用: 180GB ✅
- CPU 占用: 平均 65% ✅

### 风控测试
- 账号存活率: 92% ✅
- 人机验证触发率: 15% ⚠️

## 问题列表
1. 登录验证码识别准确率需提升
2. 人机验证触发频率较高
3. 部分代理 IP 不稳定

## 优化建议
1. 增加验证码识别算法训练样本
2. 优化登录行为模拟，降低风控触发
3. 筛选高质量代理 IP

## 结论
系统基本功能正常，性能满足需求，建议优化后投入生产使用。
```

## 📊 输出产物

- `test_results/` - 测试结果目录
- `performance_report.md` - 性能测试报告
- `bug_list.md` - 问题列表
- `optimization_log.txt` - 优化日志
