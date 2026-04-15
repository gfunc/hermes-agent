---
name: wecom-preflight
description: 企业微信原生 MCP 技能使用指南。说明 Hermes Gateway 如何自动发现 WeCom MCP 工具，以及当工具不可用时如何排查和手动重载。
---

# 企业微信 MCP 使用指南

> 本技能说明 Hermes 中企业微信原生 MCP 工具的自动发现机制。

## 自动发现机制

当 Hermes Gateway 的 **WeCom 通道成功建立 WebSocket 连接**后，Gateway 会自动向 WeCom 服务端请求 MCP 配置，并动态注册以下品类的原生 MCP 工具：

- `contact` — 通讯录查询
- `meeting` — 会议管理
- `todo` — 待办事项
- `schedule` — 日程管理
- `doc` — 文档与智能表格
- `msg` — 消息收发

注册完成后，这些工具会以前缀 `mcp_wecom_` 的形式直接可用，例如：

- `mcp_wecom_contact_get_userlist`
- `mcp_wecom_meeting_create_meeting`
- `mcp_wecom_todo_get_todo_list`
- `mcp_wecom_schedule_create_schedule`
- `mcp_wecom_doc_create_doc`
- `mcp_wecom_msg_send_message`

**无需手动配置白名单或重启 Gateway。**

---

## 触发条件

当用户在 WeCom 通道（企业微信智能机器人）中发送消息，且消息意图匹配任意 `wecom-*` skill 时，相关 MCP 工具会自动参与推理和调用。

---

## 排查：MCP 工具不可用

如果在调用时收到 `tool not found`、`no such tool` 或类似错误，说明动态注册可能尚未完成或失败。按以下步骤排查：

### 步骤 1：确认 WeCom 通道已连接

检查 Gateway 日志或状态，确认 WeCom 平台适配器已成功连接：

```bash
hermes gateway status
```

若 WeCom 状态不是 `connected`，说明 WebSocket 连接未建立，MCP 工具自然无法注册。请先排查 WeCom 配置（`WECOM_BOT_ID`、`WECOM_SECRET`、`WECOM_WEBSOCKET_URL`）是否正确。

### 步骤 2：手动重载 MCP 配置

如果 WeCom 已连接但工具仍不可用，可能是注册过程中发生了临时错误。可尝试让拥有者或管理员在对话中发送：

```
/reload-mcp
```

该命令会断开并重新连接所有 MCP 服务器，刷新工具表面。

> ⚠️ 发送 `/reload-mcp` 后，请等待 3-5 秒再重新发送原始请求。

### 步骤 3：检查具体品类是否返回了配置

某些企业可能未开启全部 MCP 能力。如果只有某个品类（如 `doc`）不可用，而其他品类（如 `contact`）正常，说明该品类可能在企业微信后台未授权或服务商未提供对应 URL。此时：

- 联系企业微信管理员确认对应能力是否已授权
- 或检查 Hermes Gateway 日志中 `MCP config discovery` 相关条目，确认该品类是否返回了有效 URL

---

## 注意事项

1. **首次连接时稍有延迟**：WeCom WebSocket 连接建立后，Gateway 会并行请求 6 个品类的 MCP 配置。通常 1-2 秒内完成，若网络延迟较高可能稍慢。
2. **仅 WeCom 通道生效**：这些 `mcp_wecom_*` 工具只在通过企业微信接收到的消息中可用。在其他平台（如 Telegram、Discord）不会注册 WeCom 专属工具。
3. **配置变更无需重启**：如果企业微信后台调整了 MCP 配置，只需等待 WebSocket 重连（或手动重启 Gateway），新配置会自动覆盖旧配置。
4. **无需 `tools.alsoAllow` 白名单**：原生 MCP 工具不走传统 toolset 白名单机制，因此不需要像旧版 OpenClaw 插件那样手动将 `mcp_wecom_*` 加入 `alsoAllow`。

---

## 快速参考

| 场景 | 处理方式 |
|------|---------|
| WeCom 已连接，工具正常可用 | 直接使用 `mcp_wecom_<品类>_<方法>` |
| 收到 tool not found | 先检查 `hermes gateway status`，确认 WeCom 为 `connected` |
| WeCom 已连接但部分工具缺失 | 发送 `/reload-mcp` 手动刷新 |
| 某个品类始终不可用 | 联系管理员确认企业微信后台授权 |
| 非 WeCom 通道请求使用 WeCom 工具 | 告知用户该能力仅在企业微信中可用 |
