from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Awaitable, Callable

from nanobot.bus.events import OutboundMessage
from nanobot.channels.base import BaseChannel

logger = logging.getLogger(__name__)


class GameCSChannelBridge:
    """
    Bridge inbound channel messages to game_cs logic and send replies back.
    """

    def __init__(
        self,
        channel: BaseChannel,
        on_inbound: Callable[[str, str, str, str | None, str | None], Awaitable[str | None]],
    ):
        self.channel = channel
        self.on_inbound = on_inbound
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._dispatch_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _dispatch_loop(self) -> None:
        while True:
            try:
                msg = await self.channel.bus.consume_inbound()
                asyncio.create_task(self._handle(msg))
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("GameCSChannelBridge dispatch error: %s", exc)

    async def _handle(self, msg) -> None:
        screenshot_url = msg.media[0] if msg.media else None
        text = (msg.content or "").strip()

        # MoChat image-only messages typically have empty content.
        if screenshot_url and not text:
            text = ""

        try:
            reply = await self.on_inbound(
                msg.sender_id,
                msg.chat_id,
                text,
                screenshot_url,
                msg.channel,
            )
            if reply:
                await self.channel.send(
                    OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content=reply,
                    )
                )
        except Exception as exc:
            logger.exception("GameCSChannelBridge handle error: %s", exc)

    async def push(self, chat_id: str, text: str) -> None:
        try:
            await self.channel.send(
                OutboundMessage(
                    channel=self.channel.name,
                    chat_id=chat_id,
                    content=text,
                )
            )
        except Exception as exc:
            logger.warning("GameCSChannelBridge push error chat_id=%s: %s", chat_id, exc)
