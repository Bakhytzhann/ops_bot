"""Telegram chat-menu integration for the Mini App."""

from loguru import logger
from telegram import MenuButtonWebApp, WebAppInfo
from telegram.error import TelegramError


async def configure_mini_app(bot, url):
    if not url:
        return
    try:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="OPS", web_app=WebAppInfo(url=url))
        )
    except TelegramError as exc:
        logger.warning("Telegram Mini App menu setup failed: {}", type(exc).__name__)
