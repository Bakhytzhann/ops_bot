"""Idempotent edits for Telegram callback messages."""

from telegram.error import BadRequest


async def edit_message_text(query, text, **kwargs):
    # CallbackQuery.message may be stale after earlier edits in the same handler.
    try:
        return await query.edit_message_text(text, **kwargs)
    except BadRequest as exc:
        if not str(exc).lower().startswith("message is not modified"):
            raise
