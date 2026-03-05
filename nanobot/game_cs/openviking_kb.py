from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

import openviking as ov
from openviking.message import TextPart


class OpenVikingKB:
    """
    Wrapper around an OpenViking client (embedded mode) that exposes:

    - add_resources()        — bulk-index knowledge files
    - search()               — simple find() with no session context
    - search_with_context()  — context-aware search() using a live OV session
    - commit_session()       — archive conversation turns and extract memories
    - close()                — graceful shutdown
    """

    def __init__(self, data_path: Path, target_uri: str = "viking://resources/game-cs/") -> None:
        self._data_path = data_path
        self._target_uri = target_uri
        self._client: ov.OpenViking | None = None
        self._initialized = False
        self._error: str | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    @property
    def available(self) -> bool:
        """True when the client is ready (or has not yet failed to initialize)."""
        return self._error is None

    def initialize(self) -> None:
        """Lazily initialize the embedded OpenViking client."""
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

    def close(self) -> None:
        """Shut down the embedded client if it was started."""
        if self._initialized and self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            finally:
                self._initialized = False

    # ── Private guard ─────────────────────────────────────────────────────────

    def _client_or_raise(self) -> ov.OpenViking:
        self.initialize()
        assert self._client is not None  # noqa: S101
        return self._client

    def _try_init(self) -> bool:
        """Attempt initialization; return False (and suppress) on failure."""
        try:
            self.initialize()
            return True
        except Exception:
            return False

    @staticmethod
    def _normalize_text(value: Any) -> str:
        if not isinstance(value, str):
            return ""
        return value.strip().replace("\n", " ")

    def _resource_snippet(self, item: Any, max_len: int = 240) -> str:
        """
        Prefer original chunk text; fall back to abstract/title/uri when needed.
        """
        candidates = (
            getattr(item, "content", None),
            getattr(item, "text", None),
            getattr(item, "chunk_text", None),
            getattr(item, "raw_text", None),
            getattr(item, "abstract", None),
            getattr(item, "title", None),
            getattr(item, "uri", None),
        )
        for candidate in candidates:
            normalized = self._normalize_text(candidate)
            if normalized:
                return normalized[:max_len]
        return ""

    def _read_l2_text(self, uri: str, max_len: int = 600, raw: bool = False) -> str:
        """
        Read L2 raw content by URI and normalize to a single-line snippet.

        Args:
            uri: Viking URI to read from
            max_len: Maximum length to return (when raw=False)
            raw: If True, return original file content without truncation or formatting
        """
        if not uri:
            return ""
        try:
            client = self._client_or_raise()
            content = client.read(uri)
        except Exception:
            return ""

        if raw:
            return content

        normalized = self._normalize_text(content)
        if not normalized:
            return ""
        return normalized[:max_len]

    def _resource_snippet_with_l2(
        self,
        item: Any,
        include_l2: bool = False,
        max_len: int = 240,
        l2_max_len: int = 600,
        raw_content: bool = False,
    ) -> str:
        """
        Render snippet for a matched item.

        When ``include_l2`` is enabled, leaf-node hits try to read L2 raw
        content first; otherwise fallback to normal fields.

        Args:
            item: The matched item
            include_l2: Whether to read L2 content for leaf nodes
            max_len: Maximum length for snippet (when raw_content=False)
            l2_max_len: Maximum length for L2 content (when raw_content=False)
            raw_content: If True, return original file content without formatting
        """
        if include_l2:
            try:
                uri = getattr(item, "uri", None)
                if isinstance(uri, str) and uri:
                    l2 = self._read_l2_text(uri, max_len=l2_max_len, raw=raw_content)
                    if l2:
                        return l2[:max_len] if not raw_content else l2
            except Exception:
                pass
        return self._resource_snippet(item, max_len=max_len)

    def _format_resources(
        self,
        resources: Iterable[Any],
        limit: int,
        include_l2: bool = False,
        max_len: int = 240,
        l2_max_len: int = 600,
        raw_content: bool = False,
    ) -> list[str]:
        """
        Render stable human-readable lines and deduplicate near-identical snippets.

        Args:
            resources: Iterable of matched items
            limit: Maximum number of results to return
            include_l2: Whether to read L2 content for leaf nodes
            max_len: Maximum length for snippet (when raw_content=False)
            l2_max_len: Maximum length for L2 content (when raw_content=False)
            raw_content: If True, return original file content without formatting
        """
        lines: list[str] = []
        seen: set[str] = set()
        for item in resources:
            level = getattr(item, "level", 0)
            if level != 2:
                continue

            snippet = self._resource_snippet_with_l2(
                item,
                include_l2=include_l2,
                max_len=max_len,
                l2_max_len=l2_max_len,
                raw_content=raw_content,
            )
            if not snippet:
                continue
            key = snippet.lower()
            if key in seen:
                continue
            seen.add(key)
            score = getattr(item, "score", 0.0)
            lines.append(f"[{score:.2f}] {snippet}")
            if len(lines) >= limit:
                break
        return lines

    # ── Knowledge indexing ────────────────────────────────────────────────────

    def add_resources(self, paths: Iterable[str], wait: bool = True) -> list[str]:
        """
        Index one or more local file paths (or URLs) into the knowledge base.

        Parameters
        ----------
        paths:
            Iterable of file paths / directory paths / URLs to index.
        wait:
            When True, block until OpenViking finishes processing (embedding,
            L0/L1 summary generation). Recommended for small batches.

        Returns
        -------
        list[str]
            Viking URIs of the successfully indexed root nodes.
        """
        client = self._client_or_raise()
        roots: list[str] = []
        for p in paths:
            try:
                result = client.add_resource(
                    path=p,
                    target=self._target_uri,
                    reason="Game customer-service knowledge document",
                )
                root_uri = result.get("root_uri")
                if isinstance(root_uri, str):
                    roots.append(root_uri)
            except Exception:
                # Log but continue — a single bad file should not abort the batch
                pass
        if wait and roots:
            try:
                client.wait_processed()
            except Exception:
                pass
        return roots

    # ── Simple semantic search (find) ─────────────────────────────────────────

    def search(
        self,
        query: str,
        limit: int = 5,
        *,
        include_l2: bool = False,
        l2_max_len: int = 600,
        raw_content: bool = False,
    ) -> list[str]:
        """
        Basic vector-similarity search with no session context.

        Returns a list of human-readable abstract strings ready to paste into
        a bot reply, prefixed with their relevance score.

        Args:
            query: Search query string
            limit: Maximum number of results to return
            include_l2: Whether to read L2 content for leaf nodes
            l2_max_len: Maximum length for L2 content (when raw_content=False)
            raw_content: If True, return original file content without formatting

        Fails silently: returns [] when OpenViking is unavailable.
        """
        if not self._try_init():
            return []
        client = self._client_or_raise()
        try:
            result = client.find(
                query=query,
                target_uri=self._target_uri,
                limit=200,
                score_threshold=0.4,
            )

        except Exception as e:
            return []

        return self._format_resources(
            result.resources,
            limit=limit,
            include_l2=include_l2,
            l2_max_len=l2_max_len,
            raw_content=raw_content,
        )

    # ── Context-aware search (search) ─────────────────────────────────────────

    def search_with_context(
        self,
        query: str,
        history: list[dict],  # [{"role": "user"|"assistant", "content": "..."}]
        limit: int = 5,
        *,
        include_l2: bool = False,
        l2_max_len: int = 600,
        raw_content: bool = False,
    ) -> list[str]:
        """
        Intent-aware search that incorporates recent conversation history.

        Uses OpenViking's ``search()`` API which runs query-expansion and
        re-ranking internally.

        Parameters
        ----------
        query:
            The user's latest message / question.
        history:
            Recent conversation turns (oldest first) used to build an OV
            session for context-aware retrieval.
        limit:
            Maximum number of results to return.
        include_l2:
            Whether to read L2 content for leaf nodes.
        l2_max_len:
            Maximum length for L2 content (when raw_content=False).
        raw_content:
            If True, return original file content without formatting.

        Returns
        -------
        list[str]
            Same format as :meth:`search` — scored abstract snippets.
        """
        # if not self._try_init():
        #     return self.search(query, limit=limit, include_l2=include_l2, l2_max_len=l2_max_len, raw_content=raw_content)
        # client = self._client_or_raise()

        # Build a transient OV session from the provided history
        # try:
        #     session = client.session()
        #     for turn in history[-8:]:          # cap at last 8 turns
        #         role = turn.get("role", "user")
        #         content = turn.get("content", "")
        #         if content:
        #             session.add_message(role, [TextPart(text=content)])

        #     result = client.search(
        #         query=query,
        #         session=session,
        #         target_uri=self._target_uri,
        #         limit=200,
        #         score_threshold=0.40,
        #     )

        #     for r in result.resources:
        #         print(f"{r}")
        #         print(f"  {r.uri} (score: {r.score:.4f})")

        #     print("=================================================")

        # except Exception as e:
        #     # Fall back to simple find() on any error
        return self.search(query, limit=limit, include_l2=include_l2, l2_max_len=l2_max_len, raw_content=raw_content)

        # return self._format_resources(
        #     result.resources,
        #     limit=limit,
        #     include_l2=include_l2,
        #     l2_max_len=l2_max_len,
        #     raw_content=raw_content,
        # )

    # ── Session memory commit ──────────────────────────────────────────────────

    def commit_session(
        self,
        messages: list[dict],  # [{"role": "user"|"assistant", "content": "..."}]
        user_id: str | None = None,
    ) -> bool:
        """
        Archive a completed conversation and let OpenViking extract long-term
        memories (profile, preferences, cases, patterns).

        This should be called after a SOP session reaches COMPLETED or SILENT
        state so that learnings are persisted into ``viking://user/memories/``.

        Parameters
        ----------
        messages:
            Full conversation turns, oldest first.
        user_id:
            Optional identifier used in log messages.

        Returns
        -------
        bool
            True on success, False when OpenViking is unavailable or an error
            occurred (errors are suppressed so as not to disrupt the main flow).
        """
        if not messages:
            return False
        if not self._try_init():
            return False
        client = self._client_or_raise()

        try:
            session = client.session()
            for turn in messages:
                role = turn.get("role", "user")
                content = turn.get("content", "")
                if content:
                    session.add_message(role, [TextPart(text=content)])

            session.commit()
            return True
        except Exception:
            return False

    # ── User memory retrieval ─────────────────────────────────────────────────

    def get_user_memories(
        self,
        user_id: str,
        query: str,
        limit: int = 3,
    ) -> list[str]:
        """
        Retrieve previously committed memories for a specific user.

        Searches ``viking://user/{user_id}/memories/`` using ``find()``.
        Used for next-day follow-up personalisation.

        Returns [] silently on any error.
        """
        if not self._try_init():
            return []
        client = self._client_or_raise()
        try:
            result = client.find(
                query=query,
                target_uri=f"viking://user/{user_id}/memories/",
                limit=limit,
            )
        except Exception:
            return []

        lines: list[str] = []
        for item in result.memories:
            abstract = (item.abstract or "").strip().replace("\n", " ")
            if abstract:
                lines.append(abstract[:240])
        return lines
