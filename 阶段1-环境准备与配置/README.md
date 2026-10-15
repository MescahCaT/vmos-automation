# 阶段1：环境准备与配置

2026-10-12：单台测试所需的环境检查和资源登记已完成。当前检查目标是 **1 台云机**。

2026-10-13 更新：已登记3条 SOCKS5 出口和 `proxy-004` 中转资源。中转为 VLESS TCP、无 TLS，最终出口复用第三条 SOCKS5；本机探测使用项目内 `tools/mihomo/mihomo.exe`，临时进程只监听回环地址，结束后自动退出并删除认证配置。阶段1现有9项离线测试通过。此前 `proxy-001` 的出口验收为历史结果，当前云机绑定与联网状态以阶段3/6最新报告为准。

## 已验证

- [x] VMOS `192.168.110.35:18182` 可访问，版本 `1.1.9.10.1`，健康检查通过。
- [x] 硬件、5 个镜像、96 个机型模板和 219 台实例信息已采集。
- [x] 所选镜像 `vcloud_android15_edge_20260731174356` 已安装。
- [x] `adiID=1039` 实际对应 OnePlus 12 / Android 15；旧配置中的 Samsung S21 描述不准确。
- [x] TikTok APK 元数据确认：`com.zhiliaoapp.musically` / `43.7.2`。
- [x] `proxy-001` 的 SOCKS5 认证和 HTTPS 出网验证通过，出口 IP `198.65.101.179`。
- [x] 用户指定测试账号 `account001@example.com` 已登记，网页取码接口返回六位验证码。
- [x] 现有云机命名和实例映射已登记。

## 运行检查

从项目根目录执行：

```powershell
python .\阶段1-环境准备与配置\check_vmos_connection.py
```

也可以切换到阶段1目录后执行 `python .\check_vmos_connection.py`。脚本根据自身位置定位配置和输出，资源路径相对项目根目录。

```powershell
# 只检查VMOS，不访问代理或邮箱取码接口
python .\check_vmos_connection.py --vmos-only

# 离线回归测试（在阶段1目录执行）
python -m unittest discover -s .\tests -q
```

脚本只做读取和探测，不创建、启动或修改云机。它同时校验 HTTP 与 VMOS 业务状态码，兼容 `data` 数组和 `data.list`。代理请求失败最多重试两次，在报告中记录 `attempts`；本次复查曾遇到一次超时，随后重试出网成功。退出码 `0` 表示当前检查范围就绪，`1` 表示有失败，`2` 表示资源或验证码待确认。

## 配置和输出

| 文件 | 用途 |
| --- | --- |
| `配置文件/config.json` | 主机、镜像/机型、APK路径、取码端点和当前目标数量 |
| `配置文件/proxy_list.json` | 实际代理，含本地认证信息 |
| `配置文件/account_list.json` | 当前测试账号，登录模式 `email_code` |
| `数据/vmos_info.json` | 主机信息与真实列表，机型模板不记录 `adiPass` |
| `数据/device_mapping.json` | 以 `db_id` 为键保存云机、端口、状态和绑定 |
| `数据/environment_check.json` | 最近一次检查报告；不含代理密码和验证码 |

JSON 配置通过 Python 标准库读取，不需要安装 PyYAML 或 PySocks；代理检查使用本机 `curl.exe`，APK 元数据检查使用 VMOS 自带的 `aapt.exe`。YAML 文件保留为旧模板参考，运行时使用上述 JSON。

设备查询失败不会覆盖既有映射。成功回查保留已有代理/账号绑定、标记消失实例为 `present=false`，不会自动给 219 台云机重复分配唯一一条代理。

## 下一阶段与全量资源

阶段1已经满足单台测试前置条件；当前真实资源为 1 条代理和 1 个测试账号。100+ 台目标需要补齐对应资源后再将 `device.count` 调整到实际数量。阶段3需要完成云机内部代理配置和出网验证，阶段6需要完成登录界面操作及新验证码验证。读取到验证码不等于已完成 TikTok 登录，也不保证该验证码仍在有效期内。
