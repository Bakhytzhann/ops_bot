import sqlite3

import pytest

from bot.access import AccessStore, access_store
from bot.admission import is_allowed
from config.settings import settings
from tests.test_admission import update


def test_desktop_grant_updates_running_admission_and_preserves_name():
    desktop = AccessStore(configuration=settings.model_copy())
    request = access_store.request(99, "Existing Telegram name")
    assert not is_allowed(update(user=99))

    assert desktop.grant_from_desktop(99)
    assert is_allowed(update(user=99))
    assert (99, "Existing Telegram name", False) in desktop.authorized_users()
    approved = access_store.get(99)
    assert approved["revision"] == request["revision"] + 1
    assert not desktop.grant_from_desktop(99)
    assert access_store.get(99) == approved


def test_desktop_revoke_keeps_other_data_and_survives_legacy_seed(monkeypatch):
    monkeypatch.setattr(settings, "telegram_admin_ids", "7")
    desktop = AccessStore(configuration=settings.model_copy())
    with desktop.connect() as connection:
        connection.execute("CREATE TABLE work_history (user_id INTEGER)")
        connection.execute("INSERT INTO work_history VALUES (42)")

    assert desktop.authorized_users() == [(7, "", True), (42, "", False)]
    assert desktop.revoke_from_desktop(42)
    assert not is_allowed(update(user=42))
    restarted = AccessStore(configuration=settings.model_copy())
    assert restarted.authorized_users() == [(7, "", True)]
    with sqlite3.connect(settings.database_path) as connection:
        assert connection.execute("SELECT user_id FROM work_history").fetchone() == (
            42,
        )
    with pytest.raises(ValueError, match="TELEGRAM_ADMIN_IDS"):
        desktop.revoke_from_desktop(7)
    assert is_allowed(update(user=7))


@pytest.mark.parametrize("user_id", [0, -1, 2**52])
def test_desktop_grant_rejects_invalid_telegram_id(user_id):
    with pytest.raises(ValueError, match="valid Telegram user ID"):
        access_store.grant_from_desktop(user_id)


def test_out_of_range_legacy_id_fails_before_sqlite_insert(monkeypatch):
    monkeypatch.setattr(
        settings, "telegram_allowed_user_ids", "42, 18446744073709551616"
    )
    with pytest.raises(ValueError, match="Telegram user IDs"):
        access_store.authorized_users()
