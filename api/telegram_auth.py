"""Translate a signed Telegram launch into access to the operational API."""

import hmac
import json
import re
from time import time
from typing import Annotated
from urllib.parse import parse_qsl

from fastapi import Header, HTTPException

from bot.access import access_store
from config.settings import TELEGRAM_USER_ID_LIMIT, get_settings

SESSION_LIFETIME = 60 * 60


def verified_launch(init_data, bot_token):
    if len(init_data) > 16_384:
        raise ValueError("Invalid launch data")
    pairs = parse_qsl(
        init_data, keep_blank_values=True, strict_parsing=True, errors="strict"
    )
    fields = dict(pairs)
    signature = fields.pop("hash", "")
    if len(fields) + 1 != len(pairs) or not re.fullmatch(r"[0-9a-f]{64}", signature):
        raise ValueError("Invalid launch signature")
    if any(
        "\n" in key or "\r" in key or "\n" in value or "\r" in value
        for key, value in fields.items()
    ):
        raise ValueError("Invalid launch fields")
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.digest(b"WebAppData", bot_token.encode(), "sha256")
    expected = hmac.digest(secret, check.encode(), "sha256").hex()
    if not hmac.compare_digest(signature, expected):
        raise ValueError("Invalid launch signature")

    timestamp = int(fields.get("auth_date", ""))
    if not -30 <= int(time()) - timestamp <= SESSION_LIFETIME:
        raise ValueError("Expired launch")
    user = json.loads(fields.get("user", "null"))
    user_id = user.get("id") if isinstance(user, dict) else None
    if type(user_id) is not int or not 0 < user_id < TELEGRAM_USER_ID_LIMIT:
        raise ValueError("Invalid Telegram account")
    return user_id, fields.get("chat_type"), "chat" in fields


def require_telegram_access(authorization: Annotated[str | None, Header()] = None):
    scheme, _, init_data = (authorization or "").partition(" ")
    if scheme.lower() != "tma" or not init_data:
        raise HTTPException(401, "Open SKED from the bot menu in Telegram.")
    token = get_settings().telegram_bot_token
    if token is None or not token.get_secret_value():
        raise HTTPException(503, "Telegram access is unavailable.")
    try:
        user_id, chat_type, has_chat = verified_launch(
            init_data, token.get_secret_value()
        )
    except (ValueError, RecursionError):
        raise HTTPException(401, "Session expired or invalid. Reopen SKED.") from None
    if has_chat or chat_type not in (None, "sender", "private"):
        raise HTTPException(403, "Open SKED from the private bot chat.")
    if not access_store.is_allowed(user_id):
        raise HTTPException(403, "This Telegram account does not have OPS access.")
    return user_id
