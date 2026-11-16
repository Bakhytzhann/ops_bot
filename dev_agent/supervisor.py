import asyncio
import contextlib
import os
import secrets
import sqlite3
from pathlib import Path

import psutil
from dotenv import dotenv_values

from .config import ROOT
from .process import group_options


class Supervisor:
    def __init__(self, config, storage, redact):
        self.config, self.storage, self.redact = config, storage, redact
        self.child = None
        self.pumps = []
        self.retries = 0
        self.log = ROOT / "logs" / "dev_agent_ops.log"
        self.log.parent.mkdir(parents=True, exist_ok=True)

    def owned(self):
        state = self.storage.get("process")
        if not state:
            return None
        try:
            process = psutil.Process(state["pid"])
            if abs(process.create_time() - state["created"]) > 0.01:
                raise RuntimeError("PID identity changed; refusing process control")
            if str(Path(state["entry"]).resolve()) not in process.cmdline():
                raise RuntimeError("Process command changed; refusing process control")
            return process if process.is_running() else None
        except psutil.NoSuchProcess:
            return None

    def status(self):
        process = self.owned()
        return {
            "running": bool(process),
            "pid": process.pid if process else None,
            "release": self.storage.get("release", str(ROOT)),
            "health": self.storage.get("health", "not checked"),
        }

    def reject_external(self):
        for process in psutil.process_iter(["pid", "cmdline", "cwd"]):
            try:
                cmd = process.info["cmdline"] or []
                if any(Path(a).name.lower() == "main.py" for a in cmd):
                    cwd = process.info["cwd"]
                    if cwd and Path(cwd).resolve() == ROOT.resolve():
                        raise RuntimeError(
                            "OPS Bot already running outside supervisor; stop it manually first"
                        )
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

    async def stop(self):
        process = self.owned()
        if process:
            children = process.children(recursive=True)
            stop_file = self.storage.get("stop_file")
            if stop_file:
                path = Path(stop_file).resolve()
                if not path.is_relative_to((ROOT / "data" / "dev_agent").resolve()):
                    raise ValueError("Invalid stop request path")
                path.touch()
            else:
                process.terminate()
            await asyncio.to_thread(psutil.wait_procs, [process], timeout=10)
            for child in children:
                with contextlib.suppress(psutil.NoSuchProcess):
                    child.terminate()
            _, alive = await asyncio.to_thread(
                psutil.wait_procs, [process, *children], timeout=5
            )
            for child in alive:
                with contextlib.suppress(psutil.NoSuchProcess):
                    child.kill()
            _, alive = await asyncio.to_thread(psutil.wait_procs, alive, timeout=5)
            if alive:
                raise RuntimeError("Old OPS process did not stop")
        self.storage.put("process", None)
        self.storage.put("health", "stopped")
        if self.child:
            await self.child.wait()
        await asyncio.gather(*self.pumps, return_exceptions=True)
        self.pumps = []

    async def pump(self, stream):
        pending = b""
        discard = False
        while block := await stream.read(8192):
            pending += block
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                if not discard and len(line) <= 65536:
                    self.write_log(line)
                discard = False
            if len(pending) > 65536:
                pending, discard = b"", True
        if pending and not discard:
            self.write_log(pending)

    def write_log(self, line):
        if self.log.exists() and self.log.stat().st_size > 2_000_000:
            self.log.write_text("", encoding="utf-8")
        with self.log.open("a", encoding="utf-8") as file:
            file.write(self.redact(line.decode("utf-8", errors="replace")) + "\n")

    async def start(self, release=None):
        if self.owned():
            raise RuntimeError("Managed OPS Bot is already running")
        self.reject_external()
        release = Path(release or self.storage.get("release", str(ROOT))).resolve()
        versions = (ROOT / "data" / "dev_agent" / "releases").resolve()
        if release != ROOT and not release.is_relative_to(versions):
            raise ValueError("Release path outside allowed directories")
        env = dict(os.environ)
        stop_file = ROOT / "data" / "dev_agent" / ("stop-" + secrets.token_hex(12))
        self.storage.put("stop_file", str(stop_file))
        env.update(
            {
                k: v
                for k, v in dotenv_values(ROOT / ".env").items()
                if v is not None
                and k != "BBOT_TOKEN"
                and not k.startswith("DEV_AGENT_")
            }
        )
        env.update(
            DATABASE_PATH=str(ROOT / "data" / "agent.db"),
            LOG_DIR=str(ROOT / "logs"),
            OPS_INSTANCE_LOCK=str(ROOT / "data" / "ops_bot.lock"),
            PYTHONUNBUFFERED="1",
            OPS_STOP_FILE=str(stop_file),
        )
        self.log.write_text("", encoding="utf-8")
        entry = str(release / "main.py")
        self.child = await asyncio.create_subprocess_exec(
            self.config.dev_agent_python,
            entry,
            cwd=release,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **group_options(),
        )
        process = psutil.Process(self.child.pid)
        self.storage.put(
            "process",
            {"pid": process.pid, "created": process.create_time(), "entry": entry},
        )
        self.storage.put("release", str(release))
        self.pumps = [
            asyncio.create_task(self.pump(self.child.stdout)),
            asyncio.create_task(self.pump(self.child.stderr)),
        ]
        await asyncio.sleep(3)
        if self.child.returncode is not None:
            raise RuntimeError("OPS Bot exited during startup; see /logs")
        # This is deliberately partial health, not proof of Telegram connectivity.
        try:
            with sqlite3.connect(
                f"file:{ROOT / 'data' / 'agent.db'}?mode=ro", uri=True
            ) as db:
                if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise RuntimeError("OPS database quick_check failed")
        except sqlite3.Error as exc:
            raise RuntimeError("OPS database startup check failed") from exc
        tail = self.log.read_text(encoding="utf-8")[-6000:] if self.log.exists() else ""
        if any(
            s in tail
            for s in (
                "Conflict:",
                "OPS Bot startup failed",
                "Traceback (most recent call last)",
            )
        ):
            raise RuntimeError("Critical error in OPS startup log")
        self.storage.put(
            "health", "partial: process alive, SQLite OK; Telegram not verified"
        )
        return self.status()

    async def restart(self):
        await self.stop()
        return await self.start()

    async def monitor(self, notify, lock):
        while True:
            await asyncio.sleep(10)
            if lock.locked():
                continue
            expected = self.storage.get("process")
            if expected and not self.owned():
                if self.retries >= 3:
                    self.storage.put("process", None)
                    await notify(
                        "OPS Bot stopped; automatic recovery limit reached (3)."
                    )
                    continue
                self.retries += 1
                await asyncio.sleep(5 * self.retries)
                try:
                    async with lock:
                        # A manual stop/start may have happened during recovery backoff.
                        if self.storage.get("process") != expected or self.owned():
                            continue
                        await self.start()
                    await notify("OPS Bot recovered; startup health is partial.")
                except (RuntimeError, OSError) as exc:
                    await notify("OPS recovery failed: " + self.redact(exc))
