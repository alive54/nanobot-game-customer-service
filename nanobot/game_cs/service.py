from __future__ import annotations

import argparse
import asyncio
import base64
import dataclasses
import hashlib
import json
import logging
import re
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import httpx
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Response
from pydantic import BaseModel

from nanobot.cron.service import CronService
from nanobot.cron.types import CronJob, CronSchedule

from ..utils.time import now_datetime, now_iso
from .config import GameCSConfig
from .human_escalation import forward_to_admin
from .intent import CollectingIntent, classify_collecting_intent
from .models import GameMessageIn, GameReply, SOPState
from .openviking_kb import OpenVikingKB
from .storage import GameCSStore, SOPSessionState
from .vision import extract_info_from_image_sync

try:
    from .channel_bridge import GameCSChannelBridge
except Exception:  # pragma: no cover - optional
    GameCSChannelBridge = None  # type: ignore[misc,assignment]

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SAFE_USER_ID = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
ALLOWED_IMAGE_EXTS = {"png", "jpg", "jpeg", "webp"}

CLARIFY_PROMPTS = [
    "请再告诉我一下区服和角色名，例如：18区 战神无双。",
    "我还缺少完整信息：区服 + 角色名，请一次发我。",
    "如果方便的话可以发截图，或直接按“18区 战神无双”格式发送。",
]

_TMPL: dict[str, dict[str, str]] = {
    SOPState.GREETING: {
        "lively": "你好呀，我是你的游戏客服助手。告诉我区服和角色名，我来帮你处理。",
        "professional": "您好，请提供区服和角色名，我将为您处理。",
        "steady": "收到，麻烦提供区服和角色名。",
        "humorous": "上号前先对个暗号：区服 + 角色名。",
    },
    SOPState.COLLECTING_INFO: {
        "lively": "{retry_prompt}",
        "professional": "{retry_prompt}",
        "steady": "{retry_prompt}",
        "humorous": "{retry_prompt}",
    },
    SOPState.SENDING_CODE: {
        "lively": "兑换码如下：\n\n{codes_block}",
        "professional": "以下是您的兑换码：\n\n{codes_block}",
        "steady": "给您发码：\n\n{codes_block}",
        "humorous": "开箱时间到，兑换码请收好：\n\n{codes_block}",
    },
    SOPState.FOLLOW_UP_30MIN: {
        "lively": "提醒一下：连续签到有额外奖励，要我帮你登记吗？",
        "professional": "温馨提示：连续签到可获得额外奖励，是否需要为您登记？",
        "steady": "提醒您，连续签到有奖励，需要我帮您登记吗？",
        "humorous": "30分钟回访：签到别断，奖励会更香。要登记吗？",
    },
    SOPState.FOLLOW_UP_1HOUR: {
        "lively": "如果你有朋友也在玩，可以一起参加邀请活动。",
        "professional": "若您有邀请需求，可参与邀请活动获得额外奖励。",
        "steady": "有需要的话也可以参加邀请活动。",
        "humorous": "一小时回访：组队拉人有活动，想了解我就发你。",
    },
    SOPState.NEXT_DAY_VISIT: {
        "lively": "昨天体验怎么样？今天也可以继续找我领福利。",
        "professional": "您好，今日仍可领取福利，如需帮助请随时联系。",
        "steady": "今天也能领福利，有需要就叫我。",
        "humorous": "次日回访：福利还在，别让它过期啦。",
    },
    SOPState.EXCEPTION: {
        "lively": "这边需要人工客服进一步处理，我先帮你转接。",
        "professional": "该问题需人工客服处理，已为您转接。",
        "steady": "这个问题我先转人工同事继续跟进。",
        "humorous": "这个问题需要真人高手，我先帮你召唤。",
    },
}

_AREA_STOP_PREFIX = {"我", "在", "是", "的", "第"}
_ROLE_STOPWORDS = {
    "你好",
    "谢谢",
    "好的",
    "没有",
    "问题",
    "就是",
    "这个",
    "那个",
}
_ROLE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?:角色名|角色|昵称|名字)\s*[：:是叫为]?\s*([\w\u4e00-\u9fff]{2,16})"),
    re.compile(r"叫(?:做)?\s*([\w\u4e00-\u9fff]{2,16})"),
]


class HumanReplyIn(BaseModel):
    user_id: str
    query_id: int
    reply: str


class AdminMessageIn(BaseModel):
    reply: str


class AdminCloseSessionIn(BaseModel):
    closed: bool = True


class KBQAIn(BaseModel):
    question: str
    answer: str
    category: str = "faq"


def _now() -> datetime:
    return now_datetime()


def _now_iso() -> str:
    return now_iso()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _safe_user_id(user_id: str) -> str:
    if not SAFE_USER_ID.fullmatch(user_id):
        raise HTTPException(status_code=400, detail="invalid user_id")
    return user_id


def _require_non_empty_text(value: str, field_name: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail=f"{field_name} cannot be empty")
    return cleaned


def _resolve_ext(ext: str) -> str:
    clean = ext.strip(".").lower()
    if clean not in ALLOWED_IMAGE_EXTS:
        raise HTTPException(status_code=400, detail="invalid screenshot_ext")
    return clean


def _tmpl(state: SOPState, personality: str, **kwargs: str) -> str:
    templates = _TMPL.get(state, {})
    text = templates.get(personality) or templates.get("lively", "")
    for key, value in kwargs.items():
        text = text.replace(f"{{{key}}}", value)
    return text


def _build_codes_block(cfg: GameCSConfig) -> str:
    return (
        f"每日打卡：{cfg.code_daily_checkin}\n"
        f"天选：{cfg.code_lucky_draw}\n"
        f"通码：{cfg.code_universal}\n"
        f"供宗号：{cfg.code_guild}"
    )


def _codes_dict(cfg: GameCSConfig) -> dict[str, str]:
    return {
        "daily_checkin": cfg.code_daily_checkin,
        "lucky_draw": cfg.code_lucky_draw,
        "universal": cfg.code_universal,
        "guild": cfg.code_guild,
    }


def _is_ai_auto_reply_enabled(session: SOPSessionState) -> bool:
    return not session.is_closed


def _extract_area_name(text: str) -> str | None:
    m = re.search(r"第?\s*(\d{1,4})\s*区", text)
    if m:
        return f"{m.group(1)}区"

    m = re.search(r"第?\s*([一二三四五六七八九十百]+)\s*区", text)
    if m:
        return f"{m.group(1)}区"

    m = re.search(r"([\u4e00-\u9fff]{0,4}\d{1,4}区)", text)
    if m:
        area = m.group(1)
        while area and area[0] in _AREA_STOP_PREFIX:
            area = area[1:]
        return area or None

    return None


def _extract_role_name(text: str, area_name: str | None = None) -> str | None:
    working = text
    if area_name:
        working = working.replace(area_name, " ")
        simple_candidate = re.sub(r"[\s，。！？、,.!?]", "", working).strip()
        if (
            simple_candidate
            and simple_candidate not in _ROLE_STOPWORDS
            and 2 <= len(simple_candidate) <= 16
            and re.fullmatch(r"[\w\u4e00-\u9fff]{2,16}", simple_candidate)
        ):
            return simple_candidate

    for pat in _ROLE_PATTERNS:
        m = pat.search(working)
        if not m:
            continue
        candidate = m.group(1).strip()
        candidate = re.sub(r"[，。！？、,.!?]+$", "", candidate)
        if candidate and candidate not in _ROLE_STOPWORDS and 2 <= len(candidate) <= 16:
            return candidate

    fallback = re.sub(r"[\s，。！？、,.!?]", "", working)
    fallback = re.sub(r"^(我|在|是|叫)+", "", fallback)
    if (
        fallback
        and fallback not in _ROLE_STOPWORDS
        and not re.search(r"\d", fallback)
        and 2 <= len(fallback) <= 12
        and "区" not in fallback
    ):
        return fallback

    return None


def _parse_user_info(text: str) -> tuple[str | None, str | None]:
    area = _extract_area_name(text)
    role = _extract_role_name(text, area_name=area)
    return area, role


def _load_screenshot_bytes(cfg: GameCSConfig, payload: GameMessageIn) -> bytes | None:
    if payload.screenshot_b64:
        if len(payload.screenshot_b64) > cfg.max_image_bytes * 2:
            raise HTTPException(status_code=413, detail="screenshot too large")
        try:
            raw = base64.b64decode(payload.screenshot_b64, validate=True)
        except Exception as exc:
            raise HTTPException(status_code=400, detail="invalid screenshot_b64") from exc
        if len(raw) > cfg.max_image_bytes:
            raise HTTPException(status_code=413, detail="screenshot too large")
        return raw

    if payload.screenshot_url:
        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.get(payload.screenshot_url)
                resp.raise_for_status()
                raw = resp.content
        except Exception as exc:
            raise HTTPException(status_code=400, detail="cannot fetch screenshot_url") from exc
        if len(raw) > cfg.max_image_bytes:
            raise HTTPException(status_code=413, detail="screenshot too large")
        return raw

    return None


def _persist_screenshot(cfg: GameCSConfig, payload: GameMessageIn) -> str | None:
    raw = _load_screenshot_bytes(cfg, payload)
    if raw is None:
        return None

    cfg.uploads_dir.mkdir(parents=True, exist_ok=True)
    user_id = _safe_user_id(payload.user_id)
    ext = _resolve_ext(payload.screenshot_ext)
    digest = hashlib.sha256(raw).hexdigest()[:16]
    path = cfg.uploads_dir / f"{user_id}_{digest}.{ext}"
    path.write_bytes(raw)
    return str(path)


def _ai_run_sync(runtime, method_name: str, *args, **kwargs):
    if runtime is None:
        return None
    try:
        fn = getattr(runtime, method_name)
        return fn(*args, **kwargs)
    except Exception:
        logger.exception("AI call failed method=%s", method_name)
        return None


def _run_coro_sync(coro, *, timeout_s: float, loop: asyncio.AbstractEventLoop | None = None):
    try:
        if loop and loop.is_running():
            return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=timeout_s)
        try:
            asyncio.get_running_loop()
            has_running_loop = True
        except RuntimeError:
            has_running_loop = False

        if not has_running_loop:
            return asyncio.run(asyncio.wait_for(coro, timeout=timeout_s))

        result_box: dict[str, Any] = {}
        err_box: dict[str, BaseException] = {}

        def _runner() -> None:
            try:
                result_box["result"] = asyncio.run(asyncio.wait_for(coro, timeout=timeout_s))
            except BaseException as exc:  # noqa: BLE001
                err_box["error"] = exc

        t = threading.Thread(target=_runner, daemon=True)
        t.start()
        t.join(timeout=timeout_s + 1.0)
        if t.is_alive():
            raise TimeoutError("async task timeout")
        if "error" in err_box:
            raise err_box["error"]
        return result_box.get("result")
    except Exception:
        try:
            coro.close()
        except Exception:
            pass
        raise


def _classify_collecting_intent_sync(
    cfg: GameCSConfig,
    text: str,
    *,
    has_area: bool,
    has_role: bool,
    ai_runtime=None,
) -> CollectingIntent:
    if not cfg.intent_enabled:
        return CollectingIntent.IRRELEVANT

    try:
        return _run_coro_sync(
            classify_collecting_intent(
                text=text,
                has_area=has_area,
                has_role=has_role,
                api_key=cfg.intent_api_key,
                api_base=cfg.intent_api_base,
                model=cfg.intent_model,
                timeout_s=max(0.1, cfg.intent_timeout_ms / 1000),
            ),
            timeout_s=max(0.1, cfg.intent_timeout_ms / 1000),
            loop=getattr(ai_runtime, "_loop", None),
        )
    except Exception:
        return CollectingIntent.IRRELEVANT


def _schedule_unique_one_shot(
    cron_service: CronService | None,
    *,
    name: str,
    at_ms: int,
    message: str,
) -> None:
    if cron_service is None:
        return

    for job in cron_service.list_jobs(include_disabled=True):
        if job.name == name:
            cron_service.remove_job(job.id)

    cron_service.add_job(
        name=name,
        schedule=CronSchedule(kind="at", at_ms=at_ms),
        message=message,
        deliver=False,
        delete_after_run=True,
    )


def _handle_message(
    cfg: GameCSConfig,
    store: GameCSStore,
    kb: OpenVikingKB,
    payload: GameMessageIn,
    *,
    ai_runtime=None,
    cron_service: CronService | None = None,
    channel_bridge: GameCSChannelBridge | None = None,
    background_tasks: BackgroundTasks | None = None,
) -> GameReply | None:
    user_id = _safe_user_id(payload.user_id)
    user_text = payload.message.strip()
    personality = cfg.personality

    store.append_message(user_id, "user", user_text or "<no text>")
    session = store.get_or_create_session(user_id, default_game_name=cfg.default_game_name)

    screenshot_path = _persist_screenshot(cfg, payload)
    if screenshot_path:
        session = store.update_session(
            user_id,
            screenshot_path=screenshot_path,
            default_game_name=cfg.default_game_name,
        )

    chat_id = (payload.metadata or {}).get("chat_id")
    if chat_id:
        session = store.update_session(
            user_id,
            channel_chat_id=chat_id,
            default_game_name=cfg.default_game_name,
        )

    # session = store.get_or_create_session(user_id, default_game_name=cfg.default_game_name)
    if session.is_closed:
        question = user_text or "用户发送了一条新消息，请人工处理。"
        query_id = store.create_human_query(user_id, question)
        if cfg.admin_gateway_enabled and cfg.admin_gateway_url:
            if background_tasks is not None:
                background_tasks.add_task(
                    forward_to_admin,
                    query_id,
                    user_id,
                    question,
                    cfg.admin_gateway_url,
                    cfg.admin_gateway_token,
                    10.0,
                )
            else:

                def _forward_runner() -> None:
                    try:
                        asyncio.run(
                            forward_to_admin(
                                query_id,
                                user_id,
                                question,
                                cfg.admin_gateway_url,
                                cfg.admin_gateway_token,
                                10.0,
                            )
                        )
                    except Exception:
                        logger.exception("forward_to_admin background thread failed")

                threading.Thread(target=_forward_runner, daemon=True).start()
        return None

    def _reply(
        text: str,
        *,
        next_step: str | None = None,
        bound: bool = False,
        codes: dict[str, str] | None = None,
    ) -> GameReply:
        store.append_message(user_id, "assistant", text)
        return GameReply(
            status="ok",
            reply=text,
            sop_state=session.sop_state,
            next_step=next_step,
            bound=bound,
            codes=codes,
            timestamp=_now(),
        )

    logger.info(f"session: {session}")
    if session.sop_state == SOPState.GREETING:
        if not user_text and not screenshot_path and not payload.screenshot_url:
            return _reply(_tmpl(SOPState.GREETING, personality), next_step="提供区服和角色名")
        session = store.update_session(
            user_id,
            sop_state=SOPState.COLLECTING_INFO,
            default_game_name=cfg.default_game_name,
        )

    if session.sop_state == SOPState.COLLECTING_INFO:
        area, role = None, None
        # 优先使用 AI 提取信息
        if cfg.ai_enabled and ai_runtime is not None and user_text:
            ai_result = _ai_run_sync(
                ai_runtime,
                "extract_info_sync",
                f"game_cs:{user_id}",
                user_text,
                timeout_ms=cfg.ai_timeout_ms,
            )
            if isinstance(ai_result, dict):
                if ai_result.get("confidence", 0.0) >= cfg.ai_info_extract_confidence_threshold:
                    area = ai_result.get("area_name")
                    role = ai_result.get("role_name")
            elif ai_result:
                return _reply(str(ai_result))
        # AI 未开启或未提取到信息时，使用正则匹配
        # if not area and not role:
        #     area, role = _parse_user_info(user_text)
        # 视觉识别作为补充
        if cfg.vision_enabled and (
            payload.screenshot_url or payload.screenshot_b64 or screenshot_path
        ):
            vision_result = extract_info_from_image_sync(
                loop=getattr(ai_runtime, "_loop", None),
                image_b64=payload.screenshot_b64,
                image_url=payload.screenshot_url,
                image_ext=payload.screenshot_ext,
                api_key=cfg.vision_api_key,
                api_base=cfg.vision_api_base,
                model=cfg.vision_model,
                timeout_s=max(0.1, cfg.vision_timeout_ms / 1000),
            )
            if vision_result.get("confidence", 0.0) >= cfg.ai_info_extract_confidence_threshold:
                area = area or vision_result.get("area_name")
                role = role or vision_result.get("role_name")

        prev_area = bool(session.area_name)
        prev_role = bool(session.role_name)
        new_area = area or session.area_name
        new_role = role or session.role_name

        if new_area and new_role:
            session = store.update_session(
                user_id,
                sop_state=SOPState.VALIDATING,
                area_name=new_area,
                role_name=new_role,
                game_name=cfg.default_game_name,
                retry_count=0,
                default_game_name=cfg.default_game_name,
            )
        else:
            captured_one_new = (bool(new_area) ^ bool(new_role)) and (
                (bool(new_area) and not prev_area) or (bool(new_role) and not prev_role)
            )
            if captured_one_new:
                intent = _classify_collecting_intent_sync(
                    cfg,
                    user_text,
                    has_area=bool(new_area),
                    has_role=bool(new_role),
                    ai_runtime=ai_runtime,
                )
                logger.debug("collecting intent=%s user_id=%s", intent, user_id)
                if intent == CollectingIntent.PARTIAL_INFO:
                    store.update_session(
                        user_id,
                        area_name=new_area,
                        role_name=new_role,
                        default_game_name=cfg.default_game_name,
                    )
                    _schedule_unique_one_shot(
                        cron_service,
                        name=f"incomplete_info_check:{user_id}",
                        at_ms=_now_ms() + cfg.collecting_timeout_s * 1000,
                        message=json.dumps(
                            {"action": "check_incomplete_info", "user_id": user_id},
                            ensure_ascii=False,
                        ),
                    )
                    return _reply("", next_step="等待完整信息，无需回复")

            if session.retry_count >= cfg.max_collect_retries:
                session = store.update_session(
                    user_id,
                    sop_state=SOPState.EXCEPTION,
                    default_game_name=cfg.default_game_name,
                )
                return _reply(_tmpl(SOPState.EXCEPTION, personality), next_step="人工介入")

            session = store.update_session(
                user_id,
                area_name=new_area,
                role_name=new_role,
                retry_count=session.retry_count + 1,
                default_game_name=cfg.default_game_name,
            )
            retry_idx = min(session.retry_count - 1, len(CLARIFY_PROMPTS) - 1)
            return _reply(
                _tmpl(
                    SOPState.COLLECTING_INFO, personality, retry_prompt=CLARIFY_PROMPTS[retry_idx]
                ),
                next_step="提供区服和角色名",
            )

    if session.sop_state == SOPState.VALIDATING:
        valid_ok, role_id = _validate_role(cfg, session)
        if valid_ok:
            session = store.update_session(
                user_id,
                sop_state=SOPState.BINDING,
                game_role_id=role_id,
                default_game_name=cfg.default_game_name,
            )
        else:
            reply = f"没有校验到该角色，请确认区服和角色名后再发一次。当前\n区服名：{session.area_name}\n角色名:{session.role_name}\n"
            session = store.update_session(
                user_id,
                sop_state=SOPState.COLLECTING_INFO,
                area_name=None,
                role_name=None,
                retry_count=0,
                default_game_name=cfg.default_game_name,
            )
            return _reply(reply)

    if session.sop_state == SOPState.BINDING:
        if _bind_user(cfg, session):
            session = store.update_session(
                user_id,
                sop_state=SOPState.SENDING_CODE,
                default_game_name=cfg.default_game_name,
            )
        else:
            session = store.update_session(
                user_id,
                sop_state=SOPState.EXCEPTION,
                default_game_name=cfg.default_game_name,
            )
            return _reply(_tmpl(SOPState.EXCEPTION, personality), next_step="人工介入")

    if session.sop_state == SOPState.SENDING_CODE:
        codes_block = _build_codes_block(cfg)
        codes = _codes_dict(cfg)
        msg = _tmpl(SOPState.SENDING_CODE, personality, codes_block=codes_block)
        session = store.update_session(
            user_id,
            sop_state=SOPState.FOLLOW_UP_PENDING,
            codes_sent_at=_now_iso(),
            default_game_name=cfg.default_game_name,
        )
        store.append_message(user_id, "assistant", msg)
        return GameReply(
            status="ok",
            reply=msg,
            sop_state=SOPState.FOLLOW_UP_PENDING,
            next_step=None,
            bound=True,
            codes=codes,
            timestamp=_now(),
        )

    if session.sop_state in (
        SOPState.FOLLOW_UP_PENDING,
        SOPState.FOLLOW_UP_30MIN,
        SOPState.FOLLOW_UP_1HOUR,
        SOPState.SILENT,
        SOPState.NEXT_DAY_VISIT,
        SOPState.REACTIVATION,
        SOPState.COMPLETED,
    ):
        if user_text:
            return _kb_reply(
                user_id,
                user_text,
                cfg,
                store,
                kb,
                session,
                personality,
                ai_runtime=ai_runtime,
                cron_service=cron_service,
                background_tasks=background_tasks,
                channel_bridge=channel_bridge,
            )

    if user_text:
        return _kb_reply(
            user_id,
            user_text,
            cfg,
            store,
            kb,
            session,
            personality,
            ai_runtime=ai_runtime,
            cron_service=cron_service,
            background_tasks=background_tasks,
            channel_bridge=channel_bridge,
        )

    return _reply("可以继续告诉我你的问题。")


def _kb_reply(
    user_id: str,
    user_text: str,
    cfg: GameCSConfig,
    store: GameCSStore,
    kb: OpenVikingKB,
    session: SOPSessionState,
    personality: str,
    *,
    ai_runtime=None,
    cron_service: CronService | None = None,
    background_tasks: BackgroundTasks | None = None,
    channel_bridge: GameCSChannelBridge | None = None,
) -> GameReply:
    if cfg.ai_enabled and ai_runtime is not None:
        history_for_ai = store.get_recent_messages(user_id, limit=cfg.ai_max_context_msgs)
        kb_ctx = (
            kb.search_with_context(user_text, history=history_for_ai, limit=10, include_l2=True)
            if len(history_for_ai) >= 2
            else kb.search(user_text, limit=10, include_l2=True)
        )
        logger.debug("KB context for AI user_id=%s context=%s", user_id, kb_ctx)
        ai_result = _ai_run_sync(
            ai_runtime,
            "ask_agent_structured_sync",
            f"game_cs:{user_id}",
            user_text,
            kb_ctx,
            history_for_ai,
            timeout_ms=cfg.ai_timeout_ms,
        )
        if (
            ai_result is not None
            and not isinstance(ai_result, (dict, str))
            and hasattr(ai_runtime, "ask_agent_sync")
        ):
            ai_result = _ai_run_sync(
                ai_runtime,
                "ask_agent_sync",
                f"game_cs:{user_id}",
                user_text,
                kb_ctx,
                history_for_ai,
                timeout_ms=cfg.ai_timeout_ms,
            )
        if isinstance(ai_result, str):
            ai_result = {"need_human": False, "reply": ai_result, "reason": "legacy_reply"}
        if ai_result and isinstance(ai_result, dict):
            need_human: bool = bool(ai_result.get("need_human", False))
            reply_text: str = str(ai_result.get("reply", "")).strip()
            reason: str = str(ai_result.get("reason", ""))
            logger.info(
                "_kb_reply: ai_result need_human=%s reason=%s user_id=%s",
                need_human,
                reason,
                user_id,
            )
            if need_human and cfg.admin_gateway_enabled:
                # AI 判断需要人工介入：先把 AI 的安抚回复发给用户，同时创建人工查询记录
                query_id = store.create_human_query(user_id, user_text)
                if background_tasks is not None:
                    background_tasks.add_task(
                        forward_to_admin,
                        query_id,
                        user_id,
                        user_text,
                        cfg.admin_gateway_url,
                        cfg.admin_gateway_token,
                        10.0,
                    )
                else:

                    def _forward_runner_ai() -> None:
                        try:
                            asyncio.run(
                                forward_to_admin(
                                    query_id,
                                    user_id,
                                    user_text,
                                    cfg.admin_gateway_url,
                                    cfg.admin_gateway_token,
                                    10.0,
                                )
                            )
                        except Exception:
                            logger.exception(
                                "forward_to_admin (ai-triggered) background thread failed"
                            )

                    threading.Thread(target=_forward_runner_ai, daemon=True).start()
                _schedule_unique_one_shot(
                    cron_service,
                    name=f"check_human_reply:{user_id}",
                    at_ms=_now_ms() + cfg.admin_query_timeout_s * 1000,
                    message=json.dumps(
                        {"action": "check_human_reply", "user_id": user_id},
                        ensure_ascii=False,
                    ),
                )
                # 如果 AI 没有提供安抚文案，使用默认文案
                if not reply_text:
                    reply_text = "小妹正在为您联系专属客服，稍等一下~"
                store.append_message(user_id, "assistant", reply_text)
                return GameReply(
                    status="ok",
                    reply=reply_text,
                    sop_state=session.sop_state,
                    next_step="等待人工回复",
                    bound=session.is_bound,
                    timestamp=_now(),
                )
            elif reply_text:
                # AI 自信可以回答，直接返回
                store.append_message(user_id, "assistant", reply_text)
                return GameReply(
                    status="ok",
                    reply=reply_text,
                    sop_state=session.sop_state,
                    next_step=None,
                    bound=session.is_bound,
                    timestamp=_now(),
                )

    history = store.get_recent_messages(user_id, limit=8)
    kb_lines = (
        kb.search_with_context(user_text, history=history, limit=4, include_l2=True)
        if len(history) >= 2
        else kb.search(user_text, limit=4, include_l2=True)
    )

    scores: list[float] = []
    for line in kb_lines:
        if not isinstance(line, str):
            continue
        m = re.match(r"^\[(\d+(?:\.\d+)?)\]", line.strip())
        if not m:
            continue
        try:
            scores.append(float(m.group(1)))
        except Exception:
            continue

    if scores and max(scores) < cfg.kb_handoff_score_threshold:
        if cfg.admin_gateway_enabled:
            query_id = store.create_human_query(user_id, user_text)
            if background_tasks is not None:
                background_tasks.add_task(
                    forward_to_admin,
                    query_id,
                    user_id,
                    user_text,
                    cfg.admin_gateway_url,
                    cfg.admin_gateway_token,
                    10.0,
                )
            else:

                def _forward_runner() -> None:
                    try:
                        asyncio.run(
                            forward_to_admin(
                                query_id,
                                user_id,
                                user_text,
                                cfg.admin_gateway_url,
                                cfg.admin_gateway_token,
                                10.0,
                            )
                        )
                    except Exception:
                        logger.exception("forward_to_admin background thread failed")

                threading.Thread(target=_forward_runner, daemon=True).start()
            _schedule_unique_one_shot(
                cron_service,
                name=f"check_human_reply:{user_id}",
                at_ms=_now_ms() + cfg.admin_query_timeout_s * 1000,
                message=json.dumps(
                    {"action": "check_human_reply", "user_id": user_id},
                    ensure_ascii=False,
                ),
            )
            reply_text = "小妹正在为您联系专属客服，稍等一下~"
            store.append_message(user_id, "assistant", reply_text)
            return GameReply(
                status="ok",
                reply=reply_text,
                sop_state=session.sop_state,
                next_step="等待人工回复",
                bound=session.is_bound,
                timestamp=_now(),
            )

        store.update_session(
            user_id,
            sop_state=SOPState.EXCEPTION,
            default_game_name=cfg.default_game_name,
        )
        reply_text = _tmpl(SOPState.EXCEPTION, personality)
        store.append_message(user_id, "assistant", reply_text)
        return GameReply(
            status="ok",
            reply=reply_text,
            sop_state=SOPState.EXCEPTION,
            next_step="人工介入",
            bound=session.is_bound,
            timestamp=_now(),
        )

    if kb_lines:
        intro = {
            "lively": "我查到这些信息：\n",
            "professional": "以下是可参考信息：\n",
            "steady": "我整理了这些内容：\n",
            "humorous": "情报到了：\n",
        }.get(personality, "相关信息如下：\n")
        reply_text = intro + "\n".join(f"- {x}" for x in kb_lines)
    else:
        reply_text = {
            "lively": "暂时没检索到明确答案，你可以补充一下细节。",
            "professional": "暂未检索到相关信息，请补充更多细节。",
            "steady": "我先没找到这条信息，再补充点细节我继续查。",
            "humorous": "这题知识库也在想，你再多给点线索。",
        }.get(personality, "暂未检索到相关信息。")

    store.append_message(user_id, "assistant", reply_text)
    return GameReply(
        status="ok",
        reply=reply_text,
        sop_state=session.sop_state,
        next_step=None,
        bound=session.is_bound,
        timestamp=_now(),
    )


def _validate_role(cfg: GameCSConfig, session: SOPSessionState) -> tuple[bool, str | None]:
    if cfg.mock_api:
        return True, "mock-role-id"

    for attempt in range(max(1, cfg.game_api_retry_max)):
        try:
            with httpx.Client(timeout=cfg.game_api_timeout_s) as client:
                resp = client.post(
                    f"{cfg.game_api_base}/api/game/verify_role",
                    json={
                        "game_name": session.game_name,
                        "area_name": session.area_name,
                        "role_name": session.role_name,
                    },
                    headers={"Authorization": f"Bearer {cfg.game_api_token}"},
                )
                data = resp.json() if resp.content else {}
                if resp.status_code == 200 and data.get("success"):
                    return True, data.get("role_id")
                if resp.status_code == 404 or data.get("error_code") == "ROLE_NOT_FOUND":
                    return False, None
        except httpx.TimeoutException:
            if attempt == max(1, cfg.game_api_retry_max) - 1:
                logger.warning(
                    "_validate_role timeout after retries=%s, best-effort pass",
                    cfg.game_api_retry_max,
                )
                return True, None
        except Exception:
            if attempt == max(1, cfg.game_api_retry_max) - 1:
                logger.exception("_validate_role unexpected error")
                return True, None
        time.sleep(1.0 * (attempt + 1))

    return False, None


def _bind_user(cfg: GameCSConfig, session: SOPSessionState) -> bool:
    if cfg.mock_api:
        return True

    for attempt in range(max(1, cfg.game_api_retry_max)):
        try:
            with httpx.Client(timeout=cfg.game_api_timeout_s) as client:
                resp = client.post(
                    f"{cfg.game_api_base}/api/user/bind",
                    json={
                        "user_id": session.user_id,
                        "game_name": session.game_name,
                        "area_name": session.area_name,
                        "role_name": session.role_name,
                        "role_id": session.game_role_id,
                    },
                    headers={"Authorization": f"Bearer {cfg.game_api_token}"},
                )
                data = resp.json() if resp.content else {}
                if resp.status_code == 200 and data.get("success"):
                    return True
                if resp.status_code in (400, 409):
                    return False
        except httpx.TimeoutException:
            if attempt == max(1, cfg.game_api_retry_max) - 1:
                return False
        except Exception:
            if attempt == max(1, cfg.game_api_retry_max) - 1:
                logger.exception("_bind_user unexpected error")
                return False
        time.sleep(1.0 * (attempt + 1))

    return False


async def _push_outbound(
    user_id: str,
    text: str,
    store: GameCSStore,
    bridge: GameCSChannelBridge | None,
) -> bool:
    if bridge is None:
        logger.warning("_push_outbound: no channel bridge, message dropped for user=%s", user_id)
        return False

    session = store.get_or_create_session(user_id)
    chat_id = session.channel_chat_id
    if not chat_id:
        logger.warning("_push_outbound: no chat_id for user=%s", user_id)
        return False

    await bridge.push(chat_id, text)
    return True


async def _run_process_followups(
    cfg: GameCSConfig,
    store: GameCSStore,
    channel_bridge: GameCSChannelBridge | None,
) -> dict[str, Any]:
    now_iso = _now_iso()

    due_30m = store.get_pending_30m_followups(now_iso)
    followup_30m_results: list[dict[str, str]] = []
    for s in due_30m:
        msg = _tmpl(SOPState.FOLLOW_UP_30MIN, cfg.personality)
        store.update_session(
            s.user_id,
            sop_state=SOPState.FOLLOW_UP_30MIN,
            follow_up_30m_sent=True,
            default_game_name=cfg.default_game_name,
        )
        store.append_message(s.user_id, "assistant", msg)
        await _push_outbound(s.user_id, msg, store, channel_bridge)
        followup_30m_results.append({"user_id": s.user_id, "message": msg})

    due_1h = store.get_pending_1h_followups(now_iso)
    followup_1h_results: list[dict[str, str]] = []
    for s in due_1h:
        msg = _tmpl(SOPState.FOLLOW_UP_1HOUR, cfg.personality)
        store.update_session(
            s.user_id,
            sop_state=SOPState.FOLLOW_UP_1HOUR,
            follow_up_1h_sent=True,
            default_game_name=cfg.default_game_name,
        )
        store.append_message(s.user_id, "assistant", msg)
        await _push_outbound(s.user_id, msg, store, channel_bridge)
        followup_1h_results.append({"user_id": s.user_id, "message": msg})

    return {
        "followup_30m": followup_30m_results,
        "followup_1h": followup_1h_results,
        "processed_at": now_iso,
    }


async def _run_next_day_visits(
    cfg: GameCSConfig,
    store: GameCSStore,
    channel_bridge: GameCSChannelBridge | None,
) -> dict[str, Any]:
    now_iso = _now_iso()
    due = store.get_pending_next_day_visits(now_iso)
    results: list[dict[str, str]] = []
    for s in due:
        msg = _tmpl(SOPState.NEXT_DAY_VISIT, cfg.personality)
        store.update_session(
            s.user_id,
            sop_state=SOPState.NEXT_DAY_VISIT,
            next_day_visited=True,
            default_game_name=cfg.default_game_name,
        )
        store.append_message(s.user_id, "assistant", msg)
        await _push_outbound(s.user_id, msg, store, channel_bridge)
        results.append({"user_id": s.user_id, "message": msg})
    return {"next_day_visits": results, "processed_at": now_iso}


async def _run_check_incomplete_info(
    user_id: str,
    cfg: GameCSConfig,
    store: GameCSStore,
    channel_bridge: GameCSChannelBridge | None,
) -> None:
    session = store.get_or_create_session(user_id, default_game_name=cfg.default_game_name)
    if session.sop_state != SOPState.COLLECTING_INFO or session.has_full_info:
        return

    retry_idx = min(session.retry_count, len(CLARIFY_PROMPTS) - 1)
    clarify = CLARIFY_PROMPTS[retry_idx]
    msg = _tmpl(SOPState.COLLECTING_INFO, cfg.personality, retry_prompt=clarify)
    store.update_session(
        user_id,
        retry_count=session.retry_count + 1,
        default_game_name=cfg.default_game_name,
    )
    store.append_message(user_id, "assistant", msg)
    await _push_outbound(user_id, msg, store, channel_bridge)


async def _deliver_human_reply(
    user_id: str,
    cfg: GameCSConfig,
    store: GameCSStore,
    channel_bridge: GameCSChannelBridge | None,
) -> None:
    queries = store.get_pending_delivery_queries(user_id)
    for q in queries:
        reply = (q.get("human_reply") or "").strip()
        if not reply:
            store.mark_query_delivered(int(q["id"]))
            continue
        store.append_message(user_id, "assistant", reply)
        pushed = await _push_outbound(user_id, reply, store, channel_bridge)
        if pushed:
            store.mark_query_delivered(int(q["id"]))


def _session_to_dict(session) -> dict[str, Any]:
    return {
        "user_id": session.user_id,
        "sop_state": session.sop_state,
        "game_name": session.game_name,
        "area_name": session.area_name,
        "role_name": session.role_name,
        "game_role_id": session.game_role_id,
        "channel_chat_id": session.channel_chat_id,
        "is_bound": session.is_bound,
        "is_closed": session.is_closed,
        "closed_at": session.closed_at,
        "ai_auto_reply_enabled": _is_ai_auto_reply_enabled(session),
        "codes_sent_at": session.codes_sent_at,
        "follow_up_30m_sent": session.follow_up_30m_sent,
        "follow_up_1h_sent": session.follow_up_1h_sent,
        "next_day_visited": session.next_day_visited,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
    }


async def _admin_send_message(
    user_id: str,
    reply: str,
    cfg: GameCSConfig,
    store: GameCSStore,
    channel_bridge: GameCSChannelBridge | None,
) -> dict[str, Any]:
    session = store.get_or_create_session(user_id, default_game_name=cfg.default_game_name)
    store.append_message(user_id, "assistant", reply)
    delivered = await _push_outbound(user_id, reply, store, channel_bridge)
    refreshed = store.get_or_create_session(user_id, default_game_name=cfg.default_game_name)
    return {
        "ok": True,
        "delivered": delivered,
        "message": reply,
        "session": _session_to_dict(refreshed),
    }


async def _run_check_human_reply(
    user_id: str,
    cfg: GameCSConfig,
    store: GameCSStore,
    channel_bridge: GameCSChannelBridge | None,
) -> None:
    answered = store.get_pending_delivery_queries(user_id)
    if answered:
        await _deliver_human_reply(user_id, cfg, store, channel_bridge)
        return

    all_pending = [q for q in store.get_pending_queries_all() if q.get("user_id") == user_id]
    waiting = [q for q in all_pending if q.get("status") == "pending"]
    if not waiting:
        return

    for q in waiting:
        store.mark_query_delivered(int(q["id"]))

    store.update_session(
        user_id,
        sop_state=SOPState.EXCEPTION,
        default_game_name=cfg.default_game_name,
    )
    exception_msg = _tmpl(SOPState.EXCEPTION, cfg.personality)
    store.append_message(user_id, "assistant", exception_msg)
    await _push_outbound(user_id, exception_msg, store, channel_bridge)


def _ensure_builtin_jobs(cron_service: CronService) -> None:
    by_name = {job.name for job in cron_service.list_jobs(include_disabled=True)}
    if "builtin_followups" not in by_name:
        cron_service.add_job(
            name="builtin_followups",
            schedule=CronSchedule(kind="every", every_ms=60_000),
            message=json.dumps({"action": "process_followups"}, ensure_ascii=False),
            deliver=False,
        )
    if "builtin_next_day" not in by_name:
        cron_service.add_job(
            name="builtin_next_day",
            schedule=CronSchedule(kind="every", every_ms=300_000),
            message=json.dumps({"action": "next_day_visits"}, ensure_ascii=False),
            deliver=False,
        )


def create_app(
    config: GameCSConfig | None = None,
    channel=None,
) -> FastAPI:
    cfg = config or GameCSConfig.from_env()
    store = GameCSStore(cfg)
    kb = OpenVikingKB(cfg.openviking_path, cfg.openviking_target_uri)

    _ai_runtime = None
    if cfg.ai_enabled:
        try:
            from .ai_runtime import build_runtime

            _ai_runtime = build_runtime(timeout_ms=cfg.ai_timeout_ms)
            if _ai_runtime is None:
                logger.warning("AI runtime build failed, continue without AI")
        except Exception:
            logger.exception("Failed to initialize AI runtime")

    _bridge: GameCSChannelBridge | None = None
    _cron_service: CronService | None = None

    async def _on_cron_job(job: CronJob) -> str | None:
        assert _cron_service is not None
        try:
            data = json.loads(job.payload.message)
        except Exception:
            data = {"action": job.payload.message}

        action = str(data.get("action", ""))
        user_id = data.get("user_id")

        if action == "process_followups":
            await _run_process_followups(cfg, store, _bridge)
        elif action == "next_day_visits":
            await _run_next_day_visits(cfg, store, _bridge)
        elif action == "check_incomplete_info" and user_id:
            await _run_check_incomplete_info(str(user_id), cfg, store, _bridge)
        elif action == "check_human_reply" and user_id:
            await _run_check_human_reply(str(user_id), cfg, store, _bridge)
        return None

    _cron_service = CronService(
        store_path=cfg.data_dir / "game_cs_cron_jobs.json",
        on_job=_on_cron_job,
    )

    if channel is not None and GameCSChannelBridge is not None:

        async def _on_inbound(
            user_id: str,
            chat_id: str,
            text: str,
            screenshot_url: str | None,
            channel_name: str | None,
        ) -> str | None:
            payload = GameMessageIn(
                user_id=user_id,
                message=text,
                screenshot_url=screenshot_url,
                metadata={"chat_id": chat_id, "channel": channel_name or ""},
            )
            result = await asyncio.to_thread(
                _handle_message,
                cfg,
                store,
                kb,
                payload,
                ai_runtime=_ai_runtime,
                cron_service=_cron_service,
                channel_bridge=_bridge,
                background_tasks=None,
            )
            if result and result.reply:
                return result.reply
            return None

        _bridge = GameCSChannelBridge(channel=channel, on_inbound=_on_inbound)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await _cron_service.start()
        _ensure_builtin_jobs(_cron_service)
        if _bridge is not None:
            await _bridge.start()
        try:
            yield
        finally:
            _cron_service.stop()
            if _bridge is not None:
                await _bridge.stop()
            if _ai_runtime is not None:
                _ai_runtime.close()

    app = FastAPI(
        title="Nanobot Game Customer Service",
        description="SOP-driven game customer service built on NanoBot + OpenViking",
        version="2.1.0",
        lifespan=lifespan,
    )

    _cfg_box: list[GameCSConfig] = [cfg]

    @app.get("/healthz", tags=["system"])
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": app.version}

    @app.post("/admin/index-kb", tags=["admin"])
    def index_kb(
        paths: list[str],
        x_game_cs_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        if x_game_cs_token != _cfg_box[0].service_token:
            raise HTTPException(status_code=401, detail="invalid token")
        try:
            roots = kb.add_resources(paths, wait=True)
            return {"ok": True, "indexed": roots}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "indexed": []}

    @app.post("/kb/qa", tags=["kb"])
    def add_kb_qa(
        payload: KBQAIn,
        x_game_cs_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        if x_game_cs_token != _cfg_box[0].service_token:
            raise HTTPException(status_code=401, detail="invalid token")

        question = _require_non_empty_text(payload.question, "question")
        answer = _require_non_empty_text(payload.answer, "answer")
        category = payload.category.strip() or "faq"

        try:
            result = kb.add_qa(
                question=question,
                answer=answer,
                category=category,
                wait=True,
            )

            logger.info(f"result of adding kb qa: {result}")
            return {
                "ok": True,
                "question": question,
                "category": result["category"],
                "file_path": result["file_path"],
                "root_uri": result["root_uri"],
            }
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"kb qa index failed: {exc}") from exc

    @app.post("/admin/update-codes", tags=["admin"])
    def update_codes(
        daily_checkin: str,
        lucky_draw: str,
        universal: str,
        guild: str,
        x_game_cs_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        if x_game_cs_token != _cfg_box[0].service_token:
            raise HTTPException(status_code=401, detail="invalid token")

        _cfg_box[0] = dataclasses.replace(
            _cfg_box[0],
            code_daily_checkin=daily_checkin,
            code_lucky_draw=lucky_draw,
            code_universal=universal,
            code_guild=guild,
        )
        return {"ok": True, "codes": _codes_dict(_cfg_box[0])}

    @app.post("/admin/reset-session", tags=["admin"])
    def reset_session(
        user_id: str,
        x_game_cs_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        if x_game_cs_token != _cfg_box[0].service_token:
            raise HTTPException(status_code=401, detail="invalid token")
        _safe_user_id(user_id)
        state = store.reset_session(user_id, default_game_name=_cfg_box[0].default_game_name)
        return {"ok": True, "sop_state": state.sop_state}

    @app.post("/admin/human-reply", tags=["admin"])
    async def human_reply(
        payload: HumanReplyIn,
        x_game_cs_token: str | None = Header(default=None),
    ) -> dict[str, bool]:
        if x_game_cs_token != _cfg_box[0].service_token:
            raise HTTPException(status_code=401, detail="invalid token")
        store.update_human_reply(payload.query_id, payload.reply)
        await _deliver_human_reply(payload.user_id, _cfg_box[0], store, _bridge)
        return {"ok": True}

    @app.get("/admin/stats", tags=["admin"])
    def admin_stats(
        x_game_cs_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        if x_game_cs_token != _cfg_box[0].service_token:
            raise HTTPException(status_code=401, detail="invalid token")
        summary = store.get_summary_counts()
        return {
            "ok": True,
            "service": {
                "version": app.version,
                "ai_enabled": bool(_cfg_box[0].ai_enabled),
                "admin_gateway_enabled": bool(_cfg_box[0].admin_gateway_enabled),
                "bridge_connected": _bridge is not None,
                "db_driver": _cfg_box[0].db_driver,
                "db_host": _cfg_box[0].db_host,
                "db_port": _cfg_box[0].db_port,
                "db_name": _cfg_box[0].db_name,
            },
            "summary": summary,
        }

    @app.get("/admin/customers", tags=["admin"])
    def list_customers(
        limit: int = 20,
        include_closed: bool = True,
        sop_state: str | None = None,
        query: str | None = None,
        x_game_cs_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        if x_game_cs_token != _cfg_box[0].service_token:
            raise HTTPException(status_code=401, detail="invalid token")
        rows = store.list_sessions(
            limit=max(1, min(limit, 500)),
            include_closed=include_closed,
            sop_state=sop_state,
            query=query,
        )
        return {
            "ok": True,
            "count": len(rows),
            "customers": [_session_to_dict(item) for item in rows],
        }

    @app.get("/admin/customer/{user_id}", tags=["admin"])
    def get_customer_detail(
        user_id: str,
        message_limit: int = 20,
        human_query_limit: int = 20,
        x_game_cs_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        if x_game_cs_token != _cfg_box[0].service_token:
            raise HTTPException(status_code=401, detail="invalid token")
        _safe_user_id(user_id)
        session = store.get_or_create_session(
            user_id, default_game_name=_cfg_box[0].default_game_name
        )
        human_queries = [
            item for item in store.get_pending_queries_all() if str(item.get("user_id")) == user_id
        ][: max(1, min(human_query_limit, 50))]
        return {
            "ok": True,
            "customer": _session_to_dict(session),
            "recent_messages": store.get_session_messages(
                user_id, limit=max(1, min(message_limit, 50))
            ),
            "human_queries": human_queries,
        }

    @app.post("/admin/customer/{user_id}/message", tags=["admin"])
    async def admin_customer_message(
        user_id: str,
        payload: AdminMessageIn,
        x_game_cs_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        if x_game_cs_token != _cfg_box[0].service_token:
            raise HTTPException(status_code=401, detail="invalid token")
        _safe_user_id(user_id)
        reply = payload.reply.strip()
        if not reply:
            raise HTTPException(status_code=400, detail="reply cannot be empty")
        return await _admin_send_message(user_id, reply, _cfg_box[0], store, _bridge)

    @app.post("/admin/customer/{user_id}/reset", tags=["admin"])
    def admin_customer_reset(
        user_id: str,
        x_game_cs_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        if x_game_cs_token != _cfg_box[0].service_token:
            raise HTTPException(status_code=401, detail="invalid token")
        _safe_user_id(user_id)
        state = store.reset_session(user_id, default_game_name=_cfg_box[0].default_game_name)
        return {"ok": True, "customer": _session_to_dict(state)}

    @app.post("/admin/customer/{user_id}/close", tags=["admin"])
    def admin_customer_close(
        user_id: str,
        payload: AdminCloseSessionIn,
        x_game_cs_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        if x_game_cs_token != _cfg_box[0].service_token:
            raise HTTPException(status_code=401, detail="invalid token")
        _safe_user_id(user_id)
        if payload.closed:
            state = store.close_session(user_id, default_game_name=_cfg_box[0].default_game_name)
        else:
            state = store.reopen_session(user_id, default_game_name=_cfg_box[0].default_game_name)
        return {
            "ok": True,
            "customer": _session_to_dict(state),
            "message": "AI auto reply disabled" if payload.closed else "AI auto reply enabled",
        }

    @app.get("/admin/human-queries", tags=["admin"])
    def admin_human_queries(
        status: str | None = None,
        limit: int = 20,
        x_game_cs_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        if x_game_cs_token != _cfg_box[0].service_token:
            raise HTTPException(status_code=401, detail="invalid token")
        items = store.list_human_queries(status=status, limit=limit)
        return {"ok": True, "count": len(items), "queries": items}

    @app.post("/webhook/game-message", tags=["webhook"])
    async def on_message(
        payload: GameMessageIn,
        background_tasks: BackgroundTasks,
        x_game_cs_token: str | None = Header(default=None),
    ):
        if x_game_cs_token != _cfg_box[0].service_token:
            raise HTTPException(status_code=401, detail="invalid token")

        _safe_user_id(payload.user_id)
        existing = store.get_or_create_session(
            payload.user_id,
            default_game_name=_cfg_box[0].default_game_name,
        )

        if (
            existing.sop_state == SOPState.GREETING
            and not payload.message.strip()
            and not payload.screenshot_b64
            and not payload.screenshot_url
        ):
            greeting_text = _tmpl(SOPState.GREETING, _cfg_box[0].personality)
            store.append_message(payload.user_id, "assistant", greeting_text)
            return GameReply(
                status="ok",
                reply=greeting_text,
                sop_state=SOPState.GREETING,
                next_step="提供区服和角色名",
                bound=False,
                timestamp=_now(),
            )

        reply = _handle_message(
            _cfg_box[0],
            store,
            kb,
            payload,
            ai_runtime=_ai_runtime,
            cron_service=_cron_service,
            channel_bridge=_bridge,
            background_tasks=background_tasks,
        )

        if reply is None:
            return Response(status_code=204)

        if reply.bound:
            history = store.get_recent_messages(payload.user_id, limit=20)
            background_tasks.add_task(
                kb.commit_session,
                messages=history,
                user_id=payload.user_id,
            )

        return reply

    @app.post("/cron/process-followups", tags=["cron"])
    async def process_followups(
        x_game_cs_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        if x_game_cs_token != _cfg_box[0].service_token:
            raise HTTPException(status_code=401, detail="invalid token")
        return await _run_process_followups(_cfg_box[0], store, _bridge)

    @app.post("/cron/next-day-visits", tags=["cron"])
    async def next_day_visits(
        x_game_cs_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        if x_game_cs_token != _cfg_box[0].service_token:
            raise HTTPException(status_code=401, detail="invalid token")
        return await _run_next_day_visits(_cfg_box[0], store, _bridge)

    @app.get("/admin/session/{user_id}", tags=["admin"])
    def get_session(
        user_id: str,
        x_game_cs_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        if x_game_cs_token != _cfg_box[0].service_token:
            raise HTTPException(status_code=401, detail="invalid token")
        _safe_user_id(user_id)
        s = store.get_or_create_session(user_id, default_game_name=_cfg_box[0].default_game_name)
        return _session_to_dict(s)

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run game customer service webhook server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host")
    parser.add_argument("--port", default=8011, type=int, help="Bind port")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload")
    args = parser.parse_args()

    import uvicorn

    uvicorn.run(
        "nanobot.game_cs.service:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
