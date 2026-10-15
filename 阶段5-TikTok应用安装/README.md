# 阶段5：TikTok应用安装

统一入口为 `install_tiktok.py`。它读取阶段1的 `配置文件/config.json`，使用当前 VMOS 地址和本地 TikTok APK。执行时必须提供云机 ID 或设备文件。

根目录 `创建云机.py` 已将本模块纳入初始化：创建、代理及地区配置后自动安装，已有云机使用 `python -X utf8 .\创建云机.py --configure-only`。沿用主配置的APK路径与包名，创建前通过aapt校验包名及版本元数据；已安装跳过上传。地区配置成功后即使网页测试超时也执行当前云机的本地安装，联网与安装分别记账，整体失败仍停止后续云机。安装报告保存到该批任务的设备ID子目录 `install_status.json`；整体最终状态以任务 `batch_status.json` 为准。初始化入口在安装结束后统一恢复原运行/停机状态。

2026-10-12 实测：19:31:47 在 `codex-test-20261012-01`（`EDGE5YDRZ1Q7D6TW`）安装本地 TikTok `43.7.2` 成功；VMOS 单台响应 `code="200"`、`msg="安装成功"`，应用列表回查包名通过。19:31:57 已恢复停机，结果为 1/1 确认、0 失败、0 待确认。仅此测试实例经过本轮实测，其他存量设备未安装。

## 单台安装

从项目根目录执行：

```powershell
python .\阶段5-TikTok应用安装\install_tiktok.py --db-ids EDGE5YDRZ1Q7D6TW --start-stopped --restore-state
```

上述命令启动停机目标、等待 ROM 就绪、查询已有应用、上传本地 APK、回查包名，并在结束后恢复本脚本启动实例的停机状态。原来运行的目标会继续运行。默认不启动 TikTok、不登录、不授予 Root 或悬浮窗权限。

## 指定设备批次

```powershell
# 读取阶段2实际创建成功的设备清单，默认逐台处理
python .\阶段5-TikTok应用安装\install_tiktok.py --devices-file .\阶段2-云机批量创建与初始化\devices_created.json --start-stopped --restore-state

# 对两个已运行的目标安装
python .\阶段5-TikTok应用安装\install_tiktok.py --db-ids EDGE001,EDGE002 --batch-size 2

# 仅回查已运行目标；不启动、上传、授权或停止
python .\阶段5-TikTok应用安装\install_tiktok.py --db-ids EDGE5YDRZ1Q7D6TW --verify-only
```

设备文件支持阶段2设备对象数组，或 `{"db_ids": ["EDGE..."]}`。批次大小允许 1 至 12，默认 1，避免把 219 台存量实例全部启动。停机目标必须指定 `--start-stopped` 或先在客户端启动；只读回查停机实例会返回待确认。

## 请求、验证与结果

- 通过 `code=200` 和 HTTP 状态判断请求；请求接收成功不直接判为安装成功。
- ROM 就绪后才上传。应用列表兼容实际 `data.app` 包名数组和旧 `data.apps` 格式。
- 发现 TikTok 已存在时记录 `already_installed` 并跳过上传；这里只核对包名，不执行已有应用版本升级。
- 安装后默认最多轮询 300 秒，支持 `--wait-time` 调整；以查询到包名确认成功。
- 上传使用 Windows 自带 `curl.exe` 流式读取 APK，避免一次在 Python 内存中装入约 457 MB 文件。
- 上传超时或响应异常时回查目标，不自动重发；期限内不能确认时记录 `unconfirmed`。
- 权限功能可通过 `--overlay` 单独开启；安装与登录测试不要求悬浮窗和 Root 权限。权限失败记录在结果中，不冒充安装失败。

结果写到项目根目录 `日志/`，与当前工作目录无关：

| 文件 | 内容 |
| --- | --- |
| `install_status.json` | 每台安装状态、原运行状态、接口原始响应、回查及恢复结果 |
| `install_log.txt` | 安装过程 |
| `failed_devices.txt` | 明确失败设备 |
| `pending_devices.txt` | 待确认设备 |
| `install_verify.json` | `--verify-only`的查询结果，不覆盖安装报告 |

退出码 `0` 表示全部确认安装且请求的状态恢复成功，`1` 表示有明确失败或恢复失败，`2` 表示安装待确认。安装成功后仍需要阶段3配置代理和阶段6登录。

## 维护与验证

`apk_uploader.py` 负责上传，`install_verifier.py` 负责包名回查，`permission_manager.py` 使用正确的 `app` 字段授予可选权限。旧 `auto_batch_install.py`、`install_on_running.py` 等脚本属于旧流程，仍含旧地址和不同解析，后续使用统一入口，不使用旧脚本执行真实批次。

```powershell
python -m unittest discover -s .\阶段5-TikTok应用安装\tests -q
```

离线检查覆盖真实应用列表、HTTP/业务状态、请求超时后成功回查、已安装跳过、明确目标、停机启动恢复及待确认状态。URL安装和多包格式有VMOS接口支持；本轮实际验收范围为本地单APK，URL/多包尚未实测。
