from telegram import KeyboardButton, ReplyKeyboardMarkup

SCHEDULE_BUTTON = "SKED"


def home_keyboard():
    return ReplyKeyboardMarkup(
        [[KeyboardButton(SCHEDULE_BUTTON)]],
        resize_keyboard=True,
        is_persistent=True,
        one_time_keyboard=False,
    )
