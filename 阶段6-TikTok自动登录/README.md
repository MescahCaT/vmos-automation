# 阶段6：TikTok单账号登录与身份核验

## 一条指令登录（2026-10-22）

在项目根目录PowerShell执行，`--accounts-file`后填自己的账号文件完整路径并加双引号：

```powershell
python -X utf8 .\登录TK.py --accounts-file "D:\000aaa_vmos自动化\阶段9-脚本开发与集成\tests\测试账号.xls" --workers 3
```

支持`.xls`、`.xlsx`、`.json`。不指定名称时按文件内明确的云机对应关系处理全部账号；单台追加`--name 157.2`，多台追加`--names 157.1 157.2`；`--workers 3`表示最多3台并发，默认1。Excel读取首个工作表，列名为`云机名、tk账号、密码、邮箱`，兼容现有测试表首列空表头及第6列`辅助验证邮箱/验证地址`；邮箱密码列不使用。每行必须明确云机名；TK密码列用文本格式，保护前导零，不按机器顺序猜账号。JSON格式沿用账号示例。

根入口默认`--completion password-transition`，使用邮箱验证码登录，新码等待240秒；跳过代理状态、出口IP、TikTok/Google网页联网、国家/时区/语言及GPS预检，直接进入TK登录。验证码通过后完成额外密码验证，密码提交请求被VMOS界面接口接受即记为登录步骤成功并保留运行；若TikTok因自身问题回到登录入口，脚本最多按返回键2次。不会因登录页截图或资料页无法读取否定已完成的密码提交。新的断点记录提交确认；旧的`password_pending`记录只有在当前仍处于TikTok登录入口时才只读补确认，且允许超过10分钟继续只读收尾，不重发验证码或密码。`--completion profile`保留本人主页用户名核验；直接密码模式须同时指定`--login-mode password --completion profile`。

指定终点完成记录为`task_completed=true`，`completed_count`统计完成台数；默认密码提交终点成功后也会设置`success=true`并更新本地账号绑定，`--completion profile`仍要求本人主页用户名核验。全部达到所选终点且收尾正常时命令退出0；未完成名单为结果目录的`failed_names.txt`。同批续接加`--resume --output-dir "原结果目录"`，已完成台沿用证据、未完成台只续接断点；续跑完成标准须与原批次一致，旧批次如未记录completion，按profile处理。过期的邮箱/验证码断点仍拒绝自动重放；邮箱验证码通过后已提交密码的断点可只读收尾。

当前电脑已安装Excel读取依赖。迁移环境时执行`python -m pip install -r .\阶段6-TikTok自动登录\requirements.txt`。Excel中的TK密码只在内存中转为当前Windows用户DPAPI密文，不生成明文中间文件、不保存邮箱密码；自定义账号表请存放在仓库外或确认已忽略。`--config "配置JSON路径"`、`--bindings "云机账号绑定JSON路径"`及`--output-dir "结果目录"`也接受用户指定路径。新封装通过离线回归及真实XLS只读解析；本轮未启动或登录实机。

2026-10-20 19:25实机进展：按用户“邮箱验证码通过→额外密码验证→页面明确跳转”的终点已确认9/10，只有157.2提交密码后回生日页，返回一次并5轮回查仍未通过，保留现场，不填生日或重复登录。161.1与161.2本轮均已完成该终点；正式本人身份核验仍为1/10，与该终点分开统计。详细结果见本地`日志/stage6-20261020/acceptance_summary.json`及交接文档最新节，不能宣称整批验收完成。

## 2026-10-20 批量入口与受保护续跑

根目录 `登录TK.py` 按名称选择一台或多台，`--workers N` 默认1；单台失败记录、恢复状态并继续后续目标。账号通过JSON的 `device_name` 或 `--bindings` 的“云机名→邮箱”对象明确分配，拒绝重复邮箱、重复用户名和含糊目标。

```powershell
# 本地专用账号配置已从用户的账号表导入
python -X utf8 .\登录TK.py --accounts-file .\配置文件\stage6_test_accounts.json --names 157.1 157.2 --workers 2

# 单台，账号必须存在于所选配置
python -X utf8 .\登录TK.py --name 157.1 --email account@example.com --login-mode password --completion profile --password-env TK_PASSWORD_157_1

# 续接原批次，不重发账号或验证码
python -X utf8 .\登录TK.py --accounts-file .\配置文件\stage6_test_accounts.json --names 157.1 157.2 --resume --output-dir .\日志\login-原批次
```

账号示例为 `配置文件/account_list.example.json`。密码通常从 `password_env` 或CLI指定的环境变量读取；本次导入的 `password_protected` 是当前Windows用户DPAPI密文，解密后核对邮箱，不保存邮箱密码。原始账号表、实际配置、断点和日志均排除于Git。

登录前核对实时名称/ID、本地映射、账号占用和独立控制端口；已删除设备的历史绑定不阻止当前目标。缺少唯一代理绑定时不启动或登录。保留本地唯一代理绑定检查，不检测代理状态、出口IP或网页，不查询出口地理信息，不检查国家、时区、语言或GPS，也不修改这些设置。报告中的 `proxy/system` 标明 `skipped=true`、`verification_requested=false`，不记为已验证。已在TikTok前台时保留页面，不重复启动activity。生日页仅返回或打开账号选择，不填写生日或创建账号；优先已有账号登录入口。本人资料编辑入口及唯一用户名匹配才记成功，网络成功不能代替身份确认。

登录选择页同时存在Skip与Use phone / email / username时，优先选择登录方式；2026-10-20在159.2实测发现旧顺序误点Skip而回到生日页，已修正并增加回归覆盖。

同设备文件锁防止重叠登录。DPAPI断点同时存于 `数据/login-locks/login_session-实例ID.json` 和实例报告目录，换目录也不自动重发。断点绑定host、实例ID、邮箱、用户名，有效期10分钟。`--resume`续接验证码或主页确认，已输入验证码后只查身份；`--restart-login`明确允许新流程且须回到邮箱输入页。未知页面、限频、人机验证、身份不一致均停止该台。

生日页导航为底部Sign up → 注册页底部Log in/Sign in → Use phone / email / username → Email / Username。续跑也可恢复此入口，并记录 `login_entry_recovered=true`；原流程提交过登录时返回 `resume_restart_required`，不因此自动重发账号或密码。2026-10-20 16:18已在用户手动打开的160.1确认进入邮箱输入页，保留running。

2026-10-20 16:20～16:22对160.1的首次邮箱验收未取得不同于发送前基线的新验证码，未输入旧码、未点击重发；报告保留在 `日志/stage6-20261020/email-160_1/`。随后按用户授权清理该台TK数据做新登录，17:00:51输入新验证码一次，17:03:29提交额外身份验证密码一次，17:09:14本人主页用户名匹配并写入绑定；17:11:13独立只读复核再次通过。成功报告为 `email-160_1-cleared/`，复核为 `verify-160_1-after-avatar/`，均在同一天日志目录。本批已确认1/10，其余按各台报告处理。

用户校正后的接管终点为“邮箱验证码通过→随后要求的密码验证→提交密码后页面明确跳转”。2026-10-20曾用本地验收包装器执行；2026-10-21已接入根入口默认模式，只在该顺序和跳转均有证据时记录 `password_transition_confirmed`，保留运行并转下一台。空白加载或仍在密码页不能通过，不重复提交密码。旧 `credentials_submitted` 仅是提交历史，严格本人主页核验通过`--completion profile`选择。

输入优先set_text；仅404/405明确不支持时使用原生输入，响应不明确只回查，不追加输入。页面、验证码等确认环节仍沿用原次数和等待限制；登录前不执行代理出口、网页或地区/GPS预检。根入口默认按需启动并保留运行；profile模式默认恢复原停机状态，`--keep-running`可保留运行。`--verify-only`使用profile规则，不启动、不导航、不发码，要求当前本人主页及已有绑定。

新版实测密码提交后可能先出现“Agree and continue”协议弹窗；只在匹配的Dialog及协议说明下确认一次，再核验本人主页。续跑可处理该弹窗而不重新提交密码；其他未知弹窗仍记录待处理。

2026-11-05：取消登录流程中的地区/GPS与联网预检；历史GPS核验规则不再用于登录。独立代理诊断与初始化命令继续使用各自原有规则。

批次输出 `batch_status.json`、`failed_names.txt` 和每实例的 `login_status.json/login_log.txt`。完成续跑的 `resumed_skip=true` 表示沿用历史完成证据，不是新一轮实时核验。退出0表示全部达到所选终点，2表示待处理，1表示执行或收尾错误。

**本批10账号实机验收尚未全部通过；以下2026-10-14内容为历史单台记录。**

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

确认设备、账号与本地代理绑定、ROM和安装 → 打开TikTok并按当前节点导航 → 输入邮箱 → 指定登录方式 → 按所选完成标准确认 → 写目标账号绑定。登录前跳过代理状态、出口IP、网页联网、地区及GPS检查。

密码方式支持新版验证码页的“Need help logging in? → Use password instead”，一次密码提交，以及与指定邮箱唯一匹配的额外身份验证。密码框允许星号/圆点掩码回查。验证码输入前重新确认仍在验证码页，避免等待期间页面变化造成误输入。当前APK已确认的Family Pairing和Avatar介绍弹窗各可返回关闭一次，其他未知页面停止。

邮箱验证码提交后若出现“Verify it’s really you → Password”，使用该账号已配置的环境变量或本地受保护密码完成一次密码验证，支持实际页面的Next提交按钮；缺密码则返回password_required。该步骤可从code_submitted断点续接，不重新发码或输入验证码；password_pending和持久化password_submitted事实阻止重复提交密码。

正式默认流程仅处理精确匹配的TikTok系统通知权限窗口，点击一次“不允许”；可确认一次Start watching引导，或返回关闭Create your TikTok avatar自拍引导，不开始创建头像。前台已是该通知窗口时保留原activity。其他应用的权限弹窗或未知页面仍停止该台。

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

密码与身份验证路径已整合并有离线覆盖；2026-10-20的160.1已确认新鲜验证码、额外密码验证、跨进程续接、本人身份匹配及独立只读复核。10账号整批尚未全部通过，不能以单台结果代替整批验收。

```powershell
python -X utf8 -m unittest discover -s .\阶段6-TikTok自动登录\tests -v
```
