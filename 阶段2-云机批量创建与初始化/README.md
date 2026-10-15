# 阶段2：云机批量创建与初始化

## 📋 任务目标

批量创建 100+ 云机实例并完成基础配置。

## ✅ 任务清单

### 2.1 批量创建云机
- [ ] 确定创建数量（100+ 个）
- [ ] 选择镜像版本
- [ ] 选择机型模板
- [ ] 配置分辨率参数
- [ ] 配置网络模式
- [ ] 批量调用创建接口

### 2.2 云机编号与备注
- [ ] 自动生成 user_name
- [ ] 设置 remark 字段
- [ ] 保存 db_id 映射表

### 2.3 自定义端口分配
- [ ] 分配视频端口（25000-26999）
- [ ] 分配音频端口（27000-28999）
- [ ] 分配控制端口（29000-30999）
- [ ] 分配 ADB 端口（31000-32999）

### 2.4 批量启动云机
- [ ] 批量启动接口调用
- [ ] 轮询检查运行状态
- [ ] 轮询检查 ROM 就绪

## 📁 核心文件

- `create_devices.py` - 批量创建云机主脚本
- `device_config.py` - 设备配置管理
- `port_allocator.py` - 端口分配器
- `status_checker.py` - 状态检查器

## 🔧 API 接口

- `POST /container_api/v1/create` - 创建实例
- `POST /container_api/v1/run` - 启动实例
- `POST /container_api/v1/get_db` - 查询状态
- `GET /container_api/v1/rom_status/{db_id}` - ROM状态

## 📊 输出产物

- `devices_created.json` - 已创建设备列表
- `port_mapping.json` - 端口映射表
- `creation_log.txt` - 创建日志
- `creation_results.json` - 每台设备的原始创建响应、确认来源及异常记录

## 创建结果判定与实机测试

创建响应支持数字或字符串 `code=200`，以及 `data.db_ids`、`data.db_id`、ID数组等返回结构。响应缺少ID、请求超时或返回错误时，脚本先查询 `list_names`，不可用时自动切换到 `get_db`，只确认本次新出现的同名实例，不自动重发创建请求。已存在的同名设备不会被计作本次创建成功。

回查每3秒一次，最多20次；仍无法确定时显示“待确认”，原始响应保存在 `creation_results.json`。创建结果在启动前保存，启动或ROM检查未通过不会改写已确认的创建结果。

当前主机地址为 `192.168.110.35:18182`，单台测试配置 `test_create_single.json` 使用已确认存在的 Android 15 镜像和现有S21分辨率。重复测试时修改 `user_name` 为新的名称。

建立连接超时会显示“连接失败”，停止后续批量创建；请求已建立连接但读取响应超时仍会回查实例，无法核实时保留“待确认”。

只创建一台设备并验证创建结果：

```powershell
Set-Location 'D:\000aaa_vmos自动化\阶段2-云机批量创建与初始化'
python .\create_devices.py .\test_create_single.json --create-only
```

查看 `devices_created.json` 的 `db_id` 和 `creation_source` 是否与客户端一致。如仍显示“待确认”，查看同目录的 `creation_results.json` 和 `creation_log.txt`。

原批量流程继续使用 `python .\create_devices.py .\你的配置文件.json`，不加 `--create-only` 会继续启动和ROM检查。

离线回归测试（所有网络请求均模拟）：

```powershell
python -m unittest discover -s .\tests -v
```

【阶段2-云机创建/create_devices.py: 兼容创建响应并回查新实例，分开创建与启动结果；修正客户端成功但脚本误报的问题】
