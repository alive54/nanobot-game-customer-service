import asyncio
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from nanobot.bus.queue import MessageBus
from nanobot.cli.commands import _build_gateway_http_app, _pick_routable_target, app
from nanobot.config.schema import Config
from nanobot.providers.litellm_provider import LiteLLMProvider
from nanobot.providers.openai_codex_provider import _strip_model_prefix
from nanobot.providers.registry import find_by_model

runner = CliRunner()


@pytest.fixture
def mock_paths():
    """Mock config/workspace paths for test isolation."""
    with (
        patch("nanobot.config.loader.get_config_path") as mock_cp,
        patch("nanobot.config.loader.save_config") as mock_sc,
        patch("nanobot.config.loader.load_config") as mock_lc,
        patch("nanobot.utils.helpers.get_workspace_path") as mock_ws,
    ):
        base_dir = Path("./test_onboard_data")
        if base_dir.exists():
            shutil.rmtree(base_dir)
        base_dir.mkdir()

        config_file = base_dir / "config.json"
        workspace_dir = base_dir / "workspace"

        mock_cp.return_value = config_file
        mock_ws.return_value = workspace_dir
        mock_sc.side_effect = lambda config: config_file.write_text("{}")

        yield config_file, workspace_dir

        if base_dir.exists():
            shutil.rmtree(base_dir)


def test_onboard_fresh_install(mock_paths):
    """No existing config — should create from scratch."""
    config_file, workspace_dir = mock_paths

    result = runner.invoke(app, ["onboard"])

    assert result.exit_code == 0
    assert "Created config" in result.stdout
    assert "Created workspace" in result.stdout
    assert "nanobot is ready" in result.stdout
    assert config_file.exists()
    assert (workspace_dir / "AGENTS.md").exists()
    assert (workspace_dir / "memory" / "MEMORY.md").exists()


def test_onboard_existing_config_refresh(mock_paths):
    """Config exists, user declines overwrite — should refresh (load-merge-save)."""
    config_file, workspace_dir = mock_paths
    config_file.write_text('{"existing": true}')

    result = runner.invoke(app, ["onboard"], input="n\n")

    assert result.exit_code == 0
    assert "Config already exists" in result.stdout
    assert "existing values preserved" in result.stdout
    assert workspace_dir.exists()
    assert (workspace_dir / "AGENTS.md").exists()


def test_onboard_existing_config_overwrite(mock_paths):
    """Config exists, user confirms overwrite — should reset to defaults."""
    config_file, workspace_dir = mock_paths
    config_file.write_text('{"existing": true}')

    result = runner.invoke(app, ["onboard"], input="y\n")

    assert result.exit_code == 0
    assert "Config already exists" in result.stdout
    assert "Config reset to defaults" in result.stdout
    assert workspace_dir.exists()


def test_onboard_existing_workspace_safe_create(mock_paths):
    """Workspace exists — should not recreate, but still add missing templates."""
    config_file, workspace_dir = mock_paths
    workspace_dir.mkdir(parents=True)
    config_file.write_text("{}")

    result = runner.invoke(app, ["onboard"], input="n\n")

    assert result.exit_code == 0
    assert "Created workspace" not in result.stdout
    assert "Created AGENTS.md" in result.stdout
    assert (workspace_dir / "AGENTS.md").exists()


def test_config_matches_github_copilot_codex_with_hyphen_prefix():
    config = Config()
    config.agents.defaults.model = "github-copilot/gpt-5.3-codex"

    assert config.get_provider_name() == "github_copilot"


def test_config_matches_openai_codex_with_hyphen_prefix():
    config = Config()
    config.agents.defaults.model = "openai-codex/gpt-5.1-codex"

    assert config.get_provider_name() == "openai_codex"


def test_find_by_model_prefers_explicit_prefix_over_generic_codex_keyword():
    spec = find_by_model("github-copilot/gpt-5.3-codex")

    assert spec is not None
    assert spec.name == "github_copilot"


def test_litellm_provider_canonicalizes_github_copilot_hyphen_prefix():
    provider = LiteLLMProvider(default_model="github-copilot/gpt-5.3-codex")

    resolved = provider._resolve_model("github-copilot/gpt-5.3-codex")

    assert resolved == "github_copilot/gpt-5.3-codex"


def test_openai_codex_strip_prefix_supports_hyphen_and_underscore():
    assert _strip_model_prefix("openai-codex/gpt-5.1-codex") == "gpt-5.1-codex"
    assert _strip_model_prefix("openai_codex/gpt-5.1-codex") == "gpt-5.1-codex"


class _DummySessionManager:
    def __init__(self, sessions):
        self._sessions = sessions

    def list_sessions(self):
        return list(self._sessions)


def test_pick_routable_target_prefers_latest_external_enabled_session():
    session_manager = _DummySessionManager(
        [
            {"key": "cli:direct"},
            {"key": "telegram:chat-b"},
            {"key": "dingtalk:chat-a"},
        ]
    )

    target = _pick_routable_target(
        session_manager,
        ["dingtalk", "telegram"],
        allow_cli_fallback=False,
    )

    assert target == ("telegram", "chat-b")


def test_gateway_http_message_uses_latest_session_when_target_missing():
    bus = MessageBus()
    app = _build_gateway_http_app(
        bus,
        _DummySessionManager([{"key": "dingtalk:admin-room"}]),
        ["dingtalk"],
    )
    client = TestClient(app)

    response = client.post("/message", json={"text": "manual handoff"})

    assert response.status_code == 200
    assert response.json()["channel"] == "dingtalk"
    assert response.json()["chat_id"] == "admin-room"

    outbound = asyncio.run(bus.consume_outbound())
    assert outbound.channel == "dingtalk"
    assert outbound.chat_id == "admin-room"
    assert outbound.content == "manual handoff"


def test_gateway_http_message_accepts_explicit_target():
    bus = MessageBus()
    app = _build_gateway_http_app(
        bus,
        _DummySessionManager([]),
        ["telegram"],
    )
    client = TestClient(app)

    response = client.post(
        "/message",
        json={"text": "manual handoff", "channel": "telegram", "chat_id": "admin42"},
    )

    assert response.status_code == 200
    outbound = asyncio.run(bus.consume_outbound())
    assert outbound.channel == "telegram"
    assert outbound.chat_id == "admin42"


def test_gateway_http_message_returns_409_without_routable_target():
    bus = MessageBus()
    app = _build_gateway_http_app(
        bus,
        _DummySessionManager([]),
        ["telegram"],
    )
    client = TestClient(app)

    response = client.post("/message", json={"text": "manual handoff"})

    assert response.status_code == 409
