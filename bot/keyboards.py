from telegram import KeyboardButton, ReplyKeyboardMarkup

SCHEDULE_BUTTON = "SKED"
HOME_PLACEHOLDERS = {"OPS BRIEF"}
QUICK_ACTIONS = (
    ("GROUND STATUS", "SKED", "OPS BRIEF"),
    ("FLEET DOCS", "ANALYTICS", "CHARTER CHECK"),
)


def home_keyboard():
    return ReplyKeyboardMarkup(
        [[KeyboardButton(label) for label in row] for row in QUICK_ACTIONS],
        resize_keyboard=True,
        is_persistent=True,
        one_time_keyboard=False,
    )
