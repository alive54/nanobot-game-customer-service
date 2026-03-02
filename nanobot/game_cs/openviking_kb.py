from __future__ import annotations

from pathlib import Path
from typing import Iterable

import openviking as ov


class OpenVikingKB:
    def __init__(self, data_path: Path, target_uri: str = "viking://resources/"):
        self._target_uri = target_uri
        self._client = None
        self._data_path = data_path
        self._initialized = False
        self._error: str | None = None

    @property
    def available(self) -> bool:
        return self._error is None

    def initialize(self) -> None:
        if self._initialized:
            return
        if self._error is not None:
            raise RuntimeError(self._error)
        try:
            self._client = ov.OpenViking(path=str(self._data_path))
            self._client.initialize()
            self._initialized = True
        except Exception as exc:
            self._error = str(exc)
            raise

    def add_resources(self, paths: Iterable[str], wait: bool = True) -> list[str]:
        self.initialize()
        assert self._client is not None
        roots: list[str] = []
        for p in paths:
            result = self._client.add_resource(path=p, target=self._target_uri)
            root_uri = result.get("root_uri")
            if isinstance(root_uri, str):
                roots.append(root_uri)
        if wait:
            self._client.wait_processed()
        return roots

    def search(self, query: str, limit: int = 5) -> list[str]:
        try:
            self.initialize()
        except Exception:
            return []
        assert self._client is not None
        result = self._client.find(query=query, target_uri=self._target_uri, limit=limit)
        lines: list[str] = []
        for item in result.resources:
            abstract = (item.abstract or "").strip().replace("\n", " ")
            lines.append(f"[{item.score:.2f}] {abstract[:220]}")
        return lines

    def close(self) -> None:
        if self._initialized and self._client is not None:
            self._client.close()
            self._initialized = False
