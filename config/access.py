"""Shared Telegram request identity and private-chat authorization."""

from contextvars import ContextVar

from config.settings import settings

# The active Telegram user for this request.
current_user_id: ContextVar[int | None] = ContextVar(
    "leon_user_id",
    default=None,
)
current_chat_id: ContextVar[int | None] = ContextVar(
    "operational_chat_id", default=None
)


def require_access():
    user_id = current_user_id.get()

    chat_id = current_chat_id.get()
    if user_id is None or user_id not in settings.allowed_user_ids:
        raise PermissionError("Operational data access denied")
    if chat_id is not None and chat_id != user_id:
        raise PermissionError("Operational data is available only in private chats")
