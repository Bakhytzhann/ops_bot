from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(BASE_DIR / ".env.local", BASE_DIR / ".env"),
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
    database_path: Path = BASE_DIR / "data" / "agent.db"
    log_dir: Path = BASE_DIR / "logs"

    # Network
    api_timeout: float = Field(default=30.0, gt=0)
    snapshot_timeout: float = Field(default=45.0, gt=0)
    snapshot_schedule_ttl: float = Field(default=30, ge=0)
    snapshot_crew_ttl: float = Field(default=60, ge=0)
    snapshot_weather_ttl: float = Field(default=120, ge=0)
    snapshot_briefing_ttl: float = Field(default=300, ge=0)
    snapshot_ftl_ttl: float = Field(default=60, ge=0)
    weather_stale_hours: float = Field(default=2, gt=0)

    @property
    def admin_ids(self) -> set[int]:
        return {
            int(value.strip())
            for value in self.telegram_admin_ids.split(",")
            if value.strip()
        }

    @property
    def allowed_user_ids(self) -> set[int]:
        if not self.telegram_allowed_user_ids.strip():
            return set()

        return {
            int(user_id.strip())
            for user_id in self.telegram_allowed_user_ids.split(",")
            if user_id.strip()
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
