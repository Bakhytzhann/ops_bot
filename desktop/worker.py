import json
import logging
import msvcrt
import os
import sqlite3
from threading import Thread

import win32api
import win32con
from dotenv.parser import parse_stream
from pydantic import ValidationError
from telegram.error import Conflict, InvalidToken, NetworkError
from win32console import STD_INPUT_HANDLE, STD_OUTPUT_HANDLE

from config.instance_lock import InstanceAlreadyRunning
from config.paths import project_directory
from config.settings import get_settings
from main import run

logger = logging.getLogger(__name__)


def pipe(standard_handle, mode):
    # The windowed bootloader leaves Python's sys.std* unset. QProcess still
    # supplies private Windows pipe handles for the worker.
    handle = win32api.GetStdHandle(standard_handle)
    current = win32api.GetCurrentProcess()
    duplicate = win32api.DuplicateHandle(
        current, handle, current, 0, False, win32con.DUPLICATE_SAME_ACCESS
    )
    flags = os.O_RDONLY if mode == "rb" else os.O_WRONLY
    return os.fdopen(
        msvcrt.open_osfhandle(duplicate.Detach(), flags | os.O_BINARY), mode
    )


def run_worker():
    with (
        pipe(STD_INPUT_HANDLE, "rb") as commands,
        pipe(STD_OUTPUT_HANDLE, "wb") as events,
    ):
        if commands.readline() != b"start\n":
            return 1

        def send(**event):
            events.write(json.dumps(event).encode("ascii") + b"\n")
            events.flush()

        def watch_for_stop(callback):
            def wait():
                commands.read(1)
                callback()

            Thread(target=wait, daemon=True).start()

        try:
            root = project_directory()
            for filename in (".env.local", ".env"):
                path = root / filename
                if path.is_file():
                    with path.open(encoding="utf-8-sig") as env_file:
                        invalid = next(
                            (b for b in parse_stream(env_file) if b.error), None
                        )
                    if invalid is not None:
                        send(
                            error=f"Invalid syntax in {filename} at line {invalid.original.line}."
                        )
                        return 1
            settings = get_settings()
            try:
                admin_ids = settings.admin_ids
                allowed_ids = settings.allowed_user_ids
            except ValueError:
                send(
                    error="Check Telegram user IDs in TELEGRAM_ADMIN_IDS and TELEGRAM_ALLOWED_USER_IDS."
                )
                return 1
            send(
                configuration={
                    "database_path": str(settings.database_path),
                    "log_dir": str(settings.log_dir),
                    "telegram_admin_ids": ",".join(map(str, admin_ids)),
                    "telegram_allowed_user_ids": ",".join(map(str, allowed_ids)),
                }
            )
            if (
                not settings.telegram_bot_token
                or not settings.telegram_bot_token.get_secret_value().strip()
            ):
                send(error="Set TELEGRAM_BOT_TOKEN in .env and start the bot again.")
                return 1
            run(watch_for_stop=watch_for_stop, on_ready=lambda: send(ready=True))
        except ValidationError as exc:
            fields = ", ".join(str(error["loc"][0]) for error in exc.errors())
            send(error=f"Invalid configuration ({fields}). Check .env and .env.local.")
        except InvalidToken:
            send(error="Telegram rejected TELEGRAM_BOT_TOKEN. Check the token in .env.")
        except (Conflict, InstanceAlreadyRunning):
            send(
                error="Another process is polling Telegram for this bot. Stop it and try again."
            )
        except NetworkError:
            send(
                error="Could not connect to the Telegram API. Check the network and logs."
            )
        except sqlite3.Error:
            send(
                error="Could not open the bot database. Check DATABASE_PATH and permissions."
            )
        except (OSError, ValueError) as exc:
            send(
                error=f"Could not start the bot ({type(exc).__name__}). Check configuration and paths."
            )
        except Exception as exc:
            logger.exception("Unexpected managed bot failure")
            send(error=f"Bot failed ({type(exc).__name__}). See Open Logs for details.")
        else:
            return 0
        return 1
