"""
Descarga del reporte TXT de comprobantes recibidos desde el portal SRI.

El portal de comprobantes (JSF legacy, no Angular) usa:
  - Tres <select> para período: año | mes (en español) | día
  - Un  <select> para tipo de comprobante ("Factura", "Nota de Crédito", etc.)
  - Botón   "Consultar" para cargar la tabla de resultados
  - Enlace  "Descargar reporte" para descargar el TXT

Si el portal NO tiene opción "Todos", se itera por cada tipo individualmente
y los resultados se combinan en un único TXT final.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Optional

import structlog
from patchright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from .browser import human_delay
from .config import SRIConfig
from .exceptions import DownloadError
from .login import assert_authenticated

log = structlog.get_logger(__name__)

# Nombres de mes en español tal como aparecen en el portal SRI
MESES_ES: dict[int, str] = {
    1: "Enero",    2: "Febrero",   3: "Marzo",     4: "Abril",
    5: "Mayo",     6: "Junio",     7: "Julio",     8: "Agosto",
    9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre",
}

# Labels que representan "todos los tipos" en el portal
_TODOS_LABELS = frozenset({"Todos", "Todas", "TODOS", "TODAS", "Todos los tipos"})


# ── API pública ────────────────────────────────────────────────────────────────

async def download_report(page: Page, config: SRIConfig) -> Optional[Path]:
    """
    Configura los filtros de fecha y descarga el reporte TXT de comprobantes.

    Si el portal tiene opción "Todos" en tipo, hace una sola descarga.
    Si no, itera por cada tipo disponible y combina los resultados.

    Returns:
        Path del TXT combinado, o ``None`` si no hay comprobantes para ese día.

    Raises:
        DownloadError: Si algún paso crítico falla.
    """
    await assert_authenticated(page)

    target_date = config.effective_report_date
    log.info("download_started", date=str(target_date))

    # ── Paso 1: Configurar período ─────────────────────────────────────────────
    await _set_periodo_selects(page, target_date)
    await human_delay(300, 600)

    # ── Paso 2: Leer opciones de tipo de comprobante ───────────────────────────
    tipo_options = await _get_tipo_options(page)
    log.info("tipo_options_found", options=[lbl for _, lbl in tipo_options])

    # ¿Hay opción "Todos"?
    todos_entry = next(
        ((val, lbl) for val, lbl in tipo_options if lbl in _TODOS_LABELS),
        None,
    )

    if todos_entry:
        # ── Ruta rápida: una sola descarga ────────────────────────────────────
        return await _download_for_tipo(
            page, config, target_date,
            tipo_value=todos_entry[0], tipo_label=todos_entry[1],
            dest=config.downloads_dir / f"sri_recibidos_{target_date.strftime('%Y%m%d')}.txt",
        )

    # ── Ruta lenta: una descarga por tipo, luego combinar ─────────────────────
    if not tipo_options:
        log.warning("no_tipo_options_found", note="usando selección por defecto")
        tipo_options = [("", "default")]

    dest = config.downloads_dir / f"sri_recibidos_{target_date.strftime('%Y%m%d')}.txt"
    header_written = False
    total_rows = 0

    for tipo_val, tipo_label in tipo_options:
        slug = tipo_label.lower().replace(" ", "_").replace("/", "-").replace("\\", "-")
        temp_dest = config.downloads_dir / f"sri_recibidos_{target_date.strftime('%Y%m%d')}_{slug}.txt"

        # Re-establecer período (JSF puede resets parciales entre consultas)
        await _set_periodo_selects(page, target_date)
        await human_delay(200, 400)

        # Seleccionar este tipo
        await _select_tipo(page, tipo_val, tipo_label)
        await human_delay(300, 500)

        downloaded = await _download_for_tipo(
            page, config, target_date,
            tipo_value=tipo_val, tipo_label=tipo_label,
            dest=temp_dest,
        )

        if downloaded is None:
            continue

        # Combinar en el archivo final
        lines = downloaded.read_text(encoding="utf-8").splitlines()
        downloaded.unlink()

        if not lines:
            continue

        if not header_written:
            dest.write_text(lines[0] + "\n", encoding="utf-8")
            header_written = True

        data_lines = [l for l in lines[1:] if l.strip()]
        if data_lines:
            with dest.open("a", encoding="utf-8") as f:
                f.write("\n".join(data_lines) + "\n")
            total_rows += len(data_lines)
            log.info("tipo_merged", tipo=tipo_label, rows=len(data_lines))

    if not header_written:
        log.info("no_comprobantes", date=str(target_date))
        return None

    log.info("download_complete_all_tipos", path=str(dest), total_rows=total_rows)
    return dest


# ── Helpers internos ──────────────────────────────────────────────────────────

async def _get_tipo_options(page: Page) -> list[tuple[str, str]]:
    """
    Lee las opciones del select de tipo de comprobante (el último select).

    Returns:
        Lista de (value, label_text) para cada opción, excluyendo placeholders vacíos.
    """
    selects = page.locator("select")
    count = await selects.count()
    if count == 0:
        return []

    tipo_select = selects.nth(count - 1)
    try:
        raw: list[list[str]] = await tipo_select.evaluate(
            "el => Array.from(el.options).map(o => [o.value, o.text.trim()])"
        )
    except Exception as e:
        log.warning("tipo_options_read_failed", error=str(e))
        return []

    # Excluir opciones placeholder (value="" o label vacío o "-Seleccione-")
    result = []
    for value, label in raw:
        if not label or label.startswith("-") or label.startswith("Seleccione"):
            continue
        result.append((value, label))

    return result


async def _select_tipo(page: Page, tipo_value: str, tipo_label: str) -> None:
    """Selecciona el tipo de comprobante en el último select de la página."""
    selects = page.locator("select")
    count = await selects.count()
    if count == 0:
        return

    tipo_select = selects.nth(count - 1)
    try:
        if tipo_value:
            await tipo_select.select_option(value=tipo_value)
        else:
            await tipo_select.select_option(label=tipo_label)
        log.debug("tipo_selected", value=tipo_value, label=tipo_label)
    except Exception as e:
        log.warning("tipo_select_failed", value=tipo_value, label=tipo_label, error=str(e))


async def _download_for_tipo(
    page: Page,
    config: SRIConfig,
    target_date: date,
    tipo_value: str,
    tipo_label: str,
    dest: Path,
) -> Optional[Path]:
    """
    Para un tipo de comprobante dado: selecciona, consulta, y descarga el TXT.

    Returns:
        Path del TXT descargado, o None si no hay comprobantes.
    """
    # Seleccionar tipo (si no es el predeterminado vacío)
    if tipo_label != "default":
        await _select_tipo(page, tipo_value, tipo_label)
        await human_delay(300, 500)

    # Consultar
    await _click_consultar(page, config)
    log.info("consultar_clicked", tipo=tipo_label)

    # Esperar AJAX (SRI tarda 5-15 s)
    await human_delay(6000, 10000)

    # Screenshot diagnóstico (solo para el primer tipo o tipo único)
    debug_shot = str(config.logs_dir / "download_debug_post_consultar.png")
    await page.screenshot(path=debug_shot, full_page=True)
    log.debug("post_consultar_screenshot", path=debug_shot, tipo=tipo_label)

    if await _is_empty_result(page):
        log.info("no_comprobantes_for_tipo", tipo=tipo_label, date=str(target_date))
        return None

    return await _do_download(page, config, dest)


async def _set_periodo_selects(page: Page, target_date: date) -> None:
    """
    Establece los tres <select> de período: año, mes y día.

    Estrategia de identificación (robusta ante cambios de ID en JSF):
      - Año  → el select cuyo innerText contiene el año como número
      - Mes  → el select que tiene el nombre del mes en español
      - Día  → el select que tiene opciones numéricas hasta 31

    Fallback: si la detección falla, se asume orden posicional 0-1-2.
    """
    year_str  = str(target_date.year)
    month_str = MESES_ES[target_date.month]
    day_str   = str(target_date.day)

    selects = page.locator("select")
    count = await selects.count()
    log.debug("selects_found_on_page", count=count)

    year_idx = month_idx = day_idx = None

    for i in range(count):
        try:
            html = await selects.nth(i).inner_html(timeout=2_000)
        except Exception:
            continue

        if year_idx is None and (
            f">{year_str}<" in html
            or f'"{year_str}"' in html
            or f"'{year_str}'" in html
        ):
            year_idx = i
        elif month_idx is None and month_str in html:
            month_idx = i
        elif day_idx is None and (
            ">31<" in html or ">30<" in html
        ) and month_str not in html and year_str not in html:
            day_idx = i

    if year_idx is None:
        log.warning("year_select_not_detected_by_content", fallback=0)
        year_idx = 0
    if month_idx is None:
        log.warning("month_select_not_detected_by_content", fallback=1)
        month_idx = 1
    if day_idx is None:
        log.warning("day_select_not_detected_by_content", fallback=2)
        day_idx = 2

    try:
        await selects.nth(year_idx).select_option(year_str)
        log.debug("year_selected", idx=year_idx, value=year_str)
    except Exception as e:
        log.warning("year_select_failed", error=str(e))

    try:
        await selects.nth(month_idx).select_option(label=month_str)
        log.debug("month_selected", idx=month_idx, value=month_str)
    except Exception as e:
        log.warning("month_select_failed", error=str(e))

    try:
        await selects.nth(day_idx).select_option(day_str)
        log.debug("day_selected", idx=day_idx, value=day_str)
    except Exception as e:
        log.warning("day_select_failed", error=str(e))


async def _humanize_before_consultar(page: Page) -> None:
    """
    Simula comportamiento humano antes de hacer click en Consultar.
    Esto mejora el score de reCAPTCHA v3 que evalúa el comportamiento del usuario.
    """
    import random
    # Scroll suave por la página
    await page.evaluate("window.scrollTo(0, 200)")
    await human_delay(400, 800)
    await page.evaluate("window.scrollTo(0, 0)")
    await human_delay(300, 600)

    # Mover el mouse sobre el formulario antes de hacer click
    try:
        # Hover sobre el campo RUC
        ruc_field = page.locator("input[type='text']").first
        await ruc_field.hover()
        await human_delay(200, 400)
    except Exception:
        pass

    # Espera adicional para que reCAPTCHA v3 score la sesión
    await human_delay(2000, 3500)


async def _click_consultar(page: Page, config: SRIConfig) -> None:
    """
    Hace click en el botón "Consultar" del portal de comprobantes.
    Incluye comportamiento humano previo para mejorar score de reCAPTCHA v3.
    Detecta "Captcha incorrecta" y reintenta una vez.

    Raises:
        DownloadError: Si el botón no se encuentra o el captcha sigue fallando.
    """
    await _humanize_before_consultar(page)

    selectors = [
        "button:has-text('Consultar')",
        "input[value='Consultar']",
        "a:has-text('Consultar')",
        "input[type='submit']",
    ]

    consultar_btn = None
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=3_000):
                consultar_btn = el
                log.debug("consultar_btn_found", selector=sel)
                break
        except PlaywrightTimeoutError:
            continue

    if consultar_btn is None:
        screenshot_path = str(config.logs_dir / "download_debug_consultar.png")
        await page.screenshot(path=screenshot_path)
        raise DownloadError(
            f"No se encontró el botón 'Consultar'. Screenshot: {screenshot_path}"
        )

    await consultar_btn.click()
    log.debug("consultar_btn_clicked")

    # Esperar respuesta AJAX
    await human_delay(3000, 5000)

    # Detectar "Captcha incorrecta" y reintentar una vez
    captcha_error_selectors = [
        "text=Captcha incorrecta",
        "text=Captcha error",
        "text=captcha",
        "[class*='captcha'][class*='error']",
        "[class*='alert']:has-text('captcha')",
    ]
    captcha_failed = False
    for sel in captcha_error_selectors:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=2_000):
                captcha_failed = True
                log.warning("captcha_failed_retrying", selector=sel)
                break
        except PlaywrightTimeoutError:
            continue

    if captcha_failed:
        # Esperar más tiempo para que reCAPTCHA v3 mejore el score
        await human_delay(5000, 8000)
        # Scroll adicional para simular más actividad
        await page.evaluate("window.scrollTo(0, 300)")
        await human_delay(500, 1000)
        await page.evaluate("window.scrollTo(0, 0)")
        await human_delay(1000, 2000)
        # Reintentar click en Consultar
        await consultar_btn.click()
        log.info("consultar_retry_after_captcha")
        await human_delay(3000, 5000)


async def _is_empty_result(page: Page) -> bool:
    """
    Detecta si la consulta no retornó comprobantes (día sin actividad = normal).

    Retorna True si el portal muestra un indicador de "sin resultados" o CAPTCHA fallido.
    """
    empty_texts = [
        "No se encontraron resultados",
        "No existen comprobantes",
        "No hay comprobantes",
        "sin registros",
        "0 registros",
        "No existen resultados",
        "no se encontraron",
        "No existe informaci",   # "No existe información para..."
        "no existe inform",
        "Sin resultados",
    ]

    for text in empty_texts:
        try:
            el = page.locator(f"text={text}").first
            if await el.is_visible(timeout=2_000):
                log.debug("empty_result_detected", text=text)
                return True
        except PlaywrightTimeoutError:
            continue

    # Si el CAPTCHA sigue fallando después del retry, tratar como vacío
    captcha_error_selectors = [
        "text=Captcha incorrecta",
        "text=Captcha error",
    ]
    for sel in captcha_error_selectors:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=1_000):
                log.warning("captcha_still_failing_treating_as_empty")
                return True
        except PlaywrightTimeoutError:
            continue

    return False


async def _do_download(
    page: Page,
    config: SRIConfig,
    dest: Path,
) -> Optional[Path]:
    """
    Hace click en "Descargar reporte" y guarda el TXT en ``dest``.

    Raises:
        DownloadError: Si el botón no se encuentra o la descarga falla.
    """
    download_selectors = [
        "a:has-text('Descargar reporte')",
        "a:has-text('Descargar')",
        "button:has-text('Descargar reporte')",
        "button:has-text('Descargar')",
        "[title*='Descargar']",
        "a[href*='descargar']",
        "a[href*='export']",
        "a[href*='download']",
        "a:has(img[src*='download'])",
        "a:has(img[src*='descargar'])",
    ]

    download_btn = None
    for sel in download_selectors:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=3_000):
                download_btn = el
                log.debug("download_btn_found", selector=sel)
                break
        except PlaywrightTimeoutError:
            continue

    if download_btn is None:
        screenshot_path = str(config.logs_dir / "download_debug_btn.png")
        await page.screenshot(path=screenshot_path)
        raise DownloadError(
            f"No se encontró el botón 'Descargar reporte'. "
            f"Screenshot: {screenshot_path}"
        )

    # ── Estrategia 1: evento nativo de descarga (Content-Disposition: attachment) ──
    try:
        async with page.expect_download(timeout=30_000) as dl_info:
            await download_btn.click()

        download = await dl_info.value
        log.info("download_received", filename=download.suggested_filename)
        await download.save_as(str(dest))

        if dest.exists() and dest.stat().st_size > 0:
            log.info("download_saved", path=str(dest), size_bytes=dest.stat().st_size)
            return dest
        raise DownloadError(f"Archivo vacío: {dest}")

    except PlaywrightTimeoutError:
        log.warning("download_event_timeout_30s", note="probando estrategia de navegación")

    # ── Estrategia 2: el link navega a una URL con el TXT directamente ──────────
    try:
        pre_url = page.url
        async with page.expect_navigation(timeout=30_000):
            await download_btn.click()

        post_url = page.url
        log.debug("post_download_url", from_url=pre_url, to_url=post_url)

        content = await page.content()
        if content and "<html" not in content[:200].lower():
            dest.write_text(content, encoding="utf-8")
            if dest.stat().st_size > 0:
                log.info("download_from_navigation", path=str(dest))
                return dest

    except PlaywrightTimeoutError:
        log.warning("navigation_strategy_timeout")
    except Exception as e:
        log.warning("navigation_strategy_failed", error=str(e))

    # ── Diagnóstico final ──────────────────────────────────────────────────────
    screenshot_path = str(config.logs_dir / "download_timeout.png")
    await page.screenshot(path=screenshot_path, full_page=True)
    raise DownloadError(
        f"No se pudo capturar la descarga del TXT tras dos estrategias. "
        f"Screenshot: {screenshot_path}"
    )
