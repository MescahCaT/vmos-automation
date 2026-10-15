# 阶段11：部署与运维

## 📋 任务目标

将系统部署到生产环境并建立运维体系。

## ✅ 任务清单

### 11.1 生产环境部署
- [ ] 部署脚本到服务器
- [ ] 配置生产环境变量
- [ ] 配置定时任务（cron/systemd）
- [ ] 配置自动重启机制
- [ ] 配置日志轮转

### 11.2 监控与告警
- [ ] 云机在线率监控
- [ ] 代理可用性监控
- [ ] TikTok 账号状态监控
- [ ] 异常情况邮件/短信告警
- [ ] 监控面板部署

### 11.3 备份与恢复
- [ ] 云机配置定期备份
- [ ] 账号数据定期备份
- [ ] 数据库备份策略
- [ ] 灾难恢复预案

### 11.4 日常运维
- [ ] 日志查看与分析
- [ ] 异常设备处理
- [ ] 代理 IP 更换
- [ ] 账号维护
- [ ] 性能调优

## 📁 核心文件

- `deploy.sh` - 自动部署脚本
- `start_service.sh` - 启动服务脚本
- `stop_service.sh` - 停止服务脚本
- `backup.sh` - 备份脚本
- `monitor_dashboard.py` - 监控面板
- `health_check.py` - 健康检查脚本

## 🚀 部署脚本

### deploy.sh
```bash
#!/bin/bash

echo "开始部署 VMOS 自动化系统..."

# 1. 检查环境
echo "检查 Python 环境..."
python3 --version || { echo "Python3 未安装"; exit 1; }

# 2. 创建虚拟环境
echo "创建虚拟环境..."
python3 -m venv venv
source venv/bin/activate

# 3. 安装依赖
echo "安装依赖包..."
pip install -r requirements.txt

# 4. 创建必要目录
echo "创建目录结构..."
mkdir -p logs data config

# 5. 复制配置文件
echo "配置文件..."
if [ ! -f config/config.yaml ]; then
    cp config/config.yaml.template config/config.yaml
    echo "请编辑 config/config.yaml 配置文件"
fi

# 6. 设置定时任务
echo "配置定时任务..."
cat > /etc/cron.d/vmos_automation << EOF
# 每小时采集一次数据
0 * * * * /path/to/venv/bin/python /path/to/data_collector.py >> /path/to/logs/cron.log 2>&1

# 每5分钟监控一次
*/5 * * * * /path/to/venv/bin/python /path/to/monitor.py >> /path/to/logs/monitor.log 2>&1
EOF

echo "部署完成！"
echo "请运行: ./start_service.sh 启动服务"
```

### start_service.sh
```bash
#!/bin/bash

echo "启动 VMOS 自动化服务..."

# 激活虚拟环境
source venv/bin/activate

# 启动主服务
nohup python main.py > logs/service.log 2>&1 &
echo $! > service.pid

echo "服务已启动，PID: $(cat service.pid)"
```

### stop_service.sh
```bash
#!/bin/bash

echo "停止 VMOS 自动化服务..."

if [ -f service.pid ]; then
    kill $(cat service.pid)
    rm service.pid
    echo "服务已停止"
else
    echo "服务未运行"
fi
```

## 📊 监控面板

### 使用 Flask 搭建简易监控面板
```python
from flask import Flask, render_template, jsonify
import json

app = Flask(__name__)

@app.route('/')
def dashboard():
    """监控面板主页"""
    return render_template('dashboard.html')

@app.route('/api/devices')
def get_devices():
    """获取设备状态"""
    with open('data/device_status.json', 'r') as f:
        data = json.load(f)
    return jsonify(data)

@app.route('/api/proxies')
def get_proxies():
    """获取代理状态"""
    with open('data/proxy_status.json', 'r') as f:
        data = json.load(f)
    return jsonify(data)

@app.route('/api/accounts')
def get_accounts():
    """获取账号状态"""
    with open('data/account_status.json', 'r') as f:
        data = json.load(f)
    return jsonify(data)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
```

## 🔔 告警配置

### 告警规则配置
```yaml
alerts:
  device_offline:
    enabled: true
    threshold: 5  # 分钟
    channels: [email, wechat]
  
  proxy_failed:
    enabled: true
    threshold: 3  # 次数
    channels: [email, wechat]
  
  login_failed:
    enabled: true
    threshold: 5  # 次数
    channels: [email]
  
  cpu_high:
    enabled: true
    threshold: 90  # 百分比
    channels: [email]

notification:
  email:
    smtp_server: smtp.example.com
    smtp_port: 587
    username: alert@example.com
    password: your_password
    to: admin@example.com
  
  wechat:
    webhook_url: https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx
```

## 💾 备份策略

### backup.sh
```bash
#!/bin/bash

BACKUP_DIR="/backup/vmos_automation"
DATE=$(date +%Y%m%d_%H%M%S)

echo "开始备份..."

# 创建备份目录
mkdir -p $BACKUP_DIR

# 备份配置文件
tar -czf $BACKUP_DIR/config_$DATE.tar.gz config/

# 备份数据文件
tar -czf $BACKUP_DIR/data_$DATE.tar.gz data/

# 备份日志（最近7天）
find logs/ -name "*.log" -mtime -7 | tar -czf $BACKUP_DIR/logs_$DATE.tar.gz -T -

# 删除 30 天前的备份
find $BACKUP_DIR -name "*.tar.gz" -mtime +30 -delete

echo "备份完成：$BACKUP_DIR"
```

### 定时备份（每天凌晨2点）
```bash
# 添加到 crontab
0 2 * * * /path/to/backup.sh >> /path/to/logs/backup.log 2>&1
```

## 🏥 健康检查

### health_check.py
```python
import requests
import time

def check_vmos_host():
    """检查 VMOS 主机"""
    try:
        response = requests.get('http://192.168.100.24:18182/v1/heartbeat', timeout=10)
        return response.status_code == 200
    except:
        return False

def check_devices():
    """检查云机状态"""
    try:
        response = requests.post('http://192.168.100.24:18182/container_api/v1/get_db')
        data = response.json()
        
        total = len(data.get('data', []))
        running = sum(1 for d in data.get('data', []) if d.get('state') == 'running')
        
        return {
            'total': total,
            'running': running,
            'online_rate': running / total * 100 if total > 0 else 0
        }
    except:
        return {'total': 0, 'running': 0, 'online_rate': 0}

def main():
    print("=" * 50)
    print("VMOS 自动化系统健康检查")
    print("=" * 50)
    
    # 检查 VMOS 主机
    print("\n1. 检查 VMOS 主机...")
    if check_vmos_host():
        print("   ✅ VMOS 主机正常")
    else:
        print("   ❌ VMOS 主机异常")
    
    # 检查云机状态
    print("\n2. 检查云机状态...")
    device_status = check_devices()
    print(f"   总数: {device_status['total']}")
    print(f"   运行中: {device_status['running']}")
    print(f"   在线率: {device_status['online_rate']:.2f}%")
    
    if device_status['online_rate'] >= 95:
        print("   ✅ 云机状态正常")
    elif device_status['online_rate'] >= 80:
        print("   ⚠️  云机状态一般")
    else:
        print("   ❌ 云机状态异常")

if __name__ == '__main__':
    main()
```

## 📖 运维手册

### 常见问题处理

#### 问题1：云机大量离线
```bash
# 1. 检查主机状态
curl http://192.168.100.24:18182/v1/heartbeat

# 2. 查看系统资源
curl http://192.168.100.24:18182/v1/systeminfo

# 3. 重启离线云机
python scripts/restart_offline_devices.py

# 4. 查看日志
tail -f logs/automation.log
```

#### 问题2：代理大量失效
```bash
# 1. 检查代理列表
python scripts/check_proxy_status.py

# 2. 更换失效代理
python scripts/replace_failed_proxies.py

# 3. 验证代理状态
python scripts/verify_all_proxies.py
```

#### 问题3：账号登录失败
```bash
# 1. 查看登录日志
grep "login_failed" logs/automation.log

# 2. 重试失败账号
python scripts/retry_failed_logins.py

# 3. 手动介入人机验证
python scripts/manual_verification.py
```

## 🔐 安全建议

1. **网络安全**
   - VMOS API 仅在可信局域网使用
   - 不要暴露 18182 端口到公网
   - 使用 VPN 或跳板机访问

2. **访问控制**
   - 配置防火墙规则
   - 限制 IP 白名单
   - 使用强密码

3. **数据安全**
   - 定期备份重要数据
   - 加密敏感配置信息
   - 日志脱敏处理

## 📊 输出产物

- `deploy.sh` - 部署脚本
- `start_service.sh` - 启动脚本
- `stop_service.sh` - 停止脚本
- `backup.sh` - 备份脚本
- `health_check.py` - 健康检查脚本
- `运维手册.md` - 运维文档
