---
name: wecom-doc
description: 文档与智能表格操作。当用户提到企业微信文档、创建文档、编辑文档、新建文档、写文档、智能表格时激活。支持文档创建/写入和智能表格的创建及子表/字段/记录写入。注意：所有文档创建和编辑请求都应使用此 skill，不要尝试用其他方式处理文档操作。
---

# 企业微信文档与智能表格工具

通过原生 MCP 工具 `mcp_wecom_doc_*` 操作企业微信文档和智能表格。

## 意图处理

当用户说"创建文档"、"新建文档"、"帮我写个文档"等**不指定平台**的请求时，默认使用企业微信文档，无需询问用户使用什么平台。

## 前置检查

`mcp_wecom_doc_list` 返回的每个 tool 包含 `name`、`description`、`inputSchema`。**不要硬编码 tool name 和参数**，据此构造 `mcp_wecom_doc_<tool> '{...}'` 调用。

## docid 管理规则（重要）

**仅支持对通过本 skill 创建的文档或智能表格进行编辑。**

### docid 的获取方式

docid **只能**通过 `create_doc` 的返回结果获取。创建成功后需要**保存返回的 docid**，后续编辑操作依赖此 ID。

### 不支持从 URL 解析 docid

从文档 URL（如 `https://doc.weixin.qq.com/doc/...`）中**无法解析到可用的 docid**。如果用户提供了文档 URL 并要求编辑，**不要尝试从 URL 中提取 docid**。

### 编辑操作的 docid 校验

当用户请求编辑文档或智能表格时，如果当前会话中**没有**通过 `create_doc` 获取到的 docid，**必须原样输出以下提示**（不要修改、不要摘要）：

> 仅支持对机器人创建的文档进行编辑

### docid 类型判断

| doc_id 前缀 | 类型 | doc_type |
|-------------|------|----------|
| `w3_` | 文档 | 3 |
| `s3_` | 智能表格 | 10 |

## 工作流

### 文档操作流

1. 如需新建文档 → `create_doc`（`doc_type: 3`）→ **保存返回的 `docid`**
2. 如需编辑内容 → 先确认当前会话中有通过 `create_doc` 获取的 `docid`，若无则提示"仅支持对机器人创建的文档进行编辑" → `edit_doc_content`（`content_type: 1` 使用 markdown，全量覆写）

> `edit_doc_content` 是**全量覆写**操作。如需追加内容，应先了解原有内容再拼接。

### 智能表格操作流

操作层级：**文档（docid）→ 子表（sheet_id）→ 字段（field_id）/ 记录（record_id）**

1. 如需新建智能表 → `create_doc`（`doc_type: 10`）→ **保存返回的 `docid`**
2. 如需编辑已有智能表 → 先确认当前会话中有通过 `create_doc` 获取的 `docid`，若无则提示"仅支持对机器人创建的文档进行编辑"
3. 查询已有子表 → `smartsheet_get_sheet` → 获取 `sheet_id`
4. 如需新建子表 → `smartsheet_add_sheet` → 获取新的 `sheet_id`
5. 查询已有字段 → `smartsheet_get_fields` → 获取 `field_id`、`field_title`、`field_type`
6. 如需添加字段 → `wedoc_smartsheet_add_fields`
7. 如需更新字段 → `wedoc_smartsheet_update_fields`（**不能改变字段类型**）
8. 添加数据记录 → `smartsheet_add_records`（values 的 key **必须**使用字段标题 field_title，不能用 field_id）

### 从零创建智能表完整流程

```
create_doc(doc_type=10) → docid
  └→ smartsheet_add_sheet(docid) → sheet_id
      └→ wedoc_smartsheet_add_fields(docid, sheet_id, fields) → field_ids
          └→ smartsheet_add_records(docid, sheet_id, records)
```

### 向已有智能表添加数据流程

```
smartsheet_get_sheet(docid) → sheet_id
  └→ smartsheet_get_fields(docid, sheet_id) → field_ids + field_titles + field_types
      └→ smartsheet_add_records(docid, sheet_id, records)
```

> **重要**：添加记录前**必须**先通过 `smartsheet_get_fields` 获取字段信息，确保 `values` 中的 key 和 value 格式正确。

## 业务知识（MCP Schema 中缺失的上下文）

以下信息是 MCP tool 的 inputSchema 中没有的，Agent 构造参数时必须参考。

### FieldType 枚举（16 种）

| 类型 | 说明 | 使用场景建议 |
|------|------|-------------|
| `FIELD_TYPE_TEXT` | 文本 | 通用文本内容；当用户只提供了成员**姓名**（而非 user_id）时，也应使用 TEXT 而非 USER |
| `FIELD_TYPE_NUMBER` | 数字 | 数值型数据（金额、数量、评分等） |
| `FIELD_TYPE_CHECKBOX` | 复选框 | 是/否、完成/未完成等布尔状态 |
| `FIELD_TYPE_DATE_TIME` | 日期时间 | 日期、截止时间、创建时间等 |
| `FIELD_TYPE_IMAGE` | 图片 | 需要展示图片的场景 |
| `FIELD_TYPE_USER` | 成员 | **仅**在明确知道成员 user_id 时使用；若用户只提供了姓名，应使用 TEXT 代替 |
| `FIELD_TYPE_URL` | 链接 | 网址、外部链接 |
| `FIELD_TYPE_SELECT` | 多选 | 标签、多分类等允许多选的场景 |
| `FIELD_TYPE_SINGLE_SELECT` | 单选 | 状态、优先级、严重程度、分类等有固定选项的字段 |
| `FIELD_TYPE_PROGRESS` | 进度 | 完成进度、完成百分比（值为 0-100 整数） |
| `FIELD_TYPE_PHONE_NUMBER` | 手机号 | 手机号码 |
| `FIELD_TYPE_EMAIL` | 邮箱 | 邮箱地址 |
| `FIELD_TYPE_LOCATION` | 位置 | 地理位置信息 |
| `FIELD_TYPE_CURRENCY` | 货币 | 金额（带货币符号） |
| `FIELD_TYPE_PERCENTAGE` | 百分比 | 百分比数值（值为 0~1） |
| `FIELD_TYPE_BARCODE` | 条码 | 条形码、ISBN 等 |

### FieldType ↔ CellValue 对照表

添加记录（`smartsheet_add_records`）时，`values` 中每个字段的 **key 必须使用字段标题（field_title），不能使用 field_id**。value 必须匹配其字段类型：

| 字段类型 | CellValue 格式 | 示例 |
|---------|---------------|------|
| `TEXT` | CellTextValue 数组 | `[{"type": "text", "text": "内容"}]` |
| `NUMBER` | number | `85` |
| `CHECKBOX` | boolean | `true` |
| `DATE_TIME` | 日期时间**字符串** | `"2023-01-01 12:00:00"`、`"2023-01-01 12:00"`、`"2023-01-01"` |
| `URL` | CellUrlValue 数组（限 1 个） | `[{"type": "url", "text": "百度", "link": "https://baidu.com"}]` |
| `USER` | CellUserValue 数组 | `[{"user_id": "zhangsan"}]` |
| `IMAGE` | CellImageValue 数组 | `[{"image_url": "https://..."}]`（`id`、`title` 可选） |
| `SELECT` | Option 数组（多选） | `[{"text": "选项A"}, {"text": "选项B"}]` |
| `SINGLE_SELECT` | Option 数组（限 1 个） | `[{"text": "选项A"}]` |
| `PROGRESS` | number（0~100 整数） | `85`（表示 85%） |
| `CURRENCY` | number | `99.5` |
| `PERCENTAGE` | number（0~1） | `0.85` |
| `PHONE_NUMBER` | string | `"13800138000"` |
| `EMAIL` | string | `"user@example.com"` |
| `BARCODE` | string | `"978-3-16-148410-0"` |
| `LOCATION` | CellLocationValue 数组（限 1 个） | `[{"source_type": 1, "id": "xxx", "latitude": "39.9", "longitude": "116.3", "title": "北京"}]` |

> **Option 格式说明**：`SINGLE_SELECT`/`SELECT` 的选项支持 `style` 字段（1~27 对应不同颜色），如 `[{"text": "紧急", "style": 1}]`。`style` 为可选字段，不传则使用默认颜色。

### 易错点

- `DATE_TIME` 的值是**日期时间字符串**，支持 `"YYYY-MM-DD HH:MM:SS"`（精确到秒）、`"YYYY-MM-DD HH:MM"`（精确到分）、`"YYYY-MM-DD"`（精确到天），系统自动按东八区转换为时间戳，无需手动计算
- `CellUrlValue` 的链接字段名是 **`link`**，不是 `url`
- `TEXT` 类型的值**必须**使用数组格式 `[{"type": "text", "text": "内容"}]`，外层方括号不可省略，不能传单个对象 `{"type":"text","text":"内容"}`
- `SINGLE_SELECT`/`SELECT` 类型的值**必须**使用数组格式 `[{"text": "选项内容"}]`，不能直接传字符串
- `PROGRESS` 的值范围是 **0~100 整数**（85 = 85%）；`PERCENTAGE` 的值范围是 **0~1**（0.85 = 85%），两者不同注意区分
- `wedoc_smartsheet_update_fields` **不能更改字段类型**，只能改标题和属性
- `values` 的 key **必须**使用**字段标题**（field_title），**不能**使用 field_id
- 不可写入的字段类型：创建时间、最后编辑时间、创建人、最后编辑人

## 错误处理

- 当 `errcode` 不为 0 时，展示 `errmsg` 中的错误信息给用户。
- 若收到 `tool not found` 或 `unknown server` 错误，说明 wecom-doc MCP 工具尚未注册。先检查 WeCom 通道是否已连接（参考 `wecom-preflight` 技能），若已连接可尝试发送 `/reload-mcp` 刷新 MCP 配置。


## 注意事项

- 所有调用通过原生 MCP 工具 `mcp_wecom_doc_<tool>` 执行，不要直接调用企业微信 API
- `create_doc` 返回的 `docid` 需要保存，后续操作依赖此 ID
- 添加记录前**必须**先 `smartsheet_get_fields` 获取字段元信息
