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

# 基础身份提示词，供所有子任务共用
GAME_CS_BASE_PROMPT = (
    "以下是你当前所要遵守的规则,如果与上述规则冲突，优先遵循下面的规则：\n"
    "你是《顽石英雄之大楚复古》客服助手。\n"
    "你需要结合提供的知识库片段回答游戏问题，保持简洁和准确。\n"
)

# 客服回复任务专用提示词（ask_agent 使用）
GAME_CS_SYSTEM_PROMPT = (
    GAME_CS_BASE_PROMPT
    + "\n"
    + "【当前任务目标】\n"
    + "根据知识库内容和对话历史，判断你是否能够自信地回答玩家的问题：\n"
    + "- 如果知识库有足够相关内容，置信度高，则直接给出准确回复（need_human=false）。\n"
    + "- 如果知识库内容与问题无关、置信度低，或问题涉及账号/充值/封号等需人工处理的事项，则标记需要人工介入（need_human=true），并给出一个安抚性的临时回复，安抚回复不要再问客户问题。\n"
    + "\n"
    + "【输出格式】\n"
    + "仅输出 JSON，不要输出其他任何内容：\n"
    + '{"need_human": true或false, "reply": "回复内容", "reason": "简短说明为何需要或不需要人工"}'
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
    ) -> dict | None:
        try:
            sys_parts = [GAME_CS_SYSTEM_PROMPT]
            if kb_context:
                kb = "\n".join(f"\u2022 {s}" for s in kb_context if s and s.strip())
                if kb:
                    sys_parts.append(f"【知识库参考】\n{kb}")
            extra_sys = "\n\n".join(sys_parts)
            # Note: timeout is handled by future.result() in ask_agent_sync
            response = await self._agent.process_direct(
                user_text,
                session_key=session_key,
                channel="game_cs",
                chat_id=session_key,
                extra_system_prompt=extra_sys,
            )
            if not response:
                return None
            # 尝试解析 JSON 结构
            cleaned = response.strip()
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r"\s*```$", "", cleaned)
            try:
                data = json.loads(cleaned.strip())
                if isinstance(data, dict) and "need_human" in data and "reply" in data:
                    return data
                # JSON 解析成功但格式不对，包装成不需要人工的结构
                return {"need_human": False, "reply": cleaned.strip(), "reason": "raw_response"}
            except json.JSONDecodeError:
                # 模型未按 JSON 格式输出，将原文作为回复内容
                return {"need_human": False, "reply": cleaned.strip(), "reason": "raw_response"}
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
        result = self.ask_agent_structured_sync(
            session_key,
            user_text,
            kb_context,
            history,
            timeout_ms=timeout_ms,
        )
        if not result:
            return None
        reply = str(result.get("reply", "")).strip()
        return reply or None

    def ask_agent_structured_sync(
        self,
        session_key: str,
        user_text: str,
        kb_context: list[str],
        history: list[dict],
        timeout_ms: int | None = None,
    ) -> dict | None:
        try:
            future = asyncio.run_coroutine_threadsafe(
                # Keep sync wrapper compatible even if an older ask_agent
                # implementation is loaded without a timeout_ms parameter.
                self.ask_agent(session_key, user_text, kb_context, history),
                self._loop,
            )
            return future.result(timeout=(timeout_ms or self._timeout_ms) / 1000)
        except Exception:
            logger.exception(
                "ai_runtime.ask_agent_structured_sync failed: session=%s",
                session_key,
                exc_info=True,
            )
            return None

    async def extract_info(
        self,
        session_key: str,
        user_text: str,
        timeout_ms: int | None = None,
        *,
        area: str | None = None,
        role: str | None = None,
    ) -> dict | str | None:
        try:
            # 构建已收集信息的提示
            collected_info = []
            if area:
                collected_info.append(f"区服: {area}")
            if role:
                collected_info.append(f"角色名: {role}")
            collected_hint = (
                f"已收集信息: {', '.join(collected_info)}"
                if collected_info
                else "尚未收集到角色信息"
            )

            # 信息提取任务使用独立提示词，避免与回复任务的 JSON 格式冲突
            extract_sys = (
                GAME_CS_BASE_PROMPT
                + "\n"
                + "【当前任务目标】\n"
                + "引导玩家给出游戏角色信息，方便绑定。\n"
                + f"{collected_hint}\n"
                + "请从玩家消息中提取游戏角色信息和区服名（区服格式可能是'几区'、'黑暗几区'或纯数字）。\n"
                + "\n"
                + "【输出格式】\n"
                + "仅输出 JSON，不要输出其他内容：\n"
                + '{"area_name": "区服名或null", "role_name": "角色名或null", "confidence": 0.0到1.0, "need_clarify": true或false}'
            )
            response = await asyncio.wait_for(
                self._agent.process_direct(
                    user_text,
                    session_key=f"{session_key}:extract",
                    channel="game_cs",
                    chat_id=session_key,
                    extra_system_prompt=extract_sys,
                ),
                timeout=(timeout_ms or self._timeout_ms) / 1000,
            )
            if not response:
                return None

            cleaned = response.strip()
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r"\s*```$", "", cleaned)
            try:
                data = json.loads(cleaned.strip())
                required = {"area_name", "role_name", "confidence", "need_clarify"}
                return data if isinstance(data, dict) and required.issubset(data.keys()) else None
            except json.JSONDecodeError:
                # 如果不是 JSON 格式，直接返回原文
                return None
        except asyncio.TimeoutError:
            logger.warning("ai_runtime.extract_info timeout: session=%s", session_key)
            return None
        except Exception:
            logger.exception(
                "ai_runtime.extract_info failed: session=%s", session_key, exc_info=True
            )
            return None

    def extract_info_sync(
        self,
        session_key: str,
        user_text: str,
        timeout_ms: int | None = None,
        *,
        area: str | None = None,
        role: str | None = None,
    ) -> dict | str | None:
        try:
            future = asyncio.run_coroutine_threadsafe(
                self.extract_info(
                    session_key, user_text, timeout_ms=timeout_ms, area=area, role=role
                ),
                self._loop,
            )
            return future.result(timeout=(timeout_ms or self._timeout_ms) / 1000)
        except Exception:
            logger.exception(
                "ai_runtime.extract_info_sync failed: session=%s", session_key, exc_info=True
            )
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
            if (
                not model.startswith("bedrock/")
                and not (p and p.api_key)
                and not (spec and spec.is_oauth)
            ):
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
        # 禁用所有工具
        try:
            agent.tools._tools.clear()
        except Exception:
            pass
        runtime = GameCSAIRuntime(agent=agent, timeout_ms=timeout_ms)
        try:
            from .vision import set_runtime_loop

            set_runtime_loop(runtime._loop)
        except Exception:
            pass
        return runtime
    except Exception:
        logger.exception("ai_runtime: failed to build runtime", exc_info=True)
        return None
