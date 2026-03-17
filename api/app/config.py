from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://sri:secret@db:5432/sri_db"
    admin_api_key: str
    fernet_key: str

    # URL interna que usa el worker para pushear XMLs a la API
    internal_api_url: str = "http://api:8000"

    # Huso horario Ecuador
    tz: str = "America/Guayaquil"


settings = Settings()
