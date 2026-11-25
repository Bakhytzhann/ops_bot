import json
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from telegram import MenuButtonDefault

import main
from bot import bot
from bot.miniapp import ops_menu, publish_tunnel_menu, restore_tunnel_menu
from config import logging_config, paths
from config.settings import Settings, settings

NGROK = "https://fixture.ngrok.app"


def test_local_api_closes_after_context():
    with httpx.Client(trust_env=False, timeout=2) as client:
        with main.local_api(0) as origin:
            assert client.get(origin + "/health").json() == {"status": "ok"}
        with pytest.raises((httpx.ConnectError, httpx.ConnectTimeout)):
            client.get(origin + "/health")


@pytest.mark.parametrize("endpoint", [NGROK, f"--url {NGROK}", f'--url="{NGROK}/"'])
def test_ngrok_setting_normalizes_url_flag(endpoint):
    config = Settings(_env_file=None, ngrok_endpoint=endpoint)
    assert config.ngrok_endpoint == NGROK


@pytest.mark.parametrize(
    "endpoint", [f"--url {NGROK} --other", NGROK + "/app", "http://fixture.ngrok.app"]
)
def test_ngrok_setting_rejects_non_origin(endpoint):
    with pytest.raises(ValidationError, match="NGROK_ENDPOINT must be an HTTPS origin"):
        Settings(_env_file=None, ngrok_endpoint=endpoint)


def test_ngrok_waits_for_own_endpoint_and_passes_token_in_child_env(monkeypatch):
    create_process = subprocess.Popen
    children = []
    config_paths = []

    def fake_ngrok(command, **options):
        assert command[:5] == ["ngrok", "http", "http://127.0.0.1:8000", "--url", NGROK]
        assert "fixture-token" not in command
        assert options["env"]["NGROK_AUTHTOKEN"] == "fixture-token"
        assert "--inspect=false" in command
        config_paths.append(command[command.index("--config") + 1])
        events = [
            json.dumps({"msg": "started tunnel", "url": "https://other.ngrok.app"}),
            json.dumps({"msg": "started tunnel", "url": NGROK}),
        ]
        process = create_process(
            [
                sys.executable,
                "-u",
                "-c",
                f"import time; print({events[0]!r}); print({events[1]!r}); time.sleep(60)",
            ],
            **options,
        )
        children.append(process)
        return process

    monkeypatch.setattr(main.subprocess, "Popen", fake_ngrok)
    health = Mock()
    monkeypatch.setattr(main, "wait_for_public_api", health)
    with main.ngrok_tunnel(
        "ngrok",
        "http://127.0.0.1:8000",
        endpoint=NGROK,
        token="fixture-token",
        timeout=5,
    ) as url:
        assert url == NGROK and children[0].poll() is None
        health.assert_called_once_with(NGROK, children[0])
    assert children[0].poll() is not None
    assert all(not Path(path).exists() for path in config_paths)


def test_public_api_waits_for_fastapi_health(monkeypatch):
    replies = iter(
        [
            httpx.Response(502, text="Upstream error"),
            httpx.Response(200, text="Unexpected page"),
            httpx.Response(200, json={"status": "ok"}),
        ]
    )
    hits = []

    def reply(request):
        hits.append(str(request.url))
        return next(replies)

    client = httpx.Client(transport=httpx.MockTransport(reply))
    monkeypatch.setattr(main.httpx, "Client", lambda **_: client)
    monkeypatch.setattr(main, "sleep", Mock())

    main.wait_for_public_api(NGROK, Mock(poll=Mock(return_value=None)))

    assert hits == [NGROK + "/health"] * 3


def test_public_api_reports_unreachable_tunnel(monkeypatch):
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(502)))
    monkeypatch.setattr(main.httpx, "Client", lambda **_: client)

    with pytest.raises(main.StartupError, match="unreachable over HTTPS.*502"):
        main.wait_for_public_api(NGROK, Mock(poll=Mock(return_value=None)), timeout=0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "previous", [ops_menu("https://ops.example.test"), MenuButtonDefault()]
)
async def test_shutdown_restores_previous_menu(previous):
    telegram = MagicMock(
        get_chat_menu_button=AsyncMock(side_effect=[previous, ops_menu(NGROK)]),
        set_chat_menu_button=AsyncMock(),
    )
    telegram.__aenter__.return_value = telegram
    application = SimpleNamespace(bot=telegram, bot_data={})

    await publish_tunnel_menu(application, NGROK)
    await restore_tunnel_menu(application)

    first, second = telegram.set_chat_menu_button.await_args_list
    assert first.kwargs["menu_button"] == ops_menu(NGROK)
    assert second.kwargs["menu_button"] == previous


@pytest.mark.parametrize("failure", [None, RuntimeError, KeyboardInterrupt])
def test_run_passes_tunnel_url_and_closes_services(
    monkeypatch, tmp_path, capsys, failure
):
    dist = tmp_path / "frontend" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").touch()
    monkeypatch.setattr(paths, "project_directory", lambda: tmp_path)
    monkeypatch.setattr(settings, "telegram_bot_token", SecretStr("123:fixture"))
    monkeypatch.setattr(settings, "ngrok_token", SecretStr("fixture-token"))
    monkeypatch.setattr(settings, "ngrok_endpoint", NGROK)
    monkeypatch.setattr(logging_config, "setup_logging", Mock())
    monkeypatch.setattr(main, "ngrok_path", lambda _root: "ngrok")
    calls = []

    @contextmanager
    def local_api():
        calls.append("api started")
        try:
            yield "http://127.0.0.1:8000"
        finally:
            calls.append("api stopped")

    @contextmanager
    def tunnel(executable, origin, *, endpoint, token):
        assert executable == "ngrok"
        assert endpoint == NGROK and token == "fixture-token"
        assert origin == "http://127.0.0.1:8000"
        calls.append("tunnel started")
        try:
            yield endpoint
        finally:
            calls.append("tunnel stopped")

    def run_bot(*, mini_app_url):
        assert mini_app_url == NGROK
        calls.append("bot started")
        if failure:
            raise failure

    monkeypatch.setattr(main, "local_api", local_api)
    monkeypatch.setattr(main, "ngrok_tunnel", tunnel)
    monkeypatch.setattr(bot, "run_bot", run_bot)
    if failure:
        with pytest.raises(failure):
            main.run()
    else:
        main.run()
    assert NGROK in capsys.readouterr().out
    assert calls == [
        "api started",
        "tunnel started",
        "bot started",
        "tunnel stopped",
        "api stopped",
    ]


@pytest.mark.parametrize(
    ("token", "endpoint"), [(None, NGROK), (SecretStr("fixture-token"), ""), (None, "")]
)
def test_run_requires_both_ngrok_settings(monkeypatch, tmp_path, token, endpoint):
    dist = tmp_path / "frontend" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").touch()
    monkeypatch.setattr(paths, "project_directory", lambda: tmp_path)
    monkeypatch.setattr(settings, "telegram_bot_token", SecretStr("123:fixture"))
    monkeypatch.setattr(settings, "ngrok_token", token)
    monkeypatch.setattr(settings, "ngrok_endpoint", endpoint)
    monkeypatch.setattr(logging_config, "setup_logging", Mock())
    local_api = Mock()
    monkeypatch.setattr(main, "local_api", local_api)

    with pytest.raises(
        main.StartupError, match="Set both NGROK_TOKEN and NGROK_ENDPOINT for ngrok"
    ):
        main.run()
    local_api.assert_not_called()
