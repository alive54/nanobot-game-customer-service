#!/usr/bin/env python3
"""MoWebChat simulator for GameCS bridge testing.

Endpoints:
- GET  /                : UI page
- POST /api/inbound     : enqueue user message for bridge polling
- GET  /api/pull-inbound: bridge long-poll consumes one inbound message
- POST /api/receive     : receive bot outbound message
- GET  /api/inbox       : query received bot messages by chat_id
- POST /api/send        : optional direct forward to game webhook (debug path)
"""

from __future__ import annotations

import argparse
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

FORWARD_TIMEOUT_SECONDS = 15
DEFAULT_TARGET = "http://localhost:8011/webhook/game-message"

HTML_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>MoWebChat 模拟器</title>
  <style>
    :root {
      --bg: #f6f8fb;
      --card: #ffffff;
      --text: #1a1f2b;
      --muted: #5a6475;
      --line: #d6dce8;
      --accent: #0a66ff;
      --ok: #1b8f3a;
      --error: #cc2e43;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: linear-gradient(140deg, #f6f8fb, #eef3ff);
      color: var(--text);
      font-family: "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
    }
    .wrap {
      max-width: 980px;
      margin: 24px auto;
      padding: 0 14px;
      display: grid;
      grid-template-columns: 1fr;
      gap: 14px;
    }
    .card {
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 14px;
      box-shadow: 0 4px 14px rgba(19, 41, 86, 0.06);
    }
    h1, h2 { margin: 0 0 10px; }
    h1 { font-size: 20px; }
    h2 { font-size: 16px; }
    .grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }
    @media (max-width: 760px) {
      .grid { grid-template-columns: 1fr; }
    }
    label {
      display: block;
      margin-bottom: 6px;
      color: var(--muted);
      font-size: 13px;
    }
    input, textarea {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 9px;
      font-size: 14px;
      outline: none;
    }
    textarea { min-height: 90px; resize: vertical; }
    .row { margin-bottom: 10px; }
    .btn {
      border: 0;
      border-radius: 8px;
      background: var(--accent);
      color: white;
      padding: 10px 14px;
      cursor: pointer;
      font-size: 14px;
      margin-right: 8px;
      margin-bottom: 8px;
    }
    .btn:disabled { opacity: 0.6; cursor: not-allowed; }
    .status { margin-top: 8px; font-size: 13px; }
    .status.ok { color: var(--ok); }
    .status.error { color: var(--error); }
    .panel {
      border: 1px dashed var(--line);
      border-radius: 10px;
      padding: 10px;
      background: #fbfcff;
      min-height: 120px;
      overflow: auto;
      max-height: 420px;
      white-space: pre-wrap;
      word-break: break-word;
      font-family: Consolas, "Courier New", monospace;
      font-size: 13px;
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>MoWebChat 模拟器</h1>
      <div class="grid">
        <div class="row">
          <label>user_id / chat_id</label>
          <input id="user_id" value="demo_user_001" />
        </div>
        <div class="row">
          <label>screenshot_url（可选）</label>
          <input id="screenshot_url" placeholder="http://..." />
        </div>
        <div class="row">
          <label>目标 webhook（仅直连调试用）</label>
          <input id="target" value="http://localhost:8011/webhook/game-message" />
        </div>
        <div class="row">
          <label>X-Game-CS-Token（仅直连调试用）</label>
          <input id="token" placeholder="可留空" />
        </div>
      </div>
      <div class="row">
        <label>message</label>
        <textarea id="message" placeholder="输入消息"></textarea>
      </div>
      <button class="btn" id="bridgeBtn">发送到 Bridge 队列</button>
      <button class="btn" id="directBtn">直连 webhook（调试）</button>
      <div id="status" class="status"></div>
    </div>

    <div class="card">
      <h2>最近一次发送结果</h2>
      <div id="reply" class="panel"></div>
    </div>

    <div class="card">
      <h2>接收接口消息（/api/receive）</h2>
      <div id="inbox" class="panel"></div>
    </div>
  </div>

  <script>
    const statusEl = document.getElementById('status');
    const replyEl = document.getElementById('reply');
    const inboxEl = document.getElementById('inbox');

    function setStatus(text, ok) {
      statusEl.textContent = text;
      statusEl.className = 'status ' + (ok ? 'ok' : 'error');
    }

    function buildBasePayload() {
      return {
        user_id: document.getElementById('user_id').value.trim(),
        message: document.getElementById('message').value,
        screenshot_url: document.getElementById('screenshot_url').value.trim()
      };
    }

    async function sendToBridge() {
      const payload = buildBasePayload();
      const resp = await fetch('/api/inbound', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      const data = await resp.json();
      replyEl.textContent = JSON.stringify(data, null, 2);
      setStatus(data.ok ? '已进入 Bridge 队列' : '发送失败', data.ok);
    }

    async function sendDirect() {
      const payload = {
        ...buildBasePayload(),
        target: document.getElementById('target').value.trim(),
        token: document.getElementById('token').value.trim()
      };
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 30000);
      try {
        const resp = await fetch('/api/send', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
          signal: controller.signal
        });
        const data = await resp.json();
        replyEl.textContent = JSON.stringify(data, null, 2);
        setStatus(data.ok ? '直连发送成功' : '直连发送失败', data.ok);
      } catch (err) {
        replyEl.textContent = err.name === 'AbortError' ? '请求超时' : '请求失败: ' + String(err);
        setStatus('直连发送失败', false);
      } finally {
        clearTimeout(timeoutId);
      }
    }

    async function refreshInbox() {
      const userId = document.getElementById('user_id').value.trim();
      const url = '/api/inbox?chat_id=' + encodeURIComponent(userId) + '&limit=50';
      try {
        const resp = await fetch(url);
        const data = await resp.json();
        inboxEl.textContent = JSON.stringify(data, null, 2);
      } catch (err) {
        inboxEl.textContent = 'inbox 拉取失败: ' + String(err);
      }
    }

    document.getElementById('bridgeBtn').addEventListener('click', sendToBridge);
    document.getElementById('directBtn').addEventListener('click', sendDirect);
    setInterval(refreshInbox, 1500);
    refreshInbox();
  </script>
</body>
</html>
"""


class AppState:
    def __init__(self) -> None:
        self._cv = threading.Condition()
        self.inbox_by_chat: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.sent_log: list[dict[str, Any]] = []
        self.inbound_queue: deque[dict[str, Any]] = deque()

    def add_inbound(self, item: dict[str, Any]) -> int:
        with self._cv:
            self.inbound_queue.append(item)
            qsize = len(self.inbound_queue)
            self._cv.notify_all()
            return qsize

    def pull_inbound(self, wait_ms: int) -> dict[str, Any] | None:
        timeout = max(0.0, wait_ms / 1000.0)
        with self._cv:
            if not self.inbound_queue:
                self._cv.wait(timeout=timeout)
            if not self.inbound_queue:
                return None
            return self.inbound_queue.popleft()

    def add_inbox(self, chat_id: str, item: dict[str, Any]) -> None:
        with self._cv:
            self.inbox_by_chat[chat_id].append(item)

    def get_inbox(self, chat_id: str, limit: int) -> list[dict[str, Any]]:
        with self._cv:
            items = self.inbox_by_chat.get(chat_id, [])
            if limit <= 0:
                return items[:]
            return items[-limit:]

    def add_sent_log(self, item: dict[str, Any]) -> None:
        with self._cv:
            self.sent_log.append(item)
            if len(self.sent_log) > 200:
                self.sent_log = self.sent_log[-200:]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except ValueError:
        length = 0
    raw = handler.rfile.read(length) if length > 0 else b""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _post_json(url: str, payload: dict[str, Any], token: str | None) -> tuple[int, dict[str, Any], str]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Game-CS-Token"] = token
    req = urllib.request.Request(url=url, data=body, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=FORWARD_TIMEOUT_SECONDS) as resp:
            status = resp.getcode()
            text = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        status = e.code
        text = e.read().decode("utf-8", errors="replace")
    except Exception as e:
        return 0, {}, str(e)

    if not text.strip():
        return status, {}, ""
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return status, parsed, ""
        return status, {"raw": parsed}, ""
    except json.JSONDecodeError:
        return status, {"raw": text}, ""


class SimulatorHandler(BaseHTTPRequestHandler):
    server_version = "MoWebChatSim/0.2"

    @property
    def state(self) -> AppState:
        return self.server.state  # type: ignore[attr-defined]

    @property
    def default_target(self) -> str:
        return self.server.default_target  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {self.address_string()} {fmt % args}")

    def _send_json(self, code: int, data: dict[str, Any]) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_html(self, html: str) -> None:
        raw = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/":
            return self._send_html(HTML_PAGE)

        if path == "/api/inbox":
            qs = urllib.parse.parse_qs(parsed.query)
            chat_id = (qs.get("chat_id") or [""])[0].strip()
            if not chat_id:
                return self._send_json(400, {"ok": False, "error": "chat_id is required"})
            limit_raw = (qs.get("limit") or ["50"])[0]
            try:
                limit = max(1, min(500, int(limit_raw)))
            except ValueError:
                limit = 50
            items = self.state.get_inbox(chat_id, limit)
            return self._send_json(200, {"ok": True, "chat_id": chat_id, "items": items})

        if path == "/api/pull-inbound":
            qs = urllib.parse.parse_qs(parsed.query)
            wait_raw = (qs.get("wait_ms") or ["15000"])[0]
            try:
                wait_ms = max(0, min(30000, int(wait_raw)))
            except ValueError:
                wait_ms = 15000

            item = self.state.pull_inbound(wait_ms)
            if item is None:
                return self._send_json(204, {"ok": True, "item": None})
            return self._send_json(200, {"ok": True, "item": item})

        return self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/api/inbound":
            payload = _read_json(self)
            user_id = str(payload.get("user_id", "")).strip()
            message = str(payload.get("message", ""))
            screenshot_url = str(payload.get("screenshot_url", "")).strip()
            metadata = payload.get("metadata")
            if not isinstance(metadata, dict):
                metadata = {}

            if not user_id:
                return self._send_json(400, {"ok": False, "error": "user_id is required"})

            item = {
                "sender_id": user_id,
                "chat_id": user_id,
                "message": message,
                "media": [screenshot_url] if screenshot_url else [],
                "metadata": {
                    "source": "mowebchat-ui",
                    **metadata,
                },
                "at": _utc_now_iso(),
            }
            qsize = self.state.add_inbound(item)
            return self._send_json(200, {"ok": True, "queued": item, "queue_size": qsize})

        if parsed.path == "/api/send":
            payload = _read_json(self)
            user_id = str(payload.get("user_id", "")).strip()
            message = str(payload.get("message", ""))
            screenshot_url = str(payload.get("screenshot_url", "")).strip()
            token = str(payload.get("token", "")).strip()
            target = str(payload.get("target", "")).strip() or self.default_target

            if not user_id:
                return self._send_json(400, {"ok": False, "error": "user_id is required"})

            forward_payload: dict[str, Any] = {
                "user_id": user_id,
                "message": message,
                "screenshot_b64": None,
                "screenshot_ext": "png",
                "screenshot_url": screenshot_url or None,
                "metadata": {
                    "source": "mowebchat-simulator-direct",
                    "chat_id": user_id,
                    "channel": "mowebchat",
                },
            }

            status, remote_data, err = _post_json(target, forward_payload, token or None)
            log_item = {
                "at": _utc_now_iso(),
                "target": target,
                "request": forward_payload,
                "status": status,
                "response": remote_data,
                "error": err,
            }
            self.state.add_sent_log(log_item)

            if err:
                return self._send_json(502, {"ok": False, "error": err, "status": 0})

            ok = 200 <= status < 300
            return self._send_json(
                status if status > 0 else 500,
                {
                    "ok": ok,
                    "target": target,
                    "status": status,
                    "request": forward_payload,
                    "response": remote_data,
                },
            )

        if parsed.path == "/api/receive":
            payload = _read_json(self)
            chat_id = str(payload.get("chat_id", "")).strip()
            text = str(payload.get("text", ""))
            channel = str(payload.get("channel", "")).strip() or "unknown"
            metadata = payload.get("metadata")
            if not isinstance(metadata, dict):
                metadata = {}

            if not chat_id:
                return self._send_json(400, {"ok": False, "error": "chat_id is required"})

            item = {
                "at": _utc_now_iso(),
                "chat_id": chat_id,
                "text": text,
                "channel": channel,
                "metadata": metadata,
            }
            self.state.add_inbox(chat_id, item)
            return self._send_json(200, {"ok": True, "saved": item})

        return self._send_json(404, {"ok": False, "error": "not found"})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MoWebChat simulator")
    parser.add_argument("--host", default="127.0.0.1", help="listen host")
    parser.add_argument("--port", type=int, default=8099, help="listen port")
    parser.add_argument("--target", default=DEFAULT_TARGET, help="default webhook target")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    server = ThreadingHTTPServer((args.host, args.port), SimulatorHandler)
    server.state = AppState()  # type: ignore[attr-defined]
    server.default_target = args.target  # type: ignore[attr-defined]

    print(f"[mowebchat-sim] listening on http://{args.host}:{args.port}")
    print(f"[mowebchat-sim] default forward target: {args.target}")
    print("[mowebchat-sim] endpoints: GET /, POST /api/inbound, GET /api/pull-inbound, POST /api/receive, GET /api/inbox, POST /api/send")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        time.sleep(0.05)


if __name__ == "__main__":
    main()
