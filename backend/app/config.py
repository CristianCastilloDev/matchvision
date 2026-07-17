from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-driven application settings with safe local defaults."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
    )

    app_name: str = "MatchVision AI"
    app_env: Literal["development", "test", "production"] = "development"
    debug: bool = False
    api_v1_prefix: str = "/api/v1"
    database_url: str = "sqlite:///./matchvision.db"
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000"]
    )
    seed_demo_data: bool = False
    log_level: str = "INFO"
    data_root: Path = Path("data")
    model_root: Path = Path("models")
    max_import_size_mb: int = Field(default=10, ge=1, le=100)
    max_page_size: int = 100
    rate_limit_per_minute: int = 120

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @property
    def auto_seed_demo(self) -> bool:
        return self.seed_demo_data

    @property
    def data_cache_dir(self) -> Path:
        return self.data_root / "cache"

    @property
    def import_dir(self) -> Path:
        return self.data_root / "imports"

    @property
    def model_dir(self) -> Path:
        return self.model_root

    @property
    def max_import_bytes(self) -> int:
        return self.max_import_size_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()
