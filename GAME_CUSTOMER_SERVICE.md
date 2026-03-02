# Nanobot 游戏客服改造（OpenViking 知识库）

本改造保持 `nanobot` 主体能力不变，仅新增 `nanobot.game_cs` 扩展模块，实现：

- 游戏客服会话入口（Webhook）
- 用户截图绑定账号（截图 + UID + 区服 + 确认）
- 基于 OpenViking 的知识库检索答复

## 1. 安装依赖

```bash
cd /path/to/nanobot-game-cs
pip install -e ".[game_cs]"
```

## 2. 环境变量

```bash
export GAME_CS_SERVICE_TOKEN="replace-with-strong-token"
export GAME_CS_DB_PATH=".nanobot/game_cs.db"
export GAME_CS_UPLOADS_DIR=".nanobot/game_cs_uploads"
export GAME_CS_OPENVIKING_PATH=".nanobot/openviking_data"
export GAME_CS_OPENVIKING_TARGET_URI="viking://resources/game-kb/"
export GAME_CS_BIND_STEPS="发送截图|发送游戏UID|发送游戏区服|确认绑定"
export GAME_CS_MAX_IMAGE_BYTES=5242880
```

## 3. 灌入知识库

```bash
python scripts/seed_game_kb.py \
  https://example.com/game-faq \
  ./knowledge_docs
```

## 4. 启动客服服务

```bash
python -m nanobot.game_cs.service --host 0.0.0.0 --port 8011
```

## 5. 消息接入协议

接口：`POST /webhook/game-message`  
Header：`X-Game-Cs-Token: $GAME_CS_SERVICE_TOKEN`

请求体示例：

```json
{
  "user_id": "player_1001",
  "message": "我的账号无法登录，UID是12345678，亚服一区",
  "screenshot_b64": "",
  "screenshot_ext": "png"
}
```

返回：

- `reply`: 客服回复
- `next_step`: 当前引导步骤
- `bound`: 是否绑定完成

## 6. 典型流程

1. 用户首次对话，客服要求上传个人主页截图。
2. 收到截图后，提示输入 UID。
3. 提示输入区服。
4. 用户回复“确认绑定”，状态置为 `bound`。
5. 后续问题走 OpenViking 检索返回知识库答案。
