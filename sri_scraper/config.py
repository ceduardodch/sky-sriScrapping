"""Configuración centralizada cargada desde .env con validación de tipos."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class SRIConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        env_ignore_empty=True,  # Empty env vars treated as unset (use defaults)
    )

    # ── Credenciales ──────────────────────────────────────────────────────────
    sri_ruc: str
    sri_password: SecretStr

    # ── Browser ───────────────────────────────────────────────────────────────
    headless: bool = False
    browser_channel: str = "chrome"
    locale: str = "es-EC"
    timezone: str = "America/Guayaquil"
    viewport_width: int = 1366
    viewport_height: int = 768

    # ── Timeouts ──────────────────────────────────────────────────────────────
    page_timeout_ms: int = 60_000
    download_timeout_ms: int = 120_000
    retry_attempts: int = 3

    # ── Fecha del reporte ─────────────────────────────────────────────────────
    report_date: Optional[date] = None  # None = ayer

    # ── API push (opcional) ───────────────────────────────────────────────────
    api_url: Optional[str] = None        # ej: "http://localhost:8000"
    api_key: Optional[str] = None        # ADMIN_API_KEY de la API
    api_tenant_id: Optional[int] = None  # ID del tenant en la API

    # ── Paths (resueltos relativos al CWD) ────────────────────────────────────
    downloads_dir: Path = Path("downloads")
    state_dir: Path = Path("state")
    logs_dir: Path = Path("logs")

    @field_validator("sri_ruc")
    @classmethod
    def ruc_must_be_valid(cls, v: str) -> str:
        v = v.strip()
        if not v.isdigit() or len(v) not in (10, 13):
            raise ValueError(
                f"SRI_RUC debe ser cédula (10 dígitos) o RUC (13 dígitos), recibido: '{v}'"
            )
        return v

    @property
    def state_file(self) -> Path:
        return self.state_dir / "auth_state.json"

    @property
    def chrome_profile_dir(self) -> Path:
        return self.state_dir / "chrome_profile"

    @property
    def effective_report_date(self) -> date:
        """Retorna la fecha configurada o ayer en hora Ecuador si no se especificó."""
        if self.report_date:
            return self.report_date
        import pytz
        from datetime import datetime
        ec_tz = pytz.timezone("America/Guayaquil")
        today_ec = datetime.now(ec_tz).date()
        return today_ec - timedelta(days=1)

    def ensure_dirs(self) -> None:
        """Crea los directorios de runtime si no existen."""
        for d in (self.downloads_dir, self.state_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)
