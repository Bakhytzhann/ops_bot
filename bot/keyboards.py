from telegram import KeyboardButton, ReplyKeyboardMarkup

SCHEDULE_BUTTON = "SKED"
HOME_SECTIONS = {
    "PREFLIGHT": "preparation",
    "ANALYTICS": "reports",
}
QUICK_ACTIONS = (
    ("SKED", "OPS BRIEF", "ANALYTICS"),
    ("FLT SEARCH", "PREFLIGHT", "SLOT"),
)


def home_keyboard():
    return ReplyKeyboardMarkup(
        [[KeyboardButton(label) for label in row] for row in QUICK_ACTIONS],
        resize_keyboard=True,
        is_persistent=True,
        one_time_keyboard=False,
    )
