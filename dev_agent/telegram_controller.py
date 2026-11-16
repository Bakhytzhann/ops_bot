import asyncio
import json
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from .security import authorized
from .session_manager import SessionManager

logger = logging.getLogger(__name__)
COMMANDS = (
    "start",
    "help",
    "status",
    "task",
    "tasks",
    "cancel",
    "diff",
    "tests",
    "git",
    "pull",
    "push",
    "start_ops",
    "stop_ops",
    "restart",
    "deploy",
    "logs",
    "rollback",
)


class Controller:
    def __init__(self, config, storage, tasks, git, tests, supervisor, deploy, redact):
        self.config, self.storage, self.tasks, self.git = config, storage, tasks, git
        self.tests, self.supervisor, self.deploy, self.redact = (
            tests,
            supervisor,
            deploy,
            redact,
        )
        self.sessions = SessionManager(storage)
        self.pending = {}
        self.text_waiters = {}
        self.new_task = set()
        self.background = set()
        self.application = None

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.background.add(task)
        task.add_done_callback(self.background.discard)

    @staticmethod
    def keyboard():
        rows = [
            [("Новая задача", "task"), ("Продолжить", "continue")],
            [("Статус", "status"), ("Git", "git"), ("Тесты", "tests")],
            [("Запустить OPS Bot", "start_ops"), ("Остановить OPS Bot", "stop_ops")],
            [("Перезапуск OPS Bot", "restart"), ("Логи", "logs")],
            [("Прогресс", "tasks"), ("Ответить Codex", "continue")],
            [("Остановить задачу", "cancel"), ("Изменения", "diff")],
            [("Подготовить деплой", "deploy")],
        ]
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(label, callback_data="cmd:" + cmd)
                    for label, cmd in row
                ]
                for row in rows
            ]
        )

    async def notify(self, owner, text, markup=None):
        text = self.redact(text)
        # UTF-16 length stays within Telegram's message limit even with emoji.
        chunks, current, units = [], "", 0
        for character in text or "(empty)":
            size = len(character.encode("utf-16-le")) // 2
            if units + size > 3500:
                chunks.append(current)
                current, units = "", 0
            current += character
            units += size
        chunks.append(current)
        for index, chunk in enumerate(chunks[:12]):
            await self.application.bot.send_message(
                owner,
                chunk,
                reply_markup=markup if index == len(chunks[:12]) - 1 else None,
            )

    async def confirm(self, owner, payload, description):
        token = self.storage.confirm(owner, payload)
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Подтвердить", callback_data="yes:" + token),
                    InlineKeyboardButton("Отказаться", callback_data="no:" + token),
                ]
            ]
        )
        await self.notify(
            owner, description + "\nПодтверждение действует 5 минут.", keyboard
        )
        return token

    async def ask(self, owner, method, params):
        if method == "item/tool/requestUserInput":
            answers = {}
            for question in params["questions"]:
                future = asyncio.get_running_loop().create_future()
                self.text_waiters[owner] = future
                options = "\n".join(
                    o["label"] + ": " + o.get("description", "")
                    for o in question.get("options") or []
                )
                await self.notify(
                    owner,
                    question["question"]
                    + "\n"
                    + options
                    + "\nОтветьте обычным сообщением.",
                )
                try:
                    answer = await asyncio.wait_for(future, 300)
                    answers[question["id"]] = {"answers": [answer]}
                finally:
                    self.text_waiters.pop(owner, None)
            return answers
        future = asyncio.get_running_loop().create_future()
        token = await self.confirm(
            owner,
            {"action": "codex"},
            "Codex запрашивает разрешение:\n" + json.dumps(params, ensure_ascii=False),
        )
        self.pending[token] = future
        try:
            return await asyncio.wait_for(future, 300)
        finally:
            self.pending.pop(token, None)

    async def handle(self, update, context):
        if not authorized(update, self.config.admins):
            return
        if self.storage.seen(update.update_id):
            if update.callback_query:
                await update.callback_query.answer()
            return
        owner = update.effective_user.id
        try:
            if update.callback_query:
                query = update.callback_query
                await query.answer()
                kind, value = query.data.split(":", 1)
                if kind in ("yes", "no"):
                    payload = self.storage.consume(owner, value)
                    await query.edit_message_reply_markup(reply_markup=None)
                    if payload["action"] == "codex":
                        future = self.pending.get(value)
                        if not future or future.done():
                            raise ValueError("Codex request no longer active")
                        future.set_result(kind == "yes")
                    elif kind == "yes":
                        self.spawn(self.operation(owner, payload["action"], payload))
                    else:
                        await self.notify(owner, "Операция отклонена.")
                    return
                command, argument = value, ""
            else:
                text = update.effective_message.text or ""
                if not text.startswith("/"):
                    if owner in self.text_waiters:
                        if self.redact(text) != text:
                            raise ValueError("Do not send secrets")
                        if not self.text_waiters[owner].done():
                            self.text_waiters[owner].set_result(text)
                        return
                    current = (
                        None if owner in self.new_task else self.sessions.current(owner)
                    )
                    if owner not in self.new_task and not current:
                        await self.notify(
                            owner, "Создайте задачу: /task описание", self.keyboard()
                        )
                        return
                    task_id = self.tasks.enqueue(owner, text, current)
                    self.new_task.discard(owner)
                    await self.notify(owner, f"Задача #{task_id} в очереди.")
                    return
                command, _, argument = text.partition(" ")
                command = command[1:].split("@")[0]
            if command in ("start", "help"):
                await self.notify(
                    owner,
                    "OPS Dev Agent\n" + "\n".join("/" + c for c in COMMANDS),
                    self.keyboard(),
                )
            elif command == "task":
                if argument:
                    task_id = self.tasks.enqueue(owner, argument)
                    await self.notify(owner, f"Задача #{task_id} в очереди.")
                else:
                    self.new_task.add(owner)
                    await self.notify(owner, "Отправьте описание новой задачи.")
            elif command == "continue":
                self.new_task.discard(owner)
                await self.notify(
                    owner, "Ответьте обычным сообщением для продолжения текущей сессии."
                )
            elif command == "cancel":
                await self.tasks.cancel(owner, int(argument) if argument else None)
                await self.notify(owner, "Задача отменена.")
            elif command == "tasks":
                await self.notify(
                    owner,
                    "\n".join(
                        f"#{t['id']}: {t['status']}"
                        for t in self.storage.tasks()
                        if t["owner"] == owner
                    )
                    or "Нет задач.",
                )
            elif command == "status":
                await self.notify(
                    owner,
                    f"Agent online; Codex task: {self.tasks.active}\n"
                    f"{await self.git.identity()}\nOPS: {self.supervisor.status()}",
                )
            elif command == "git":
                await self.notify(owner, await self.git.review())
            elif command == "diff":
                await self.notify(
                    owner, await self.git.git("diff", "HEAD", "--", ".", ":!.env*")
                )
            elif command == "logs":
                path = self.supervisor.log
                await self.notify(
                    owner,
                    path.read_text(encoding="utf-8")[-10000:]
                    if path.exists()
                    else "Логов пока нет.",
                )
            elif command in (
                "tests",
                "pull",
                "push",
                "restart",
                "deploy",
                "rollback",
                "start_ops",
                "stop_ops",
            ):
                self.spawn(self.operation(owner, command))
        except Exception as exc:  # noqa: BLE001 -- Telegram boundary
            await self.notify(owner, "Ошибка: " + self.redact(exc))

    async def operation(self, owner, action, payload=None):
        if self.tasks.lock.locked():
            await self.notify(
                owner, "Другая операция выполняется; повторите после завершения."
            )
            return
        try:
            async with self.tasks.lock:
                if action in ("start_ops", "stop_ops"):
                    if action == "start_ops":
                        await self.supervisor.start()
                        message = "OPS Bot запущен."
                    else:
                        await self.supervisor.stop()
                        message = "OPS Bot остановлен."
                    self.storage.put(
                        "last_operation",
                        {"action": action, "owner": owner, "result": message},
                    )
                    logger.info("Operation %s completed for %s", action, owner)
                    await self.notify(owner, message, self.keyboard())
                elif payload:
                    if payload["fingerprint"] != await self.git.fingerprint():
                        raise ValueError(
                            "Код или Git изменились; подготовьте операцию повторно."
                        )
                    if action == "push":
                        result = await self.git.push()
                    elif action == "pull":
                        result = await self.git.pull()
                    elif action == "restart":
                        result = await self.supervisor.restart()
                    elif action == "deploy":
                        result = await self.deploy.deploy(payload["prepared"])
                    elif action == "rollback":
                        result = await self.deploy.rollback()
                    else:
                        raise ValueError("Unknown operation")
                    self.storage.put(
                        "last_operation",
                        {
                            "action": action,
                            "owner": owner,
                            "result": self.redact(result),
                        },
                    )
                    logger.info("Operation %s completed for %s", action, owner)
                    await self.notify(owner, str(result) or "Выполнено.")
                elif action == "tests":
                    await self.notify(owner, "Проверки запущены.")
                    await self.notify(owner, str(await self.tests.execute()))
                else:
                    prepared = None
                    if action == "deploy":
                        await self.notify(
                            owner, "Подготовка деплоя: полный набор проверок."
                        )
                        prepared = await self.deploy.prepare()
                    if action == "push":
                        await self.git.publication_check()
                    payload = {
                        "action": action,
                        "fingerprint": await self.git.fingerprint(),
                        "prepared": prepared,
                    }
                    description = {
                        "push": "Создать коммит всех изменений и выполнить обычный push в origin текущей ветки.",
                        "pull": "Получить origin и обновить текущую чистую ветку только fast-forward.",
                        "restart": "Остановить управляемый OPS Bot и запустить его снова.",
                        "deploy": "Остановить OPS Bot и запустить проверенный коммит из отдельного каталога версии.",
                        "rollback": "Остановить OPS Bot и запустить предыдущую версию без изменения рабочих файлов.",
                    }[action]
                    await self.confirm(
                        owner, payload, description + "\n" + await self.git.review()
                    )
        except Exception as exc:  # noqa: BLE001 -- isolate operation failure
            logger.error("Operation %s failed: %s", action, self.redact(exc))
            await self.notify(owner, "Операция не выполнена: " + self.redact(exc))

    def build(self):
        application = (
            Application.builder()
            .token(self.config.bbot_token.get_secret_value())
            .build()
        )
        self.application = application
        for command in COMMANDS:
            application.add_handler(CommandHandler(command, self.handle))
        application.add_handler(CallbackQueryHandler(self.handle))
        application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle)
        )

        async def start(app):
            self.spawn(self.tasks.worker(self.notify, self.ask))

            async def broadcast(text):
                for owner in self.config.admins:
                    await self.notify(owner, text)

            self.spawn(self.supervisor.monitor(broadcast, self.tasks.lock))

        async def stop(app):
            for task in list(self.background):
                task.cancel()
            await asyncio.gather(*self.background, return_exceptions=True)
            # Stop owned children while their log-draining coroutines still exist.
            await self.supervisor.stop()

        application.post_init, application.post_stop = start, stop
        return application
