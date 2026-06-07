from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    ocean_io_api_key: str = ""
    prospeo_api_key: str = ""
    eazyreach_api_key: str = ""
    eazyreach_base_url: str = "https://api.eazyreach.app"
    brevo_api_key: str = ""
    sender_email: str = ""
    sender_name: str = "Outreach Engine"

    max_companies: int = Field(default=20, ge=1, le=10000)
    max_contacts_per_company: int = Field(default=3, ge=1, le=25)
    retry_max_attempts: int = Field(default=5, ge=1, le=10)
    log_level: str = "INFO"

    database_path: Path = BASE_DIR / "data" / "outreach.db"
    output_dir: Path = BASE_DIR / "outputs"
    log_dir: Path = BASE_DIR / "logs"

    @property
    def has_eazyreach(self) -> bool:
        return bool(self.eazyreach_api_key.strip())

    @property
    def has_brevo(self) -> bool:
        return bool(self.brevo_api_key.strip())

    def ensure_dirs(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
