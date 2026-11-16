import sys
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")
    bbot_token: SecretStr = SecretStr("")
    dev_agent_admin_ids: str = ""
    dev_agent_codex: str = Field(default="codex", min_length=1)
    dev_agent_python: str = Field(default=sys.executable, min_length=1)
    dev_agent_timeout: int = Field(default=1800, ge=1, le=86400)
    dev_agent_retention_days: int = Field(default=7, ge=1, le=90)

    @property
    def admins(self):
        return {
            int(x.strip()) for x in self.dev_agent_admin_ids.split(",") if x.strip()
        }

    def validate_startup(self):
        if not self.bbot_token.get_secret_value() or not self.admins:
            raise ValueError("Set BBOT_TOKEN and DEV_AGENT_ADMIN_IDS in .env")
        if not (ROOT / ".git").exists() or not (ROOT / "main.py").is_file():
            raise ValueError("OPS repository not found")
