import asyncio

from nanobot.agent.tools.game_cs_admin import (
    GameCSAdminTool,
    GameCSGetCustomerTool,
    GameCSListCustomersTool,
    build_game_cs_admin_tools,
)


class _DummyResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class _DummyClient:
    def __init__(self, *args, **kwargs):
        self.requests = kwargs.pop("requests_sink")

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def request(self, method, url, headers=None, params=None, json=None):
        self.requests.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "params": params,
                "json": json,
            }
        )
        return _DummyResponse(payload={"ok": True})


def test_game_cs_admin_tool_routes_actions(monkeypatch):
    requests = []

    def _factory(*args, **kwargs):
        kwargs["requests_sink"] = requests
        return _DummyClient(*args, **kwargs)

    monkeypatch.setattr("nanobot.agent.tools.game_cs_admin.httpx.AsyncClient", _factory)
    tool = GameCSAdminTool(base_url="http://127.0.0.1:8011", token="secret")

    result = asyncio.run(tool.execute(action="send_message", user_id="u1", reply="hello"))

    assert '"ok": true' in result
    assert requests[0]["method"] == "POST"
    assert requests[0]["url"] == "http://127.0.0.1:8011/admin/customer/u1/message"
    assert requests[0]["headers"] == {"X-Game-Cs-Token": "secret"}
    assert requests[0]["json"] == {"reply": "hello"}


def test_game_cs_list_customers_tool_routes_request(monkeypatch):
    requests = []

    def _factory(*args, **kwargs):
        kwargs["requests_sink"] = requests
        return _DummyClient(*args, **kwargs)

    monkeypatch.setattr("nanobot.agent.tools.game_cs_admin.httpx.AsyncClient", _factory)
    tool = GameCSListCustomersTool(base_url="http://127.0.0.1:8011", token="secret")

    result = asyncio.run(
        tool.execute(limit=20, include_closed=False, sop_state="collecting_info", query="player")
    )

    assert '"ok": true' in result
    assert requests[0]["method"] == "GET"
    assert requests[0]["url"] == "http://127.0.0.1:8011/admin/customers"
    assert requests[0]["params"] == {
        "limit": 20,
        "include_closed": "false",
        "sop_state": "collecting_info",
        "query": "player",
    }


def test_build_game_cs_admin_tools_exposes_dedicated_tool_names():
    tools = build_game_cs_admin_tools(base_url="http://127.0.0.1:8011", token="secret")

    assert [tool.name for tool in tools] == [
        "game_cs_stats",
        "game_cs_list_customers",
        "game_cs_get_customer",
        "game_cs_send_message",
        "game_cs_reset_customer",
        "game_cs_close_customer",
        "game_cs_reopen_customer",
        "game_cs_list_human_queries",
        "game_cs_human_reply",
    ]


def test_game_cs_get_customer_tool_limits_messages_and_queries(monkeypatch):
    requests = []

    def _factory(*args, **kwargs):
        kwargs["requests_sink"] = requests
        return _DummyClient(*args, **kwargs)

    monkeypatch.setattr("nanobot.agent.tools.game_cs_admin.httpx.AsyncClient", _factory)
    tool = GameCSGetCustomerTool(base_url="http://127.0.0.1:8011", token="secret")

    result = asyncio.run(
        tool.execute(user_id="u1", message_limit=20, human_query_limit=20)
    )

    assert '"ok": true' in result
    assert requests[0]["method"] == "GET"
    assert requests[0]["url"] == "http://127.0.0.1:8011/admin/customer/u1"
    assert requests[0]["params"] == {
        "message_limit": 20,
        "human_query_limit": 20,
    }
