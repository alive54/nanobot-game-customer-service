# Nanobot 大楚复古智能客服

本文档描述 `nanobot.game_cs` 扩展模块的完整使用方式。  
该模块在 NanoBot 主体能力基础上，实现了面向《顽石英雄之大楚复古》的 SOP 驱动智能客服。

---

## 1. 核心特性

| 特性 | 说明 |
|------|------|
| **SOP 状态机** | GREETING → COLLECTING_INFO → VALIDATING → BINDING → SENDING_CODE → 回访 |
| **三要素提取** | 从自由文本中自动提取 area_name（几区）+ role_name（角色名），game_name 默认填充 |
| **四码发放** | 绑定成功后自动发送每日打卡码、天选码、通码、供宗号 |
| **回访调度** | 30分钟签到引导 + 1小时裂变引导，通过 `/cron/process-followups` 触发 |
| **次日回访** | 通过 `/cron/next-day-visits` 触发，询问游戏情况 |
| **知识库检索** | 绑定后用户问题通过 OpenViking find() / search() 实时检索答案 |
| **会话记忆** | 对话结束后异步 commit 至 OpenViking，积累用户画像 |
| **多性格** | 支持 lively / professional / steady / humorous 四种客服性格 |
| **Mock 模式** | 默认开启，无需真实游戏 API 即可演示完整流程 |

---

## 2. 安装依赖

```bash
pip install -e ".[game_cs]"
```

`game_cs` extra 包含：`openviking`, `fastapi`, `uvicorn[standard]`。

---

## 3. 环境变量

### 3.1 必填

| 变量 | 说明 | 示例 |
|------|------|------|
| `GAME_CS_SERVICE_TOKEN` | 所有接口的认证令牌 | `replace-with-strong-token` |

### 3.2 存储

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `GAME_CS_DB_PATH` | `.nanobot/game_cs.db` | SQLite 数据库路径 |
| `GAME_CS_UPLOADS_DIR` | `.nanobot/game_cs_uploads` | 截图上传目录 |

### 3.3 OpenViking 知识库

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `GAME_CS_OPENVIKING_PATH` | `.nanobot/openviking_data` | 嵌入式 OpenViking 数据目录 |
| `GAME_CS_OPENVIKING_TARGET_URI` | `viking://resources/` | 知识库检索根 URI |

### 3.4 游戏参数

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `GAME_CS_DEFAULT_GAME_NAME` | `顽石英雄之大楚复古` | 用户未提供时的默认游戏名 |
| `GAME_CS_PERSONALITY` | `lively` | 客服性格：lively / professional / steady / humorous |
| `GAME_CS_GAME_API_BASE` | `（空）` | 游戏服务器 API 基础 URL（生产环境必填） |
| `GAME_CS_MOCK_API` | `true` | `true` = 跳过真实验证/绑定 API（演示模式） |

### 3.5 每日兑换码（每日由运营更新）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `GAME_CS_CODE_DAILY_CHECKIN` | `DCXXX` | 每日打卡码（每天更新） |
| `GAME_CS_CODE_LUCKY_DRAW` | `TXYYY` | 天选码（每天更新） |
| `GAME_CS_CODE_UNIVERSAL` | `ws888` | 通码（相对固定） |
| `GAME_CS_CODE_GUILD` | `FgYdqf6` | 供宗号（相对固定） |

### 3.6 回访时间配置

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `GAME_CS_FOLLOWUP_30M_DELAY` | `1800` | 30分钟回访延迟（秒） |
| `GAME_CS_FOLLOWUP_1H_DELAY` | `3600` | 1小时裂变回访延迟（秒） |
| `GAME_CS_MAX_COLLECT_RETRIES` | `3` | 信息收集最大重试次数 |

### 3.7 其他

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `GAME_CS_MAX_IMAGE_BYTES` | `5242880` | 截图最大字节数（5MB） |

---

## 4. 启动服务

```bash
# 默认监听 0.0.0.0:8011
nanobot-game-cs

# 指定地址和端口
nanobot-game-cs --host 127.0.0.1 --port 8020

# 开发模式（自动重载）
nanobot-game-cs --reload
```

或直接以模块方式运行：

```bash
python -m nanobot.game_cs.service --host 0.0.0.0 --port 8011
```

---

## 5. SOP 流程说明

```
用户进入
   │
   ▼
GREETING           ← 机器人主动发送开场白，索要几区+角色名
   │ 用户回复
   ▼
COLLECTING_INFO    ← 解析 area_name / role_name（最多重试3次）
   │ 解析成功
   ▼
VALIDATING         ← 调用游戏 API 验证角色（mock 模式直接通过）
   │ 验证通过
   ▼
BINDING            ← 绑定用户ID与游戏角色
   │ 绑定成功
   ▼
SENDING_CODE       ← 发送4个兑换码，记录 codes_sent_at
   │ 发送完成
   ▼
FOLLOW_UP_PENDING  ← 等待定时器触发
   │
   ├─ 30分钟 ──► FOLLOW_UP_30MIN   ← 签到引导（连续3天抽奖）
   │
   └─ 1小时  ──► FOLLOW_UP_1HOUR   ← 裂变引导（邀请朋友抽路费转盘）
                      │
                      ▼
                   SILENT           ← 沉默期
                      │
                      ├─ 24小时 ──► NEXT_DAY_VISIT  ← 次日回访
                      │
                      └─ 3天无响应► REACTIVATION    ← 召回推送
```

**用户主动发消息时**：回访定时任务将被取消，机器人直接基于 OpenViking 知识库回答问题。

---

## 6. API 接口

所有接口均需在 Header 中携带：`X-Game-Cs-Token: <GAME_CS_SERVICE_TOKEN>`

### 6.1 Webhook（核心入口）

#### POST `/webhook/game-message`

接收玩家消息，返回 SOP 驱动的机器人回复。

**请求体**

```json
{
  "user_id": "player_1001",
  "message": "裁决18区，角色叫战神无双",
  "screenshot_b64": null,
  "screenshot_ext": "png",
  "screenshot_url": null,
  "metadata": {}
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `user_id` | string | ✅ | 平台唯一用户 ID（字母/数字/下划线/连字符，≤64字符） |
| `message` | string | — | 用户文本消息（为空时触发开场白） |
| `screenshot_b64` | string | — | Base64 截图（OCR 辅助，可选） |
| `screenshot_ext` | string | — | 截图格式：png / jpg / jpeg / webp |
| `screenshot_url` | string | — | 截图 URL（与 screenshot_b64 二选一） |

**响应体**

```json
{
  "status": "ok",
  "reply": "哥~ 兑换码来啦！记得每天找小妹打卡哦😉\n\n每日打卡：DC001\n天选：TX002\n通码：ws888\n供宗号：FgYdqf6\n\n有效期24小时，有啥问题随时喊我~🌹",
  "sop_state": "follow_up_pending",
  "next_step": null,
  "bound": true,
  "codes": {
    "daily_checkin": "DC001",
    "lucky_draw":    "TX002",
    "universal":     "ws888",
    "guild":         "FgYdqf6"
  },
  "timestamp": "2026-03-03T10:00:00Z"
}
```

| 字段 | 说明 |
|------|------|
| `sop_state` | 当前 SOP 状态（见第5节） |
| `bound` | `true` 表示已完成角色绑定 |
| `codes` | 非空时表示本次刚发放了兑换码 |
| `next_step` | 引导用户下一步操作的提示（信息收集阶段）|

### 6.2 Cron 接口（由外部调度器调用）

#### POST `/cron/process-followups`

检查并触发到期的 30分钟 / 1小时回访。建议每分钟调用一次。

**响应体**

```json
{
  "followup_30m": [
    {"user_id": "player_1001", "message": "哥 跟您说个好事..."}
  ],
  "followup_1h": [
    {"user_id": "player_1002", "message": "哥，先给您登记上了..."}
  ],
  "processed_at": "2026-03-03T10:30:00Z"
}
```

调用方需将返回的消息通过对应平台（MoChat / Telegram / 企微等）推送给用户。

#### POST `/cron/next-day-visits`

检查并触发到期的次日回访（codes_sent_at + 24小时）。建议每天定时调用。

```json
{
  "next_day_visits": [
    {"user_id": "player_1001", "message": "老板下午好 打扰一下下..."}
  ],
  "processed_at": "2026-03-04T14:00:00Z"
}
```

### 6.3 Admin 接口

#### POST `/admin/index-kb`

将本地文件或 URL 索引到 OpenViking 知识库。

```bash
curl -X POST http://localhost:8011/admin/index-kb \
  -H "X-Game-Cs-Token: $GAME_CS_SERVICE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '["./knowledge/faq.md", "./knowledge/sop_guide.md"]'
```

#### POST `/admin/update-codes`

热更新当日兑换码（无需重启服务）。

```bash
curl -X POST "http://localhost:8011/admin/update-codes" \
  -H "X-Game-Cs-Token: $GAME_CS_SERVICE_TOKEN" \
  -G \
  --data-urlencode "daily_checkin=DCnew1" \
  --data-urlencode "lucky_draw=TXnew2" \
  --data-urlencode "universal=ws888" \
  --data-urlencode "guild=FgYdqf6"
```

#### POST `/admin/reset-session`

重置用户 SOP 会话回 GREETING（用于测试或重新注册）。

```bash
curl -X POST "http://localhost:8011/admin/reset-session?user_id=player_1001" \
  -H "X-Game-Cs-Token: $GAME_CS_SERVICE_TOKEN"
```

#### GET `/admin/session/{user_id}`

查看用户当前 SOP 状态。

```bash
curl http://localhost:8011/admin/session/player_1001 \
  -H "X-Game-Cs-Token: $GAME_CS_SERVICE_TOKEN"
```

```json
{
  "user_id": "player_1001",
  "sop_state": "follow_up_pending",
  "game_name": "顽石英雄之大楚复古",
  "area_name": "裁决18区",
  "role_name": "战神无双",
  "is_bound": true,
  "codes_sent_at": "2026-03-03T10:00:00Z",
  "follow_up_30m_sent": false,
  "follow_up_1h_sent": false,
  "next_day_visited": false,
  "created_at": "2026-03-03T09:58:00Z",
  "updated_at": "2026-03-03T10:00:00Z"
}
```

---

## 7. 典型对话示例

```
[系统]  用户首次进入（message 为空）
[Bot]   Hello! 哥~ 来啦！是玩顽石英雄的老板吧😊
        告诉小妹您在哪个大区、几区，角色名叫啥，小妹马上给您安排兑换码~

[User]  裁决18区，角色叫战神无双

[Bot]   哥~ 兑换码来啦！记得每天找小妹打卡哦😉

        每日打卡：DC001
        天选：TX002
        通码：ws888
        供宗号：FgYdqf6

        有效期24小时，有啥问题随时喊我~🌹

— 30分钟后（由 /cron/process-followups 触发）—

[Bot]   哥 跟您说个好事[愉快]连续找小妹签到3天，有一次抽奖机会，
        最高免费抽充值赞助，小妹特地给您申请的，要帮您登记嘛~[玫瑰]

— 1小时后（由 /cron/process-followups 触发）—

[Bot]   哥，先给您登记上了[爱心]您喊朋友一起来玩，小妹给您安排抽路费转盘，
        最高拿1000真冲[玫瑰]人多热闹才有意思，您身边有爱玩传奇的朋友嘛？

— 用户次日发消息 —

[User]  装备怎么提升

[Bot]   找到啦哥！知识库里有这些参考：
        • [0.88] 装备强化指南：通过锻造系统可将装备强化至+15级…
        • [0.82] 套装获取攻略：参与每日副本可获得稀有套装碎片…
```

---

## 8. 灌入知识库

```bash
# 方式一：启动后通过 Admin API 索引
curl -X POST http://localhost:8011/admin/index-kb \
  -H "X-Game-Cs-Token: $GAME_CS_SERVICE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '["./knowledge_docs/", "https://game.example.com/faq"]'

# 方式二：使用原有脚本（如有）
python scripts/seed_game_kb.py ./knowledge_docs/
```

知识库支持的格式：Markdown、PDF、HTML、TXT、JSON/YAML、图片（需 OpenViking VLM 配置）。

---

## 9. 定时任务集成

### 使用系统 cron

```bash
# crontab -e
# 每分钟处理回访任务
* * * * * curl -s -X POST http://localhost:8011/cron/process-followups \
  -H "X-Game-Cs-Token: $GAME_CS_SERVICE_TOKEN" >> /var/log/game_cs_cron.log 2>&1

# 每天下午2点触发次日回访
0 14 * * * curl -s -X POST http://localhost:8011/cron/next-day-visits \
  -H "X-Game-Cs-Token: $GAME_CS_SERVICE_TOKEN" >> /var/log/game_cs_cron.log 2>&1
```

### 每日更新兑换码

```bash
# 每天零点由运营/CI 脚本调用
curl -X POST "http://localhost:8011/admin/update-codes" \
  -H "X-Game-Cs-Token: $GAME_CS_SERVICE_TOKEN" \
  -G \
  --data-urlencode "daily_checkin=$(cat /secrets/code_daily)" \
  --data-urlencode "lucky_draw=$(cat /secrets/code_lucky)" \
  --data-urlencode "universal=ws888" \
  --data-urlencode "guild=FgYdqf6"
```

---

## 10. 生产环境配置

```bash
# 关闭 mock 模式，接入真实游戏 API
export GAME_CS_MOCK_API=false
export GAME_CS_GAME_API_BASE=http://game-api.internal

# 使用专业性格
export GAME_CS_PERSONALITY=professional

# OpenViking 使用 HTTP 模式连接独立服务（可选）
# 若使用嵌入式模式，只需设置 GAME_CS_OPENVIKING_PATH 即可
export GAME_CS_OPENVIKING_TARGET_URI=viking://resources/dachu-cs/

# 强令牌
export GAME_CS_SERVICE_TOKEN=$(openssl rand -hex 32)
```

---

## 11. Docker 快速启动

```yaml
# docker-compose.yml（片段）
services:
  game-cs:
    image: nanobot-game-cs:latest
    ports:
      - "8011:8011"
    environment:
      GAME_CS_SERVICE_TOKEN: "replace-with-strong-token"
      GAME_CS_DB_PATH: "/data/game_cs.db"
      GAME_CS_OPENVIKING_PATH: "/data/openviking_data"
      GAME_CS_CODE_DAILY_CHECKIN: "DC001"
      GAME_CS_CODE_LUCKY_DRAW: "TX002"
      GAME_CS_CODE_UNIVERSAL: "ws888"
      GAME_CS_CODE_GUILD: "FgYdqf6"
      GAME_CS_MOCK_API: "true"
    volumes:
      - game-cs-data:/data

volumes:
  game-cs-data:
```

---

## 12. 接口文档

服务启动后，访问 Swagger UI：

```
http://localhost:8011/docs
```

ReDoc 文档：

```
http://localhost:8011/redoc
```
