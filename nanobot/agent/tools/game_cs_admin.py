from __future__ import annotations

import json
from typing import Any

import httpx

from nanobot.agent.tools.base import Tool


class _GameCSAdminHTTPTool(Tool):
    """Shared HTTP helper for game_cs admin tools."""

    def __init__(self, base_url: str, token: str, timeout_s: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_s = timeout_s

    def _headers(self) -> dict[str, str]:
        return {"X-Game-Cs-Token": self.token}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> str:
        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            response = await client.request(
                method,
                url,
                headers=self._headers(),
                params=params,
                json=json_body,
            )
        if response.status_code >= 400:
            return f"Error: game_cs admin API {response.status_code} {response.text[:300]}"
        try:
            data = response.json()
        except Exception:
            return response.text
        return json.dumps(data, ensure_ascii=False, indent=2)


class GameCSStatsTool(_GameCSAdminHTTPTool):
    @property
    def name(self) -> str:
        return "game_cs_stats"

    @property
    def description(self) -> str:
        return (
            "Get live game customer service statistics. Use this for customer counts, SOP stage "
            "counts, and human handoff summary instead of reading local session files."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **_: Any) -> str:
        return await self._request("GET", "/admin/stats")


class GameCSListCustomersTool(_GameCSAdminHTTPTool):
    @property
    def name(self) -> str:
        return "game_cs_list_customers"

    @property
    def description(self) -> str:
        return (
            "List live game customer sessions from the admin API. Use this for requests like "
            "'list all customers' or filtering by SOP stage. Do not inspect workspace sessions/"
            "files for this."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 500,
                    "description": "Maximum number of customers to list.",
                },
                "include_closed": {
                    "type": "boolean",
                    "description": "Whether closed customers should be included in the list.",
                },
                "sop_state": {
                    "type": "string",
                    "description": "Optional SOP state filter such as collecting_info.",
                },
                "query": {
                    "type": "string",
                    "description": "Optional filter for user_id, area_name, or role_name.",
                },
            },
        }

    async def execute(
        self,
        limit: int = 100,
        include_closed: bool = True,
        sop_state: str | None = None,
        query: str | None = None,
        **_: Any,
    ) -> str:
        return await self._request(
            "GET",
            "/admin/customers",
            params={
                "limit": limit,
                "include_closed": str(include_closed).lower(),
                "sop_state": sop_state,
                "query": query,
            },
        )


class GameCSGetCustomerTool(_GameCSAdminHTTPTool):
    @property
    def name(self) -> str:
        return "game_cs_get_customer"

    @property
    def description(self) -> str:
        return "Get one live customer record, including recent messages and related handoff tickets."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "Customer user_id to inspect.",
                },
                "message_limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "description": "How many recent messages to include.",
                },
            },
            "required": ["user_id"],
        }

    async def execute(self, user_id: str, message_limit: int = 20, **_: Any) -> str:
        return await self._request(
            "GET",
            f"/admin/customer/{user_id}",
            params={"message_limit": message_limit},
        )


class GameCSSendMessageTool(_GameCSAdminHTTPTool):
    @property
    def name(self) -> str:
        return "game_cs_send_message"

    @property
    def description(self) -> str:
        return "Send a proactive admin reply to a customer."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "Customer user_id to receive the message.",
                },
                "reply": {
                    "type": "string",
                    "description": "Reply text to send.",
                },
            },
            "required": ["user_id", "reply"],
        }

    async def execute(self, user_id: str, reply: str, **_: Any) -> str:
        return await self._request(
            "POST",
            f"/admin/customer/{user_id}/message",
            json_body={"reply": reply},
        )


class GameCSResetCustomerTool(_GameCSAdminHTTPTool):
    @property
    def name(self) -> str:
        return "game_cs_reset_customer"

    @property
    def description(self) -> str:
        return "Reset a customer's SOP session."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "Customer user_id whose session should be reset.",
                }
            },
            "required": ["user_id"],
        }

    async def execute(self, user_id: str, **_: Any) -> str:
        return await self._request("POST", f"/admin/customer/{user_id}/reset")


class GameCSCloseCustomerTool(_GameCSAdminHTTPTool):
    @property
    def name(self) -> str:
        return "game_cs_close_customer"

    @property
    def description(self) -> str:
        return "Close a customer session without deleting its history."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "Customer user_id to close.",
                }
            },
            "required": ["user_id"],
        }

    async def execute(self, user_id: str, **_: Any) -> str:
        return await self._request(
            "POST",
            f"/admin/customer/{user_id}/close",
            json_body={"closed": True},
        )


class GameCSReopenCustomerTool(_GameCSAdminHTTPTool):
    @property
    def name(self) -> str:
        return "game_cs_reopen_customer"

    @property
    def description(self) -> str:
        return "Reopen a previously closed customer session."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "Customer user_id to reopen.",
                }
            },
            "required": ["user_id"],
        }

    async def execute(self, user_id: str, **_: Any) -> str:
        return await self._request(
            "POST",
            f"/admin/customer/{user_id}/close",
            json_body={"closed": False},
        )


class GameCSListHumanQueriesTool(_GameCSAdminHTTPTool):
    @property
    def name(self) -> str:
        return "game_cs_list_human_queries"

    @property
    def description(self) -> str:
        return "List pending or answered human handoff tickets from the live admin API."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "Optional status filter such as pending, answered, or delivered.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 500,
                    "description": "Maximum number of tickets to list.",
                },
            },
        }

    async def execute(self, status: str | None = None, limit: int = 100, **_: Any) -> str:
        return await self._request(
            "GET",
            "/admin/human-queries",
            params={"status": status, "limit": limit},
        )


class GameCSHumanReplyTool(_GameCSAdminHTTPTool):
    @property
    def name(self) -> str:
        return "game_cs_human_reply"

    @property
    def description(self) -> str:
        return "Reply to a human handoff ticket and optionally deliver the reply to the customer."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "Customer user_id associated with the ticket.",
                },
                "query_id": {
                    "type": "integer",
                    "description": "Pending human query id to answer.",
                },
                "reply": {
                    "type": "string",
                    "description": "Reply text for the ticket.",
                },
            },
            "required": ["user_id", "query_id", "reply"],
        }

    async def execute(self, user_id: str, query_id: int, reply: str, **_: Any) -> str:
        return await self._request(
            "POST",
            "/admin/human-reply",
            json_body={"user_id": user_id, "query_id": query_id, "reply": reply},
        )


class GameCSAdminTool(_GameCSAdminHTTPTool):
    """Legacy multiplexer kept for compatibility with existing imports/tests."""

    @property
    def name(self) -> str:
        return "game_cs_admin"

    @property
    def description(self) -> str:
        return (
            "Legacy multiplexer for game_cs admin operations. Prefer the dedicated game_cs_* tools "
            "such as game_cs_list_customers and game_cs_get_customer."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "stats",
                        "list_customers",
                        "get_customer",
                        "send_message",
                        "reset_customer",
                        "close_customer",
                        "reopen_customer",
                        "list_human_queries",
                        "human_reply",
                    ],
                    "description": "Legacy management operation to run.",
                },
                "user_id": {"type": "string"},
                "reply": {"type": "string"},
                "query_id": {"type": "integer"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                "include_closed": {"type": "boolean"},
                "sop_state": {"type": "string"},
                "query": {"type": "string"},
                "message_limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "status": {"type": "string"},
            },
            "required": ["action"],
        }

    async def execute(
        self,
        action: str,
        user_id: str | None = None,
        reply: str | None = None,
        query_id: int | None = None,
        limit: int = 100,
        include_closed: bool = True,
        sop_state: str | None = None,
        query: str | None = None,
        message_limit: int = 20,
        status: str | None = None,
        **_: Any,
    ) -> str:
        if action == "stats":
            return await GameCSStatsTool(self.base_url, self.token, self.timeout_s).execute()
        if action == "list_customers":
            return await GameCSListCustomersTool(self.base_url, self.token, self.timeout_s).execute(
                limit=limit,
                include_closed=include_closed,
                sop_state=sop_state,
                query=query,
            )
        if action == "get_customer":
            if not user_id:
                return "Error: user_id is required for get_customer"
            return await GameCSGetCustomerTool(self.base_url, self.token, self.timeout_s).execute(
                user_id=user_id,
                message_limit=message_limit,
            )
        if action == "send_message":
            if not user_id or not reply:
                return "Error: user_id and reply are required for send_message"
            return await GameCSSendMessageTool(self.base_url, self.token, self.timeout_s).execute(
                user_id=user_id,
                reply=reply,
            )
        if action == "reset_customer":
            if not user_id:
                return "Error: user_id is required for reset_customer"
            return await GameCSResetCustomerTool(self.base_url, self.token, self.timeout_s).execute(
                user_id=user_id
            )
        if action == "close_customer":
            if not user_id:
                return "Error: user_id is required for close_customer"
            return await GameCSCloseCustomerTool(self.base_url, self.token, self.timeout_s).execute(
                user_id=user_id
            )
        if action == "reopen_customer":
            if not user_id:
                return "Error: user_id is required for reopen_customer"
            return await GameCSReopenCustomerTool(self.base_url, self.token, self.timeout_s).execute(
                user_id=user_id
            )
        if action == "list_human_queries":
            return await GameCSListHumanQueriesTool(self.base_url, self.token, self.timeout_s).execute(
                status=status,
                limit=limit,
            )
        if action == "human_reply":
            if not user_id or query_id is None or not reply:
                return "Error: user_id, query_id, and reply are required for human_reply"
            return await GameCSHumanReplyTool(self.base_url, self.token, self.timeout_s).execute(
                user_id=user_id,
                query_id=query_id,
                reply=reply,
            )
        return f"Error: unsupported action '{action}'"


def build_game_cs_admin_tools(base_url: str, token: str, timeout_s: float = 10.0) -> list[Tool]:
    """Return the dedicated game_cs admin tools."""
    return [
        GameCSStatsTool(base_url=base_url, token=token, timeout_s=timeout_s),
        GameCSListCustomersTool(base_url=base_url, token=token, timeout_s=timeout_s),
        GameCSGetCustomerTool(base_url=base_url, token=token, timeout_s=timeout_s),
        GameCSSendMessageTool(base_url=base_url, token=token, timeout_s=timeout_s),
        GameCSResetCustomerTool(base_url=base_url, token=token, timeout_s=timeout_s),
        GameCSCloseCustomerTool(base_url=base_url, token=token, timeout_s=timeout_s),
        GameCSReopenCustomerTool(base_url=base_url, token=token, timeout_s=timeout_s),
        GameCSListHumanQueriesTool(base_url=base_url, token=token, timeout_s=timeout_s),
        GameCSHumanReplyTool(base_url=base_url, token=token, timeout_s=timeout_s),
    ]
