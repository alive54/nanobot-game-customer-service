from __future__ import annotations

import asyncio
import logging
from enum import Enum

logger = logging.getLogger(__name__)


class CollectingIntent(str, Enum):
    PARTIAL_INFO = "partial_info"
    FULL_INFO = "full_info"
    QUESTION = "question"
    IRRELEVANT = "irrelevant"


def _normalize_intent(raw: str) -> CollectingIntent:
    text = (raw or "").strip().lower()
    for intent in CollectingIntent:
        if text == intent.value:
            return intent
    for intent in CollectingIntent:
        if intent.value in text:
            return intent
    return CollectingIntent.IRRELEVANT


async def classify_collecting_intent(
    text: str,
    has_area: bool,
    has_role: bool,
    api_key: str,
    api_base: str = "https://dashscope.aliyuncs.com/compatible-mode/v1",
    model: str = "qwen-turbo",
    timeout_s: float = 5.0,
) -> CollectingIntent:
    if not text.strip() or not api_key:
        return CollectingIntent.IRRELEVANT

    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        from langchain_openai import ChatOpenAI
    except Exception:
        logger.warning("intent classifier dependency missing: langchain_openai")
        return CollectingIntent.IRRELEVANT

    system_prompt = (
        "You classify a message in game customer-service collecting-info stage. "
        "Return only one token from: partial_info, full_info, question, irrelevant. "
        "Do not output any extra text."
    )
    user_prompt = (
        f"has_area={has_area}\n"
        f"has_role={has_role}\n"
        f"text={text}"
    )

    try:
        llm = ChatOpenAI(
            model=model,
            api_key=api_key,
            base_url=api_base,
            temperature=0,
            timeout=timeout_s,
        )
        response = await asyncio.wait_for(
            llm.ainvoke(
                [
                    SystemMessage(content=system_prompt),
                    HumanMessage(content=user_prompt),
                ]
            ),
            timeout=timeout_s,
        )
        content = response.content
        if isinstance(content, list):
            parts: list[str] = []
            for chunk in content:
                if isinstance(chunk, str):
                    parts.append(chunk)
                elif isinstance(chunk, dict):
                    parts.append(str(chunk.get("text", "")))
            content = " ".join(parts)
        return _normalize_intent(str(content))
    except Exception:
        logger.exception("classify_collecting_intent failed")
        return CollectingIntent.IRRELEVANT
