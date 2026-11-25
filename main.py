import json
import logging
import os
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
NGROK_ERROR = re.compile(r"\bERR_NGROK_\d+\b")


class StartupError(RuntimeError):
    pass


def ngrok_path(root):
    executable = "ngrok.exe" if sys.platform == "win32" else "ngrok"
    for path in (root / ".cache" / "ngrok" / executable, root / executable):
        if path.is_file():
            return str(path)
    if installed := shutil.which(executable):
        return installed
    raise StartupError("ngrok not found. Install ngrok or put it in .cache/ngrok.")


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
                    f"ngrok tunnel started, but FastAPI is unreachable over HTTPS ({status})."
                )
            sleep(min(1, remaining))
    raise StartupError("ngrok exited before the public API was reachable.")


def _stop_tunnel(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def _read_ngrok_output(output, ready, endpoint):
    announced = False
    error_code = None
    try:
        for line in output:
            if match := NGROK_ERROR.search(line):
                error_code = match.group()
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if (
                not announced
                and isinstance(event, dict)
                and (
                    event.get("msg") in {"started tunnel", "started endpoint"}
                    and event.get("url") in {endpoint, endpoint + "/"}
                )
            ):
                announced = True
                ready.put((True, None))
    finally:
        if not announced:
            ready.put((False, error_code))


@contextmanager
def ngrok_tunnel(executable, origin, *, endpoint, token, timeout=45):
    with NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as config:
        config.write(
            'version: "3"\nagent:\n  web_addr: "false"\n  update_check: false\n'
        )
    config_path = Path(config.name)
    process_options = (
        {"creationflags": subprocess.CREATE_NO_WINDOW}
        if sys.platform == "win32"
        else {"start_new_session": True}
    )
    try:
        with subprocess.Popen(
            [
                executable,
                "http",
                origin,
                "--url",
                endpoint,
                "--config",
                str(config_path),
                "--inspect=false",
                "--log=stdout",
                "--log-format=json",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "NGROK_AUTHTOKEN": token},
            **process_options,
        ) as process:
            ready = SimpleQueue()
            reader = Thread(
                target=_read_ngrok_output,
                args=(process.stdout, ready, endpoint),
                daemon=True,
            )
            try:
                reader.start()
                try:
                    started, error_code = ready.get(timeout=timeout)
                except Empty:
                    raise StartupError(
                        "ngrok did not start the configured endpoint in time."
                    ) from None
                if not started or process.poll() is not None:
                    detail = f" ({error_code})" if error_code else ""
                    raise StartupError(
                        f"ngrok exited before the configured endpoint started{detail}."
                    )
                wait_for_public_api(endpoint, process)
                yield endpoint
            finally:
                _stop_tunnel(process)
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
    if not settings.telegram_bot_token:
        raise StartupError("TELEGRAM_BOT_TOKEN not configured.")
    endpoint = settings.ngrok_endpoint
    token = (
        settings.ngrok_token.get_secret_value().strip() if settings.ngrok_token else ""
    )
    if not (endpoint and token):
        raise StartupError("Set both NGROK_TOKEN and NGROK_ENDPOINT for ngrok.")
    executable = ngrok_path(root)
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    with local_api() as origin:
        print(f"FastAPI: {origin}", flush=True)
        with ngrok_tunnel(executable, origin, endpoint=endpoint, token=token) as url:
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
