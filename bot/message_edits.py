from telegram.error import BadRequest


async def edit_message_text(query, text, **kwargs):
    try:
        return await query.edit_message_text(text, **kwargs)
    except BadRequest as exc:
        if not str(exc).lower().startswith("message is not modified"):
            raise
