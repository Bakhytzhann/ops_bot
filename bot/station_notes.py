import asyncio
import re
from datetime import UTC, datetime
from secrets import token_hex
from time import monotonic

from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup

from services.snapshot_models import zulu
from services.station_notes import MODERATORS, notes


def keyboard(rows):
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(label, callback_data=data) for label, data in row]
            for row in rows
        ]
    )


async def show_station(update, context, station, page=0):
    user = update.effective_user.id
    rows = await asyncio.to_thread(notes.list, user, station, 1, page)
    role = await asyncio.to_thread(notes.role, user)
    text = [
        f"Станция {station} · заметки сотрудников",
        "Crew reports — наблюдения. Ops verified — отдельное подтверждение.",
        "Хэндлер и контакты: источник пока не подключён.",
    ]
    buttons = []
    for row in rows:
        status = (
            "Ops verified" if row["verified_by"] else f"{row['author_role']} report"
        )
        text.append(
            f"\n#{row['id']} · {status} · v{row['version']}\nНаблюдение: {zulu(row['observed_at_utc'])}\n{row['text']}"
        )
        prefix = f"{row['id']}:{row['version']}"
        actions = []
        if row["author_id"] == user:
            actions.append((f"#{row['id']} Править", f"sn:edit:{prefix}"))
        if row["author_id"] == user or role in MODERATORS:
            actions.append((f"#{row['id']} Архив", f"sn:archive:{prefix}"))
        if role in MODERATORS:
            buttons.append(
                [
                    (f"#{row['id']} Подтвердить", f"sn:verify:{prefix}"),
                    (f"#{row['id']} Закрепить / снять", f"sn:pin:{prefix}"),
                ]
            )
        if actions:
            buttons.append(actions)
    if not rows:
        text.append("Заметок на этой странице нет.")
    buttons.append([("+ Заметка", f"sn:add:{station}:0")])
    buttons.append([("AI: сводка заметок", f"sn:summary:{station}:0")])
    if page:
        buttons.append([("← Назад", f"sn:list:{station}:{page - 1}")])
    if len(rows) == 1:
        buttons.append([("Далее →", f"sn:list:{station}:{page + 1}")])
    buttons.append([("↻ Обновить", f"sn:list:{station}:{page}")])
    content = "\n".join(text)
    if update.callback_query and len(content) <= 3800:
        if (
            update.effective_message.text != content
            or update.effective_message.reply_markup != keyboard(buttons)
        ):
            await update.callback_query.edit_message_text(
                content, reply_markup=keyboard(buttons)
            )
        return
    for start in range(0, len(content), 3800):
        await update.effective_message.reply_text(
            content[start : start + 3800],
            reply_markup=keyboard(buttons) if start + 3800 >= len(content) else None,
        )


async def private_message(update, context):
    if update.effective_chat.type != "private":
        return
    message, user = update.effective_message, update.effective_user
    if not user or user.is_bot:
        return
    text = (message.text or "").strip()
    role = await asyncio.to_thread(notes.role, user.id)
    if not role:
        if text.startswith(("/station", "/notes", "/start s_")):
            await message.reply_text(
                "Нужна подтверждённая привязка Telegram ID к сотруднику. Обратитесь к администратору."
            )
        return
    if text == "/cancel":
        context.user_data.pop("station_note_draft", None)
        await message.reply_text("Ввод отменён.")
        return
    match = re.fullmatch(r"(?:/(?:station|notes)\s+|/start\s+s_)([A-Za-z]{4})", text)
    if match:
        context.user_data.pop("station_note_draft", None)
        await show_station(update, context, match[1].upper())
        return
    draft = context.user_data.get("station_note_draft")
    if not draft or draft["chat"] != update.effective_chat.id:
        return
    if draft["expires"] < monotonic():
        context.user_data.pop("station_note_draft", None)
        await message.reply_text("Время ввода истекло. Откройте станцию заново.")
        return
    reply = getattr(message, "reply_to_message", None)
    if not reply or reply.message_id != draft["prompt_id"]:
        return
    observed = draft.get("observed", datetime.now(UTC).isoformat())
    if text.startswith("OBS "):
        stamp, _, text = text.partition("\n")
        try:
            value = datetime.fromisoformat(stamp[4:])
            if value.tzinfo is None or value > datetime.now(UTC):
                raise ValueError("Invalid observation date")
            observed = value.astimezone(UTC).isoformat()
        except ValueError:
            await message.reply_text(
                "Укажите OBS 2026-11-12T08:00:00Z и текст со следующей строки."
            )
            return
    if not 1 <= len(text) <= 2000:
        await message.reply_text("Допустимо 1–2000 символов.")
        return
    draft.update(text=text, observed=observed)
    await message.reply_text(
        f"Предпросмотр · {draft['station']}\nНаблюдение: {zulu(observed)}\n{text}",
        reply_markup=keyboard(
            [
                [
                    ("Сохранить", f"sn:save:{draft['token']}:0"),
                    ("Отмена", f"sn:cancel:{draft['token']}:0"),
                ]
            ]
        ),
    )


async def private_callback(update, context):
    query = update.callback_query
    if update.effective_chat.type != "private" or update.effective_user.is_bot:
        await query.answer(
            "Заметки доступны в личном чате сотрудника.", show_alert=True
        )
        return
    user = update.effective_user.id
    if not await asyncio.to_thread(notes.role, user):
        await query.answer(
            "Нужна подтверждённая учётная запись сотрудника.", show_alert=True
        )
        return
    await query.answer()
    parts = (query.data or "").split(":")
    if len(parts) != 4:
        return
    _, action, value, version = parts
    try:
        if action == "summary" and re.fullmatch(r"[A-Z]{4}", value):
            from services.ai_analysis import analyze_sources

            rows = await asyncio.to_thread(notes.list, user, value, 20)
            sources = {
                f"NOTE-{row['id']}": {
                    "text": row["text"],
                    "observed_at": row["observed_at_utc"],
                    "status": "Ops verified"
                    if row["verified_by"]
                    else row["author_role"] + " report",
                    "version": row["version"],
                }
                for row in rows
            }
            answer = (
                await analyze_sources(sources, (update.effective_chat.id, user))
                if sources
                else "Заметок нет."
            )
            await update.effective_message.reply_text(
                "Сводка максимум 20 доступных записей.\n" + answer
            )
            return
        if action == "list" and re.fullmatch(r"[A-Z]{4}", value) and version.isdigit():
            await show_station(update, context, value, int(version))
        elif action in {"add", "edit"}:
            draft = {
                "token": token_hex(8),
                "chat": update.effective_chat.id,
                "expires": monotonic() + 600,
            }
            if action == "add" and re.fullmatch(r"[A-Z]{4}", value):
                draft["station"] = value
            elif action == "edit" and value.isdigit() and version.isdigit():
                with notes.connect() as conn:
                    row = conn.execute(
                        "SELECT * FROM station_notes WHERE id=? AND author_id=? AND status='active'",
                        (int(value), user),
                    ).fetchone()
                if not row or row["version"] != int(version):
                    raise ValueError("Запись недоступна или изменена.")
                draft.update(
                    station=row["station_icao"],
                    note_id=int(value),
                    version=int(version),
                )
            else:
                return
            prompt = await update.effective_message.reply_text(
                "Ответьте текстом заметки (до 2000 символов). По умолчанию время наблюдения — время сообщения. Для другого времени первая строка: OBS YYYY-MM-DDTHH:MM:SSZ. /cancel — отмена.",
                reply_markup=ForceReply(),
            )
            draft["prompt_id"] = prompt.message_id
            context.user_data["station_note_draft"] = draft
        elif action in {"save", "cancel"}:
            draft = context.user_data.get("station_note_draft")
            if (
                not draft
                or draft["token"] != value
                or draft["chat"] != update.effective_chat.id
                or draft["expires"] < monotonic()
            ):
                raise ValueError("Черновик устарел или уже сохранён.")
            if action == "save":
                if "text" not in draft:
                    raise ValueError("Нет текста для сохранения.")
                if "note_id" in draft:
                    await asyncio.to_thread(
                        notes.change,
                        user,
                        draft["note_id"],
                        draft["version"],
                        "edit",
                        draft["text"],
                    )
                else:
                    await asyncio.to_thread(
                        notes.create,
                        user,
                        draft["station"],
                        draft["text"],
                        draft["token"],
                        observed_at=draft["observed"],
                    )
            context.user_data.pop("station_note_draft", None)
            await show_station(update, context, draft["station"])
        elif (
            action in {"archive", "verify", "pin"}
            and value.isdigit()
            and version.isdigit()
        ):
            station = await asyncio.to_thread(
                notes.change, user, int(value), int(version), action
            )
            await show_station(update, context, station)
    except (ValueError, PermissionError):
        await update.effective_message.reply_text(
            "Действие недоступно, данные изменились или черновик истёк. Откройте /station ICAO заново."
        )
