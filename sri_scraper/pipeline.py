from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .browser import browser_context
from .config import SRIConfig
from .exceptions import SessionExpiredError
from .login import login
from .navigator import go_to_comprobantes_recibidos
from .parser import ClaveDeAcceso, extract_claves_from_file
from .downloader import download_report


@dataclass
class ScrapeRecibidosResult:
    txt_path: Path | None
    claves: list[ClaveDeAcceso]


async def scrape_recibidos(config: SRIConfig) -> ScrapeRecibidosResult:
    """
    Ejecuta el flujo compartido browser -> TXT -> parse de claves.

    Esta función es la ruta común para `run.py scrape` y el worker, con el fin
    de evitar divergencias entre la validación manual y la ejecución automática.
    """
    async with browser_context(config) as ctx:
        try:
            page = await login(ctx, config)
        except SessionExpiredError:
            await ctx.clear_cookies()
            page = await login(ctx, config)

        await go_to_comprobantes_recibidos(
            page,
            config.page_timeout_ms,
            prefer_menu_first=not config.headless,
        )
        txt_path = await download_report(page, config)

    if txt_path is None:
        return ScrapeRecibidosResult(txt_path=None, claves=[])

    return ScrapeRecibidosResult(
        txt_path=txt_path,
        claves=extract_claves_from_file(txt_path),
    )
