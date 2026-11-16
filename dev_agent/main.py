import argparse
import asyncio
import logging
import os
from logging.handlers import RotatingFileHandler

from .config import ROOT, Config
from .deploy_manager import DeployManager
from .git_manager import GitManager
from .locking import FileLock
from .process import run
from .security import Redactor, SecretFilter
from .session_manager import prepare_codex_home
from .storage import Storage
from .supervisor import Supervisor
from .task_manager import TaskManager
from .telegram_controller import Controller
from .test_runner import TestRunner


def main():
    parser = argparse.ArgumentParser(
        description="Independent OPS Telegram development agent"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Local preflight only; no Telegram requests",
    )
    parser.add_argument(
        "--start-ops",
        action="store_true",
        help="Start OPS under supervisor at agent startup",
    )
    args = parser.parse_args()
    config = Config()
    config.validate_startup()
    redact = Redactor()
    log_dir = ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    handler = RotatingFileHandler(
        log_dir / "dev_agent.log", maxBytes=2_000_000, backupCount=2, encoding="utf-8"
    )
    handler.addFilter(SecretFilter(redact))
    logging.basicConfig(handlers=[handler], level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    lock = FileLock(ROOT / "data" / "dev_agent" / "agent.lock")
    storage = Storage(ROOT / "data" / "dev_agent" / "state.sqlite3")
    try:
        codex_home = prepare_codex_home(ROOT, config.dev_agent_retention_days)
        code, version = asyncio.run(
            run(
                [config.dev_agent_codex, "--version"],
                ROOT,
                env={**os.environ, "CODEX_HOME": str(codex_home)},
            )
        )
        if code:
            raise RuntimeError("Codex CLI unavailable")
        if args.check:
            print(redact(version))
            code, _ = asyncio.run(
                run(
                    [config.dev_agent_codex, "login", "status"],
                    ROOT,
                    env={**os.environ, "CODEX_HOME": str(codex_home)},
                )
            )
            print(
                "Isolated Codex login: "
                + ("OK" if code == 0 else "required (see DEV_AGENT.md)")
            )
            print("Repository, configuration and SQLite OK; no Telegram requests sent.")
            return
        storage.prune(config.dev_agent_retention_days)
        git = GitManager(ROOT, redact)
        tests = TestRunner(config.dev_agent_python, ROOT)
        supervisor = Supervisor(config, storage, redact)
        tasks = TaskManager(config, storage, git, tests, supervisor, redact)
        deploy = DeployManager(git, tests, supervisor, storage)
        controller = Controller(
            config, storage, tasks, git, tests, supervisor, deploy, redact
        )
        app = controller.build()
        if args.start_ops:
            original = app.post_init

            async def initialize(application):
                await git.clean()
                baseline = await deploy.release((await git.identity())["head"])
                await supervisor.start(baseline)
                storage.put("verified_release", str(baseline))
                await original(application)

            app.post_init = initialize
        # asyncio.run(preflight) closes its loop; PTB's synchronous runner needs one.
        asyncio.set_event_loop(asyncio.new_event_loop())
        app.run_polling(drop_pending_updates=False)
    finally:
        storage.db.close()
        lock.close()


if __name__ == "__main__":
    main()
