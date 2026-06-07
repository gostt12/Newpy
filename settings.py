"""
config/settings.py
──────────────────
Centralised, type-safe configuration loaded from environment variables.
All secrets live here; no hard-coded credentials anywhere else.
"""

from functools import lru_cache
from typing import List

from pydantic import AnyHttpUrl, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Application ──────────────────────────────────────────────────────────
    app_env: str = "development"
    secret_key: SecretStr = SecretStr("change-me")
    debug: bool = False

    # ── Database ─────────────────────────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://user:password@localhost:5432/botmanager_db"
    database_pool_size: int = 10
    database_max_overflow: int = 20

    # ── Redis / Celery ────────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"

    # ── Telegram ─────────────────────────────────────────────────────────────
    telegram_bot_token: SecretStr = SecretStr("7855033465:AAEZAW5--M-FG2zHijshkizzIENbuQcAFiA")
    telegram_webhook_secret: SecretStr = SecretStr("")
    telegram_webhook_url: str = ""

    # ── Chapa ─────────────────────────────────────────────────────────────────
    chapa_secret_key: SecretStr = SecretStr("CHAPUBK_TEST-Njw0NDXhG4BzQZ6GroFkhDJsDJK3gwNr")
    chapa_public_key: str = ""
    chapa_webhook_secret: SecretStr = SecretStr("")
    chapa_base_url: str = "https://api.chapa.co/v1"

    # ── Stripe ────────────────────────────────────────────────────────────────
    stripe_secret_key: SecretStr = SecretStr("")
    stripe_publishable_key: str = ""
    stripe_webhook_secret: SecretStr = SecretStr("")

    # ── PayPal ────────────────────────────────────────────────────────────────
    paypal_client_id: SecretStr = SecretStr("")
    paypal_client_secret: SecretStr = SecretStr("")
    paypal_webhook_id: str = ""
    paypal_base_url: str = "https://api-m.sandbox.paypal.com"

    # ── Security / JWT ────────────────────────────────────────────────────────
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 60
    allowed_origins: str = "https://yourminiapp.com"

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def parse_cors(cls, v: str) -> str:
        return v  # kept as str; split at usage time

    def get_allowed_origins(self) -> List[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached singleton Settings instance."""
    return Settings()
