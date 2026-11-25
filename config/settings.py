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

    debug: bool = False

    ollama_host: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen3:4b"
    ollama_temperature: float = Field(default=0.3, ge=0, le=2)
    ollama_num_ctx: int = Field(default=4096, ge=512)
    ollama_keep_alive: str = "30m"
    ollama_max_parallel: int = Field(default=1, ge=1, le=4)
    telegram_max_parallel: int = Field(default=4, ge=1, le=16)

    telegram_bot_token: SecretStr | None = None
    telegram_mini_app_url: str = ""
    ngrok_token: SecretStr | None = None
    ngrok_endpoint: str = ""
    telegram_allowed_user_ids: str = ""
    telegram_admin_ids: str = ""
    charter_sales_user_ids: str = ""
    charter_crew_operations_path: Path | None = None
    telegram_bot_username: str = "OPS_bot"

    leon_api_key: SecretStr | None = None

    leon_operator: str = ""
    leon_mcp_url: str = ""

    foreflight_api_key: SecretStr | None = None
    foreflight_vendor_id: SecretStr | None = None
    foreflight_api_url: str = ""
    foreflight_charter_autoroute: bool = False

    database_path: Path = Path("data/agent.db")
    log_dir: Path = Path("logs")

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

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        if (env_settings.env_vars.get("debug") or "").casefold() == "release":
            env_settings.env_vars.pop("debug")
        return init_settings, env_settings, dotenv_settings, file_secret_settings

    @field_validator("database_path", "log_dir")
    @classmethod
    def resolve_path(cls, value):
        return (project_directory() / value).resolve()

    @field_validator("charter_crew_operations_path", mode="before")
    @classmethod
    def resolve_crew_operations_path(cls, value):
        return (project_directory() / value).resolve() if value else None

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

    @field_validator("ngrok_endpoint")
    @classmethod
    def validate_ngrok_endpoint(cls, value):
        value = value.strip()
        if not value:
            return ""
        flag, sep, endpoint = value.partition("=")
        if flag == "--url" and sep:
            value = endpoint.strip().strip("\"'")
        elif value.split(maxsplit=1)[0] == "--url":
            value = value.removeprefix("--url").strip().strip("\"'")
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.port is not None
            or url.path not in {"", "/"}
            or "?" in value
            or "#" in value
            or any(character.isspace() for character in value)
        ):
            raise ValueError("NGROK_ENDPOINT must be an HTTPS origin.")
        return f"https://{url.hostname}"

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
