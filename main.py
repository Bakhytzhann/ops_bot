import logging
import sys

logger = logging.getLogger(__name__)


def run(*, watch_for_stop=None, on_ready=None):
    from bot.bot import run_bot
    from config.logging_config import setup_logging
    from config.settings import settings

    setup_logging(capture_standard=watch_for_stop is not None)
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    run_bot(watch_for_stop=watch_for_stop, on_ready=on_ready)


def main():
    logging.basicConfig(level=logging.WARNING)
    print("Initializing OPS Bot Agent...")
    try:
        run()
    except KeyboardInterrupt:
        print("\nOPS Bot stopped.")
    except Exception:
        logger.exception("OPS Bot startup failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
