"""Mowebchat channel adapter for GameCS bridge testing."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import MowebchatConfig


class MowebchatChannel(BaseChannel):
    """HTTP polling channel for local mowebchat simulator."""

    name = "mowebchat"

    def __init__(self, config: MowebchatConfig, bus: MessageBus):
        super().__init__(config, bus)
        self.config: MowebchatConfig = config
        self._http: httpx.AsyncClient | None = None
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._http = httpx.AsyncClient(timeout=self.config.timeout_s)
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("Mowebchat channel started: {}", self.config.base_url)
        await self._task

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._task = None
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        logger.info("Mowebchat channel stopped")

    async def send(self, msg: OutboundMessage) -> None:
        if not self._http:
            raise RuntimeError("Mowebchat HTTP client not initialized")

        text_parts: list[str] = []
        if msg.content and msg.content.strip():
            text_parts.append(msg.content.strip())
        text_parts.extend(m.strip() for m in msg.media if isinstance(m, str) and m.strip())
        text = "\n".join(text_parts).strip()
        if not text:
            return

        body = {
            "chat_id": msg.chat_id,
            "text": text,
            "channel": self.name,
            "metadata": msg.metadata or {},
        }
        await self._post_json(self.config.receive_path, body)

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                data = await self._poll_once()
                item = data.get("item")
                if not isinstance(item, dict):
                    continue

                sender_id = str(item.get("sender_id", "")).strip()
                chat_id = str(item.get("chat_id", "")).strip()
                content = str(item.get("message", ""))
                media = item.get("media")
                metadata = item.get("metadata")

                if not sender_id or not chat_id:
                    logger.warning("mowebchat inbound missing sender_id/chat_id: {}", item)
                    continue

                if not isinstance(media, list):
                    media = []
                media = [m for m in media if isinstance(m, str) and m.strip()]

                if not isinstance(metadata, dict):
                    metadata = {}

                await self._handle_message(
                    sender_id=sender_id,
                    chat_id=chat_id,
                    content=content,
                    media=media,
                    metadata=metadata,
                )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Mowebchat poll error: {}", exc)
                await asyncio.sleep(max(0.1, self.config.retry_delay_ms / 1000.0))

    async def _poll_once(self) -> dict[str, Any]:
        assert self._http is not None
        url = f"{self.config.base_url.rstrip('/')}{self.config.pull_path}"
        response = await self._http.get(url, params={"wait_ms": self.config.pull_wait_ms})
        if response.status_code == 204:
            return {}
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, dict) else {}

    async def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        assert self._http is not None
        url = f"{self.config.base_url.rstrip('/')}{path}"
        response = await self._http.post(url, json=payload)
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, dict) else {}
