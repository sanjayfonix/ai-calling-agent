"""
Application configuration loaded from environment variables.
All secrets come from .env — never hardcode them.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    """Central application settings. Loaded once, cached."""

    # ── OpenAI ──────────────────────────────────────────────
    openai_api_key: str = Field(..., alias="OPENAI_API_KEY")

    # ── Twilio ──────────────────────────────────────────────
    twilio_account_sid: str = Field(..., alias="TWILIO_ACCOUNT_SID")
    twilio_auth_token: str = Field(..., alias="TWILIO_AUTH_TOKEN")
    twilio_api_key: str = Field("", alias="TWILIO_API_KEY")
    twilio_api_secret: str = Field("", alias="TWILIO_API_SECRET")
    twilio_twiml_app_sid: str = Field("", alias="TWILIO_TWIML_APP_SID")
    twilio_phone_number: str = Field(..., alias="TWILIO_PHONE_NUMBER")
    agent_phone_number: str = Field("", alias="AGENT_PHONE_NUMBER")

    # ── Database ────────────────────────────────────────────
    database_url: str = Field(
        "postgresql+asyncpg://postgres:postgres@localhost:5432/aicallingagent",
        alias="DATABASE_URL",
    )

    # ── Application ─────────────────────────────────────────
    app_host: str = Field("0.0.0.0", alias="APP_HOST")
    app_port: int = Field(8000, alias="APP_PORT")
    port: int | None = Field(None, alias="PORT")  # Render sets this automatically
    app_env: str = Field("production", alias="APP_ENV")
    app_debug: bool = Field(False, alias="APP_DEBUG")
    base_url: str = Field("https://yourdomain.com", alias="BASE_URL")
    
    # ── Backend API ─────────────────────────────────────────
    backend_webhook_url: str = Field(
        "https://xd363v4j-5000.inc1.devtunnels.ms/api/ai-call/call-complete",
        alias="BACKEND_WEBHOOK_URL"
    )

    # ── Security ────────────────────────────────────────────
    secret_key: str = Field("change-me", alias="SECRET_KEY")
    encryption_key: str = Field("change-me-32-bytes-key-here!!!!!", alias="ENCRYPTION_KEY")
    api_key: str = Field("", alias="API_KEY")
    allowed_origins: str = Field("*", alias="ALLOWED_ORIGINS")  # Comma-separated list

    # ── Logging ─────────────────────────────────────────────
    log_level: str = Field("INFO", alias="LOG_LEVEL")
    log_file: str = Field("logs/app.log", alias="LOG_FILE")

    # ── Call Settings ───────────────────────────────────────
    max_call_duration_seconds: int = Field(600, alias="MAX_CALL_DURATION_SECONDS")
    silence_timeout_seconds: int = Field(30, alias="SILENCE_TIMEOUT_SECONDS")
    max_retries_per_question: int = Field(3, alias="MAX_RETRIES_PER_QUESTION")

    # ── OpenAI Realtime ─────────────────────────────────────
    openai_realtime_model: str = "gpt-4o-mini-realtime-preview-2024-12-17"
    openai_realtime_voice: str = "shimmer"  # Professional female voice — calm, clear, mid-paced

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        populate_by_name = True

    @property
    def effective_port(self) -> int:
        """Render sets PORT env var. Use it if available, else APP_PORT."""
        return self.port or self.app_port

    @property
    def effective_database_url(self) -> str:
        """Convert postgres:// to postgresql+asyncpg:// for SQLAlchemy.
        Strips parameters not supported by asyncpg (sslmode, channel_binding).
        SSL is handled separately via connect_args in database.py.
        """
        import re
        url = self.database_url
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+asyncpg://", 1)
        elif url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        # Remove params not supported by asyncpg
        url = re.sub(r'[&?]channel_binding=[^&]*', '', url)
        url = re.sub(r'[&?]sslmode=[^&]*', '', url)
        # Clean up leftover ? if all params were stripped
        if url.endswith('?'):
            url = url[:-1]
        return url

    @property
    def requires_ssl(self) -> bool:
        """Check if the original DATABASE_URL requested SSL."""
        return 'sslmode=require' in self.database_url or 'neon.tech' in self.database_url


@lru_cache()
def get_settings() -> Settings:
    """Return cached settings singleton."""
    return Settings()
