from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from config.paths import project_directory

TELEGRAM_USER_ID_LIMIT = 2**52


def parse_user_ids(value: str) -> set[int]:
    users = {int(part.strip()) for part in value.split(",") if part.strip()}
    if any(not 0 < user < TELEGRAM_USER_ID_LIMIT for user in users):
        raise ValueError("Telegram user IDs must be positive integers below 2^52")
    return users


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file_encoding="utf-8-sig",
        extra="ignore",
        case_sensitive=False,
    )

    # Application
    app_name: str = "OPS Bot"
    app_env: str = "development"
    debug: bool = False

    # Ollama / Qwen3
    ollama_host: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen3:4b"
    ollama_temperature: float = Field(default=0.3, ge=0, le=2)
    ollama_num_ctx: int = Field(default=4096, ge=512)
    ollama_keep_alive: str = "30m"
    ollama_max_parallel: int = Field(default=1, ge=1, le=4)
    telegram_max_parallel: int = Field(default=4, ge=1, le=16)

    # Telegram
    telegram_bot_token: SecretStr | None = None
    telegram_mini_app_url: str = ""
    telegram_allowed_user_ids: str = ""
    telegram_admin_ids: str = ""
    charter_sales_user_ids: str = ""
    telegram_bot_username: str = "OPS_bot"

    # Leon Software API
    leon_api_key: SecretStr | None = None

    # Leon MCP
    leon_operator: str = ""
    leon_mcp_url: str = ""

    # ForeFlight
    foreflight_api_key: SecretStr | None = None
    foreflight_vendor_id: SecretStr | None = None
    foreflight_api_url: str = ""

    # Storage
    database_path: Path = Path("data/agent.db")
    log_dir: Path = Path("logs")

    # Network
    api_timeout: float = Field(default=30.0, gt=0)
    snapshot_timeout: float = Field(default=45.0, gt=0)
    snapshot_schedule_ttl: float = Field(default=30, ge=0)
    snapshot_crew_ttl: float = Field(default=60, ge=0)
    snapshot_weather_ttl: float = Field(default=120, ge=0)
    snapshot_briefing_ttl: float = Field(default=300, ge=0)
    snapshot_ftl_ttl: float = Field(default=60, ge=0)
    weather_stale_hours: float = Field(default=2, gt=0)

    def __init__(self, **values):
        if "_env_file" not in values:
            root = project_directory()
            values["_env_file"] = (root / ".env.local", root / ".env")
        super().__init__(**values)

    @field_validator("database_path", "log_dir")
    @classmethod
    def resolve_path(cls, value):
        return (project_directory() / value).resolve()

    @field_validator("telegram_mini_app_url")
    @classmethod
    def validate_mini_app_url(cls, value):
        if not value:
            return value
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.fragment
            or any(character.isspace() for character in value)
        ):
            raise ValueError("The Telegram Mini App URL must be a valid HTTPS URL")
        return value

    @property
    def admin_ids(self) -> set[int]:
        return parse_user_ids(self.telegram_admin_ids)

    @property
    def allowed_user_ids(self) -> set[int]:
        return parse_user_ids(self.telegram_allowed_user_ids)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def __getattr__(name):
    if name == "settings":
        return get_settings()
    raise AttributeError(name)
