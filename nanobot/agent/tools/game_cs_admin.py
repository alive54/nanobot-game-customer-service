from __future__ import annotations

import json
from typing import Any

import httpx

from nanobot.agent.tools.base import Tool


class GameCSAdminTool(Tool):
    """Manage a running nanobot.game_cs service over its admin HTTP API."""

    def __init__(self, base_url: str, token: str, timeout_s: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_s = timeout_s

    @property
    def name(self) -> str:
        return "game_cs_admin"

    @property
    def description(self) -> str:
        return (
            "Manage the game customer service process. Use this to check customer counts, "
            "list customers and SOP stages, inspect recent messages, send proactive replies, "
            "reset a customer session, mark a customer closed or reopened, inspect pending "
            "human handoff tickets, and submit human replies."
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
                    "description": "Management operation to run.",
                },
                "user_id": {
                    "type": "string",
                    "description": "Customer user_id for customer-specific actions.",
                },
                "reply": {
                    "type": "string",
                    "description": "Reply text for send_message or human_reply.",
                },
                "query_id": {
                    "type": "integer",
                    "description": "Pending human query id for human_reply.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 500,
                    "description": "Maximum number of customers to list.",
                },
                "include_closed": {
                    "type": "boolean",
                    "description": "Whether closed customers should be included in lists.",
                },
                "sop_state": {
                    "type": "string",
                    "description": "Optional SOP state filter for list_customers.",
                },
                "query": {
                    "type": "string",
                    "description": "Optional free-text filter for user_id, area_name, or role_name.",
                },
                "message_limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "description": "How many recent messages to include for get_customer.",
                },
                "status": {
                    "type": "string",
                    "description": "Optional status filter for list_human_queries.",
                },
            },
            "required": ["action"],
        }

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
            return await self._request("GET", "/admin/stats")
        if action == "list_customers":
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
        if action == "get_customer":
            if not user_id:
                return "Error: user_id is required for get_customer"
            return await self._request(
                "GET",
                f"/admin/customer/{user_id}",
                params={"message_limit": message_limit},
            )
        if action == "send_message":
            if not user_id or not reply:
                return "Error: user_id and reply are required for send_message"
            return await self._request(
                "POST",
                f"/admin/customer/{user_id}/message",
                json_body={"reply": reply},
            )
        if action == "reset_customer":
            if not user_id:
                return "Error: user_id is required for reset_customer"
            return await self._request("POST", f"/admin/customer/{user_id}/reset")
        if action == "close_customer":
            if not user_id:
                return "Error: user_id is required for close_customer"
            return await self._request(
                "POST",
                f"/admin/customer/{user_id}/close",
                json_body={"closed": True},
            )
        if action == "reopen_customer":
            if not user_id:
                return "Error: user_id is required for reopen_customer"
            return await self._request(
                "POST",
                f"/admin/customer/{user_id}/close",
                json_body={"closed": False},
            )
        if action == "list_human_queries":
            return await self._request(
                "GET",
                "/admin/human-queries",
                params={"status": status},
            )
        if action == "human_reply":
            if not user_id or query_id is None or not reply:
                return "Error: user_id, query_id, and reply are required for human_reply"
            return await self._request(
                "POST",
                "/admin/human-reply",
                json_body={"user_id": user_id, "query_id": query_id, "reply": reply},
            )
        return f"Error: unsupported action '{action}'"
