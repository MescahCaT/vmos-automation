# 阶段8：账号信息采集与监控

## 📋 任务目标

定时采集账号数据并监控云机、代理、账号状态。

## ✅ 任务清单

### 8.1 账号信息采集（已有脚本）
- [ ] 记录账号添加时间
- [ ] 记录每日粉丝数量
- [ ] 记录是否开通橱窗
- [ ] 保存到 Excel/CSV 表格

### 8.2 云机状态监控
- [ ] 定时查询实例状态
- [ ] 监控运行状态（running/stopped）
- [ ] 监控 ROM 就绪状态
- [ ] 记录异常设备

### 8.3 代理 IP 监控
- [ ] 定时查询代理 IP
- [ ] 检测 IP 是否变更
- [ ] 检测地理位置是否正常
- [ ] 记录 IP 异常设备

### 8.4 告警通知
- [ ] 云机离线告警
- [ ] 代理失效告警
- [ ] 账号异常告警
- [ ] 邮件/短信通知

## 📁 核心文件

- `data_collector.py` - 数据采集主脚本
- `device_monitor.py` - 云机监控
- `proxy_monitor.py` - 代理监控
- `account_monitor.py` - 账号监控
- `alert_manager.py` - 告警管理器

## 🔧 API 接口

### 状态查询
- `POST /container_api/v1/get_db` - 获取实例状态
- `GET /container_api/v1/list_names` - 获取实例列表
- `GET /container_api/v1/rom_status/{db_id}` - ROM状态
- `GET /android_api/v1/proxy_get/{db_id}` - 查询代理状态

### 系统信息
- `GET /v1/systeminfo` - 主机系统信息
- `GET /v1/heartbeat` - 主机心跳检查

## 📊 数据采集示例

### 账号信息表格结构
```csv
账号编号,邮箱,添加时间,粉丝数,橱窗状态,最后登录,备注
TK-001,account001@example.com,2025-01-01,1250,已开通,2025-01-15,正常
TK-002,account002@example.com,2025-01-01,890,未开通,2025-01-14,待审核
```

### 云机状态表格
```csv
设备编号,db_id,状态,代理IP,地理位置,最后检查时间,备注
TK-001,EDGE001,running,1.2.3.4,美国纽约,2025-01-15 10:30:00,正常
TK-002,EDGE002,stopped,1.2.3.5,美国洛杉矶,2025-01-15 10:30:00,异常停止
```

## 📈 监控指标

### 云机监控
- 在线率
- CPU 使用率
- 内存使用率
- 磁盘使用率
- 运行时长

### 代理监控
- IP 可用性
- 连接速度
- IP 变更频率
- 地理位置准确性

### 账号监控
- 登录状态
- 粉丝增长趋势
- 发布内容数量
- 互动数据（点赞、评论）

## ⏰ 定时任务配置

```python
# 使用 schedule 库配置定时任务
import schedule
import time

def collect_data():
    """采集数据"""
    print("开始采集数据...")
    # 采集逻辑

def monitor_devices():
    """监控云机"""
    print("开始监控云机...")
    # 监控逻辑

# 每小时采集一次数据
schedule.every(1).hours.do(collect_data)

# 每5分钟监控一次云机状态
schedule.every(5).minutes.do(monitor_devices)

while True:
    schedule.run_pending()
    time.sleep(1)
```

## 🔔 告警规则

### 云机告警
- 云机离线超过 5 分钟 → 发送告警
- ROM 未就绪超过 10 分钟 → 发送告警
- CPU 使用率超过 90% → 发送告警

### 代理告警
- 代理无法连接 → 立即告警
- IP 地理位置异常 → 发送告警
- IP 频繁变更 → 发送告警

### 账号告警
- 账号登录失败 → 发送告警
- 粉丝数异常下降 → 发送告警
- 发布失败次数过多 → 发送告警

## 📧 通知方式

### 邮件通知
```python
import smtplib
from email.mime.text import MIMEText

def send_alert_email(subject, content):
    msg = MIMEText(content)
    msg['Subject'] = subject
    msg['From'] = 'alert@example.com'
    msg['To'] = 'admin@example.com'
    
    smtp = smtplib.SMTP('smtp.example.com')
    smtp.send_message(msg)
    smtp.quit()
```

### 企业微信通知
```python
import requests

def send_wechat_alert(message):
    webhook_url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..."
    
    data = {
        "msgtype": "text",
        "text": {
            "content": message
        }
    }
    
    requests.post(webhook_url, json=data)
```

## 📊 输出产物

- `account_data.xlsx` - 账号数据表格
- `device_status.json` - 云机状态记录
- `proxy_status.json` - 代理状态记录
- `monitor_log.txt` - 监控日志
- `alerts.json` - 告警记录
