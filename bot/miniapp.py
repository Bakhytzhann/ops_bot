from loguru import logger
from telegram import MenuButtonWebApp, WebAppInfo
from telegram.error import TelegramError

TUNNEL_MENU = "ops_tunnel_menu"


def ops_menu(url):
    return MenuButtonWebApp(text="OPS", web_app=WebAppInfo(url=url))


async def configure_mini_app(bot, url):
    if not url:
        return
    try:
        await bot.set_chat_menu_button(menu_button=ops_menu(url))
    except TelegramError as exc:
        logger.warning("Telegram Mini App menu setup failed: {}", type(exc).__name__)


async def publish_tunnel_menu(application, url):
    bot = application.bot
    try:
        previous = await bot.get_chat_menu_button()
        application.bot_data[TUNNEL_MENU] = (url, previous)
        await bot.set_chat_menu_button(menu_button=ops_menu(url))
    except TelegramError as exc:
        logger.error(
            "Telegram could not update the OPS menu button: {}", type(exc).__name__
        )
        raise


async def restore_tunnel_menu(application):
    state = application.bot_data.get(TUNNEL_MENU)
    if state is None:
        return
    url, previous = state
    try:
        async with application.bot as bot:
            active = await bot.get_chat_menu_button()
            if (
                isinstance(active, MenuButtonWebApp)
                and active.text == "OPS"
                and active.web_app.url == url
            ):
                await bot.set_chat_menu_button(menu_button=previous)
    except TelegramError as exc:
        logger.warning("Telegram OPS menu cleanup failed: {}", type(exc).__name__)
