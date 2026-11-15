"""Fleet document navigation; approved files are not yet configured."""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

DOCUMENT_CATEGORIES = {
    "aircraft": "AIRCRAFT",
    "operator": "OPERATOR",
    "checklists": "PRINTABLE CHECKLISTS",
}


def fleet_docs_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(title, callback_data=f"docs:{key}")]
            for key, title in DOCUMENT_CATEGORIES.items()
        ]
    )


def fleet_docs_response(category):
    if category not in DOCUMENT_CATEGORIES:
        raise ValueError("Unknown fleet document category")
    return (
        f"{DOCUMENT_CATEGORIES[category]}: утверждённые документы пока не загружены. "
        "Используйте только действующие документы из официального источника."
    )
