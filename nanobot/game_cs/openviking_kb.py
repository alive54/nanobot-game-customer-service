from __future__ import annotations

from pathlib import Path
from typing import Iterable

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

    def __init__(self, data_path: Path, target_uri: str = "viking://resources/") -> None:
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

    def search(self, query: str, limit: int = 5) -> list[str]:
        """
        Basic vector-similarity search with no session context.

        Returns a list of human-readable abstract strings ready to paste into
        a bot reply, prefixed with their relevance score.

        Fails silently: returns [] when OpenViking is unavailable.
        """
        if not self._try_init():
            return []
        client = self._client_or_raise()
        try:
            result = client.find(
                query=query,
                target_uri=self._target_uri,
                limit=limit,
                score_threshold=0.45,
            )
        except Exception:
            return []

        lines: list[str] = []
        for item in result.resources:
            abstract = (item.abstract or "").strip().replace("\n", " ")
            if abstract:
                lines.append(f"[{item.score:.2f}] {abstract[:240]}")
        return lines

    # ── Context-aware search (search) ─────────────────────────────────────────

    def search_with_context(
        self,
        query: str,
        history: list[dict],  # [{"role": "user"|"assistant", "content": "..."}]
        limit: int = 5,
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

        Returns
        -------
        list[str]
            Same format as :meth:`search` — scored abstract snippets.
        """
        if not self._try_init():
            return self.search(query, limit=limit)
        client = self._client_or_raise()

        # Build a transient OV session from the provided history
        try:
            session = client.session()
            for turn in history[-8:]:          # cap at last 8 turns
                role = turn.get("role", "user")
                content = turn.get("content", "")
                if content:
                    session.add_message(role, [TextPart(text=content)])

            result = client.search(
                query=query,
                session=session,
                target_uri=self._target_uri,
                limit=limit,
                score_threshold=0.40,
            )
        except Exception:
            # Fall back to simple find() on any error
            return self.search(query, limit=limit)

        lines: list[str] = []
        for item in result.resources:
            abstract = (item.abstract or "").strip().replace("\n", " ")
            if abstract:
                lines.append(f"[{item.score:.2f}] {abstract[:240]}")
        return lines

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
