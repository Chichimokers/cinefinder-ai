"""Configuración centralizada de CineFinder AI.

Lee los valores de .env.local (desarrollo/administración) o de las variables
de entorno del proceso (producción en Render). NUNCA imprime secretos.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import List

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT / ".env.local"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Administración local ---
    render_api_key: str = Field(default="", alias="RENDER_API_KEY")
    render_service_id: str = Field(default="", alias="RENDER_SERVICE_ID")

    # --- Base de datos ---
    supabase_database_url: str = Field(default="", alias="SUPABASE_DATABASE_URL")
    db_pool_min_size: int = Field(default=1, alias="DB_POOL_MIN_SIZE")
    db_pool_max_size: int = Field(default=10, alias="DB_POOL_MAX_SIZE")

    # --- NVIDIA ---
    nvidia_api_key: str = Field(default="", alias="NVIDIA_API_KEY")
    nvidia_model: str = Field(
        default="nvidia/nemotron-3.5-super-vl-120b-a12b", alias="NVIDIA_MODEL"
    )
    nvidia_base_url: str = Field(
        default="https://integrate.api.nvidia.com/v1", alias="NVIDIA_BASE_URL"
    )
    nvidia_temperature: float = Field(default=0.2, alias="NVIDIA_TEMPERATURE")
    nvidia_max_tokens: int = Field(default=2048, alias="NVIDIA_MAX_TOKENS")

    # --- Telegram ---
    telegram_api_id: int = Field(default=0, alias="TELEGRAM_API_ID")
    telegram_api_hash: str = Field(default="", alias="TELEGRAM_API_HASH")
    telegram_phone: str = Field(default="", alias="TELEGRAM_PHONE")
    telegram_owner_id: int = Field(default=0, alias="TELEGRAM_OWNER_ID")
    telegram_session: str = Field(default="", alias="TELEGRAM_SESSION")
    telegram_catalog_channel_id: int | None = Field(
        default=None, alias="TELEGRAM_CATALOG_CHANNEL_ID"
    )

    # --- Proveedores opcionales ---
    tmdb_api_key: str = Field(default="", alias="TMDB_API_KEY")
    tavily_api_key: str = Field(default="", alias="TAVILY_API_KEY")
    brave_search_api_key: str = Field(default="", alias="BRAVE_SEARCH_API_KEY")
    youtube_data_api_key: str = Field(default="", alias="YOUTUBE_DATA_API_KEY")

    # --- Secretos generados ---
    api_access_token: str = Field(default="", alias="API_ACCESS_TOKEN")
    session_secret: str = Field(default="", alias="SESSION_SECRET")
    job_encryption_key: str = Field(default="", alias="JOB_ENCRYPTION_KEY")

    # --- Agente ---
    agent_max_steps: int = Field(default=8, alias="AGENT_MAX_STEPS")
    agent_max_parallel_tools: int = Field(default=4, alias="AGENT_MAX_PARALLEL_TOOLS")
    agent_timeout_seconds: int = Field(default=120, alias="AGENT_TIMEOUT_SECONDS")
    agent_max_candidates: int = Field(default=10, alias="AGENT_MAX_CANDIDATES")

    # --- API ---
    api_host: str = Field(default="0.0.0.0", alias="API_HOST")
    port: int = Field(default=8000, alias="PORT")
    rate_limit_per_minute: int = Field(default=60, alias="RATE_LIMIT_PER_MINUTE")
    environment: str = Field(default="development", alias="ENVIRONMENT")

    @field_validator("supabase_database_url")
    @classmethod
    def _normalize_db_url(cls, v: str) -> str:
        if v.startswith("postgresql://"):
            return v.replace("postgresql://", "postgresql+asyncpg://", 1)
        return v

    @property
    def sync_database_url(self) -> str:
        return self.supabase_database_url.replace("postgresql+asyncpg://", "postgresql://")

    @property
    def configured_search_providers(self) -> List[str]:
        providers = []
        if self.tmdb_api_key:
            providers.append("tmdb")
        if self.tavily_api_key:
            providers.append("tavily")
        if self.brave_search_api_key:
            providers.append("brave")
        if self.youtube_data_api_key:
            providers.append("youtube")
        return providers

    def public_dict(self) -> dict:
        """Resumen seguro para logs/diagnóstico: sin secretos."""
        return {
            "environment": self.environment,
            "nvidia_model": self.nvidia_model,
            "nvidia_configured": bool(self.nvidia_api_key),
            "database_configured": bool(self.supabase_database_url),
            "telegram_configured": bool(
                self.telegram_api_id and self.telegram_api_hash and self.telegram_session
            ),
            "render_admin_configured": bool(self.render_api_key),
            "optional_providers": self.configured_search_providers,
            "agent": {
                "max_steps": self.agent_max_steps,
                "max_parallel_tools": self.agent_max_parallel_tools,
                "timeout_seconds": self.agent_timeout_seconds,
                "max_candidates": self.agent_max_candidates,
            },
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


settings = get_settings()
