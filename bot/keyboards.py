from telegram import KeyboardButton, ReplyKeyboardMarkup

SCHEDULE_BUTTON = "Расписание"
HOME_SECTIONS = {
    "Подготовка": "preparation",
    "Аналитика": "reports",
}
QUICK_ACTIONS = (
    ("Расписание", "Сводка"),
    ("Аналитика", "Поиск"),
    ("Подготовка", "СЛОТ"),
)


def home_keyboard():
    return ReplyKeyboardMarkup(
        [[KeyboardButton(label) for label in row] for row in QUICK_ACTIONS],
        resize_keyboard=True,
        is_persistent=True,
        one_time_keyboard=False,
    )
