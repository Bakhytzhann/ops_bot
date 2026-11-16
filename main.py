import logging
import os
import sys
from pathlib import Path

from bot.bot import run_bot
from config.logging_config import setup_logging
from config.settings import settings

logger = logging.getLogger(__name__)


def main():
    from dev_agent.locking import FileLock

    instance_lock = FileLock(
        Path(
            os.environ.get(
                "OPS_INSTANCE_LOCK",
                str(Path(__file__).resolve().parent / "data" / "ops_bot.lock"),
            )
        )
    )
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

        settings.log_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        run_bot()

    except KeyboardInterrupt:
        print("\nOPS Bot stopped.")

    except Exception:
        logger.exception("OPS Bot startup failed")
        sys.exit(1)
    finally:
        instance_lock.close()


if __name__ == "__main__":
    main()
