# 阶段6：TikTok单账号登录与身份核验

2026-10-14 已将“邮箱验证码”和“密码＋额外邮箱身份验证”整合到 `auto_login.py`。当前目标为 `codex-test-20261012-01`（`EDGE5YDRZ1Q7D6TW`），指定账号 `account001@example.com / @example_user_001`。

13:50已有密码＋邮箱验证的实际新登录成功记录：`account_attempts/password_login_20261014/password_login_status.json`。本轮保留已有账号，验证正式入口、重启后的登录保持及只读身份复核，不将这些复核宣称为再次从退出状态完整登录。纯邮箱方式此前等待150秒未取得不同于基线的新码，仍有取码时效的验收缺口。

## 使用

从项目根目录执行。已登录指定账号会直接核验本人主页并保留账号：

```powershell
python -X utf8 .\阶段6-TikTok自动登录\auto_login.py --db-id EDGE5YDRZ1Q7D6TW --email account001@example.com --login-mode password --keep-running
$LASTEXITCODE
Get-Content -Encoding UTF8 .\阶段6-TikTok自动登录\login_status.json
```

新登录需要密码时，从环境变量读取，不把密码写入命令参数或账号配置。PowerShell可使用交互式隐藏输入：

```powershell
$tkCredential = Get-Credential -UserName 'account001@example.com' -Message '输入TikTok密码'
$env:VMOS_TIKTOK_PASSWORD = $tkCredential.GetNetworkCredential().Password
try {
    python -X utf8 .\阶段6-TikTok自动登录\auto_login.py --db-id EDGE5YDRZ1Q7D6TW --email account001@example.com --login-mode password --password-env VMOS_TIKTOK_PASSWORD --keep-running
} finally {
    Remove-Item Env:\VMOS_TIKTOK_PASSWORD -ErrorAction SilentlyContinue
}
```

当前已登录实例无需执行这一段，也不应为了测试主动退出账号。

`--login-mode email_code` 选择纯邮箱验证码，默认使用账号配置。`--code-wait 120` 控制新码等待，范围1至600秒。取码通过主配置的HTTP GET接口，参数为 `username=邮箱`，不需要IMAP或邮箱密码。

默认仅接受与发送前基线不同的六位数。接口持续返回同值时停止；`--allow-current-code` 是显式兼容选项，等待结束后单次尝试接口当前值，报告会记录其新旧是否可确认。该选项不能证明验证码新鲜，也不会反复重发。

停机目标需 `--start-stopped`，默认结束恢复停机；`--keep-running` 保留脚本启动的实例。原本运行的实例保持运行。

## 流程与边界

确认设备、ROM和安装 → 本机代理与云机出口/连续网页验证 → 配置了region时只读核验国家/时区/语言/GPS → 打开TikTok并按当前节点导航 → 输入邮箱 → 指定登录方式 → 本人主页与预期用户名核对 → 写目标账号绑定。

密码方式支持新版验证码页的“Need help logging in? → Use password instead”，一次密码提交，以及与指定邮箱唯一匹配的额外身份验证。密码框允许星号/圆点掩码回查。验证码输入前重新确认仍在验证码页，避免等待期间页面变化造成误输入。当前APK已确认的Family Pairing和Avatar介绍弹窗各可返回关闭一次，其他未知页面停止。

网络、地区、错密码、验证码拒绝、限频、人机验证、未知账号和身份不符都不能记为成功。人机验证交给客户端人工处理。不退出已有账号，不清应用数据，不自动轮换账号或反复提交。UI节点暂时无效XML只做有限只读重试。密码、验证码和取码响应不写日志、报告或敏感阶段截图。

## 如何确认完成

需要同时满足：

1. 本人主页有资料编辑入口及 `@用户名`，且与指定账号匹配。
2. `login_status.json` 为 `status=logged_in`、`success=true`，设置模式有 `mapping_updated=true`。
3. 设备映射包含该邮箱、用户名及 `login_verified_at`。
4. 如脚本负责恢复停机，恢复结果成功。

当前停留本人主页、已有绑定的实例可独立只读复核：

```powershell
python -X utf8 .\阶段6-TikTok自动登录\auto_login.py --db-id EDGE5YDRZ1Q7D6TW --email account001@example.com --verify-only
```

此命令不导航、不启动、不发码、不更新映射，仅确认当前页面与已有绑定；不重新证明联网或取码。结果另存 `login_verify.json`。退出0表示成功，2表示待处理/未确认，1表示执行错误或恢复失败。

## 报告与验证范围

`login_status.json/login_log.txt` 为最近正式运行；`login_verify.json` 为只读复核；`screenshots/logged_in_profile.png` 为非敏感身份截图。本轮归档见 `阶段12-文档与交付/closure_346_20261014/`，历史网络诊断不代表当前状态。

密码与身份验证路径已整合并有离线覆盖；完整新登录实机证据来自本轮整合前的单台定向操作。批量登录、新鲜验证码全链路和跨进程验证码续跑尚未验收。

```powershell
python -X utf8 -m unittest discover -s .\阶段6-TikTok自动登录\tests -v
```
