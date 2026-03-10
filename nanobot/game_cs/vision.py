from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_RESULT = {"area_name": None, "role_name": None, "confidence": 0.0}
_RUNTIME_LOOP: asyncio.AbstractEventLoop | None = None


def set_runtime_loop(loop: asyncio.AbstractEventLoop | None) -> None:
    global _RUNTIME_LOOP
    _RUNTIME_LOOP = loop


def _as_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for chunk in content:
            if isinstance(chunk, str):
                parts.append(chunk)
            elif isinstance(chunk, dict):
                parts.append(str(chunk.get("text", "")))
        return " ".join(parts)
    return str(content)


def _parse_result(text: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        data = json.loads(cleaned)
    except Exception:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            return dict(_DEFAULT_RESULT)
        try:
            data = json.loads(match.group(0))
        except Exception:
            return dict(_DEFAULT_RESULT)

    if not isinstance(data, dict):
        return dict(_DEFAULT_RESULT)

    area_name = data.get("area_name")
    role_name = data.get("role_name")
    confidence = data.get("confidence", 0.0)

    if area_name is not None:
        area_name = str(area_name).strip() or None
    if role_name is not None:
        role_name = str(role_name).strip() or None

    try:
        confidence_f = float(confidence)
    except Exception:
        confidence_f = 0.0
    confidence_f = max(0.0, min(1.0, confidence_f))

    return {
        "area_name": area_name,
        "role_name": role_name,
        "confidence": confidence_f,
    }


async def extract_info_from_image(
    image_url: str | None = None,
    image_b64: str | None = None,
    image_ext: str = "png",
    api_key: str | None = None,
    api_base: str = "https://dashscope.aliyuncs.com/compatible-mode/v1",
    model: str = "qwen3-vl-plus-2025-12-19",
    timeout_s: float = 30.0,
) -> dict:
    if not api_key:
        return dict(_DEFAULT_RESULT)

    image_url_or_data_url = None
    if image_b64:
        image_url_or_data_url = f"data:image/{image_ext};base64,{image_b64}"
    elif image_url:
        image_url_or_data_url = image_url

    if not image_url_or_data_url:
        return dict(_DEFAULT_RESULT)

    try:
        from langchain_openai import ChatOpenAI
    except Exception:
        logger.warning("vision dependency missing: langchain_openai")
        return dict(_DEFAULT_RESULT)

    prompt = (
        "请从这张游戏截图中提取：\n"
        "1. 区服名（通常是X区或大区X区格式）\n"
        "2. 角色名（玩家角色名称）\n"
        "3. 如果没有找到区服信息，返回null\n"
        "仅输出JSON，不得输出其他内容：\n"
        '{"area_name": "区服名或null", "role_name": "角色名或null", "confidence": 0.0到1.0}'
    )

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": image_url_or_data_url}},
            ],
        }
    ]

    async def _invoke() -> dict:
        llm = ChatOpenAI(
            model=model,
            api_key=api_key,
            base_url=api_base,
            temperature=0,
            timeout=timeout_s,
        )
        response = await llm.ainvoke(messages)
        return _parse_result(_as_text(response.content))

    try:
        return await asyncio.wait_for(_invoke(), timeout=timeout_s)
    except Exception:
        logger.exception("extract_info_from_image failed")
        return dict(_DEFAULT_RESULT)


def extract_info_from_image_sync(
    loop: asyncio.AbstractEventLoop | None = None,
    **kwargs,
) -> dict:
    timeout_s = float(kwargs.get("timeout_s", 30.0))
    target_loop = loop or _RUNTIME_LOOP

    try:
        if target_loop and target_loop.is_running():
            future = asyncio.run_coroutine_threadsafe(
                extract_info_from_image(**kwargs),
                target_loop,
            )
            return future.result(timeout=timeout_s)

        try:
            asyncio.get_running_loop()
            has_running_loop = True
        except RuntimeError:
            has_running_loop = False

        if not has_running_loop:
            return asyncio.run(
                asyncio.wait_for(extract_info_from_image(**kwargs), timeout=timeout_s)
            )

        result_box: dict[str, dict] = {}
        err_box: dict[str, BaseException] = {}

        def _runner() -> None:
            try:
                result_box["result"] = asyncio.run(
                    asyncio.wait_for(extract_info_from_image(**kwargs), timeout=timeout_s)
                )
            except BaseException as exc:  # noqa: BLE001
                err_box["error"] = exc

        thread = threading.Thread(target=_runner, daemon=True)
        thread.start()
        thread.join(timeout=timeout_s + 1.0)
        if thread.is_alive():
            raise TimeoutError("vision extraction timeout")
        if "error" in err_box:
            raise err_box["error"]
        return result_box.get("result", dict(_DEFAULT_RESULT))

    except Exception:
        return dict(_DEFAULT_RESULT)
