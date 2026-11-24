# 阶段4：代理地区同步与重启核验

2026-10-14 已实现 `setup_system.py`，供阶段3复用，也提供独立入口。阶段4负责已有云机补设、代理地区变更以及创建后的实际参数核验。

2026-10-21最新：根目录 `创建云机.py` 及`--configure-only`按代理请求→国家/时区/语言写入→TK安装执行，使用`configure(..., verify_result=False)`。不查设置前地区，不重启，不查Android属性、地区接口、GPS坐标或持久属性；每项写入响应不明确时记录后继续，安装只确认TK包名。地区报告`settings_attempted=true/verification_requested=false/success=false`区分已尝试写入与核验成功，不写`system_config`完成标记。GPS成对配置时注入及持久写入，`isRestart=false`；缺省GPS记录`gps_skipped`并直接跳过，不联网探测或解析坐标。完整region仍须通过格式检查。初始化等待最多120秒、不限轮数；独立地区入口保留原严格默认。

根目录`设置地区.py`及下文阶段4直接入口仍保留严格设置和重启回查；默认`verify_result=True`，原有只读诊断及登录前地区检查保留。下方回查说明适用于这些严格入口。

## 使用

```powershell
Set-Location 'D:\000aaa_vmos自动化'
python -X utf8 .\阶段4-系统设置与环境配置\setup_system.py --db-id EDGE5YDRZ1Q7D6TW --proxy-id proxy-004 --keep-running
$LASTEXITCODE
Get-Content -Encoding UTF8 .\阶段4-系统设置与环境配置\system_status.json
```

独立入口复用阶段3全流程，必要时会修复代理，地区变更后会重启指定云机。停机目标需要显式加 `--start-stopped`。

```powershell
python -X utf8 .\阶段4-系统设置与环境配置\setup_system.py --db-id EDGE5YDRZ1Q7D6TW --proxy-id proxy-004 --verify-only
```

只读入口不启动、不修改云机、不重启、不更新映射；输出 `system_verify.json`。阶段3也会生成相应代理报告。退出0表示确认，退出1表示失败或未确认。

## 地区来源与顺序

每条代理必须明确配置 `region.country/language/timezone`。国家用两位大写代码，语言用小写代码，时区按实际地区指定，如 `US/en/America/Los_Angeles`。完整初始化的GPS缺省跳过；独立严格模式仍取出口经纬度，也支持显式配置成对经纬度。

完整初始化在GPS成对配置时按国家 → 时区 → 语言 → GPS顺序提交，缺省GPS直接跳过。国家、语言、GPS沿用 `country_set/language_set/gps_inject`；时区通过目标独立 `http_api_port` 的 `POST /api/system/update_settings`，只发送时区字段。GPS注入后通过 `container_api/v1/update_user_prop` 保存持久坐标，`isRestart=false`。独立地区入口仍解析缺省GPS，并按原规则重启及核验；初始化不执行这些检查。每个写请求只发一次。

回查同时检查 `get_timezone_locale`、`location/get_data` 和Android实际 `persist.sys.country/locale/timezone`。GPS允许小于0.001度的差异。已有目标映射的country接口成功记录、`gps_persisted=true`、期望地区和实际值均一致时，返回 `action=already_configured`，不重复重启。旧记录没有GPS持久化标记时，需重新完成一次配置及重启核验。

2026-10-16 GPS读取已改为通过阶段3的统一控制请求，使用目标云机独立 `http_api_port`。bridge模式下主机记录的容器IP可能过期，共享转发的GPS可能来自其他实例；旧成功记录仍须用独立端口核对实际坐标。145.1已在两并发入口复测中修正国内坐标，西雅图GPS及持久属性在重启后均通过，见 `日志/create-20261016-174923-ipnc49vx/`。

以下回查规则仅适用于独立地区及严格验证模式；完整初始化已取消设置前后读取。严格模式将实际值与接口值记录到 `system.before`，不一致时不能跳过写入；只读查询和设置后的回查均要求匹配目标地区，失败最多5轮。

国家、时区或语言写入响应超时、HTTP/业务码不成功（包括业务0）时，不重发设置，最多5轮只读回查，默认60秒窗口，从2秒起退避。先读取Android属性 `persist.sys.country`、`persist.sys.timezone` 或 `persist.sys.locale`；实际值匹配目标后再查 `get_timezone_locale` 对应的 `country`、`timezone` 或 `locale`。Android属性作为直接读取系统实际配置的首查，值不匹配时省去当轮地区接口请求；两种来源都匹配才继续GPS、重启、联网和TK安装，未匹配或查询一直失败则记录该台失败。语言目标由language-country组成，如 `en-US`；联网成功不作为语言正确的依据。报告保留该步骤 `accepted=false/error/readback_verified=true` 及Android/API实际值，区分写入受理与实际状态确认。GPS设置及持久属性响应不明确时，分别回查独立坐标接口与Android持久属性，确认后仍须重启核验。接口失败信息包含HTTP与业务码，不保存任意响应原文。

重启后启动ID变化及Android/地区API就绪最多等待300秒、20轮，成功立即继续；重启前启动ID及最终地区/GPS回查仍最多5轮。必要回查只检查VMOS地区接口、Android国家/语言/时区属性、GPS运行坐标及持久属性是否与配置一致，不检测联网可用性。嵌套确认不叠加轮次，写请求不因回查失败重发。

2026-10-18，154.2/155.1/155.2在旧主机时区接口均出现HTTP200/业务0、实际仍GMT；再次启动后旧接口又成功，内部原因尚未确定。当前改用上述独立端口接口，所用镜像20260731满足该接口要求的20260113及以上版本。154.2已实测通过独立接口将America/Phoenix改为UTC再恢复，两种回查结果均匹配且国家/语言未改变，证据 `日志/timezone-route-154_2-155-20261018-ni3uukui/independent_timezone_change.json`。三台完整入口复测的地区步骤均明确受理，重启后的US/en-US/America/Phoenix、GPS33.4484/-112.074及GPS持久属性均确认；报告见 `日志/create-20261018-004427-51503hxv/`。

VMOS的地理数据源有时返回国家全名；脚本通过同一出口IP的国家代码再次核验，不把全名直接与两位代码比较。每项只读查询最多5轮，国家不匹配时仍判该台失败。

## 成功判据与范围

根地区入口的 `success=true` 表示地区设置与回查完成；完整初始化还要求代理配置、TK包名及状态恢复完成，均不以联网结果作为条件。底层阶段4直接入口仍有阶段3网络诊断，范围见其报告。`system.success=true` 单独表示地区完成，成功后保存目标映射的 `system_config.expected/country_api_confirmed/gps_persisted/verified_at`，供后续幂等判断。

已实测US、en-US、America/Los_Angeles、洛杉矶GPS重启保留及重复运行跳过。国家接口已执行，但完整SIM/运营商字段未独立验证，报告保留 `sim_details_verified=false`。随机设备指纹、额外GPS属性、防火墙及批量调度不在本轮验收范围。

2026-10-15 对新批次 `144.1` 通过根创建入口实测：代理和中转绑定、四个地区接口、重启与实际参数回查全部完成，地区为美国、英语、洛杉矶时区、西雅图GPS。TikTok首页HTTP200，Google 204端点超时，因此地区成功、联网失败分别记录，总体返回失败。单台报告见 `日志/create-20261015-181202-68fmb0dw/EDGEFIMKCFD755UO/proxy_status.json`；外层测试最终恢复停机，见 `日志/full_initialization_144_1_20261015.json`。其他35台未执行本轮配置。

```powershell
python -X utf8 -m unittest discover -s .\阶段4-系统设置与环境配置\tests -v
```

证据：`阶段12-文档与交付/closure_346_20261014/`。
