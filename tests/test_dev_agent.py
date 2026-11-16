import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from dev_agent.codex_bridge import CodexBridge
from dev_agent.config import Config
from dev_agent.deploy_manager import DeployManager
from dev_agent.git_manager import GitManager
from dev_agent.locking import FileLock
from dev_agent.process import run
from dev_agent.security import Redactor, SecretFilter, authorized
from dev_agent.session_manager import SessionManager
from dev_agent.storage import Storage
from dev_agent.supervisor import Supervisor
from dev_agent.task_manager import TaskManager
from dev_agent.telegram_controller import Controller
from dev_agent.test_runner import TestRunner


@pytest.fixture
def store(tmp_path):
    storage = Storage(tmp_path / "state.db")
    yield storage
    storage.db.close()


@pytest.fixture
def redactor():
    result = Redactor()
    result.secrets = ["my-long-private-credential"]
    return result


@pytest.mark.parametrize(
    "owner,chat,admins,expected",
    [
        (42, "private", {42}, True),
        (43, "private", {42}, False),
        (42, "group", {42}, False),
        (42, "supergroup", {42}, False),
        (42, "private", set(), False),
    ],
)
def test_access(owner, chat, admins, expected):
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=owner),
        effective_chat=SimpleNamespace(type=chat),
    )
    assert authorized(update, admins) == expected


def test_confirmations_are_bound_expiring_and_single_use(store):
    token = store.confirm(42, {"action": "push"})
    with pytest.raises(ValueError):
        store.consume(43, token)
    assert store.consume(42, token)["action"] == "push"
    with pytest.raises(ValueError):
        store.consume(42, token)
    token = store.confirm(42, {}, ttl=-1)
    with pytest.raises(ValueError):
        store.consume(42, token)


def test_recovery_preserves_queued_and_session_but_invalidates_buttons(tmp_path):
    path = tmp_path / "state.db"
    first = Storage(path)
    running = first.create(42, "task", "thread-1")
    first.change(running, status="running")
    queued = first.create(42, "next")
    token = first.confirm(42, {"action": "restart"})
    first.db.close()
    second = Storage(path)
    assert second.task(running)["status"] == "failed"
    assert second.task(running)["thread"] == "thread-1"
    assert second.next_queued()["id"] == queued
    with pytest.raises(ValueError):
        second.consume(42, token)
    second.db.close()


def test_duplicate_updates_persist(store):
    assert not store.seen(100)
    assert store.seen(100)


def test_queue_does_not_drop_old_tasks(store):
    first = store.create(42, "first")
    for _ in range(40):
        store.create(42, "later")
    assert store.next_queued()["id"] == first


def test_retention_removes_content_and_session_reference(store):
    task = store.create(42, "private task", "thread")
    store.change(task, status="completed", result="private result", tests="output")
    store.db.execute("UPDATE tasks SET created=?", (time.time() - 10 * 86400,))
    store.prune(7)
    row = store.task(task)
    assert row["prompt"] == row["result"] == row["tests"] == ""
    assert row["thread"] is None


def test_redaction_and_logging(redactor):
    import logging

    text = (
        "my-long-private-credential 123456789:abcdefghijklmnopqrstuvwxyz password=hello"
    )
    result = redactor(text)
    assert "my-long" not in result and "abcdefghijklmnopqrstuvwxyz" not in result
    assert "hello" not in result
    record = logging.LogRecord("test", 20, "", 0, "%s", (text,), None)
    SecretFilter(redactor).filter(record)
    assert record.getMessage() == result
    assert redactor.contains_secret(text)
    assert not redactor.contains_secret("token = self.storage.confirm(owner, payload)")


def test_duplicate_instance_lock(tmp_path):
    first = FileLock(tmp_path / "instance.lock")
    try:
        with pytest.raises(RuntimeError):
            FileLock(tmp_path / "instance.lock")
    finally:
        first.close()
    FileLock(tmp_path / "instance.lock").close()


@pytest.mark.asyncio
async def test_process_timeout_and_nonzero_exit(tmp_path):
    with pytest.raises(TimeoutError):
        await run([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path, 0.1)
    code, text = await run(
        [sys.executable, "-c", 'print("failed"); raise SystemExit(7)'], tmp_path
    )
    assert code == 7 and "failed" in text


@pytest.fixture
def controller(store, redactor, tmp_path):
    config = Config(_env_file=None, dev_agent_admin_ids="42")
    git = SimpleNamespace(
        root=tmp_path,
        clean=AsyncMock(),
        identity=AsyncMock(
            return_value={"head": "base", "branch": "main", "remote": "origin"}
        ),
        fingerprint=AsyncMock(return_value="fingerprint"),
        review=AsyncMock(return_value="M file.py"),
    )
    tests = SimpleNamespace(execute=AsyncMock(return_value={"passed": True}))
    supervisor = SimpleNamespace(
        status=Mock(return_value={"running": True}),
        start=AsyncMock(),
        stop=AsyncMock(),
        restart=AsyncMock(),
        log=tmp_path / "ops.log",
    )
    tasks = TaskManager(config, store, git, tests, supervisor, redactor)
    ctl = Controller(config, store, tasks, git, tests, supervisor, Mock(), redactor)
    ctl.application = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
    return ctl


def update(text, number=1, owner=42, chat="private"):
    return SimpleNamespace(
        update_id=number,
        effective_user=SimpleNamespace(id=owner),
        effective_chat=SimpleNamespace(type=chat),
        effective_message=SimpleNamespace(text=text),
        callback_query=None,
    )


@pytest.mark.asyncio
async def test_controller_rejects_unauthorized_and_groups(controller, store):
    await controller.handle(update("/task unsafe", owner=99), None)
    await controller.handle(update("/task unsafe", chat="group"), None)
    assert store.tasks() == []
    controller.application.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_plain_text_requires_session_and_task_is_deduplicated(controller, store):
    await controller.handle(update("random shell command"), None)
    assert not store.tasks()
    await controller.handle(update("/task Fix the card", 2), None)
    await controller.handle(update("/task Fix the card", 2), None)
    assert len(store.tasks()) == 1
    assert store.tasks()[0]["prompt"] == "Fix the card"


@pytest.mark.asyncio
async def test_plain_text_resumes_same_task(controller, store):
    task = store.create(42, "task", "thread")
    store.change(task, status="completed")
    await controller.handle(update("Now adjust the date"), None)
    assert len(store.tasks()) == 1
    assert store.task(task)["thread"] == "thread"
    assert store.task(task)["status"] == "queued"


@pytest.mark.asyncio
async def test_text_question_answer(controller):
    question = asyncio.create_task(
        controller.ask(
            42,
            "item/tool/requestUserInput",
            {"questions": [{"id": "date", "question": "Which date?", "options": None}]},
        )
    )
    await asyncio.sleep(0)
    await controller.handle(update("Tomorrow"), None)
    assert await question == {"date": {"answers": ["Tomorrow"]}}


@pytest.mark.asyncio
async def test_stale_confirmation_cannot_restart(controller, store):
    await controller.operation(42, "restart", {"fingerprint": "old"})
    controller.supervisor.restart.assert_not_awaited()


@pytest.mark.asyncio
async def test_busy_operation_does_not_restart(controller):
    async with controller.tasks.lock:
        await controller.operation(42, "restart", {"fingerprint": "fingerprint"})
    controller.supervisor.restart.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirmed_restart(controller):
    await controller.operation(42, "restart", {"fingerprint": "fingerprint"})
    controller.supervisor.restart.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,method", [("start_ops", "start"), ("stop_ops", "stop")]
)
@pytest.mark.parametrize("callback", [False, True])
async def test_ops_controls(controller, store, action, method, callback):
    request = update("/" + action)
    if callback:
        request.callback_query = SimpleNamespace(
            data="cmd:" + action, answer=AsyncMock()
        )
    await controller.handle(request, None)
    await asyncio.gather(*controller.background)
    getattr(controller.supervisor, method).assert_awaited_once_with()
    assert store.get("last_operation")["action"] == action
    controller.git.fingerprint.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,method", [("start_ops", "start"), ("stop_ops", "stop")]
)
async def test_ops_controls_respect_operation_lock(controller, action, method):
    async with controller.tasks.lock:
        await controller.operation(42, action)
    getattr(controller.supervisor, method).assert_not_awaited()


def test_ops_controls_present_in_keyboard():
    callbacks = {
        button.callback_data
        for row in Controller.keyboard().inline_keyboard
        for button in row
    }
    assert {"cmd:start_ops", "cmd:stop_ops"} <= callbacks


@pytest.mark.asyncio
async def test_manual_stop_cancels_pending_recovery(store, redactor, monkeypatch):
    supervisor = Supervisor(Config(_env_file=None), store, redactor)
    store.put("process", {"pid": 123, "created": 1, "entry": "main.py"})
    monkeypatch.setattr(supervisor, "owned", lambda: None)
    supervisor.start = AsyncMock()
    notify = AsyncMock()
    sleeps = 0

    async def sleep(delay):
        nonlocal sleeps
        sleeps += 1
        if sleeps == 2:
            await supervisor.stop()
        elif sleeps == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr("dev_agent.supervisor.asyncio.sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await supervisor.monitor(notify, asyncio.Lock())
    supervisor.start.assert_not_awaited()
    notify.assert_not_awaited()
    assert supervisor.status()["health"] == "stopped"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("timeout"),
        ConnectionError("lost connection"),
        RuntimeError("child crash"),
    ],
)
async def test_task_failure_is_recorded(controller, store, monkeypatch, failure):
    bridge = Mock(execute=AsyncMock(side_effect=failure))
    monkeypatch.setattr("dev_agent.task_manager.CodexBridge", Mock(return_value=bridge))
    task = store.create(42, "Fix date")
    await controller.tasks.execute(store.task(task), AsyncMock(), AsyncMock())
    assert store.task(task)["status"] == "failed"


@pytest.mark.asyncio
async def test_task_completion_and_continuation_with_owned_changes(
    controller, store, monkeypatch
):
    bridge = Mock(execute=AsyncMock(return_value="Updated file.py"))
    monkeypatch.setattr("dev_agent.task_manager.CodexBridge", Mock(return_value=bridge))
    task = store.create(42, "Fix date", "thread")
    store.put(f"task_files:{task}", "fingerprint")
    await controller.tasks.execute(store.task(task), AsyncMock(), AsyncMock())
    assert store.task(task)["status"] == "completed"
    controller.git.clean.assert_not_awaited()
    assert bridge.execute.await_args.args[1] == "thread"


@pytest.mark.asyncio
async def test_git_conflict_blocks_codex(controller, store, monkeypatch):
    controller.git.clean.side_effect = ValueError("dirty tree")
    bridge = Mock()
    monkeypatch.setattr("dev_agent.task_manager.CodexBridge", bridge)
    task = store.create(42, "Fix date")
    await controller.tasks.execute(store.task(task), AsyncMock(), AsyncMock())
    assert store.task(task)["status"] == "failed"
    bridge.assert_not_called()


@pytest.mark.asyncio
async def test_test_runner_stops_on_error(monkeypatch, tmp_path):
    runner = AsyncMock(return_value=(1, "syntax failed"))
    monkeypatch.setattr("dev_agent.test_runner.run", runner)
    result = await TestRunner(sys.executable, tmp_path).execute()
    assert not result["passed"] and len(result["results"]) == 1


@pytest.mark.asyncio
async def test_bridge_rpc_events_approval_and_questions():
    bridge = CodexBridge("codex", Path("."))
    bridge.send = AsyncMock()
    bridge.ask = AsyncMock(return_value=False)
    await bridge.event(
        {
            "id": 99,
            "method": "item/commandExecution/requestApproval",
            "params": {"command": "unsafe"},
        }
    )
    assert bridge.send.await_args.args[0]["result"] == {"decision": "decline"}
    bridge.ask.return_value = {"question": {"answers": ["yes"]}}
    await bridge.event(
        {"id": 100, "method": "item/tool/requestUserInput", "params": {}}
    )
    assert "answers" in bridge.send.await_args.args[0]["result"]
    await bridge.event({"id": 101, "method": "unknown/request", "params": {}})
    assert bridge.send.await_args.args[0]["error"]["code"] == -32601


@pytest.mark.asyncio
async def test_bridge_eof_and_timeout():
    bridge = CodexBridge("codex", Path("."), timeout=0.01)
    bridge.process = SimpleNamespace(
        stdout=SimpleNamespace(readline=AsyncMock(return_value=b""))
    )
    with pytest.raises(ConnectionError):
        await bridge.read()

    async def hang():
        await asyncio.sleep(1)

    bridge.process.stdout.readline = hang
    with pytest.raises(TimeoutError):
        await bridge.read()


@pytest.mark.asyncio
async def test_deploy_failure_restores_previous_release(store, tmp_path):
    git = SimpleNamespace(clean=AsyncMock(), fingerprint=AsyncMock(return_value="same"))
    supervisor = SimpleNamespace(
        stop=AsyncMock(),
        status=Mock(return_value={"running": True}),
        start=AsyncMock(side_effect=[RuntimeError("crash"), {"running": True}]),
    )
    deploy = DeployManager(git, Mock(), supervisor, store)
    deploy.release = AsyncMock(return_value=tmp_path / "candidate")
    store.put("verified_release", "previous")
    with pytest.raises(RuntimeError):
        await deploy.deploy({"fingerprint": "same", "commit": "abc"})
    assert supervisor.start.await_args_list[1].args == ("previous",)
    assert store.get("deployment")["restored"]


@pytest.mark.asyncio
async def test_deploy_rejects_failed_checks_and_changed_files(store):
    git = SimpleNamespace(
        clean=AsyncMock(), fingerprint=AsyncMock(return_value="changed")
    )
    tests = SimpleNamespace(execute=AsyncMock(return_value={"passed": False}))
    supervisor = SimpleNamespace(stop=AsyncMock())
    deploy = DeployManager(git, tests, supervisor, store)
    with pytest.raises(RuntimeError):
        await deploy.prepare()
    with pytest.raises(ValueError):
        await deploy.deploy({"fingerprint": "old"})
    supervisor.stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_rollback_preserves_checkout_and_updates_metadata(store, tmp_path):
    previous = tmp_path / "previous"
    previous.mkdir()
    store.put("previous_release", str(previous))
    store.put("verified_release", "current")
    supervisor = SimpleNamespace(
        stop=AsyncMock(), start=AsyncMock(return_value={"running": True})
    )
    git = Mock(git=AsyncMock(return_value=""))
    deploy = DeployManager(git, Mock(), supervisor, store)
    await deploy.rollback()
    assert store.get("verified_release") == str(previous)
    git.assert_not_called()


def test_supervisor_refuses_reused_pid(store, redactor, monkeypatch):
    supervisor = Supervisor(Config(_env_file=None), store, redactor)
    store.put("process", {"pid": 123, "created": 10, "entry": "/main.py"})
    monkeypatch.setattr(
        "dev_agent.supervisor.psutil.Process",
        Mock(return_value=SimpleNamespace(create_time=lambda: 20)),
    )
    with pytest.raises(RuntimeError, match="PID identity"):
        supervisor.owned()


@pytest.mark.asyncio
async def test_log_redaction_across_chunks(store, redactor, tmp_path):
    supervisor = Supervisor(Config(_env_file=None), store, redactor)
    supervisor.log = tmp_path / "ops.log"
    stream = SimpleNamespace(
        read=AsyncMock(side_effect=[b"my-long-private-", b"credential\n", b""])
    )
    await supervisor.pump(stream)
    assert supervisor.log.read_text() == "[REDACTED]\n"


@pytest.mark.asyncio
async def test_git_fast_forward_and_fingerprint(tmp_path, redactor):
    async def git(*args):
        code, result = await run(["git", *args], tmp_path)
        assert code == 0, result

    await git("init", "-b", "main")
    await git("config", "user.email", "test@example.invalid")
    await git("config", "user.name", "test")
    await git("remote", "add", "origin", "https://github.com/Bakhytzhann/ops_bot.git")
    (tmp_path / "file.py").write_text("x = 1\n")
    await git("add", "file.py")
    await git("commit", "-m", "Initial")
    manager = GitManager(tmp_path, redactor)
    await manager.clean()
    first = await manager.fingerprint()
    (tmp_path / "file.py").write_text("x = 2\n")
    assert first != await manager.fingerprint()
    with pytest.raises(ValueError):
        await manager.clean()
    (tmp_path / "secret.key").write_text("private")
    with pytest.raises(ValueError, match="Sensitive file"):
        await manager.publication_check()


@pytest.mark.asyncio
async def test_git_first_push_and_existing_branch(tmp_path, redactor):
    remote = tmp_path / "remote.git"
    root = tmp_path / "work"
    root.mkdir()
    manager = GitManager(root, redactor)
    await manager.git("init", "--bare", str(remote))
    await manager.git("init", "-b", "feature/test")
    await manager.git("config", "user.email", "test@example.invalid")
    await manager.git("config", "user.name", "test")
    await manager.git("remote", "add", "origin", str(remote))
    (root / "file.py").write_text("x = 1\n")
    await manager.git("add", ".")
    await manager.git("commit", "-m", "Initial")
    # Exercise real Git against a local bare remote, with no network access.
    manager.publication_check = AsyncMock(side_effect=manager.identity)
    await manager.push()
    assert await manager.git("rev-parse", "@{upstream}") == await manager.git(
        "rev-parse", "HEAD"
    )
    (root / "file.py").write_text("x = 2\n")
    await manager.push()
    assert await manager.git("rev-parse", "@{upstream}") == await manager.git(
        "rev-parse", "HEAD"
    )
    await manager.git("switch", "-c", "feature/secret")
    (root / "secret.key").write_text("private")
    await manager.git("add", ".")
    await manager.git("commit", "-m", "Sensitive file")
    (root / "secret.key").unlink()
    await manager.git("add", "-u")
    await manager.git("commit", "-m", "Remove sensitive file")
    with pytest.raises(ValueError, match="Sensitive file in outgoing history"):
        await manager.push()
    assert not await manager.git(
        "ls-remote", "--heads", "origin", "refs/heads/feature/secret"
    )


def test_config_empty_allowlist():
    config = Config(_env_file=None, bbot_token="example", dev_agent_admin_ids="")
    with pytest.raises(ValueError):
        config.validate_startup()


def test_session_is_per_owner(store):
    store.create(99, "task", "other-thread")
    assert SessionManager(store).current(42) is None


def test_protocol_payload_matches_installed_schema():
    # Local generated schema is an optional additional verification, never required in CI.
    path = Path("data/dev_agent_schema/ToolRequestUserInputResponse.json")
    if not path.exists():
        pytest.skip("Local Codex schema not generated")
    import jsonschema

    jsonschema.validate(
        {"answers": {"q": {"answers": ["answer"]}}}, json.loads(path.read_text())
    )


@pytest.mark.asyncio
async def test_full_app_server_turn_and_resume_without_network(monkeypatch, tmp_path):
    """Drive real bridge control flow with a protocol-speaking fake child."""
    messages = asyncio.Queue()
    sent = []

    class Writer:
        def write(self, line):
            request = json.loads(line)
            sent.append(request)
            method = request.get("method")
            if method == "initialize":
                response = {}
            elif method in ("thread/start", "thread/resume"):
                response = {"thread": {"id": "thread-1"}}
            elif method == "turn/start":
                response = {"turn": {"id": "turn-1"}}
            else:
                return
            messages.put_nowait(
                json.dumps({"id": request["id"], "result": response}).encode() + b"\n"
            )
            if method == "turn/start":
                for event in [
                    {
                        "method": "item/completed",
                        "params": {
                            "item": {"type": "agentMessage", "text": "Updated date"}
                        },
                    },
                    {
                        "method": "turn/completed",
                        "params": {"turn": {"id": "turn-1", "status": "completed"}},
                    },
                ]:
                    messages.put_nowait(json.dumps(event).encode() + b"\n")

        async def drain(self):
            pass

    child = SimpleNamespace(
        stdin=Writer(),
        stdout=SimpleNamespace(readline=messages.get),
        stderr=SimpleNamespace(read=AsyncMock(return_value=b"")),
    )
    monkeypatch.setattr(
        "dev_agent.codex_bridge.asyncio.create_subprocess_exec",
        AsyncMock(return_value=child),
    )
    monkeypatch.setattr("dev_agent.codex_bridge.terminate", AsyncMock())
    monkeypatch.setattr(
        "dev_agent.codex_bridge.prepare_codex_home", lambda *args: tmp_path
    )
    bridge = CodexBridge("codex", tmp_path)
    save = Mock()
    result = await bridge.execute(
        "Fix date", "thread-1", AsyncMock(), AsyncMock(), save
    )
    assert result == "Updated date"
    save.assert_called_once_with("thread-1")
    request = next(m for m in sent if m.get("method") == "thread/resume")
    assert request["params"]["sandbox"] == "workspace-write"
    assert request["params"]["approvalPolicy"] == "on-request"
    assert request["params"]["threadId"] == "thread-1"


@pytest.mark.asyncio
async def test_supervisor_successful_start_uses_shared_data_and_partial_health(
    store, redactor, tmp_path, monkeypatch
):
    import sqlite3

    monkeypatch.setattr("dev_agent.supervisor.ROOT", tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "dev_agent").mkdir()
    with sqlite3.connect(tmp_path / "data" / "agent.db") as db:
        db.execute("CREATE TABLE example(id INTEGER)")
    child = SimpleNamespace(
        pid=123,
        returncode=None,
        stdout=SimpleNamespace(read=AsyncMock(return_value=b"")),
        stderr=SimpleNamespace(read=AsyncMock(return_value=b"")),
    )
    process = SimpleNamespace(
        pid=123,
        create_time=lambda: 10,
        cmdline=lambda: [sys.executable, str(tmp_path / "main.py")],
        is_running=lambda: True,
    )
    monkeypatch.setattr("dev_agent.supervisor.psutil.process_iter", lambda *args: [])
    monkeypatch.setattr("dev_agent.supervisor.psutil.Process", lambda pid: process)
    create = AsyncMock(return_value=child)
    monkeypatch.setattr("dev_agent.supervisor.asyncio.create_subprocess_exec", create)
    monkeypatch.setattr("dev_agent.supervisor.asyncio.sleep", AsyncMock())
    supervisor = Supervisor(Config(_env_file=None), store, redactor)
    result = await supervisor.start()
    assert result["running"] and "partial" in result["health"]
    assert create.await_args.kwargs["env"]["DATABASE_PATH"] == str(
        tmp_path / "data" / "agent.db"
    )
    assert store.get("process")["created"] == 10
    await asyncio.gather(*supervisor.pumps)


@pytest.mark.asyncio
async def test_supervisor_crash_is_not_reported_as_healthy(
    store, redactor, tmp_path, monkeypatch
):
    monkeypatch.setattr("dev_agent.supervisor.ROOT", tmp_path)
    (tmp_path / "data" / "dev_agent").mkdir(parents=True)
    child = SimpleNamespace(
        pid=123,
        returncode=1,
        stdout=SimpleNamespace(read=AsyncMock(return_value=b"")),
        stderr=SimpleNamespace(read=AsyncMock(return_value=b"")),
    )
    monkeypatch.setattr("dev_agent.supervisor.psutil.process_iter", lambda *args: [])
    monkeypatch.setattr(
        "dev_agent.supervisor.psutil.Process",
        lambda pid: SimpleNamespace(pid=pid, create_time=lambda: 10),
    )
    monkeypatch.setattr(
        "dev_agent.supervisor.asyncio.create_subprocess_exec",
        AsyncMock(return_value=child),
    )
    monkeypatch.setattr("dev_agent.supervisor.asyncio.sleep", AsyncMock())
    supervisor = Supervisor(Config(_env_file=None), store, redactor)
    with pytest.raises(RuntimeError, match="exited during startup"):
        await supervisor.start()
    assert store.get("health") is None
    await asyncio.gather(*supervisor.pumps)


def test_separate_codex_home_only_expires_owned_sessions(tmp_path, monkeypatch):
    import os

    from dev_agent.session_manager import prepare_codex_home

    source = tmp_path / "original"
    source.mkdir()
    (source / "auth.json").write_text('{"test":true}')
    (source / "sessions").mkdir()
    original = source / "sessions" / "keep.jsonl"
    original.write_text("ordinary IDE conversation")
    monkeypatch.setenv("CODEX_HOME", str(source))
    directory = prepare_codex_home(tmp_path)
    assert (directory / "auth.json").read_text() == '{"test":true}'
    assert not (directory / "sessions").exists()
    (directory / "sessions").mkdir()
    old = directory / "sessions" / "old.jsonl"
    old.write_text("agent conversation")
    os.utime(old, (1, 1))
    prepare_codex_home(tmp_path, 7)
    assert not old.exists()
    assert original.exists() and (directory / "auth.json").exists()


def test_agent_preflight_recreates_event_loop_before_polling(tmp_path, monkeypatch):
    from dev_agent import main as entry

    monkeypatch.setattr(entry, "ROOT", tmp_path)
    config = Config(
        _env_file=None, bbot_token="offline-placeholder", dev_agent_admin_ids="42"
    )
    monkeypatch.setattr(entry, "Config", lambda: config)
    monkeypatch.setattr(entry, "prepare_codex_home", lambda *args: tmp_path)
    monkeypatch.setattr(entry, "run", AsyncMock(return_value=(0, "codex-cli 0.169.0")))
    checked = []

    def polling(**kwargs):
        loop = asyncio.get_event_loop()
        assert not loop.is_closed()
        checked.append(True)
        loop.close()
        asyncio.set_event_loop(None)

    app = SimpleNamespace(run_polling=polling)
    monkeypatch.setattr(
        entry, "Controller", Mock(return_value=SimpleNamespace(build=lambda: app))
    )
    monkeypatch.setattr(sys, "argv", ["dev_agent.main"])
    entry.main()
    assert checked == [True]


@pytest.mark.asyncio
async def test_callback_confirmation_runs_once(controller, store):
    token = store.confirm(42, {"action": "restart", "fingerprint": "fingerprint"})
    controller.spawn = Mock()
    query = SimpleNamespace(
        data="yes:" + token, answer=AsyncMock(), edit_message_reply_markup=AsyncMock()
    )
    first = update("", 200)
    first.callback_query = query
    await controller.handle(first, None)
    assert controller.spawn.call_count == 1
    # Close the captured coroutine because this unit test deliberately does not run it.
    controller.spawn.call_args.args[0].close()
    await controller.handle(first, None)
    second = update("", 201)
    second.callback_query = query
    await controller.handle(second, None)
    assert controller.spawn.call_count == 1
