from __future__ import annotations

import argparse
import base64
import hashlib
import re
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException

from .config import GameCSConfig
from .models import GameMessageIn, GameReply, SOPState
from .openviking_kb import OpenVikingKB
from .storage import GameCSStore, SOPSessionState

# ── Constants ─────────────────────────────────────────────────────────────────

SAFE_USER_ID = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
ALLOWED_IMAGE_EXTS = {"png", "jpg", "jpeg", "webp"}

# ── Clarification prompts for COLLECTING_INFO retries ──────────────────────────

CLARIFY_PROMPTS = [
    "哥，麻烦再说清楚一点，您是在哪个区呀？角色名是什么？",
    "不好意思小妹没听明白，能告诉小妹几区和角色名吗？",
    '可能网络有点问题，要不您截图给小妹看看，或者直接发"18区 战神无双"这样的格式？',
]

# ── Personality response templates ────────────────────────────────────────────

# Each key is a SOPState value; each sub-key is a PersonalityType value.
# Use {area_name}, {role_name}, {game_name}, {codes_block}, {retry_prompt} as placeholders.

_TMPL: dict[str, dict[str, str]] = {
    # ── GREETING ──────────────────────────────────────────────────────────────
    SOPState.GREETING: {
        "lively": (
            "Hello! 哥~ 来啦！是玩顽石英雄的老板吧😊 "
            "告诉小妹您在哪个大区、几区，角色名叫啥，小妹马上给您安排兑换码~"
        ),
        "professional": (
            "您好，欢迎咨询顽石英雄客服。"
            "为准确为您服务，请提供：区服（几区）以及您的角色名。"
        ),
        "steady": (
            "老板您好，小妹在这儿呢。"
            "麻烦告知一下您是在哪个区，以及角色名，小妹来帮您安排兑换码。"
        ),
        "humorous": (
            "哥，掉线了还是来找小妹补给了😄 "
            "快告诉小妹几区+角色名，兑换码已经蓄势待发！"
        ),
    },
    # ── COLLECTING_INFO (missing info, re-ask) ────────────────────────────────
    SOPState.COLLECTING_INFO: {
        "lively": "{retry_prompt}",
        "professional": "{retry_prompt}",
        "steady": "{retry_prompt}",
        "humorous": "{retry_prompt}",
    },
    # ── VALIDATING (tells user we're checking) ────────────────────────────────
    SOPState.VALIDATING: {
        "lively": "好嘞哥！小妹帮您查一下角色信息，稍等一秒~✨",
        "professional": "正在为您核实角色信息，请稍候。",
        "steady": "收到，小妹这就帮您确认角色，稍微等一下。",
        "humorous": "好嘞！让小妹施法查一下~ 🔮",
    },
    # ── BINDING ───────────────────────────────────────────────────────────────
    SOPState.BINDING: {
        "lively": "角色验证通过啦！正在帮您绑定账号，马上就好~😊",
        "professional": "角色信息验证通过，正在为您完成账号绑定。",
        "steady": "验证通过，绑定中，请稍等。",
        "humorous": "查到啦！帮您锁定角色，绑定走起！⚡",
    },
    # ── SENDING_CODE ──────────────────────────────────────────────────────────
    SOPState.SENDING_CODE: {
        "lively": (
            "哥~ 兑换码来啦！记得每天找小妹打卡哦😉\n\n"
            "{codes_block}\n\n"
            "有效期24小时，有啥问题随时喊我~🌹"
        ),
        "professional": (
            "已为您发放今日兑换码，请注意查收：\n\n"
            "{codes_block}\n\n"
            "有效期24小时，请及时使用。如有疑问请随时联系客服。"
        ),
        "steady": (
            "老板，您的兑换码来了，请收好：\n\n"
            "{codes_block}\n\n"
            "有效期24小时，小妹随时在这儿。"
        ),
        "humorous": (
            "哥，宝贝码码来咯！🎁\n\n"
            "{codes_block}\n\n"
            "24小时内用掉，过期不候哦～😄"
        ),
    },
    # ── FOLLOW_UP_30MIN ───────────────────────────────────────────────────────
    SOPState.FOLLOW_UP_30MIN: {
        "lively": (
            "哥 跟您说个好事[愉快]连续找小妹签到3天，"
            "有一次抽奖机会，最高免费抽充值赞助，"
            "小妹特地给您申请的，要帮您登记嘛~[玫瑰]"
        ),
        "professional": (
            "您好，提醒您参与连续签到活动：连续3天签到可获得一次抽奖机会，"
            "最高可免费获得充值赞助，需要为您登记吗？"
        ),
        "steady": (
            "老板，小妹来提醒您一下。连续3天找小妹签到可以抽一次奖，"
            "最高免费充值赞助哦，要不要帮您登记上？"
        ),
        "humorous": (
            "哥，小妹来给你送buff了！🎲 "
            "签到3天能抽大奖，最高充值赞助，要不要上车？"
        ),
    },
    # ── FOLLOW_UP_1HOUR (fission) ─────────────────────────────────────────────
    SOPState.FOLLOW_UP_1HOUR: {
        "lively": (
            "哥，先给您登记上了[爱心]"
            "您喊朋友一起来玩，小妹给您安排抽路费转盘，"
            "最高拿1000真冲[玫瑰]"
            "人多热闹才有意思，您身边有爱玩传奇的朋友嘛？"
        ),
        "professional": (
            "已为您完成登记。若邀请朋友加入顽石英雄，"
            "您可获得抽路费转盘资格，最高1000元真实充值奖励。"
            "您是否有意向邀请朋友？"
        ),
        "steady": (
            "老板，帮您登记好了。"
            "您要是能喊朋友一起来玩就更棒了，小妹给您安排路费转盘，最高1000真冲，"
            "有没有合适的朋友推荐呀？"
        ),
        "humorous": (
            "哥，帮你锁定席位了！🎰 "
            "拉朋友来玩，最高1000真冲等你拿，人多爆率也高哦，快叫队友！"
        ),
    },
    # ── NEXT_DAY_VISIT ────────────────────────────────────────────────────────
    SOPState.NEXT_DAY_VISIT: {
        "lively": (
            "老板下午好 打扰一下下 "
            "就是想问问您，昨天的那款传奇 还在玩嘛 "
            "可以找我领取每日福利的😊"
        ),
        "professional": (
            "您好，请问您昨天的游戏体验如何？"
            "如有需要，今日福利可继续领取，欢迎随时联系。"
        ),
        "steady": (
            "老板，昨天玩得咋样？有没有继续上线呢？"
            "每天找小妹签到可以领福利哦。"
        ),
        "humorous": (
            "哥，昨天爆出啥好装备了没？😄 "
            "每日签到福利还等着你，快来！"
        ),
    },
    # ── REACTIVATION ──────────────────────────────────────────────────────────
    SOPState.REACTIVATION: {
        "lively": (
            "哥~ 最近想您啦！😢 "
            "游戏新开了一个超火爆的区，装备爆率直接翻倍！"
            "要不要回来试试？小妹给您准备了回归大礼包！"
        ),
        "professional": (
            "您好，近期游戏新区开放，装备爆率大幅提升。"
            "欢迎回游体验，如需协助请联系客服。"
        ),
        "steady": (
            "老板，好久不见！新区刚开，爆率翻倍，"
            "要不要回来看看，小妹帮您准备了回归礼。"
        ),
        "humorous": (
            "哥，消失这么久，是练到满级了还是去充钱了？😏 "
            "新区爆率翻倍，快回来薅羊毛！"
        ),
    },
    # ── EXCEPTION ────────────────────────────────────────────────────────────
    SOPState.EXCEPTION: {
        "lively": "哥不好意思，小妹这边遇到了点小问题，马上帮您转接专属客服！",
        "professional": "非常抱歉，处理过程中出现异常，正在为您转接人工客服，请稍候。",
        "steady": "老板不好意思，这边有个小状况，已经通知人工帮您跟进了。",
        "humorous": "哥，出BUG了…小妹赶紧召唤人工客服来帮您！",
    },
}

# ── Info-extraction helpers ────────────────────────────────────────────────────

# Characters that are NOT valid as the start of an area-name prefix
# (pronouns, common verbs, particles).  Used to trim over-captured prefixes.
_AREA_STOP_CHARS: set[str] = set("我在是了的也都玩你他它她吧呢哦啊嘛哈好对嗯正")

# Stopwords that should not be mistaken for a role name
_ROLE_STOPWORDS = {
    "的", "我", "我的", "是", "就是", "对", "好", "谢谢", "您好", "你好",
    "OK", "ok", "好的", "嗯", "哦", "啊", "呢", "吧",
}

# Patterns for role_name (ordered from most specific to least)
_ROLE_PATTERNS: list[re.Pattern] = [
    # 角色名：战神无双 / 角色名叫战神无双
    re.compile(r"角色名[：:是叫为\s]\s*([\S]{2,12})", re.UNICODE),
    # 角色叫战神无双 / 角色：战神无双 / 角色是战神无双
    re.compile(r"角色[：:是叫为\s]\s*([\S]{2,12})", re.UNICODE),
    # 昵称叫战神无双
    re.compile(r"昵称[：:是叫为\s]\s*([\S]{2,12})", re.UNICODE),
    # 名字叫战神无双
    re.compile(r"名字[：:是叫为\s]\s*([\S]{2,12})", re.UNICODE),
    # 叫战神无双 / 叫做战神无双
    re.compile(r"叫做?\s*([\S]{2,12})", re.UNICODE),
    # 角色是战神无双
    re.compile(r"角色是\s*([\S]{2,12})", re.UNICODE),
]


def _extract_area_name(text: str) -> str | None:
    """
    Try to extract an area/server name from user text.

    Strategy (highest priority first):
    1. 第N区  →  "N区"
    2. N区 (bare digits) with optional ≤3-char area-name prefix that doesn't
       start with a pronoun/verb (e.g. "裁决18区" OK, "我在18区" → "18区").
    3. Chinese-numeral + 区  (一区 / 二区 …)
    """
    # 1. 第N区
    m = re.search(r"第(\d+)区", text)
    if m:
        return f"{m.group(1)}区"

    m = re.search(r"第([一二三四五六七八九十百]+)区", text)
    if m:
        return f"{m.group(1)}区"

    # 2. digits + 区 — capture an optional clean Chinese prefix (≤3 chars)
    m = re.search(r"(\d+)\s*区", text)
    if m:
        digit_start = m.start(1)
        # Walk backwards up to 3 Chinese chars before the digit
        prefix_chars: list[str] = []
        idx = digit_start - 1
        while idx >= 0 and len(prefix_chars) < 3:
            ch = text[idx]
            if "\u4e00" <= ch <= "\u9fff" and ch not in _AREA_STOP_CHARS:
                prefix_chars.insert(0, ch)
                idx -= 1
            else:
                break
        prefix = "".join(prefix_chars)
        return f"{prefix}{m.group(1)}区"

    # 3. Chinese numeral + 区
    m = re.search(r"([一二三四五六七八九十百]+区)", text)
    if m:
        return m.group(1)

    return None


def _extract_role_name(text: str, area_name: str | None = None) -> str | None:
    """
    Try to extract a role/character name from user text.
    Optionally strips the area_name out of the text first.
    """
    working = text
    if area_name:
        working = working.replace(area_name, "").strip()
    # Strip leading punctuation / connectors
    working = re.sub(r"^[\s，,。.、]+", "", working)
    # Strip leading single-char pronouns / ordinals that are never part of a name
    working = re.sub(r"^[我你他她它在第的]", "", working).strip()

    for pat in _ROLE_PATTERNS:
        m = pat.search(working)
        if m:
            candidate = m.group(1).strip()
            # Strip trailing connectors / particles that got captured
            candidate = re.sub(r"[，,。.、！!？?在里的吧呢哦啊嘛哈]+$", "", candidate)
            # Reject candidates that are themselves keywords or stopwords
            if (
                candidate not in _ROLE_STOPWORDS
                and 2 <= len(candidate) <= 12
                and not re.search(r"[区服大小中]$", candidate)
            ):
                return candidate

    # Heuristic fallback: if the remaining text (after stripping area/punctuation)
    # is 2–12 chars with no whitespace, treat it as the role name.
    remaining = re.sub(r"\s+", "", working)
    remaining = re.sub(r"[，,。.、！!？?]+", "", remaining)
    # Strip leading ordinals/particles that may remain after area removal
    remaining = re.sub(r"^[我你他她它在第的]", "", remaining)
    # Don't treat mixed digit+Chinese strings as role names (likely a mis-parsed area)
    if re.search(r"\d", remaining):
        return None
    if (
        2 <= len(remaining) <= 12
        and remaining not in _ROLE_STOPWORDS
        and not re.search(r"[区服]", remaining)
    ):
        return remaining

    return None


def _parse_user_info(text: str) -> tuple[str | None, str | None]:
    """
    Return (area_name, role_name) extracted from a free-form Chinese message.
    Either or both may be None when not found.
    """
    area = _extract_area_name(text)
    role = _extract_role_name(text, area_name=area)
    return area, role


# ── Utility helpers ───────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _safe_user_id(user_id: str) -> str:
    if not SAFE_USER_ID.fullmatch(user_id):
        raise HTTPException(status_code=400, detail="invalid user_id")
    return user_id


def _resolve_ext(ext: str) -> str:
    clean = ext.strip(".").lower()
    if clean not in ALLOWED_IMAGE_EXTS:
        raise HTTPException(status_code=400, detail="invalid screenshot_ext")
    return clean


def _load_screenshot_bytes(cfg: GameCSConfig, payload: GameMessageIn) -> bytes | None:
    """Download/decode screenshot bytes, or return None when absent."""
    if payload.screenshot_b64:
        if len(payload.screenshot_b64) > cfg.max_image_bytes * 2:
            raise HTTPException(status_code=413, detail="screenshot too large")
        try:
            raw = base64.b64decode(payload.screenshot_b64, validate=True)
        except Exception:
            raise HTTPException(status_code=400, detail="invalid screenshot_b64")
        if len(raw) > cfg.max_image_bytes:
            raise HTTPException(status_code=413, detail="screenshot too large")
        return raw

    if payload.screenshot_url:
        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.get(payload.screenshot_url)
                resp.raise_for_status()
                raw = resp.content
        except Exception:
            raise HTTPException(status_code=400, detail="cannot fetch screenshot_url")
        if len(raw) > cfg.max_image_bytes:
            raise HTTPException(status_code=413, detail="screenshot too large")
        return raw

    return None


def _persist_screenshot(cfg: GameCSConfig, payload: GameMessageIn) -> str | None:
    """Save screenshot bytes to uploads_dir; return the local file path or None."""
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


def _tmpl(state: SOPState, personality: str, **kwargs: str) -> str:
    """Render a personality template for a given SOP state."""
    templates = _TMPL.get(state, {})
    text = templates.get(personality) or templates.get("lively", "")
    for key, value in kwargs.items():
        text = text.replace(f"{{{key}}}", value)
    return text


def _build_codes_block(cfg: GameCSConfig) -> str:
    """Format the four daily codes into a readable block."""
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


# ── SOP state-machine handler ─────────────────────────────────────────────────

def _handle_message(
    cfg: GameCSConfig,
    store: GameCSStore,
    kb: OpenVikingKB,
    payload: GameMessageIn,
) -> GameReply:
    """
    Core SOP state machine.  Reads the current session state, advances it
    according to PRD rules, and returns the bot reply.
    """
    user_id = _safe_user_id(payload.user_id)
    user_text = payload.message.strip()
    personality = cfg.personality

    store.append_message(user_id, "user", user_text or "<no text>")
    session = store.get_or_create_session(user_id, default_game_name=cfg.default_game_name)

    # Persist screenshot if attached (used for OCR assist in COLLECTING_INFO)
    screenshot_path: str | None = None
    try:
        screenshot_path = _persist_screenshot(cfg, payload)
    except HTTPException:
        raise
    except Exception:
        pass  # non-fatal

    if screenshot_path:
        session = store.update_session(
            user_id,
            screenshot_path=screenshot_path,
            default_game_name=cfg.default_game_name,
        )

    def _reply(text: str, *, next_step: str | None = None,
               bound: bool = False, codes: dict | None = None) -> GameReply:
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

    # ─────────────────────────────────────────────────────────────────────────
    # STATE: GREETING
    # Bot should have already sent the greeting when the session was created.
    # When the user sends their first message, move to COLLECTING_INFO.
    # ─────────────────────────────────────────────────────────────────────────
    if session.sop_state == SOPState.GREETING:
        # Try to extract info right from the first message
        area, role = _parse_user_info(user_text)
        if area or role:
            session = store.update_session(
                user_id,
                sop_state=SOPState.COLLECTING_INFO,
                area_name=area,
                role_name=role,
                default_game_name=cfg.default_game_name,
            )
            # Re-use COLLECTING_INFO logic below
        else:
            session = store.update_session(
                user_id,
                sop_state=SOPState.COLLECTING_INFO,
                default_game_name=cfg.default_game_name,
            )

    # ─────────────────────────────────────────────────────────────────────────
    # STATE: COLLECTING_INFO
    # Extract area_name and role_name; retry up to max_collect_retries times.
    # ─────────────────────────────────────────────────────────────────────────
    if session.sop_state == SOPState.COLLECTING_INFO:
        # Parse new info from the current message (merge with already captured)
        area, role = _parse_user_info(user_text)
        new_area = area or session.area_name
        new_role = role or session.role_name

        # If screenshot was attached, note it; OCR extraction TBD (passed as note)
        if screenshot_path and not new_area and not new_role:
            # Screenshot present but no text info yet; ask for text confirmation
            session = store.update_session(
                user_id,
                area_name=new_area,
                role_name=new_role,
                retry_count=session.retry_count + 1,
                default_game_name=cfg.default_game_name,
            )
            retry_idx = min(session.retry_count - 1, len(CLARIFY_PROMPTS) - 1)
            clarify = CLARIFY_PROMPTS[retry_idx]
            msg = (
                "已收到截图，小妹正在核对~\n"
                f"{clarify}"
            )
            return _reply(msg, next_step="提供角色信息")

        if new_area and new_role:
            # Both fields captured → advance to VALIDATING
            session = store.update_session(
                user_id,
                sop_state=SOPState.VALIDATING,
                area_name=new_area,
                role_name=new_role,
                game_name=cfg.default_game_name,
                retry_count=0,
                default_game_name=cfg.default_game_name,
            )
        elif session.retry_count >= cfg.max_collect_retries:
            # Too many retries → escalate
            session = store.update_session(
                user_id,
                sop_state=SOPState.EXCEPTION,
                default_game_name=cfg.default_game_name,
            )
            msg = _tmpl(SOPState.EXCEPTION, personality)
            return _reply(msg, next_step="人工介入")
        else:
            # Missing info → ask again
            session = store.update_session(
                user_id,
                area_name=new_area,
                role_name=new_role,
                retry_count=session.retry_count + 1,
                default_game_name=cfg.default_game_name,
            )
            retry_idx = min(session.retry_count - 1, len(CLARIFY_PROMPTS) - 1)
            retry_prompt = CLARIFY_PROMPTS[retry_idx]
            msg = _tmpl(SOPState.COLLECTING_INFO, personality, retry_prompt=retry_prompt)
            return _reply(msg, next_step="提供区服和角色名")

    # ─────────────────────────────────────────────────────────────────────────
    # STATE: VALIDATING
    # Verify role against the game API (mock mode skips the real call).
    # ─────────────────────────────────────────────────────────────────────────
    if session.sop_state == SOPState.VALIDATING:
        validation_ok = _validate_role(cfg, session)
        if validation_ok:
            session = store.update_session(
                user_id,
                sop_state=SOPState.BINDING,
                default_game_name=cfg.default_game_name,
            )
        else:
            # Validation failed → go back to collecting_info
            session = store.update_session(
                user_id,
                sop_state=SOPState.COLLECTING_INFO,
                area_name=None,
                role_name=None,
                retry_count=0,
                default_game_name=cfg.default_game_name,
            )
            msg = (
                "哥，没查到这个角色呢，是不是区服或者名字输错啦？"
                "麻烦再发一下几区和角色名~"
            )
            return _reply(msg, next_step="重新提供角色信息")

    # ─────────────────────────────────────────────────────────────────────────
    # STATE: BINDING
    # Bind the user ↔ game role (mock mode stores locally).
    # ─────────────────────────────────────────────────────────────────────────
    if session.sop_state == SOPState.BINDING:
        bind_ok = _bind_user(cfg, session)
        if bind_ok:
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
            msg = _tmpl(SOPState.EXCEPTION, personality)
            return _reply(msg, next_step="人工介入")

    # ─────────────────────────────────────────────────────────────────────────
    # STATE: SENDING_CODE
    # Send the four daily codes and register follow-up timers.
    # ─────────────────────────────────────────────────────────────────────────
    if session.sop_state == SOPState.SENDING_CODE:
        codes_block = _build_codes_block(cfg)
        msg = _tmpl(SOPState.SENDING_CODE, personality, codes_block=codes_block)
        codes = _codes_dict(cfg)
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

    # ─────────────────────────────────────────────────────────────────────────
    # STATE: FOLLOW_UP_PENDING / FOLLOW_UP_30MIN / FOLLOW_UP_1HOUR
    # User sent a message while waiting for follow-up timers.
    # Cancel pending follow-ups and answer via KB.
    # ─────────────────────────────────────────────────────────────────────────
    if session.sop_state in (
        SOPState.FOLLOW_UP_PENDING,
        SOPState.FOLLOW_UP_30MIN,
        SOPState.FOLLOW_UP_1HOUR,
    ):
        if user_text:
            # Cancel pending 30-min follow-up if not yet sent
            if not session.follow_up_30m_sent:
                store.update_session(
                    user_id,
                    follow_up_30m_sent=True,
                    default_game_name=cfg.default_game_name,
                )
            return _kb_reply(user_id, user_text, cfg, store, kb, session, personality)

    # ─────────────────────────────────────────────────────────────────────────
    # STATE: SILENT / NEXT_DAY_VISIT / REACTIVATION / COMPLETED
    # Post-SOP: answer user questions via KB search.
    # ─────────────────────────────────────────────────────────────────────────
    if session.sop_state in (
        SOPState.SILENT,
        SOPState.NEXT_DAY_VISIT,
        SOPState.REACTIVATION,
        SOPState.COMPLETED,
    ):
        if user_text:
            return _kb_reply(user_id, user_text, cfg, store, kb, session, personality)

    # ─────────────────────────────────────────────────────────────────────────
    # FALLBACK: unhandled state — just do a KB search if we have text
    # ─────────────────────────────────────────────────────────────────────────
    if user_text:
        return _kb_reply(user_id, user_text, cfg, store, kb, session, personality)

    # Empty message, no state change
    fallback = "有什么需要帮忙的，随时找小妹~😊"
    return _reply(fallback)


# ── KB-based reply helper ─────────────────────────────────────────────────────

def _kb_reply(
    user_id: str,
    user_text: str,
    cfg: GameCSConfig,
    store: GameCSStore,
    kb: OpenVikingKB,
    session: SOPSessionState,
    personality: str,
) -> GameReply:
    """
    Answer a user question using OpenViking knowledge base search.
    Uses context-aware search() when message history is available,
    falls back to simple find() otherwise.
    """
    history = store.get_recent_messages(user_id, limit=8)

    if len(history) >= 2:
        kb_lines = kb.search_with_context(user_text, history=history, limit=4)
    else:
        kb_lines = kb.search(user_text, limit=4)

    if kb_lines:
        intro = {
            "lively": "找到啦哥！知识库里有这些参考：\n",
            "professional": "以下是相关知识库信息，供您参考：\n",
            "steady": "小妹查到了一些信息，希望对您有帮助：\n",
            "humorous": "情报get！知识库给我发现了这些：\n",
        }.get(personality, "相关信息如下：\n")
        reply_text = intro + "\n".join(f"• {x}" for x in kb_lines)
    else:
        reply_text = {
            "lively": "哥，小妹暂时没找到这个问题的答案呢，麻烦描述得更详细一点，我再帮您查~😊",
            "professional": "暂未检索到相关信息，请提供更多细节，我们将尽快协助您。",
            "steady": "这个问题小妹没有现成答案，您能多说一点情况吗？小妹再帮您查找。",
            "humorous": "啊这，知识库也不知道…您再描述详细一点，小妹再施法！🔮",
        }.get(personality, "暂时没有找到相关信息，请描述更多细节。")

    store.append_message(user_id, "assistant", reply_text)
    return GameReply(
        status="ok",
        reply=reply_text,
        sop_state=session.sop_state,
        next_step=None,
        bound=session.is_bound,
        timestamp=_now(),
    )


# ── Game API stubs ─────────────────────────────────────────────────────────────

def _validate_role(cfg: GameCSConfig, session: SOPSessionState) -> bool:
    """
    Validate area_name + role_name against the game API.

    When ``cfg.mock_api`` is True (default for dev/demo), always returns True.
    In production, set GAME_CS_MOCK_API=false and GAME_CS_GAME_API_BASE to the
    real game server URL.
    """
    if cfg.mock_api:
        return True  # Skip real validation in demo mode

    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.post(
                f"{cfg.game_api_base}/api/game/verify_role",
                json={
                    "game_name": session.game_name,
                    "area_name": session.area_name,
                    "role_name": session.role_name,
                },
            )
            data = resp.json()
            return bool(data.get("success"))
    except Exception:
        return False


def _bind_user(cfg: GameCSConfig, session: SOPSessionState) -> bool:
    """
    Bind user_id ↔ game role via the game API.

    Mock mode always succeeds.  In production, set GAME_CS_MOCK_API=false.
    """
    if cfg.mock_api:
        return True

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(
                f"{cfg.game_api_base}/api/user/bind",
                json={
                    "user_id": session.user_id,
                    "game_name": session.game_name,
                    "area_name": session.area_name,
                    "role_name": session.role_name,
                },
            )
            data = resp.json()
            return bool(data.get("success"))
    except Exception:
        return False


# ── FastAPI application factory ────────────────────────────────────────────────

def create_app(config: GameCSConfig | None = None) -> FastAPI:
    cfg = config or GameCSConfig.from_env()
    store = GameCSStore(cfg.db_path)
    kb = OpenVikingKB(cfg.openviking_path, cfg.openviking_target_uri)

    app = FastAPI(
        title="Nanobot 大楚复古智能客服",
        description="SOP-driven game customer service built on NanoBot + OpenViking",
        version="2.0.0",
    )

    # ── Health check ──────────────────────────────────────────────────────────

    @app.get("/healthz", tags=["system"])
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": app.version}

    # ── Admin: index knowledge base ───────────────────────────────────────────

    @app.post("/admin/index-kb", tags=["admin"])
    def index_kb(
        paths: list[str],
        x_game_cs_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """Index local files / URLs into OpenViking knowledge base."""
        if x_game_cs_token != cfg.service_token:
            raise HTTPException(status_code=401, detail="invalid token")
        try:
            roots = kb.add_resources(paths, wait=True)
            return {"ok": True, "indexed": roots}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "indexed": []}

    # ── Admin: update daily codes ─────────────────────────────────────────────

    # Mutable container so inner functions can rebind cfg (frozen dataclass).
    _cfg_box: list[GameCSConfig] = [cfg]

    @app.post("/admin/update-codes", tags=["admin"])
    def update_codes(
        daily_checkin: str,
        lucky_draw: str,
        universal: str,
        guild: str,
        x_game_cs_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """
        Hot-update today's redeem codes without restarting the service.

        In production you can also rotate codes by restarting with updated
        GAME_CS_CODE_* environment variables.  This endpoint is provided for
        convenience (e.g. called from a daily cron job after the operator
        refreshes the codes).
        """
        if x_game_cs_token != _cfg_box[0].service_token:
            raise HTTPException(status_code=401, detail="invalid token")
        import dataclasses
        _cfg_box[0] = dataclasses.replace(
            _cfg_box[0],
            code_daily_checkin=daily_checkin,
            code_lucky_draw=lucky_draw,
            code_universal=universal,
            code_guild=guild,
        )
        return {"ok": True, "codes": _codes_dict(_cfg_box[0])}

    # ── Admin: reset a user session ───────────────────────────────────────────

    @app.post("/admin/reset-session", tags=["admin"])
    def reset_session(
        user_id: str,
        x_game_cs_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """Reset a user's SOP session back to GREETING (for testing / re-registration)."""
        if x_game_cs_token != cfg.service_token:
            raise HTTPException(status_code=401, detail="invalid token")
        _safe_user_id(user_id)
        state = store.reset_session(user_id, default_game_name=cfg.default_game_name)
        return {"ok": True, "sop_state": state.sop_state}

    # ── Main webhook ──────────────────────────────────────────────────────────

    @app.post("/webhook/game-message", response_model=GameReply, tags=["webhook"])
    def on_message(
        payload: GameMessageIn,
        background_tasks: BackgroundTasks,
        x_game_cs_token: str | None = Header(default=None),
    ) -> GameReply:
        """
        Receive a player message and return the next SOP-driven bot reply.

        The bot will:
        1. Advance through GREETING → COLLECTING_INFO → VALIDATING → BINDING
           → SENDING_CODE automatically.
        2. After sending codes (FOLLOW_UP_PENDING), answer via KB search.
        3. On session completion, commit the conversation to OpenViking memory
           as a background task.
        """
        if x_game_cs_token != cfg.service_token:
            raise HTTPException(status_code=401, detail="invalid token")

        # First-time greeting: if no session exists yet, create and greet.
        _safe_user_id(payload.user_id)
        existing = store.get_or_create_session(
            payload.user_id, default_game_name=cfg.default_game_name
        )
        if existing.sop_state == SOPState.GREETING and not payload.message.strip():
            # Bot proactively sends greeting on first contact (empty message)
            greeting_text = _tmpl(SOPState.GREETING, cfg.personality)
            store.append_message(payload.user_id, "assistant", greeting_text)
            return GameReply(
                status="ok",
                reply=greeting_text,
                sop_state=SOPState.GREETING,
                next_step="提供区服和角色名",
                bound=False,
                timestamp=_now(),
            )

        reply = _handle_message(cfg, store, kb, payload)

        # Background: commit conversation memory once the user is bound
        if reply.bound:
            history = store.get_recent_messages(payload.user_id, limit=20)
            background_tasks.add_task(
                kb.commit_session,
                messages=history,
                user_id=payload.user_id,
            )

        return reply

    # ── Cron: process due follow-ups ──────────────────────────────────────────

    @app.post("/cron/process-followups", tags=["cron"])
    def process_followups(
        x_game_cs_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """
        Called by an external scheduler (e.g. system cron, APScheduler) to
        identify users whose follow-up messages are due and return them so the
        caller can push the messages via the appropriate channel.

        Response shape::

            {
              "followup_30m": [
                {"user_id": "...", "message": "..."},
                ...
              ],
              "followup_1h": [
                {"user_id": "...", "message": "..."},
                ...
              ]
            }

        The caller is responsible for delivering the messages over the
        platform (WeChat / MoChat / Telegram / etc.) and then marking them
        sent by calling PATCH /cron/mark-followup-sent.
        """
        if x_game_cs_token != cfg.service_token:
            raise HTTPException(status_code=401, detail="invalid token")

        now_iso = _now_iso()

        # 30-minute follow-ups
        due_30m = store.get_pending_30m_followups(now_iso)
        followup_30m_results = []
        for s in due_30m:
            msg = _tmpl(SOPState.FOLLOW_UP_30MIN, cfg.personality)
            store.update_session(
                s.user_id,
                sop_state=SOPState.FOLLOW_UP_30MIN,
                follow_up_30m_sent=True,
                default_game_name=cfg.default_game_name,
            )
            store.append_message(s.user_id, "assistant", msg)
            followup_30m_results.append({"user_id": s.user_id, "message": msg})

        # 1-hour follow-ups (fission)
        due_1h = store.get_pending_1h_followups(now_iso)
        followup_1h_results = []
        for s in due_1h:
            msg = _tmpl(SOPState.FOLLOW_UP_1HOUR, cfg.personality)
            store.update_session(
                s.user_id,
                sop_state=SOPState.FOLLOW_UP_1HOUR,
                follow_up_1h_sent=True,
                default_game_name=cfg.default_game_name,
            )
            store.append_message(s.user_id, "assistant", msg)
            followup_1h_results.append({"user_id": s.user_id, "message": msg})

        return {
            "followup_30m": followup_30m_results,
            "followup_1h": followup_1h_results,
            "processed_at": now_iso,
        }

    # ── Cron: next-day visits ─────────────────────────────────────────────────

    @app.post("/cron/next-day-visits", tags=["cron"])
    def next_day_visits(
        x_game_cs_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """
        Return users who are due for their next-day follow-up visit.
        Marks them as visited in the store.
        """
        if x_game_cs_token != cfg.service_token:
            raise HTTPException(status_code=401, detail="invalid token")

        now_iso = _now_iso()
        due = store.get_pending_next_day_visits(now_iso)
        results = []
        for s in due:
            msg = _tmpl(SOPState.NEXT_DAY_VISIT, cfg.personality)
            store.update_session(
                s.user_id,
                sop_state=SOPState.NEXT_DAY_VISIT,
                next_day_visited=True,
                default_game_name=cfg.default_game_name,
            )
            store.append_message(s.user_id, "assistant", msg)
            results.append({"user_id": s.user_id, "message": msg})

        return {"next_day_visits": results, "processed_at": now_iso}

    # ── Session info (for debugging / admin panel) ────────────────────────────

    @app.get("/admin/session/{user_id}", tags=["admin"])
    def get_session(
        user_id: str,
        x_game_cs_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """Retrieve the current SOP session state for a user."""
        if x_game_cs_token != cfg.service_token:
            raise HTTPException(status_code=401, detail="invalid token")
        _safe_user_id(user_id)
        s = store.get_or_create_session(user_id, default_game_name=cfg.default_game_name)
        return {
            "user_id": s.user_id,
            "sop_state": s.sop_state,
            "game_name": s.game_name,
            "area_name": s.area_name,
            "role_name": s.role_name,
            "is_bound": s.is_bound,
            "codes_sent_at": s.codes_sent_at,
            "follow_up_30m_sent": s.follow_up_30m_sent,
            "follow_up_1h_sent": s.follow_up_1h_sent,
            "next_day_visited": s.next_day_visited,
            "created_at": s.created_at,
            "updated_at": s.updated_at,
        }

    return app


# ── CLI entry point ────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the 大楚复古 game customer service webhook server"
    )
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", default=8011, type=int, help="Bind port (default: 8011)")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload (dev mode)")
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
