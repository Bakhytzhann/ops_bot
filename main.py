import logging
import re
import shutil
import socket
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from queue import Empty, SimpleQueue
from tempfile import NamedTemporaryFile
from threading import Thread
from time import monotonic, sleep

import httpx

logger = logging.getLogger(__name__)
HOST = "127.0.0.1"
PORT = 8000
TUNNEL_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com(?![\w.-])")


class StartupError(RuntimeError):
    pass


def cloudflared_path(root):
    executable = "cloudflared.exe" if sys.platform == "win32" else "cloudflared"
    for path in (root / ".cache" / "cloudflared" / executable, root / executable):
        if path.is_file():
            return str(path)
    installed = shutil.which(executable)
    if installed:
        return installed
    raise StartupError(
        "cloudflared not found. Install it or put it in the project root."
    )


@contextmanager
def local_api(port=PORT):
    import uvicorn

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        if sys.platform != "win32":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((HOST, port))
        except OSError as exc:
            raise StartupError(f"Cannot bind FastAPI to {HOST}:{port}.") from exc
        address = f"http://{HOST}:{listener.getsockname()[1]}"
        server = uvicorn.Server(
            uvicorn.Config(
                "api.main:app",
                log_config=None,
                access_log=False,
                timeout_graceful_shutdown=5,
                proxy_headers=False,
            )
        )
        worker = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        worker.start()
        try:
            deadline = monotonic() + 15
            while not server.started:
                if not worker.is_alive() or monotonic() >= deadline:
                    raise StartupError("FastAPI did not start.")
                sleep(0.05)
            yield address
        finally:
            server.should_exit = True
            worker.join(timeout=8)


def read_tunnel_output(output, ready):
    public_url = None
    connected = False
    try:
        for line in output:
            if public_url is None and (match := TUNNEL_URL.search(line)):
                public_url = match.group()
            if public_url and not connected and "Registered tunnel connection" in line:
                connected = True
                ready.put(public_url)
            if " ERR " in line or " FTL " in line:
                logger.warning("cloudflared: %s", line.strip())
    finally:
        if not connected:
            ready.put(None)


def wait_for_public_api(url, process, timeout=90):
    deadline = monotonic() + timeout
    status = "no HTTPS response"
    with httpx.Client(timeout=3) as client:
        while process.poll() is None:
            try:
                response = client.get(url + "/health")
                status = f"HTTP {response.status_code}"
                if response.status_code == 200 and response.json() == {"status": "ok"}:
                    return
            except httpx.RequestError as exc:
                status = type(exc).__name__
            except ValueError:
                status = "invalid health response"
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise StartupError(
                    f"Cloudflare tunnel registered, but FastAPI is unreachable over HTTPS ({status})."
                )
            sleep(min(1, remaining))
    raise StartupError("cloudflared exited before the public API was reachable.")


@contextmanager
def quick_tunnel(executable, origin, timeout=45):
    with NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as config:
        config.write("{}\n")
    config_path = Path(config.name)
    process_options = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if sys.platform == "win32"
        else {"start_new_session": True}
    )
    try:
        with subprocess.Popen(
            [
                executable,
                "tunnel",
                "--config",
                str(config_path),
                "--no-autoupdate",
                "--url",
                origin,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
            **process_options,
        ) as process:
            ready = SimpleQueue()
            reader = Thread(
                target=read_tunnel_output,
                args=(process.stdout, ready),
                daemon=True,
            )
            try:
                reader.start()
                deadline = monotonic() + timeout
                while True:
                    try:
                        url = ready.get(timeout=0.2)
                    except Empty:
                        if monotonic() >= deadline:
                            raise StartupError(
                                "Cloudflare Quick Tunnel startup timed out."
                            ) from None
                    else:
                        break
                if url is None or process.poll() is not None:
                    raise StartupError(
                        "cloudflared exited before the tunnel was ready."
                    )
                print("Cloudflare tunnel registered. Waiting for HTTPS...", flush=True)
                wait_for_public_api(url, process)
                yield url
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                if reader.ident is not None:
                    reader.join(timeout=1)
    finally:
        config_path.unlink(missing_ok=True)


def run():
    from bot.bot import run_bot
    from config.logging_config import setup_logging
    from config.paths import project_directory
    from config.settings import settings

    setup_logging()
    root = project_directory()
    if not (root / "frontend" / "dist" / "index.html").is_file():
        raise StartupError("Mini App build is missing. Run npm run build in frontend.")
    executable = cloudflared_path(root)
    if not settings.telegram_bot_token:
        raise StartupError("TELEGRAM_BOT_TOKEN not configured.")
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    with local_api() as origin:
        print(f"FastAPI: {origin}", flush=True)
        with quick_tunnel(executable, origin) as url:
            print(f"Mini App tunnel: {url}", flush=True)
            run_bot(mini_app_url=url)


def main():
    logging.basicConfig(level=logging.WARNING)
    try:
        run()
    except KeyboardInterrupt:
        pass
    except StartupError as exc:
        logger.error("%s", exc)
        sys.exit(1)
    except Exception:
        logger.exception("OPS Bot startup failed")
        sys.exit(1)
    print("OPS Bot stopped.", flush=True)


if __name__ == "__main__":
    main()
