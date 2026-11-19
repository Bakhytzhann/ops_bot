import logging
import re
import sys
from urllib.parse import quote

from loguru import logger
from pydantic import SecretStr

from config.settings import settings


class StandardLogHandler(logging.Handler):
    def emit(self, record):
        logger.log(record.levelname, "{}: {}", record.name, record.getMessage())


def redact_record(record):
    for field in type(settings).model_fields:
        value = getattr(settings, field)
        if isinstance(value, SecretStr) and value.get_secret_value():
            secret = value.get_secret_value()
            for text in (secret, quote(secret, safe="")):
                record["message"] = record["message"].replace(text, "[REDACTED]")
    record["message"] = re.sub(
        r"(https?://[^\s?]+)\?[^\s]+", r"\1?[REDACTED]", record["message"]
    )
    if record["exception"] is not None:
        record["message"] += f" ({record['exception'].type.__name__})"
        record["exception"] = None


def setup_logging(*, capture_standard=False):
    settings.log_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    logger.remove()

    if capture_standard:
        logger.configure(patcher=redact_record)
        logging.basicConfig(handlers=[StandardLogHandler()], level=logging.INFO)
    elif sys.stderr is not None:
        logger.add(
            sys.stderr,
            level="DEBUG" if settings.debug else "INFO",
            format=("{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}"),
            backtrace=False,
            diagnose=False,
        )

    logger.add(
        settings.log_dir / "agent.log",
        rotation="10 MB",
        retention="14 days",
        level="INFO",
        backtrace=False,
        diagnose=False,
        enqueue=not capture_standard,
    )

    for name in ("httpx", "httpcore", "telegram"):
        logging.getLogger(name).setLevel(logging.WARNING)
