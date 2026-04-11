"""
Browser factory: lanza Chrome real con Patchright en modo stealth.

Patchright parchea a nivel binario el CDP leak (Runtime.enable) y los
flags de automatización de Chromium, eliminando las dos causas raíz
de detección por parte del WAF del SRI.
"""

from __future__ import annotations

import asyncio
import random
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import structlog
from patchright.async_api import (
    BrowserContext,
    Page,
    async_playwright,
)

from .config import SRIConfig

log = structlog.get_logger(__name__)

# ── Argumentos Chrome para reducir la huella de automatización ────────────────
STEALTH_ARGS: list[str] = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    # Apariencia de Chrome de usuario normal
    "--disable-infobars",
    "--start-maximized",
    # Reducir ruido de red que delata herramientas headless
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    # Estabilidad en Windows
    "--disable-gpu-sandbox",
    "--no-first-run",
    "--no-default-browser-check",
]

# ── User-Agent de Chrome 131 en Windows 11 ────────────────────────────────────
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def _clear_profile_lock(profile_dir: Path) -> None:
    """
    Elimina lock files del perfil Chrome si quedaron de un crash anterior.
    Chrome usa distintos nombres según la versión/plataforma:
      - 'SingletonLock' (versiones antiguas / Linux)
      - 'lockfile'      (Chrome 130+ en Windows)
    """
    for name in ("SingletonLock", "lockfile"):
        lock = profile_dir / name
        if lock.exists():
            try:
                lock.unlink()
                log.warning("profile_lock_removed", path=str(lock))
            except OSError as e:
                log.warning("profile_lock_remove_failed", path=str(lock), error=str(e))


@asynccontextmanager
async def browser_context(config: SRIConfig) -> AsyncIterator[BrowserContext]:
    """
    Context manager que lanza Chrome con Patchright y retorna el contexto.

    Usa persistent context para que las cookies y el perfil de Chrome
    persistan entre ejecuciones (sesión reutilizable).

    Ejemplo de uso:
        async with browser_context(config) as ctx:
            page = await ctx.new_page()
            await page.goto("https://srienlinea.sri.gob.ec/")
    """
    config.ensure_dirs()
    profile_dir = config.chrome_profile_dir
    profile_dir.mkdir(parents=True, exist_ok=True)
    _clear_profile_lock(profile_dir)

    log.info(
        "browser_launching",
        headless=config.headless,
        channel=config.browser_channel,
        executable_path=str(config.browser_executable_path) if config.browser_executable_path else None,
        profile=str(profile_dir),
    )

    async with async_playwright() as pw:
        launch_kwargs = {
            "user_data_dir": str(profile_dir),
            "headless": config.headless,
            "args": STEALTH_ARGS,
            "locale": config.locale,
            "timezone_id": config.timezone,
            "viewport": {
                "width": config.viewport_width,
                "height": config.viewport_height,
            },
            "accept_downloads": True,
            "downloads_path": str(config.downloads_dir),
            # Geolocalización de Ecuador para no levantar sospecha
            "geolocation": {"latitude": -0.1807, "longitude": -78.4678},  # Quito
            "permissions": ["geolocation"],
            # No pasar extra_http_headers globales — el SRI los valida
        }

        if config.browser_user_agent:
            launch_kwargs["user_agent"] = config.browser_user_agent

        if config.browser_channel:
            # "chrome" → Chrome real instalado en el host.
            launch_kwargs["channel"] = config.browser_channel

        if config.browser_executable_path:
            launch_kwargs["executable_path"] = str(config.browser_executable_path)

        context = await pw.chromium.launch_persistent_context(**launch_kwargs)

        log.info("browser_launched")

        try:
            yield context
        finally:
            # Guardar estado de sesión antes de cerrar
            try:
                state_file = config.state_file
                await context.storage_state(path=str(state_file))
                log.info("session_saved", path=str(state_file))
            except Exception as e:
                log.warning("session_save_failed", error=str(e))

            await context.close()
            log.info("browser_closed")


async def human_delay(min_ms: int = 400, max_ms: int = 1200) -> None:
    """
    Pausa aleatoria que imita el tiempo de reacción humano.

    Usar entre acciones significativas (navegaciones, clicks de menú,
    relleno de formularios) para evitar patrones de timing robótico.
    """
    wait = random.randint(min_ms, max_ms) / 1000.0
    await asyncio.sleep(wait)


async def type_humanlike(element, text: str) -> None:
    """
    Escribe texto caracter por caracter con delay variable (80-160ms/tecla).

    Más natural que fill() para campos de credenciales donde
    los WAFs miden la velocidad de escritura.
    """
    await element.click()
    await human_delay(200, 500)
    await element.type(text, delay=random.randint(80, 160))


async def human_page_dwell(page: Page, *, rounds: int = 2) -> None:
    """
    Simula exploración visual de la página con movimientos suaves y scroll corto.

    Esto ayuda a que la sesión headed se parezca más a una navegación humana
    antes de acciones sensibles como login o consultas con reCAPTCHA.
    """
    viewport = page.viewport_size or {"width": 1366, "height": 768}
    width = max(320, viewport["width"])
    height = max(240, viewport["height"])

    for _ in range(rounds):
        x = random.randint(80, max(81, width - 80))
        y = random.randint(80, max(81, height - 80))
        await page.mouse.move(x, y, steps=random.randint(12, 28))
        await human_delay(250, 700)

        scroll_y = random.randint(80, 260)
        await page.mouse.wheel(0, scroll_y)
        await human_delay(400, 900)
        await page.mouse.wheel(0, -scroll_y)
        await human_delay(300, 800)
