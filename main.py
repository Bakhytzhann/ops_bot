import logging
import sys

from bot.bot import run_bot
from config.logging_config import setup_logging
from config.settings import settings

logger = logging.getLogger(__name__)


def main():
    setup_logging()

    logging.basicConfig(
        level=logging.WARNING,
    )

    print("Initializing OPS Bot Agent...")

    try:
        settings.database_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        run_bot()

    except KeyboardInterrupt:
        print("\nOPS Bot stopped.")

    except Exception:
        logger.exception("OPS Bot startup failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
