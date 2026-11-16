import asyncio
import logging

from .codex_bridge import CodexBridge

logger = logging.getLogger(__name__)


class TaskManager:
    def __init__(self, config, storage, git, tests, supervisor, redact):
        self.config, self.storage, self.git = config, storage, git
        self.tests, self.supervisor, self.redact = tests, supervisor, redact
        self.lock = asyncio.Lock()
        self.wakeup = asyncio.Event()
        self.active = None
        self.execution = None
        self.bridge = None

    async def notify_safely(self, notify, owner, text):
        try:
            await notify(owner, text)
        except Exception as exc:  # noqa: BLE001 -- delivery must not stop the queue
            logger.warning("Task notification failed: %s", self.redact(exc))

    def enqueue(self, owner, prompt, current=None):
        if self.redact(prompt) != prompt:
            raise ValueError("Do not include secrets in task text")
        if not prompt.strip() or len(prompt) > 16000:
            raise ValueError("Task must contain 1–16000 characters")
        if current:
            if current["status"] in ("running", "queued", "waiting_user"):
                raise ValueError("Wait for completion or answer the pending question")
            task_id = current["id"]
            self.storage.change(task_id, prompt=prompt, status="queued")
        else:
            task_id = self.storage.create(owner, prompt)
        self.wakeup.set()
        return task_id

    async def cancel(self, owner, task_id=None):
        task = (
            self.storage.task(task_id)
            if task_id
            else next(
                (
                    t
                    for t in reversed(self.storage.tasks())
                    if t["owner"] == owner
                    and t["status"] in ("queued", "running", "waiting_user")
                ),
                None,
            )
        )
        if not task or task["owner"] != owner:
            raise ValueError("Task not found")
        self.storage.change(task["id"], status="cancelled", prompt="")
        if self.active == task["id"] and self.execution:
            self.execution.cancel()
            await asyncio.gather(self.execution, return_exceptions=True)

    async def execute(self, task, notify, ask):
        task_id, owner = task["id"], task["owner"]
        deliver = notify

        async def notify(owner, text):
            await self.notify_safely(deliver, owner, text)

        try:
            saved = self.storage.get(f"task_files:{task_id}")
            if not task["thread"] or not saved:
                await self.git.clean()
            elif saved != await self.git.fingerprint():
                raise ValueError(
                    "Files changed outside this session; preserve changes and create a new task"
                )
            identity = await self.git.identity()
            if not identity["branch"]:
                raise ValueError("Detached HEAD is not allowed for development")
            if task["base"] and task["base"] != identity["head"]:
                raise ValueError("Repository commit changed; create a new task")
            self.storage.change(task_id, status="running", base=identity["head"])
            await notify(
                owner, f"Task #{task_id}: Codex started on {identity['branch']}."
            )
            self.bridge = CodexBridge(
                self.config.dev_agent_codex,
                self.git.root,
                self.config.dev_agent_timeout,
                self.config.dev_agent_retention_days,
            )

            async def progress(text):
                # Only complete agent messages, never raw command output.
                await notify(owner, text)

            async def question(method, params):
                self.storage.change(task_id, status="waiting_user")
                try:
                    return await ask(owner, method, params)
                finally:
                    if self.storage.task(task_id)["status"] != "cancelled":
                        self.storage.change(task_id, status="running")

            result = await self.bridge.execute(
                task["prompt"],
                task["thread"],
                question,
                progress,
                lambda thread: self.storage.change(task_id, thread=thread),
            )
            checks = await self.tests.execute()
            self.storage.put(f"task_files:{task_id}", await self.git.fingerprint())
            self.storage.change(
                task_id,
                tests=self.redact(str(checks)),
                status="completed",
                result=self.redact(result[-6000:]),
                prompt="",
            )
            failure = next((r for r in checks.get("results", []) if r["code"]), None)
            diagnostics = (
                "Failed check: "
                + " ".join(failure["command"])
                + "\n"
                + failure["output"][-2500:]
                + "\n"
                if failure
                else ""
            )
            await notify(
                owner,
                f"Task #{task_id} completed.\n"
                f"Tests: {'passed' if checks['passed'] else 'FAILED'}\n"
                f"{diagnostics}"
                f"{await self.git.review()}\nPublication requires /push confirmation.\n"
                f"OPS Bot: {self.supervisor.status()}",
            )
        except asyncio.CancelledError:
            self.storage.change(task_id, status="cancelled", prompt="")
            await notify(
                owner, f"Task #{task_id} cancelled. Inspect /diff before continuing."
            )
            raise
        except Exception as exc:  # noqa: BLE001 -- isolate task failures from controller
            self.storage.change(
                task_id, status="failed", result=self.redact(exc), prompt=""
            )
            logger.error("Task %s failed: %s", task_id, self.redact(exc))
            await notify(owner, f"Task #{task_id} failed: {self.redact(exc)}")

    async def worker(self, notify, ask):
        while True:
            queued = self.storage.next_queued()
            if not queued:
                self.wakeup.clear()
                await self.wakeup.wait()
                continue
            async with self.lock:
                # Cancellation may have happened while waiting for the operation lock.
                if self.storage.task(queued["id"])["status"] != "queued":
                    continue
                self.active = queued["id"]
                self.execution = asyncio.create_task(self.execute(queued, notify, ask))
                try:
                    await self.execution
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling():
                        raise
                except Exception as exc:  # noqa: BLE001 -- isolate each queued task
                    logger.error("Queue task failed: %s", self.redact(exc))
                finally:
                    self.active, self.execution = None, None
