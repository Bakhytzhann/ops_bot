import os
import shutil
import time
from pathlib import Path


def prepare_codex_home(root, retention_days=7):
    """Own session storage; copy file-based login without copying chat history."""
    directory = root / "data" / "dev_agent" / "codex"
    directory.mkdir(parents=True, exist_ok=True)
    source = (
        Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json"
    )
    target = directory / "auth.json"
    if (
        not target.exists()
        and source.is_file()
        and source.resolve() != target.resolve()
    ):
        shutil.copyfile(source, target)
    sessions = directory / "sessions"
    if sessions.is_dir() and not sessions.is_symlink():
        cutoff = time.time() - max(retention_days, 1) * 86400
        for path in sessions.rglob("*.jsonl"):
            if (
                not path.is_symlink()
                and path.resolve().is_relative_to(sessions.resolve())
                and path.stat().st_mtime < cutoff
            ):
                path.unlink()
    return directory


class SessionManager:
    def __init__(self, storage):
        self.storage = storage

    def current(self, owner):
        return next(
            (
                task
                for task in self.storage.tasks()
                if task["owner"] == owner
                and task["thread"]
                and task["status"] != "cancelled"
            ),
            None,
        )
