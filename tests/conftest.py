import pytest

from config.settings import settings


@pytest.fixture(autouse=True)
def local_access_settings(monkeypatch):
    monkeypatch.setattr(settings, "telegram_allowed_user_ids", "42")
