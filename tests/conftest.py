import pytest

from config.settings import settings


@pytest.fixture(autouse=True)
def local_access_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "telegram_allowed_user_ids", "42")
    monkeypatch.setattr(settings, "telegram_admin_ids", "")
    monkeypatch.setattr(settings, "database_path", tmp_path / "agent.db")
