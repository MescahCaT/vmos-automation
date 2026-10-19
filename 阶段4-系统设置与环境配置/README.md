# 阶段4：代理地区同步与重启核验

2026-10-14 已实现 `setup_system.py`，供阶段3复用，也提供独立入口。阶段4负责已有云机补设、代理地区变更以及创建后的实际参数核验。

根目录 `创建云机.py` 已强制接入完整流程：绑定代理及中转 → 国家 → 时区 → 语言 → GPS → 重启回查 → 联网验证。每条代理必须有完整region，缺失时在创建前停止。阶段4已经有可信记录且实际参数一致时跳过重复写入。

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

每条代理必须明确配置 `region.country/language/timezone`。国家用两位大写代码，语言用小写代码，时区按实际地区指定，如 `US/en/America/Los_Angeles`，不能将美国统一猜成纽约。GPS缺省取该代理出口的经纬度，也支持显式配置成对经纬度。

先核对出口IP及国家，再按国家 → 时区 → 语言 → GPS顺序提交。国家、语言、GPS沿用 `country_set/language_set/gps_inject`；时区通过目标云机独立 `http_api_port` 的 `POST /api/system/update_settings`，只发送 `{"timezone":"目标时区"}`。报告仍用逻辑步骤名 `timezone_set`，同时记录 `transport=instance_port` 和 `api=system/update_settings`；不再调用旧主机时区写接口，也不在失败后回退该接口。GPS注入后通过 `container_api/v1/update_user_prop` 仅追加/更新 `persist.sys.cloud.gps.lat/lon`，不覆盖其他属性且不触发额外重启。随后统一重启单台，确认 `/proc/sys/kernel/random/boot_id` 改变、Android启动完成、地区及定位API就绪，再同时核验GPS持久属性和实际坐标。新机重启较慢时，启动后uptime可能已超过重启前的值，因此不用uptime大小判定是否重启。报告保留前后启动ID、等待时长和最后回查错误。每个写请求只发一次，超时不重复设置或重启。

回查同时检查 `get_timezone_locale`、`location/get_data` 和Android实际 `persist.sys.country/locale/timezone`。GPS允许小于0.001度的差异。已有目标映射的country接口成功记录、`gps_persisted=true`、期望地区和实际值均一致时，返回 `action=already_configured`，不重复重启。旧记录没有GPS持久化标记时，需重新完成一次配置及重启核验。

2026-10-16 GPS读取已改为通过阶段3的统一控制请求，使用目标云机独立 `http_api_port`。bridge模式下主机记录的容器IP可能过期，共享转发的GPS可能来自其他实例；旧成功记录仍须用独立端口核对实际坐标。145.1已在两并发入口复测中修正国内坐标，西雅图GPS及持久属性在重启后均通过，见 `日志/create-20261016-174923-ipnc49vx/`。

初始化写入前，地区接口与Android属性尚未一致或属性为空时，将实际值及接口值记录到 `system.before`，继续按顺序设置；存在差异时不能跳过写入。只读验证、重启及设置后的回查仍要求两者一致且匹配目标地区。接口失败、GPS查询失败或命令执行失败仍会停止。

时区或语言写入响应超时、HTTP/业务码不成功（包括业务0）时，不重发设置，最多三轮只读回查，轮间等待1秒。先读取Android属性 `persist.sys.timezone` 或 `persist.sys.locale`；实际值匹配目标后再查 `get_timezone_locale` 对应的 `timezone` 或 `locale`。Android属性作为直接读取系统实际配置的首查，值不匹配时省去当轮地区接口请求；两种来源都匹配才继续GPS、重启、联网和TK安装，未匹配或查询一直失败仍停止。语言目标由language-country组成，如 `en-US`；联网成功不作为语言正确的依据。报告保留该步骤 `accepted=false/error/readback_verified=true` 及 `android_locale/api_locale`（或timezone字段），区分写入受理与实际状态确认。重启后持久化及完整地区核验保持严格。接口失败信息包含HTTP与业务码，不保存任意响应原文。

2026-10-18，154.2/155.1/155.2在旧主机时区接口均出现HTTP200/业务0、实际仍GMT；再次启动后旧接口又成功，内部原因尚未确定。当前改用上述独立端口接口，所用镜像20260731满足该接口要求的20260113及以上版本。154.2已实测通过独立接口将America/Phoenix改为UTC再恢复，两种回查结果均匹配且国家/语言未改变，证据 `日志/timezone-route-154_2-155-20261018-ni3uukui/independent_timezone_change.json`。三台完整入口复测的地区步骤均明确受理，重启后的US/en-US/America/Phoenix、GPS33.4484/-112.074及GPS持久属性均确认；报告见 `日志/create-20261018-004427-51503hxv/`。

VMOS的地理数据源有时返回国家全名；脚本通过同一出口IP的国家代码再次核验，不把全名直接与两位代码比较。只读查询异常最多重试三次，国家不匹配仍停止。

## 成功判据与范围

报告顶层 `success=true` 表示地区和联网均通过；`system.success=true` 单独表示地区完成。地区设置及重启回查成功后，即使后续网页超时，也会保存目标映射的 `system_config.expected/country_api_confirmed/gps_persisted/verified_at`，供后续幂等判断；不会将联网结果记为通过。

已实测US、en-US、America/Los_Angeles、洛杉矶GPS重启保留及重复运行跳过。国家接口已执行，但完整SIM/运营商字段未独立验证，报告保留 `sim_details_verified=false`。随机设备指纹、额外GPS属性、防火墙及批量调度不在本轮验收范围。

2026-10-15 对新批次 `144.1` 通过根创建入口实测：代理和中转绑定、四个地区接口、重启与实际参数回查全部完成，地区为美国、英语、洛杉矶时区、西雅图GPS。TikTok首页HTTP200，Google 204端点超时，因此地区成功、联网失败分别记录，总体返回失败。单台报告见 `日志/create-20261015-181202-68fmb0dw/EDGEFIMKCFD755UO/proxy_status.json`；外层测试最终恢复停机，见 `日志/full_initialization_144_1_20261015.json`。其他35台未执行本轮配置。

```powershell
python -X utf8 -m unittest discover -s .\阶段4-系统设置与环境配置\tests -v
```

证据：`阶段12-文档与交付/closure_346_20261014/`。
