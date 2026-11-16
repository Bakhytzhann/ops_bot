"""SQLite snapshots made while OPS is stopped, including committed WAL data."""

import sqlite3
from pathlib import Path
from uuid import uuid4


class DatabaseSnapshots:
    def __init__(self, root):
        self.database = root / "data" / "agent.db"
        self.directory = root / "data" / "dev_agent" / "database_snapshots"

    def save(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.directory / (uuid4().hex + ".sqlite3")
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with (
            sqlite3.connect(self.database) as source,
            sqlite3.connect(target) as backup,
        ):
            source.backup(backup)
            if backup.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("Database snapshot is corrupt")
        return str(target)

    def validate(self, snapshot):
        path = Path(snapshot).resolve()
        if (
            not path.is_relative_to(self.directory.resolve())
            or not path.is_file()
            or Path(snapshot).is_symlink()
        ):
            raise ValueError("Invalid database snapshot")
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as source:
            if source.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("Database snapshot is corrupt")
        return path

    def restore(self, snapshot):
        path = self.validate(snapshot)
        with (
            sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as source,
            sqlite3.connect(self.database) as target,
        ):
            source.backup(target)
