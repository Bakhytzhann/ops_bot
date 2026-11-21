import subprocess
import sys
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import httpx
import pytest
from pydantic import SecretStr
from telegram import MenuButtonDefault

import main
from bot import bot
from bot.miniapp import ops_menu, publish_tunnel_menu, restore_tunnel_menu
from config import logging_config, paths
from config.settings import settings

TUNNEL = "https://fixture-tunnel.trycloudflare.com"


def test_local_api_closes_after_context():
    with httpx.Client(trust_env=False, timeout=2) as client:
        with main.local_api(0) as origin:
            assert client.get(origin + "/health").json() == {"status": "ok"}
        with pytest.raises((httpx.ConnectError, httpx.ConnectTimeout)):
            client.get(origin + "/health")


def test_quick_tunnel_waits_for_connection_and_closes_child(monkeypatch):
    create_process = subprocess.Popen
    children = []

    def fake_cloudflared(_command, **options):
        process = create_process(
            [
                sys.executable,
                "-u",
                "-c",
                f"import time; print('{TUNNEL}'); "
                "print('INF Registered tunnel connection'); time.sleep(60)",
            ],
            **options,
        )
        children.append(process)
        return process

    monkeypatch.setattr(main.subprocess, "Popen", fake_cloudflared)
    wait = Mock()
    monkeypatch.setattr(main, "wait_for_public_api", wait)
    with main.quick_tunnel("cloudflared", "http://127.0.0.1:8000", timeout=5) as url:
        assert url == TUNNEL
        assert children[0].poll() is None
        wait.assert_called_once_with(TUNNEL, children[0])
    assert children[0].poll() is not None


def test_public_api_waits_for_fastapi_health(monkeypatch):
    replies = iter(
        [
            httpx.Response(530, text="Cloudflare error"),
            httpx.Response(200, text="Cloudflare page"),
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

    main.wait_for_public_api(TUNNEL, Mock(poll=Mock(return_value=None)))

    assert hits == [TUNNEL + "/health"] * 3


def test_public_api_reports_unreachable_tunnel(monkeypatch):
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(530)))
    monkeypatch.setattr(main.httpx, "Client", lambda **_: client)

    with pytest.raises(main.StartupError, match="unreachable over HTTPS.*530"):
        main.wait_for_public_api(TUNNEL, Mock(poll=Mock(return_value=None)), timeout=0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "old_url", ["https://ops.example.test", "https://old.trycloudflare.com"]
)
async def test_shutdown_restores_previous_non_temporary_menu(old_url):
    previous = ops_menu(old_url)
    telegram = MagicMock(
        get_chat_menu_button=AsyncMock(side_effect=[previous, ops_menu(TUNNEL)]),
        set_chat_menu_button=AsyncMock(),
    )
    telegram.__aenter__.return_value = telegram
    application = SimpleNamespace(bot=telegram, bot_data={})

    await publish_tunnel_menu(application, TUNNEL)
    await restore_tunnel_menu(application)

    first, second = telegram.set_chat_menu_button.await_args_list
    assert first.kwargs["menu_button"] == ops_menu(TUNNEL)
    assert second.kwargs["menu_button"] == (
        MenuButtonDefault() if old_url.endswith(".trycloudflare.com") else previous
    )


@pytest.mark.parametrize("failure", [None, RuntimeError, KeyboardInterrupt])
def test_run_passes_tunnel_url_and_closes_services(
    monkeypatch, tmp_path, capsys, failure
):
    dist = tmp_path / "frontend" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").touch()
    monkeypatch.setattr(paths, "project_directory", lambda: tmp_path)
    monkeypatch.setattr(settings, "telegram_bot_token", SecretStr("123:fixture"))
    monkeypatch.setattr(logging_config, "setup_logging", Mock())
    monkeypatch.setattr(main, "cloudflared_path", lambda _root: "cloudflared")
    calls = []

    @contextmanager
    def local_api():
        calls.append("api started")
        try:
            yield "http://127.0.0.1:8000"
        finally:
            calls.append("api stopped")

    @contextmanager
    def quick_tunnel(executable, origin):
        assert executable == "cloudflared"
        assert origin == "http://127.0.0.1:8000"
        calls.append("tunnel started")
        try:
            yield TUNNEL
        finally:
            calls.append("tunnel stopped")

    def run_bot(*, mini_app_url):
        assert mini_app_url == TUNNEL
        calls.append("bot started")
        if failure:
            raise failure

    monkeypatch.setattr(main, "local_api", local_api)
    monkeypatch.setattr(main, "quick_tunnel", quick_tunnel)
    monkeypatch.setattr(bot, "run_bot", run_bot)
    if failure:
        with pytest.raises(failure):
            main.run()
    else:
        main.run()
    assert TUNNEL in capsys.readouterr().out
    assert calls == [
        "api started",
        "tunnel started",
        "bot started",
        "tunnel stopped",
        "api stopped",
    ]
