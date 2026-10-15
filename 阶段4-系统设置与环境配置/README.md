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

先核对出口IP及国家，再按 `country_set → timezone_set → language_set → gps_inject` 顺序提交。随后重启单台，确认uptime重置、Android启动完成、地区及定位API就绪，最后核验参数保留。每个写请求只发一次，超时不重复设置或重启。

回查同时检查 `get_timezone_locale`、`location/get_data` 和Android实际 `persist.sys.country/locale/timezone`。GPS允许小于0.001度的差异。已有目标映射的country接口成功记录、期望地区和实际值均一致时，返回 `action=already_configured`，不重复重启。

VMOS的地理数据源有时返回国家全名；脚本通过同一出口IP的国家代码再次核验，不把全名直接与两位代码比较。只读查询异常最多重试三次，国家不匹配仍停止。

## 成功判据与范围

报告顶层 `success=true` 表示地区和联网均通过；`system.success=true` 单独表示地区完成。地区设置及重启回查成功后，即使后续网页超时，也会保存目标映射的 `system_config.expected/country_api_confirmed/verified_at`，供后续幂等判断；不会将联网结果记为通过。

已实测US、en-US、America/Los_Angeles、洛杉矶GPS重启保留及重复运行跳过。国家接口已执行，但完整SIM/运营商字段未独立验证，报告保留 `sim_details_verified=false`。随机设备指纹、额外GPS属性、防火墙及批量调度不在本轮验收范围。

2026-10-15 对新批次 `144.1` 通过根创建入口实测：代理和中转绑定、四个地区接口、重启与实际参数回查全部完成，地区为美国、英语、洛杉矶时区、西雅图GPS。TikTok首页HTTP200，Google 204端点超时，因此地区成功、联网失败分别记录，总体返回失败。单台报告见 `日志/create-20261015-181202-68fmb0dw/EDGEFIMKCFD755UO/proxy_status.json`；外层测试最终恢复停机，见 `日志/full_initialization_144_1_20261015.json`。其他35台未执行本轮配置。

```powershell
python -X utf8 -m unittest discover -s .\阶段4-系统设置与环境配置\tests -v
```

证据：`阶段12-文档与交付/closure_346_20261014/`。
