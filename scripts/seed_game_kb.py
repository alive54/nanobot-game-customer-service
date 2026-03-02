#!/usr/bin/env python3
from __future__ import annotations

import argparse

from nanobot.game_cs.config import GameCSConfig
from nanobot.game_cs.openviking_kb import OpenVikingKB


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed OpenViking knowledge base for game customer service")
    parser.add_argument("paths", nargs="+", help="File/dir/url paths to index")
    args = parser.parse_args()

    cfg = GameCSConfig.from_env()
    kb = OpenVikingKB(cfg.openviking_path, cfg.openviking_target_uri)
    roots = kb.add_resources(args.paths, wait=True)
    print({"ok": True, "indexed_roots": roots})
    kb.close()


if __name__ == "__main__":
    main()
