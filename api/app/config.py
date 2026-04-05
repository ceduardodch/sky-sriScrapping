from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_ROOT_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_ROOT_ENV_FILE, extra="ignore", env_ignore_empty=True)

    database_url: str = "postgresql+asyncpg://sri:secret@127.0.0.1:5432/sri_db"
    data_database_urls: Dict[str, str] = Field(default_factory=dict)
    admin_api_key: str
    fernet_key: str

    # Base maestra de clientes (misma instancia PG, base distinta)
    base_maestra_url: Optional[str] = None
    maestra_api_key: Optional[str] = None

    # URL interna que usa el worker para pushear XMLs a la API
    internal_api_url: str = "http://127.0.0.1:8000"

    # Config compartida con el scraper nativo
    headless: bool = True
    browser_channel: str = "chrome"
    browser_executable_path: Optional[Path] = None
    worker_runtime_root: Path = Path("runtime")

    # Huso horario Ecuador
    tz: str = "America/Guayaquil"

    def resolved_data_database_urls(self) -> Dict[str, str]:
        """Mapa de data stores disponibles; ``default`` cae a DATABASE_URL."""
        urls = dict(self.data_database_urls)
        urls.setdefault("default", self.database_url)
        return urls

    def get_data_database_url(self, storage_key: str) -> str:
        urls = self.resolved_data_database_urls()
        try:
            return urls[storage_key]
        except KeyError as exc:
            available = ", ".join(sorted(urls))
            raise ValueError(
                f"storage_key '{storage_key}' no está configurado. Disponibles: {available}"
            ) from exc

    def available_storage_keys(self) -> List[str]:
        return sorted(self.resolved_data_database_urls())


settings = Settings()
