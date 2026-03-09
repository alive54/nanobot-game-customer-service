from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from nanobot.game_cs.config import GameCSConfig
from nanobot.game_cs.service import create_app


class _DummyKB:
    def __init__(self, *args, **kwargs):
        pass

    def add_resources(self, paths, wait=True):
        return list(paths)

    def commit_session(self, *args, **kwargs):
        return None

    def search(self, *args, **kwargs):
        return []

    def search_with_context(self, *args, **kwargs):
        return []


class _EscalateKB(_DummyKB):
    def search(self, *args, **kwargs):
        return ["[0.10] missing answer"]

    def search_with_context(self, *args, **kwargs):
        return ["[0.10] missing answer"]


def _make_cfg(db_path: Path) -> GameCSConfig:
    return GameCSConfig(
        service_token="test-token",
        db_path=db_path,
        uploads_dir=db_path.parent / "uploads",
        openviking_path=db_path.parent / "openviking",
        openviking_target_uri="viking://test/",
        max_image_bytes=1024 * 1024,
        default_game_name="test-game",
        personality="lively",
        game_api_base="",
        mock_api=True,
        code_daily_checkin="DC001",
        code_lucky_draw="TX001",
        code_universal="ws888",
        code_guild="GUILD001",
        followup_30m_delay=1800,
        followup_1h_delay=3600,
        max_collect_retries=3,
        ai_enabled=False,
        ai_timeout_ms=1000,
        ai_max_context_msgs=8,
        ai_fallback_mode="best_effort",
        kb_handoff_score_threshold=0.45,
        ai_tool_whitelist=(),
        ai_info_extract_confidence_threshold=0.7,
    )


def test_admin_stats_and_customer_lifecycle(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path / "game_cs.db")

    with patch("nanobot.game_cs.service.OpenVikingKB", _DummyKB):
        client = TestClient(create_app(config=cfg))

    headers = {"X-Game-Cs-Token": cfg.service_token}

    first = client.post(
        "/webhook/game-message",
        headers=headers,
        json={"user_id": "u100", "message": "hello", "metadata": {}},
    )
    assert first.status_code == 200

    stats = client.get("/admin/stats", headers=headers)
    assert stats.status_code == 200
    summary = stats.json()["summary"]
    assert summary["total_customers"] == 1
    assert summary["open_customers"] == 1
    assert "collecting_info" in summary["sop_state_counts"]

    customers = client.get("/admin/customers", headers=headers)
    assert customers.status_code == 200
    assert customers.json()["customers"][0]["user_id"] == "u100"
    assert customers.json()["customers"][0]["is_closed"] is False

    detail = client.get("/admin/customer/u100", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["recent_messages"][0]["content"] == "hello"

    send = client.post(
        "/admin/customer/u100/message",
        headers=headers,
        json={"reply": "manual follow-up"},
    )
    assert send.status_code == 200
    assert send.json()["delivered"] is False
    assert send.json()["session"]["user_id"] == "u100"

    close = client.post(
        "/admin/customer/u100/close",
        headers=headers,
        json={"closed": True},
    )
    assert close.status_code == 200
    assert close.json()["customer"]["is_closed"] is True

    reopen = client.post(
        "/admin/customer/u100/close",
        headers=headers,
        json={"closed": False},
    )
    assert reopen.status_code == 200
    assert reopen.json()["customer"]["is_closed"] is False

    reset = client.post("/admin/customer/u100/reset", headers=headers)
    assert reset.status_code == 200
    assert reset.json()["customer"]["sop_state"] == "greeting"


def test_admin_human_queries_and_reply(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path / "game_cs.db")

    with patch("nanobot.game_cs.service.OpenVikingKB", _EscalateKB):
        client = TestClient(create_app(config=cfg))

    headers = {"X-Game-Cs-Token": cfg.service_token}
    setup_response = client.post(
        "/webhook/game-message",
        headers=headers,
        json={"user_id": "u200", "message": "18区 战神无双", "metadata": {}},
    )
    assert setup_response.status_code == 200
    with patch("nanobot.game_cs.service.forward_to_admin", AsyncMock(return_value=True)):
        response = client.post(
            "/webhook/game-message",
            headers=headers,
            json={"user_id": "u200", "message": "where is my reward", "metadata": {}},
        )
    assert response.status_code == 200

    app_state = client.get("/admin/human-queries", headers=headers)
    assert app_state.status_code == 200
    assert app_state.json()["count"] >= 1

    query = app_state.json()["queries"][0]
    reply = client.post(
        "/admin/human-reply",
        headers=headers,
        json={
            "user_id": query["user_id"],
            "query_id": query["id"],
            "reply": "please try again later",
        },
    )
    assert reply.status_code == 200
    delivered = client.get("/admin/customer/u200", headers=headers)
    assert delivered.status_code == 200
    assert delivered.json()["human_queries"][0]["status"] in {"answered", "delivered"}
