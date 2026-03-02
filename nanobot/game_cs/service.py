from __future__ import annotations

import argparse
import base64
import hashlib
import re
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Header, HTTPException

from .config import GameCSConfig
from .models import GameMessageIn, GameReply
from .openviking_kb import OpenVikingKB
from .storage import GameCSStore

UID_PATTERN = re.compile(r"\b\d{6,20}\b")
SAFE_USER_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
ALLOWED_IMAGE_EXTS = {"png", "jpg", "jpeg", "webp"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _extract_uid(text: str) -> str | None:
    m = UID_PATTERN.search(text)
    return m.group(0) if m else None


def _extract_server(text: str) -> str | None:
    text = text.strip()
    if not text:
        return None
    lowered = text.lower()
    for kw in ("一区", "二区", "三区", "asia", "eu", "na", "sea", "jp", "kr", "美服", "欧服", "亚服"):
        if kw in lowered:
            return text
    if "服" in text:
        return text
    return None


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
    if payload.screenshot_b64:
        if len(payload.screenshot_b64) > (cfg.max_image_bytes * 2):
            raise HTTPException(status_code=413, detail="screenshot too large")
        try:
            raw = base64.b64decode(payload.screenshot_b64, validate=True)
        except Exception:
            raise HTTPException(status_code=400, detail="invalid screenshot_b64")
        if len(raw) > cfg.max_image_bytes:
            raise HTTPException(status_code=413, detail="screenshot too large")
        return raw
    if not payload.screenshot_url:
        return None

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(payload.screenshot_url)
            resp.raise_for_status()
            raw = resp.content
    except Exception:
        raise HTTPException(status_code=400, detail="invalid screenshot_url")
    if len(raw) > cfg.max_image_bytes:
        raise HTTPException(status_code=413, detail="screenshot too large")
    return raw


def _persist_screenshot(cfg: GameCSConfig, payload: GameMessageIn) -> str | None:
    raw = _load_screenshot_bytes(cfg, payload)
    if raw is None:
        return None

    cfg.uploads_dir.mkdir(parents=True, exist_ok=True)
    try:
        user_id = _safe_user_id(payload.user_id)
        ext = _resolve_ext(payload.screenshot_ext)
    except HTTPException:
        raise

    digest = hashlib.sha256(raw).hexdigest()[:16]
    path = cfg.uploads_dir / f"{user_id}_{digest}.{ext}"
    path.write_bytes(raw)
    return str(path)


def _build_guide(steps: list[str], idx: int) -> str:
    lines = []
    for i, step in enumerate(steps, start=1):
        prefix = ">>" if (i - 1) == idx else "  "
        lines.append(f"{prefix} 第{i}步：{step}")
    return "\n".join(lines)


def create_app(config: GameCSConfig | None = None) -> FastAPI:
    cfg = config or GameCSConfig.from_env()
    store = GameCSStore(cfg.db_path)
    kb = OpenVikingKB(cfg.openviking_path, cfg.openviking_target_uri)

    app = FastAPI(title="Nanobot Game Customer Service")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/admin/index-kb")
    def index_kb(paths: list[str], x_game_cs_token: str | None = Header(default=None)) -> dict:
        if x_game_cs_token != cfg.service_token:
            raise HTTPException(status_code=401, detail="invalid token")
        try:
            roots = kb.add_resources(paths, wait=True)
            return {"ok": True, "indexed": roots}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "indexed": []}

    @app.post("/webhook/game-message", response_model=GameReply)
    def on_message(payload: GameMessageIn, x_game_cs_token: str | None = Header(default=None)) -> GameReply:
        if x_game_cs_token != cfg.service_token:
            raise HTTPException(status_code=401, detail="invalid token")
        _safe_user_id(payload.user_id)

        user_text = payload.message.strip()
        store.append_message(payload.user_id, "user", user_text or "<empty>")
        state = store.get_or_create_binding(payload.user_id)

        screenshot_path = _persist_screenshot(cfg, payload)
        if screenshot_path:
            state = store.update_binding(payload.user_id, screenshot_path=screenshot_path, current_step=max(1, state.current_step))

        if state.status == "bound":
            kb_lines = kb.search(user_text, limit=3) if user_text else []
            if kb_lines:
                reply = "已绑定账号，以下是你问题的知识库答案：\n" + "\n".join(f"- {x}" for x in kb_lines)
            else:
                reply = "已绑定账号。请告诉我你遇到的问题，我会继续协助。"
            store.append_message(payload.user_id, "assistant", reply)
            return GameReply(status="ok", reply=reply, bound=True, next_step=None, timestamp=_now())

        current_step = state.current_step
        next_step_text = cfg.bind_steps[current_step] if current_step < len(cfg.bind_steps) else None

        if current_step == 0:
            if not (payload.screenshot_b64 or payload.screenshot_url):
                reply = "欢迎来到游戏客服。先发送一张“游戏内个人主页截图”用于绑定账号。"
                reply += "\n\n当前流程：\n" + _build_guide(cfg.bind_steps, 0)
                store.append_message(payload.user_id, "assistant", reply)
                return GameReply(status="ok", reply=reply, next_step=cfg.bind_steps[0], timestamp=_now())
            state = store.update_binding(payload.user_id, current_step=1)
            current_step = 1

        if current_step == 1:
            uid = _extract_uid(user_text)
            if not uid:
                reply = "已收到截图。请发送你的游戏UID（6-20位数字）。"
                reply += "\n\n当前流程：\n" + _build_guide(cfg.bind_steps, 1)
                store.append_message(payload.user_id, "assistant", reply)
                return GameReply(status="ok", reply=reply, next_step=cfg.bind_steps[1], timestamp=_now())
            state = store.update_binding(payload.user_id, game_uid=uid, current_step=2)
            current_step = 2

        if current_step == 2:
            server = _extract_server(user_text)
            if not server:
                reply = "请回复你的游戏区服，例如：`亚服一区`、`NA-2`。"
                reply += "\n\n当前流程：\n" + _build_guide(cfg.bind_steps, 2)
                store.append_message(payload.user_id, "assistant", reply)
                return GameReply(status="ok", reply=reply, next_step=cfg.bind_steps[2], timestamp=_now())
            state = store.update_binding(payload.user_id, server=server, current_step=3)
            current_step = 3

        if current_step == 3:
            if "确认" not in user_text and "ok" not in user_text.lower():
                reply = (
                    "请回复“确认绑定”完成绑定。\n"
                    f"UID: {state.game_uid or '(未识别)'}\n"
                    f"区服: {state.server or '(未填写)'}"
                )
                reply += "\n\n当前流程：\n" + _build_guide(cfg.bind_steps, 3)
                store.append_message(payload.user_id, "assistant", reply)
                return GameReply(status="ok", reply=reply, next_step=cfg.bind_steps[3], timestamp=_now())
            store.update_binding(payload.user_id, status="bound", current_step=4)
            reply = "账号绑定完成。现在你可以直接描述游戏问题，我会基于知识库给你解决步骤。"
            store.append_message(payload.user_id, "assistant", reply)
            return GameReply(status="ok", reply=reply, bound=True, next_step=None, timestamp=_now())

        reply = "请继续描述你的问题，我会继续协助。"
        store.append_message(payload.user_id, "assistant", reply)
        return GameReply(status="ok", reply=reply, bound=False, next_step=next_step_text, timestamp=_now())

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run nanobot game customer service webhook")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", default=8011, type=int)
    args = parser.parse_args()

    import uvicorn

    uvicorn.run("nanobot.game_cs.service:create_app", factory=True, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
