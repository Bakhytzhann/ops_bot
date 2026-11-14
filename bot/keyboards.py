from telegram import KeyboardButton, ReplyKeyboardMarkup

SCHEDULE_BUTTON = "SCHEDULE"
HOME_SECTIONS = {
    "Flight Preparation": "preparation",
    "Crew & FTL": "crew",
    "Weather & NOTAM": "weather",
    "Operational Reports": "reports",
}


def home_keyboard():
    return ReplyKeyboardMarkup(
        [[KeyboardButton(SCHEDULE_BUTTON)]],
        resize_keyboard=True,
        is_persistent=True,
        one_time_keyboard=False,
    )
