# 云机命名与映射

现有云机保留 VMOS 的 `user_name`，例如 `s145.1`、`s145.2`。验证实例使用 `codex-test-20261012-01`。本次不会批量重命名现有实例。

后续新批次可使用 `TK-001` 至 `TK-999`；创建前检查同名，实例关系始终以 VMOS 分配的 `db_id` 为键，避免同名实例混淆。

`数据/device_mapping.json` 格式：

```json
{
  "host": "http://192.168.110.35:18182",
  "timestamp": "2026-10-12T18:00:00+08:00",
  "devices": {
    "EDGE5YDRZ1Q7D6TW": {
      "name": "codex-test-20261012-01",
      "state": "stopped",
      "present": true,
      "proxy_id": null,
      "account_email": null,
      "last_seen": "2026-10-12T18:00:00+08:00",
      "ports": {"adb": 31022, "http_api_port": 35000}
    }
  }
}
```

`proxy_id` 引用代理配置中的 `id`，不保存含密码的代理 URL；`account_email` 引用账号清单。空值表示尚未绑定，阶段3/6验证后再记录实际绑定。环境检查更新状态与端口、保留已存绑定。主机查询失败时保留原映射，消失实例标为 `present=false` 而不是删除历史记录。
