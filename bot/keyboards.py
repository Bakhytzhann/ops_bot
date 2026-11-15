from telegram import KeyboardButton, ReplyKeyboardMarkup

SCHEDULE_BUTTON = "SKED"
HOME_SECTIONS = {
    "PREFLIGHT": "preparation",
    "ANALYTICS": "reports",
}
HOME_PLACEHOLDERS = {
    "GROUND STATUS",
    "OPS BRIEF",
    "CHARTER CHECK",
}
QUICK_ACTIONS = (
    ("GROUND STATUS", "SKED", "OPS BRIEF"),
    ("PREFLIGHT", "ANALYTICS", "CHARTER CHECK"),
)


def home_keyboard():
    return ReplyKeyboardMarkup(
        [[KeyboardButton(label) for label in row] for row in QUICK_ACTIONS],
        resize_keyboard=True,
        is_persistent=True,
        one_time_keyboard=False,
    )
