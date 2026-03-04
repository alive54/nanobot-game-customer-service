"""Optional AI runtime for game customer-service reply and extraction tasks."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nanobot.agent.loop import AgentLoop

logger = logging.getLogger(__name__)

GAME_CS_SYSTEM_PROMPT = (
    "你是《顽石英雄之大楚复古》客服助手。\n"
    "你需要结合提供的知识库片段回答游戏问题，保持简洁和准确。\n"
    "严禁触发代码下发、账号绑定、或任何外部 API 调用，这些由业务系统处理。\n"
    "当任务是信息提取时，只能输出合法 JSON，不得输出 Markdown 代码块或额外文本。"
)


class GameCSAIRuntime:
    def __init__(self, agent: AgentLoop, timeout_ms: int = 5000):
        self._agent: AgentLoop = agent
        self._timeout_ms: int = timeout_ms
        self._loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
        self._thread: threading.Thread = threading.Thread(
            target=self._run_loop, name="game-cs-ai-runtime", daemon=True
        )
        self._thread.start()
        try:
            asyncio.run_coroutine_threadsafe(self._agent._connect_mcp(), self._loop)
        except Exception:
            pass

    def _run_loop(self) -> None:
        try:
            asyncio.set_event_loop(self._loop)
            self._loop.run_forever()
        except Exception:
            pass

    async def ask_agent(
        self,
        session_key: str,
        user_text: str,
        kb_context: list[str],
        history: list[dict],
        timeout_ms: int | None = None,  # noqa: ARG002 - timeout handled by ask_agent_sync's future.result()
    ) -> str | None:
        try:
            parts = [f"【系统要求】\n{GAME_CS_SYSTEM_PROMPT}"]
            if kb_context:
                kb = "\n".join(f"\u2022 {s}" for s in kb_context if s)
                if kb:
                    parts.append(f"【知识库参考】\n{kb}")
            if history:
                recent: list[str] = []
                for item in history[-6:]:
                    content = str(item.get("content") or "").strip()
                    if not content:
                        continue
                    role = "用户" if item.get("role") == "user" else "助手"
                    recent.append(f"{role}: {content}")
                if recent:
                    parts.append("【近期对话】\n" + "\n".join(recent))
            parts.append("【用户提问】\n" + user_text)
            # Note: timeout is handled by future.result() in ask_agent_sync
            response = await self._agent.process_direct(
                "\n\n".join(parts),
                session_key=session_key,
                channel="game_cs",
                chat_id=session_key,
            )
            return response.strip() if response else None
        except Exception:
            logger.exception("ai_runtime.ask_agent failed: session=%s", session_key, exc_info=True)
            return None

    def ask_agent_sync(
        self,
        session_key: str,
        user_text: str,
        kb_context: list[str],
        history: list[dict],
        timeout_ms: int | None = None,
    ) -> str | None:
        try:
            future = asyncio.run_coroutine_threadsafe(
                # Keep sync wrapper compatible even if an older ask_agent
                # implementation is loaded without a timeout_ms parameter.
                self.ask_agent(session_key, user_text, kb_context, history),
                self._loop,
            )
            return future.result(timeout=(timeout_ms or self._timeout_ms) / 1000)
        except Exception:
            logger.exception("ai_runtime.ask_agent_sync failed: session=%s", session_key, exc_info=True)
            return None

    async def extract_info(self, session_key: str, user_text: str, timeout_ms: int | None = None) -> dict | None:
        try:
            prompt = (
                f"【系统要求】\n{GAME_CS_SYSTEM_PROMPT}\n\n"
                "请从以下玩家消息中提取游戏角色信息，仅输出 JSON，不要输出其他内容。\n"
                'JSON 格式：{"area_name": "区服名或null", "role_name": "角色名或null", '
                '"confidence": 0.0到1.0, "need_clarify": true或false}\n'
                "玩家消息：" + user_text
            )
            response = await asyncio.wait_for(
                self._agent.process_direct(
                    prompt,
                    session_key=f"{session_key}:extract",
                    channel="game_cs",
                    chat_id=session_key,
                ),
                timeout=(timeout_ms or self._timeout_ms) / 1000,
            )
            if not response:
                return None
            cleaned = response.strip()
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r"\s*```$", "", cleaned)
            data = json.loads(cleaned.strip())
            required = {"area_name", "role_name", "confidence", "need_clarify"}
            return data if isinstance(data, dict) and required.issubset(data.keys()) else None
        except asyncio.TimeoutError:
            logger.warning("ai_runtime.extract_info timeout: session=%s", session_key)
            return None
        except Exception:
            logger.exception("ai_runtime.extract_info failed: session=%s", session_key, exc_info=True)
            return None

    def extract_info_sync(self, session_key: str, user_text: str, timeout_ms: int | None = None) -> dict | None:
        try:
            future = asyncio.run_coroutine_threadsafe(
                self.extract_info(session_key, user_text, timeout_ms=timeout_ms),
                self._loop,
            )
            return future.result(timeout=(timeout_ms or self._timeout_ms) / 1000)
        except Exception:
            logger.exception("ai_runtime.extract_info_sync failed: session=%s", session_key, exc_info=True)
            return None

    def close(self) -> None:
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:
            pass
        try:
            self._thread.join(timeout=2)
        except Exception:
            pass


def build_runtime(workspace_path=None, timeout_ms: int = 5000) -> "GameCSAIRuntime | None":
    try:
        try:
            from nanobot.config.loader import load_config
            from nanobot.config.schema import Config

            config: Config = load_config()
        except Exception as exc:
            logger.warning("ai_runtime: cannot load nanobot config: %s", exc)
            return None

        model = config.agents.defaults.model
        provider_name = config.get_provider_name(model)
        p = config.get_provider(model)
        if provider_name == "openai_codex" or model.startswith("openai-codex/"):
            from nanobot.providers.openai_codex_provider import OpenAICodexProvider

            provider = OpenAICodexProvider(default_model=model)
        elif provider_name == "custom":
            from nanobot.providers.custom_provider import CustomProvider

            provider = CustomProvider(
                api_key=p.api_key if p else "no-key",
                api_base=config.get_api_base(model) or "http://localhost:8000/v1",
                default_model=model,
            )
        else:
            from nanobot.providers.litellm_provider import LiteLLMProvider
            from nanobot.providers.registry import find_by_name

            spec = find_by_name(provider_name)
            if not model.startswith("bedrock/") and not (p and p.api_key) and not (spec and spec.is_oauth):
                logger.warning("ai_runtime: no API key configured for model %s", model)
                return None
            provider = LiteLLMProvider(
                api_key=p.api_key if p else None,
                api_base=config.get_api_base(model),
                default_model=model,
                extra_headers=p.extra_headers if p else None,
                provider_name=provider_name,
            )

        from pathlib import Path
        ws = Path(workspace_path) if workspace_path else config.workspace_path
        from nanobot.agent.loop import AgentLoop
        from nanobot.bus.queue import MessageBus
        from nanobot.session.manager import SessionManager

        agent = AgentLoop(
            bus=MessageBus(),
            provider=provider,
            workspace=ws,
            model=config.agents.defaults.model,
            temperature=config.agents.defaults.temperature,
            max_tokens=config.agents.defaults.max_tokens,
            max_iterations=5,
            memory_window=20,
            reasoning_effort=config.agents.defaults.reasoning_effort,
            brave_api_key=None,
            web_proxy=None,
            exec_config=None,
            cron_service=None,
            restrict_to_workspace=True,
            session_manager=SessionManager(ws),
            mcp_servers={},
            channels_config=None,
        )
        # 能力禁用
        for name in ("exec", "write_file","read_file", "edit_file", "spawn", "web_search", "web_fetch", "message", "cron"):
            try:
                agent.tools._tools.pop(name, None)
            except Exception:
                pass
        return GameCSAIRuntime(agent=agent, timeout_ms=timeout_ms)
    except Exception:
        logger.exception("ai_runtime: failed to build runtime", exc_info=True)
        return None
