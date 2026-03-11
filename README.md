# Nanobot Game Customer Service

基于 `nanobot` 和 `nanobot.game_cs` 的游戏客服系统，支持：

- SOP 驱动的客户接待流程
- 知识库检索与 AI 辅助回复
- 人工转接
- 管理端通过 `gateway` + 钉钉 / 飞书机器人进行客服管理
- 对运行中的 `game_cs` 进程进行统计、查询、主动发消息、重置会话、处理人工工单

本文档按“从零启动到可管理”来写，直接照着配置即可。

## 1. 项目结构

- `nanobot.game_cs.service`
  运行游戏客服服务，默认对外提供 webhook、admin、cron 接口
- `nanobot.cli.commands gateway`
  运行管理端 gateway，承接钉钉、飞书等机器人消息
- `nanobot.agent.tools.game_cs_admin`
  管理端 agent 调用的工具，用于访问 `game_cs` 的 admin API

推荐部署形态：

1. `game_cs` 进程负责接待玩家
2. `gateway` 进程负责接待管理员
3. 管理员在钉钉 / 飞书里给机器人发自然语言指令
4. `gateway` 调用 `game_cs` admin API 完成管理动作

## 2. 安装

### 2.1 Python

要求：

- Python `3.11+`

### 2.2 安装依赖

```bash
pip install -e ".[game_cs]"
```

如果你还要跑测试：

```bash
pip install -e ".[game_cs,dev]"
```

## 3. 核心进程

本项目通常会启动两个进程。

### 3.1 客服进程

```bash
set GAME_CS_AI_ENABLED=true
python -m nanobot.game_cs.service --host 127.0.0.1 --port 8011
```

这个进程负责：

- 接收客户消息
- 推进客户 SOP
- 调用知识库 / AI
- 生成人工待处理工单
- 暴露管理接口给 `gateway`

### 3.2 管理端进程

```bash
set NANOBOT_GAME_CS_ADMIN_BASE_URL=http://127.0.0.1:8011
set NANOBOT_GAME_CS_ADMIN_TOKEN=你的_GAME_CS_SERVICE_TOKEN
python -m nanobot.cli.commands gateway -p 18790
```

这个进程负责：

- 承接钉钉 / 飞书等管理员消息
- 让 agent 调用 `game_cs_admin` 工具
- 将管理动作转发到 `game_cs` admin API

## 4. 最小可用配置

### 4.1 game_cs 必填环境变量

最少需要：

```bash
set GAME_CS_SERVICE_TOKEN=replace-with-strong-token
set GAME_CS_AI_ENABLED=true
```

推荐再配置：

```bash
set GAME_CS_DB_PATH=.nanobot/game_cs.db
set GAME_CS_UPLOADS_DIR=.nanobot/game_cs_uploads
set GAME_CS_OPENVIKING_PATH=.nanobot/openviking_data
set GAME_CS_OPENVIKING_TARGET_URI=viking://resources/game-cs/
set GAME_CS_PERSONALITY=lively
set GAME_CS_ADMIN_GATEWAY_ENABLED=true
set GAME_CS_ADMIN_GATEWAY_URL=http://127.0.0.1:18790/message
```

### 4.2 gateway 对接 game_cs 必填环境变量

```bash
set NANOBOT_GAME_CS_ADMIN_BASE_URL=http://127.0.0.1:8011
set NANOBOT_GAME_CS_ADMIN_TOKEN=replace-with-strong-token
```

`NANOBOT_GAME_CS_ADMIN_TOKEN` 必须和 `GAME_CS_SERVICE_TOKEN` 保持一致。

## 5. 管理能力说明

当前管理端已经支持通过 `gateway` 管理运行中的 `python -m nanobot.game_cs.service` 进程。

### 5.1 可查看的数据

- 当前总客户数
- 打开中的客户数
- 已关闭客户数
- 最近 24 小时活跃客户数
- 已绑定客户数
- 各个 SOP 阶段的客户数量
- 待人工处理工单数
- 已回答工单数
- 已送达工单数
- 客户列表
- 单个客户详情
- 单个客户最近消息

### 5.2 可执行的动作

- 查看客户列表
- 查看某个客户详情
- 查看某个客户最近消息
- 给某个客户主动发消息
- 重置某个客户会话
- 关闭某个客户的 AI 自动接待
- 恢复某个客户的 AI 自动接待
- 查看人工待处理工单
- 对人工工单直接回复

## 6. 管理员在钉钉 / 飞书里的使用方式

前提：

1. `gateway` 已启动
2. 对应的钉钉 / 飞书渠道已在 nanobot 配置中启用
3. 管理员账号已加入该渠道的允许名单
4. `NANOBOT_GAME_CS_ADMIN_BASE_URL` 和 `NANOBOT_GAME_CS_ADMIN_TOKEN` 已配置

管理员可以直接给机器人发自然语言，例如：

- 查看当前客服统计
- 列出最近 20 个客户
- 列出当前处于 collecting_info 的客户
- 查看客户 `player_1001` 的详情
- 查看客户 `player_1001` 最近消息
- 给客户 `player_1001` 发消息：请重新登录后再试
- 重置客户 `player_1001` 的会话
- 关闭客户 `player_1001` 的 AI 自动接待
- 恢复客户 `player_1001` 的 AI 自动接待
- 查看待人工处理工单
- 回复工单 `42`：请提供角色截图，我帮你继续处理

建议让管理员按这种格式提问，模型调用工具会更稳定：

```text
查看客户 player_1001 详情
给客户 player_1001 发消息：请稍后 5 分钟再试
回复工单 42，用户 player_1001：请重新登录后再尝试
```

## 7. 客服进程对外接口

所有 admin / webhook / cron 接口都需要：

```http
X-Game-Cs-Token: <GAME_CS_SERVICE_TOKEN>
```

### 7.1 健康检查

#### `GET /healthz`

示例：

```bash
curl http://127.0.0.1:8011/healthz
```

### 7.2 客户消息入口

#### `POST /webhook/game-message`

用于你的真实聊天系统把客户消息转给 `game_cs`。

示例：

```bash
curl -X POST http://127.0.0.1:8011/webhook/game-message ^
  -H "Content-Type: application/json" ^
  -H "X-Game-Cs-Token: %GAME_CS_SERVICE_TOKEN%" ^
  -d "{\"user_id\":\"player_1001\",\"message\":\"18区 战神无双\",\"metadata\":{\"chat_id\":\"chat_001\",\"channel\":\"mowebchat\"}}"
```

请求体字段：

- `user_id`: 平台内唯一客户 ID
- `message`: 客户文本消息
- `screenshot_b64`: 可选，base64 图片
- `screenshot_ext`: 可选，默认 `png`
- `screenshot_url`: 可选，图片 URL
- `metadata.chat_id`: 可选，后续主动推送消息时使用
- `metadata.channel`: 可选，来源渠道名

返回值包含：

- `reply`
- `sop_state`
- `next_step`
- `bound`
- `codes`
- `timestamp`

### 7.3 Cron 接口

#### `POST /cron/process-followups`

处理 30 分钟 / 1 小时回访。

```bash
curl -X POST http://127.0.0.1:8011/cron/process-followups ^
  -H "X-Game-Cs-Token: %GAME_CS_SERVICE_TOKEN%"
```

#### `POST /cron/next-day-visits`

处理次日回访。

```bash
curl -X POST http://127.0.0.1:8011/cron/next-day-visits ^
  -H "X-Game-Cs-Token: %GAME_CS_SERVICE_TOKEN%"
```

## 8. game_cs Admin API

这部分接口是 `gateway` 管理端实际调用的接口。

### 8.1 知识库和基础管理

#### `POST /admin/index-kb`

导入知识库资源。

```bash
curl -X POST http://127.0.0.1:8011/admin/index-kb ^
  -H "Content-Type: application/json" ^
  -H "X-Game-Cs-Token: %GAME_CS_SERVICE_TOKEN%" ^
  -d "[\"./knowledge/faq.md\",\"./knowledge/sop_guide.md\"]"
```

#### `POST /admin/update-codes`

热更新兑换码。

#### `POST /admin/reset-session?user_id=<user_id>`

重置某个客户会话。

#### `GET /admin/session/{user_id}`

获取兼容旧接口的会话详情。

### 8.2 统计与客户管理

#### `GET /admin/stats`

返回整体统计信息。

```bash
curl http://127.0.0.1:8011/admin/stats ^
  -H "X-Game-Cs-Token: %GAME_CS_SERVICE_TOKEN%"
```

返回重点字段：

- `service.version`
- `service.ai_enabled`
- `summary.total_customers`
- `summary.open_customers`
- `summary.closed_customers`
- `summary.bound_customers`
- `summary.active_24h`
- `summary.pending_human_queries`
- `summary.answered_human_queries`
- `summary.delivered_human_queries`
- `summary.sop_state_counts`

#### `GET /admin/customers`

查询客户列表。

支持参数：

- `limit`
- `include_closed`
- `sop_state`
- `query`

示例：

```bash
curl "http://127.0.0.1:8011/admin/customers?limit=20&include_closed=false" ^
  -H "X-Game-Cs-Token: %GAME_CS_SERVICE_TOKEN%"
```

#### `GET /admin/customer/{user_id}`

查看客户详情、最近消息、相关人工工单。

支持参数：

- `message_limit`

示例：

```bash
curl "http://127.0.0.1:8011/admin/customer/player_1001?message_limit=20" ^
  -H "X-Game-Cs-Token: %GAME_CS_SERVICE_TOKEN%"
```

#### `POST /admin/customer/{user_id}/message`

主动给客户发送一条消息。

```bash
curl -X POST http://127.0.0.1:8011/admin/customer/player_1001/message ^
  -H "Content-Type: application/json" ^
  -H "X-Game-Cs-Token: %GAME_CS_SERVICE_TOKEN%" ^
  -d "{\"reply\":\"请重新登录后再试\"}"
```

注意：

- 只有该客户已经记录了 `metadata.chat_id` / 会话 chat_id，消息才能真正推送出去
- 如果没有 chat_id，接口仍会记录消息，但返回 `delivered=false`

#### `POST /admin/customer/{user_id}/reset`

重置客户 SOP。

```bash
curl -X POST http://127.0.0.1:8011/admin/customer/player_1001/reset ^
  -H "X-Game-Cs-Token: %GAME_CS_SERVICE_TOKEN%"
```

#### `POST /admin/customer/{user_id}/close`

关闭或恢复某个客户的 AI 自动接待。

关闭：

```bash
curl -X POST http://127.0.0.1:8011/admin/customer/player_1001/close ^
  -H "Content-Type: application/json" ^
  -H "X-Game-Cs-Token: %GAME_CS_SERVICE_TOKEN%" ^
  -d "{\"closed\":true}"
```

恢复：

```bash
curl -X POST http://127.0.0.1:8011/admin/customer/player_1001/close ^
  -H "Content-Type: application/json" ^
  -H "X-Game-Cs-Token: %GAME_CS_SERVICE_TOKEN%" ^
  -d "{\"closed\":false}"
```

说明：

- `closed=true` 表示关闭 AI 自动接待，后续用户消息不会再触发自动回复
- 用户消息仍会被记录，并进入人工处理链路
- `closed=false` 表示恢复 AI 自动接待

### 8.3 人工工单管理

#### `GET /admin/human-queries`

查看人工工单。

支持参数：

- `status`

示例：

```bash
curl "http://127.0.0.1:8011/admin/human-queries?status=pending" ^
  -H "X-Game-Cs-Token: %GAME_CS_SERVICE_TOKEN%"
```

#### `POST /admin/human-reply`

回复人工工单。

```bash
curl -X POST http://127.0.0.1:8011/admin/human-reply ^
  -H "Content-Type: application/json" ^
  -H "X-Game-Cs-Token: %GAME_CS_SERVICE_TOKEN%" ^
  -d "{\"user_id\":\"player_1001\",\"query_id\":42,\"reply\":\"请重新登录后再试\"}"
```

如果客户当前有可用 chat_id，回复会尝试直接推送给客户。

## 9. gateway 接口

### 9.1 健康检查

#### `GET /healthz`

```bash
curl http://127.0.0.1:18790/healthz
```

### 9.2 发消息到管理员当前会话

#### `POST /message`

`game_cs` 在需要人工介入时，会通过这个接口把待处理消息发给管理员所在的钉钉 / 飞书会话。

```bash
curl -X POST http://127.0.0.1:18790/message ^
  -H "Content-Type: application/json" ^
  -d "{\"text\":\"【待处理】Query ID: 42\\n用户ID: player_1001\\n问题: 登录失败\"}"
```

也可以显式指定目标：

```bash
curl -X POST http://127.0.0.1:18790/message ^
  -H "Content-Type: application/json" ^
  -d "{\"text\":\"测试消息\",\"channel\":\"dingtalk\",\"chat_id\":\"admin_room_1\"}"
```

## 10. 真实聊天系统接入建议

你后续接入自研聊天系统时，推荐按下面方式做：

1. 聊天系统收到客户消息
2. 转发到 `POST /webhook/game-message`
3. 把客户平台 ID 作为 `user_id`
4. 把当前会话 ID 作为 `metadata.chat_id`
5. 把渠道名写入 `metadata.channel`
6. 将返回的 `reply` 回发给客户

只要 `metadata.chat_id` 存进来了，后续管理端就能：

- 主动给客户发消息
- 回复人工工单后直接下发给客户
- 继续跟踪该客户的 SOP

## 11. 常见启动示例

### 11.1 本地最小联调

终端 1：

```bash
set GAME_CS_SERVICE_TOKEN=test-token
set GAME_CS_AI_ENABLED=true
python -m nanobot.game_cs.service --host 127.0.0.1 --port 8011


```

终端 2：

```bash
set NANOBOT_GAME_CS_ADMIN_BASE_URL=http://127.0.0.1:8011
set NANOBOT_GAME_CS_ADMIN_TOKEN=test-token
python -m nanobot.cli.commands gateway -p 18790
```

### 11.2 手工测试客户消息

```bash
curl -X POST http://127.0.0.1:8011/webhook/game-message ^
  -H "Content-Type: application/json" ^
  -H "X-Game-Cs-Token: test-token" ^
  -d "{\"user_id\":\"player_1001\",\"message\":\"18区 战神无双\",\"metadata\":{\"chat_id\":\"chat_001\",\"channel\":\"mowebchat\"}}"
```

### 11.3 手工测试统计

```bash
curl http://127.0.0.1:8011/admin/stats ^
  -H "X-Game-Cs-Token: test-token"
```

## 12. 测试

本次管理能力相关测试可以直接运行：

```bash
.\.venv\Scripts\python.exe -m pytest tests\game_cs\test_admin_api.py tests\test_game_cs_admin_tool.py tests\test_commands.py -q
```

## 13. 关键文件

- [nanobot/game_cs/service.py](F:/project/nanobot-customer-service/nanobot-game-customer-service/nanobot/game_cs/service.py)
- [nanobot/game_cs/storage.py](F:/project/nanobot-customer-service/nanobot-game-customer-service/nanobot/game_cs/storage.py)
- [nanobot/agent/tools/game_cs_admin.py](F:/project/nanobot-customer-service/nanobot-game-customer-service/nanobot/agent/tools/game_cs_admin.py)
- [nanobot/cli/commands.py](F:/project/nanobot-customer-service/nanobot-game-customer-service/nanobot/cli/commands.py)
- [GAME_CUSTOMER_SERVICE.md](F:/project/nanobot-customer-service/nanobot-game-customer-service/GAME_CUSTOMER_SERVICE.md)

## 14. 当前版本结论

当前代码已经满足下面这条链路：

1. `python -m nanobot.game_cs.service --host 127.0.0.1 --port 8011` 运行客户接待进程
2. `python -m nanobot.cli.commands gateway -p 18790` 运行管理端
3. 管理员通过钉钉 / 飞书机器人进入管理端
4. 管理端可查看统计、查看客户、查看消息、主动发消息、重置会话、关闭客户、查看人工工单、回复人工工单

如果后续你还要继续扩展，我建议下一步加这两项：

- 为管理员约束一套固定指令模板，降低大模型误调用概率
- 给 `game_cs` 增加专门的“客户标签 / 来源渠道 / 最近处理人”字段，便于运营统计
