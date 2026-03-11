"""
Run game_cs with a real channel bridge.

Example:
    python -m nanobot.game_cs.bridge_runner --channel mochat --host 127.0.0.1 --port 8011
"""

from __future__ import annotations

import argparse
import asyncio
import os

import uvicorn

from nanobot.bus.queue import MessageBus
from nanobot.config.loader import load_config
from nanobot.game_cs.config import GameCSConfig
from nanobot.game_cs.service import create_app


def _build_channel(channel_name: str, bus: MessageBus):
    cfg = load_config()
    if channel_name == "mowebchat":
        from nanobot.channels.mowebchat import MowebchatChannel

        channel_cfg = cfg.channels.mowebchat.model_copy(
            update={
                "base_url": os.getenv("MOWEBCHAT_BASE_URL", cfg.channels.mowebchat.base_url),
                "pull_wait_ms": int(
                    os.getenv("MOWEBCHAT_PULL_WAIT_MS", str(cfg.channels.mowebchat.pull_wait_ms))
                ),
                "timeout_s": float(
                    os.getenv("MOWEBCHAT_TIMEOUT_S", str(cfg.channels.mowebchat.timeout_s))
                ),
            }
        )
        return MowebchatChannel(
            channel_cfg,
            bus,
        )
    if channel_name == "mochat":
        from nanobot.channels.mochat import MochatChannel

        return MochatChannel(cfg.channels.mochat, bus)
    if channel_name == "dingtalk":
        from nanobot.channels.dingtalk import DingTalkChannel

        return DingTalkChannel(cfg.channels.dingtalk, bus)
    raise ValueError(f"unsupported channel: {channel_name}")


async def _run(channel_name: str, host: str, port: int) -> None:
    bus = MessageBus()
    channel = _build_channel(channel_name, bus)
    game_cs_config = GameCSConfig.from_env()
    app = create_app(config=game_cs_config, channel=channel)

    channel_task = asyncio.create_task(channel.start())
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="info"))

    try:
        await server.serve()
    finally:
        channel_task.cancel()
        await channel.stop()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--channel", default="mowebchat", help="Channel name: mowebchat | mochat | dingtalk"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8011)
    args = parser.parse_args()
    asyncio.run(_run(args.channel, args.host, args.port))


if __name__ == "__main__":
    main()
