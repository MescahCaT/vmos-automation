# 阶段3：代理绑定与联网验证

默认执行代理绑定、阶段4国家/时区/语言/GPS同步、必要的重启及联网回查。2026-10-16 已修复主机旧容器IP转发造成的代理串台，标准引擎0使用每台独立控制端口；144.1、145.1通过根入口两并发完整核验。最新证据见 `日志/create-20261016-174923-ipnc49vx/`，下方保留原单台入口示例。

## 使用

在项目根目录执行：

```powershell
python -X utf8 .\阶段3-网络代理配置\setup_proxy.py --db-id EDGE5YDRZ1Q7D6TW --proxy-id proxy-004 --start-stopped --keep-running
$LASTEXITCODE
Get-Content -Encoding UTF8 .\阶段3-网络代理配置\proxy_status.json
```

只读核验代理、地区和网络：

```powershell
python -X utf8 .\阶段3-网络代理配置\setup_proxy.py --db-id EDGE5YDRZ1Q7D6TW --proxy-id proxy-004 --verify-only --wait-time 60
```

`--proxy-only` 跳过地区同步。其他代理尚未配置region时，应先补齐地区，或明确使用此选项。`--verify-only` 不启动、不改代理、不重启、不写设备映射，结果另存 `proxy_verify.json`。退出0表示此次全部确认，退出1表示失败或未确认。

`--start-stopped` 允许启动指定停机实例，默认结束后恢复停机；`--keep-running` 仅在成功后保留脚本启动的实例。原本运行的实例保持运行。`--wait-time` 为重试窗口，单个请求另有超时，总耗时可能超过窗口。

根目录初始化入口会在代理及地区之后安装TikTok，内部调用 `run(..., restore_state=False)`，由该入口在安装结束或异常时统一恢复原状态。阶段3单独运行的默认恢复行为保持不变。

## 配置与执行

读取 `配置文件/config.json`、`proxy_list.json` 和 `数据/device_mapping.json`。每次只写明确指定的单台。

代理资源除已有认证和transit外，使用：

```json
{
  "engine_type": 0,
  "region": {
    "country": "US",
    "language": "en",
    "timezone": "America/Los_Angeles"
  }
}
```

GPS未配置时，从实际出口的地理结果解析；手动配置时需成对提供 `latitude/longitude`。多时区国家必须明确指定时区。认证与UUID只存本地资源，不进入日志和报告。

流程为本机代理检查 → 云机ROM、Android启动及独立代理服务就绪 → 核对节点、认证和引擎 → 按需绑定 → 代理绑定确认后解析国家/时区/语言/GPS → 按需重启并检查Android实际属性 → 出口与连续网页验证 → 更新目标映射。正常的相同代理跳过重挂；地区已确认且仍匹配时跳过写入和重启。云机JSON探测会保留curl退出码，启动瞬态失败按等待窗口重试。

新机的ROM可能先于代理服务就绪；代理接口只返回主机元数据时，按60秒窗口等待，读到明确的代理配置或CBS启用状态后才允许配置。停止旧代理未返回成功时，仅在回查明确没有旧配置或已关闭后继续；仍启用、查询不明确或请求超时均不会重发停止请求。

标准代理停止请求超时时也通过独立端口只读核对关闭状态，确认disabled后才提交后续启用请求。地区配置已确认重启后，再核对代理是否保留；节点丢失、关闭或引擎改变时恢复一次，记录 `proxy_after_reboot/reapply_api`，随后仍须通过节点及认证、出口和连续网页验证。代理仍匹配时不重挂，写请求响应不明确时不重发。

标准引擎0通过实例列表中的独立 `http_api_port` 调用云机 `/api/proxy/get`、`/api/proxy/set_enabled`，两个nodes仍按中转→出口提交，节点协议字段使用 `proxyName`。先明确停止旧代理，再设置；停止结果不明确时只回查一次，只有确认已关闭才继续。GPS和非root网页控制请求也走该实例端口。端口缺失、无效或重复直接停止，不回退主机按容器IP转发的接口，避免bridge实例IP记录过期造成串台。legacy引擎1与CBS接口保留兼容。设置、启动、重启超时只回查，不自动重发。地区查询可有限重试；国家全名通过同IP的国家代码核验。GPS在0.001度内的差异视为匹配。

## 验收与范围

`proxy_status.json` 的 `success=true` 表示本次要求的节点、引擎、出口、网页及地区均已确认；`system.success` 为地区结果，`mapping_updated` 表示已写目标映射。

标准模式网页从root上下文请求，报告标注 `proxy_exit_and_root_web`；其他模式使用system上下文。网页通过不能代替TikTok应用或登录身份验证。阶段6单独完成本人主页核验。网络出现波动仍会阻止通过，不凭出口IP或HTTP200接口受理就判成功。

单台全流程、标准模式重挂、重复运行地区跳过及两台并发已有实测。完整SIM字段、UDP专测、实际DNS解析器及更高并发容量未验收。标准链路提交DNS配置不代表ROM一定回显或使用该解析器。

```powershell
python -X utf8 -m unittest discover -s .\阶段3-网络代理配置\tests -v
```

证据及本轮代码备份：`阶段12-文档与交付/closure_346_20261014/`。
