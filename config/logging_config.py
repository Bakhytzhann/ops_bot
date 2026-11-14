import logging
import sys

from loguru import logger

from config.settings import settings


def setup_logging():
    settings.log_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    logger.remove()

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
        enqueue=True,
    )

    # Disable verbose HTTP library logging.
    for name in ("httpx", "httpcore", "telegram"):
        logging.getLogger(name).setLevel(logging.WARNING)

    return logger
