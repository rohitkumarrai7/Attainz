import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BASE_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    ocean_io_api_key: str = ""
    prospeo_api_key: str = ""
    brevo_api_key: str = ""
    brevo_smtp_login: str = ""
    sender_email: str = ""
    sender_name: str = "Rohit | DivFixer"

    max_companies: int = Field(default=20, ge=1, le=10000)
    max_contacts_per_company: int = Field(default=3, ge=1, le=25)
    retry_max_attempts: int = Field(default=5, ge=1, le=10)
    log_level: str = "INFO"

    database_path: Path = Field(default_factory=lambda: _default_database_path())
    output_dir: Path = Field(default_factory=lambda: _default_output_dir())
    log_dir: Path = Field(default_factory=lambda: _default_log_dir())

    @property
    def has_brevo(self) -> bool:
        return bool(self.brevo_api_key.strip())

    def ensure_dirs(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)


def _is_render() -> bool:
    return bool(os.getenv("RENDER"))


def _resolve_data_dir() -> Path:
    legacy = PROJECT_ROOT / "data"
    if legacy.exists():
        return legacy
    return BASE_DIR / "data"


def _default_database_path() -> Path:
    return _resolve_data_dir() / "outreach.db"


def _default_output_dir() -> Path:
    legacy = PROJECT_ROOT / "outputs"
    if legacy.exists():
        return legacy
    return BASE_DIR / "outputs"


def _default_log_dir() -> Path:
    legacy = PROJECT_ROOT / "logs"
    if legacy.exists():
        return legacy
    return BASE_DIR / "logs"


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
