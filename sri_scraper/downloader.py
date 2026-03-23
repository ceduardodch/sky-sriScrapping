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

import asyncio
import re
from datetime import date
from pathlib import Path
from typing import Optional

import httpx
import structlog
from patchright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from .browser import human_delay
from .config import SRIConfig
from .exceptions import DownloadError
from .login import assert_authenticated

log = structlog.get_logger(__name__)

# ── reCAPTCHA Enterprise (portal SRI comprobantes) ────────────────────────────
_RECAPTCHA_SITE_KEY = "6LdukTQsAAAAAIcciM4GZq4ibeyplUhmWvlScuQE"
_RECAPTCHA_ACTION   = "consulta_cel_recibidos"

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


async def _solve_recaptcha_2captcha(page_url: str, api_key: str) -> Optional[str]:
    """
    Resuelve reCAPTCHA Enterprise v3 usando la API de 2captcha.

    Documentación: https://2captcha.com/api-docs/recaptcha-v3
    Costo: ~$3 / 1000 solves ≈ $1/año con un scrape diario por tenant.

    Returns:
        Token válido (string largo) o None si falla.
    """
    log.info("2captcha_solving_started", url=page_url, action=_RECAPTCHA_ACTION)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            # 1) Crear tarea
            resp = await client.post(
                "https://api.2captcha.com/createTask",
                json={
                    "clientKey": api_key,
                    "task": {
                        "type": "RecaptchaV3TaskProxyless",
                        "websiteURL": page_url,
                        "websiteKey": _RECAPTCHA_SITE_KEY,
                        "pageAction": _RECAPTCHA_ACTION,
                        "isEnterprise": True,
                        "minScore": 0.5,
                    },
                },
            )
        data = resp.json()
        if data.get("errorId", 1) != 0:
            log.warning("2captcha_create_failed", error=data.get("errorDescription"), code=data.get("errorCode"))
            return None

        task_id = data["taskId"]
        log.info("2captcha_task_created", task_id=task_id)

        # 2) Polling para resultado (máx 2 minutos)
        for attempt in range(24):
            await asyncio.sleep(5)
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    "https://api.2captcha.com/getTaskResult",
                    json={"clientKey": api_key, "taskId": task_id},
                )
            result = resp.json()
            status = result.get("status")
            if status == "ready":
                token = result["solution"]["gRecaptchaResponse"]
                log.info("2captcha_solved", attempts=attempt + 1, token_prefix=token[:20])
                return token
            elif status == "processing":
                log.debug("2captcha_still_processing", attempt=attempt + 1)
                continue
            else:
                log.warning("2captcha_unexpected_status", status=status, data=result)
                return None

        log.warning("2captcha_timeout_2min")
        return None

    except Exception as e:
        log.warning("2captcha_exception", error=str(e))
        return None


async def _inject_and_submit_with_token(page: Page, token: str) -> None:
    """
    Inyecta el token de reCAPTCHA en el campo oculto y dispara
    onSubmit() directamente (bypasando el flujo normal de reCAPTCHA).
    """
    await page.evaluate(
        """
        (token) => {
            // Rellenar todos los campos g-recaptcha-response de la página
            document.querySelectorAll('[name="g-recaptcha-response"]').forEach(el => {
                el.value = token;
            });
            // Llamar onSubmit() que internamente llama rcBuscar() → PrimeFaces AJAX
            if (typeof onSubmit === 'function') {
                onSubmit();
            } else if (typeof rcBuscar === 'function') {
                rcBuscar();
            }
        }
        """,
        token,
    )
    log.info("2captcha_token_injected_and_submitted")


async def _humanize_before_consultar(page: Page, config: Optional["SRIConfig"] = None) -> None:
    """
    Simula comportamiento humano antes de hacer click en Consultar.
    Mejora el score de reCAPTCHA v3 Enterprise para el modo sin solver.
    """
    # Scroll suave por la página
    await page.evaluate("window.scrollTo(0, 200)")
    await human_delay(400, 800)
    await page.evaluate("window.scrollTo(0, 0)")
    await human_delay(300, 600)

    # Hover sobre el campo RUC para simular lectura
    try:
        ruc_field = page.locator("input[type='text']").first
        await ruc_field.hover()
        await human_delay(200, 400)
    except Exception:
        pass

    # Espera adicional para que reCAPTCHA Enterprise score la sesión
    await human_delay(2000, 3500)


async def _click_consultar(page: Page, config: SRIConfig) -> None:
    """
    Hace click en el botón "Consultar" del portal de comprobantes.

    Estrategia 1 (si TWOCAPTCHA_API_KEY configurado):
      - Resuelve reCAPTCHA Enterprise via 2captcha
      - Inyecta token en g-recaptcha-response
      - Dispara onSubmit() directamente

    Estrategia 2 (fallback sin solver):
      - Humanización + click normal
      - 3 reintentos con delays crecientes
      - Si sigue fallando, deja que _is_empty_result() lo detecte

    Raises:
        DownloadError: Si el botón no se encuentra.
    """
    await _humanize_before_consultar(page, config)

    # ── Estrategia 1: 2captcha solver ─────────────────────────────────────────
    if config.twocaptcha_api_key:
        token = await _solve_recaptcha_2captcha(page.url, config.twocaptcha_api_key)
        if token:
            await _inject_and_submit_with_token(page, token)
            log.info("consultar_via_2captcha")
            return
        else:
            log.warning("2captcha_failed_falling_back_to_click")

    # ── Estrategia 2: click normal con reintentos ──────────────────────────────
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

    captcha_error_selectors = [
        "text=Captcha incorrecta",
        "text=Captcha error",
        "[class*='alert']:has-text('captcha')",
    ]

    # Hasta 3 intentos con delays crecientes
    for attempt in range(3):
        await consultar_btn.click()
        log.debug("consultar_btn_clicked", attempt=attempt + 1)
        await human_delay(3000, 5000)

        captcha_failed = False
        for sel in captcha_error_selectors:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=2_000):
                    captcha_failed = True
                    log.warning("captcha_failed_retrying", selector=sel, attempt=attempt + 1)
                    break
            except PlaywrightTimeoutError:
                continue

        if not captcha_failed:
            log.info("consultar_clicked", attempt=attempt + 1)
            return  # Éxito

        if attempt < 2:
            wait_ms = (attempt + 1) * 10_000
            log.info("consultar_retry_after_captcha", wait_ms=wait_ms)
            await human_delay(wait_ms, wait_ms + 5000)
            await page.evaluate("window.scrollTo(0, 300)")
            await human_delay(800, 1500)
            await page.evaluate("window.scrollTo(0, 0)")
            await human_delay(500, 1000)

    log.warning("consultar_captcha_exhausted_retries")


async def _is_empty_result(page: Page) -> bool:
    """
    Detecta si la consulta no retornó comprobantes (día sin actividad = normal).

    Retorna True si:
    - El portal muestra texto de "sin resultados"
    - No hay tabla de resultados con filas de datos (RichFaces / tabla JSF)
    - El CAPTCHA sigue fallando
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
        "No existen datos para los par",  # "No existen datos para los parámetros ingresados"
        "no existen datos",
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

    # ── PrimeFaces formMessages: solo mensajes VISIBLES con texto exacto ─────────
    # Solo aplica si el mensaje de advertencia es visible en pantalla (no en JS)
    try:
        msg_el = page.locator("#formMessages\\:messages .ui-messages-warn")
        if await msg_el.is_visible(timeout=2_000):
            msg_text = await msg_el.inner_text()
            log.debug("form_message_warn", text=msg_text[:100])
            # Solo textos muy específicos del portal SRI (no genéricos)
            specific_empty = [
                "no existen datos para los par",
                "no existen comprobantes recibidos",
            ]
            if any(t in msg_text.lower() for t in specific_empty):
                return True
    except PlaywrightTimeoutError:
        pass
    except Exception:
        pass

    # ── RichFaces "no data" row (visible en tabla vacía) ─────────────────────
    rf_nodata_selectors = [
        ".rf-nodata",
        ".rf-nodata-i",
        "tr.rf-dt-nd-r",
    ]
    for sel in rf_nodata_selectors:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=2_000):
                log.debug("rf_nodata_detected", selector=sel)
                return True
        except PlaywrightTimeoutError:
            continue

    return False


_CLAVE_RE = re.compile(r"(?<!\d)(\d{49})(?!\d)")

# Regex para 49-digit access keys used in DOM scraping strategy


async def _extract_claves_from_dom(page: Page, dest: Path) -> Optional[Path]:
    """
    Extrae claves de acceso (49 dígitos) directamente del DOM de la página.

    Estrategia principal post-Consultar:
    El portal SRI renderiza la tabla de resultados en el DOM. Aunque el botón
    "Descargar reporte" usa JSF stateful POST (frágil), las claves de acceso
    ya están visibles en la página tras el AJAX de Consultar.

    Buscamos primero en el HTML completo, luego en el innerText de las tablas.

    Returns:
        Path del TXT con las claves encontradas, o None si no hay ninguna.
    """
    try:
        # Estrategia A: HTML completo (rápido, captura data-* attrs, value= ocultos, etc.)
        content = await page.content()
        claves = _CLAVE_RE.findall(content)

        if claves:
            unique_claves = list(dict.fromkeys(claves))  # Deduplicar preservando orden
            dest.write_text("\n".join(unique_claves) + "\n", encoding="utf-8")
            log.info(
                "dom_extraction_html_success",
                claves_found=len(unique_claves),
                path=str(dest),
            )
            return dest

        # Estrategia B: innerText de tablas (por si el JS oculta en attrs data)
        table_text: str = await page.evaluate(
            """
            () => {
                const tables = document.querySelectorAll('table');
                let text = '';
                tables.forEach(t => { text += t.innerText + '\\n'; });
                return text;
            }
            """
        )
        claves_b = _CLAVE_RE.findall(table_text)
        if claves_b:
            unique_b = list(dict.fromkeys(claves_b))
            dest.write_text("\n".join(unique_b) + "\n", encoding="utf-8")
            log.info(
                "dom_extraction_table_success",
                claves_found=len(unique_b),
                path=str(dest),
            )
            return dest

        log.info("dom_extraction_no_claves_in_page")
        return None

    except Exception as e:
        log.warning("dom_extraction_failed", error=str(e))
        return None


async def _do_download(
    page: Page,
    config: SRIConfig,
    dest: Path,
) -> Optional[Path]:
    """
    Extrae el reporte de comprobantes y lo guarda en ``dest``.

    Estrategia 0 (primera y más robusta): extracción directa del DOM.
      El portal JSF renderiza los resultados en la tabla justo después del
      AJAX de Consultar. Buscamos directamente en el HTML las claves de 49
      dígitos — sin depender del botón "Descargar reporte".

    Si Strategy 0 no encuentra claves, intenta las estrategias de botón:
      1. Evento nativo download de Playwright (Content-Disposition: attachment)
      2. Intercepción de respuesta HTTP (JSF/PrimeFaces iframe oculto)
      3. Espera extra 10 s (respuesta tardía)
      4. Captura del popup/nueva pestaña
      5. page.route() JSF intercept
      6. Fetch directo con httpx + cookies

    Raises:
        DownloadError: Si todas las estrategias fallan.
    """
    # ── Estrategia 0: DOM scraping (no requiere click) ───────────────────────────
    dom_result = await _extract_claves_from_dom(page, dest)
    if dom_result is not None:
        return dom_result

    log.info("dom_strategy_no_claves_trying_download_button")

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

    # Loguear atributos del botón para diagnóstico
    try:
        btn_href = await download_btn.get_attribute("href")
        btn_onclick = await download_btn.get_attribute("onclick")
        btn_id = await download_btn.get_attribute("id")
        log.debug("download_btn_attrs", href=btn_href, onclick=btn_onclick, id=btn_id)
    except Exception:
        btn_href = None

    # URLs de monitoreo/beacon a ignorar (Dynatrace, analytics)
    _BEACON_PATTERNS = ("rb_bf", "dynatrace", "/beacon", "analytics", "tracking")

    # ── Estrategia 1: evento nativo de descarga (Content-Disposition: attachment) ──
    # Simultáneamente interceptamos toda respuesta real del portal SRI
    captured_responses: list[bytes] = []
    all_sri_responses: list[dict] = []  # Debug: todas las respuestas SRI
    popup_pages: list = []

    async def _on_response(response):
        """Captura respuestas reales descargables del portal SRI."""
        try:
            url = response.url
            ct = response.headers.get("content-type", "")
            cd = response.headers.get("content-disposition", "")
            status = response.status

            # Solo nos interesan respuestas de SRI
            if "srienlinea.sri.gob.ec" not in url:
                return

            # Guardar para debug (todas las respuestas SRI)
            all_sri_responses.append({
                "url": url[-100:],
                "status": status,
                "ct": ct[:60],
                "cd": cd[:60],
            })

            # Saltar beacons conocidos de monitoreo
            if any(p in url for p in _BEACON_PATTERNS):
                return

            # Criterios para capturar como descarga real:
            is_attachment = "attachment" in cd
            # text/plain de más de 100 bytes que NO sea beacon
            is_large_text = "text/plain" in ct

            if is_attachment or is_large_text or "octet-stream" in ct:
                body = await response.body()
                # Validar que sea contenido de texto (no JSON de beacon)
                if body and len(body) > 100:
                    # Verificar que NO sea JSON (beacon) ni binary corto
                    try:
                        preview = body[:50].decode("utf-8", errors="replace")
                    except Exception:
                        preview = ""
                    if preview.startswith("{") or preview.startswith("<"):
                        # JSON o HTML — no es el TXT
                        log.debug("response_skipped_not_txt", url=url[-60:], preview=preview[:30])
                        return
                    captured_responses.append(body)
                    log.debug("response_captured", url=url[-80:], ct=ct, cd=cd, size=len(body))
                elif body and len(body) > 0 and is_attachment:
                    # Cualquier adjunto, aunque pequeño
                    captured_responses.append(body)
                    log.debug("response_captured_attachment", url=url[-80:], size=len(body))
        except Exception:
            pass

    async def _on_popup(popup_page):
        popup_pages.append(popup_page)
        log.debug("popup_detected", url=popup_page.url)

    page.on("response", _on_response)
    page.context.on("page", _on_popup)

    try:
        async with page.expect_download(timeout=25_000) as dl_info:
            await download_btn.click()

        download = await dl_info.value
        log.info("download_received", filename=download.suggested_filename)
        await download.save_as(str(dest))

        if dest.exists() and dest.stat().st_size > 0:
            log.info("download_saved", path=str(dest), size_bytes=dest.stat().st_size)
            return dest

    except PlaywrightTimeoutError:
        log.warning("download_event_timeout_25s", note="probando interceptación HTTP")
        # Loguear todas las respuestas SRI vistas (diagnóstico)
        for r in all_sri_responses[-20:]:
            log.debug("sri_response_seen", **r)

    finally:
        page.remove_listener("response", _on_response)
        page.context.remove_listener("page", _on_popup)

    # ── Estrategia 2: respuesta HTTP interceptada ya capturada ───────────────────
    if captured_responses:
        dest.write_bytes(captured_responses[0])
        if dest.stat().st_size > 0:
            log.info("download_via_interception", path=str(dest), size_bytes=dest.stat().st_size)
            return dest

    # ── Estrategia 3: esperar más sin re-click (la respuesta puede venir tarde) ──
    log.info("download_waiting_extra_10s")
    extra_captured: list[bytes] = []

    async def _on_response_late(response):
        try:
            url = response.url
            ct = response.headers.get("content-type", "")
            cd = response.headers.get("content-disposition", "")
            if "srienlinea.sri.gob.ec" not in url:
                return
            if any(p in url for p in _BEACON_PATTERNS):
                return
            if "attachment" in cd or ("text/plain" in ct and "octet-stream" not in ct):
                body = await response.body()
                if body and len(body) > 100:
                    preview = body[:50].decode("utf-8", errors="replace")
                    if not preview.startswith("{") and not preview.startswith("<"):
                        extra_captured.append(body)
                        log.debug("late_response_captured", url=url[-80:], size=len(body))
        except Exception:
            pass

    page.on("response", _on_response_late)
    await page.wait_for_timeout(10_000)
    page.remove_listener("response", _on_response_late)

    if extra_captured:
        dest.write_bytes(extra_captured[0])
        if dest.stat().st_size > 0:
            log.info("download_via_late_interception", path=str(dest), size_bytes=dest.stat().st_size)
            return dest

    # ── Estrategia 4: popup / nueva pestaña ──────────────────────────────────────
    if popup_pages:
        popup = popup_pages[0]
        try:
            await popup.wait_for_load_state("domcontentloaded", timeout=10_000)
            popup_content = await popup.content()
            if popup_content and "<html" not in popup_content[:100].lower():
                dest.write_text(popup_content, encoding="utf-8")
                if dest.stat().st_size > 0:
                    log.info("download_via_popup", path=str(dest))
                    await popup.close()
                    return dest
            try:
                async with popup.expect_download(timeout=5_000) as dl_info2:
                    pass
                dl2 = await dl_info2.value
                await dl2.save_as(str(dest))
                if dest.exists() and dest.stat().st_size > 0:
                    log.info("download_via_popup_dl", path=str(dest))
                    await popup.close()
                    return dest
            except PlaywrightTimeoutError:
                pass
            await popup.close()
        except Exception as e:
            log.warning("popup_strategy_failed", error=str(e))

    # ── Estrategia 5: interceptar POST JSF via page.route() ──────────────────────
    # mojarra.jsfcljs envía un POST al URL .jsf actual.
    # Interceptamos esa respuesta y la guardamos directamente.
    log.info("download_trying_jsf_route_intercept")
    jsf_bodies: list[bytes] = []
    jsf_debug: list[dict] = []

    async def _catch_jsf(route, request):
        try:
            response = await route.fetch()
            ct = response.headers.get("content-type", "")
            cd = response.headers.get("content-disposition", "")
            body = await response.body()
            preview = body[:120].decode("utf-8", errors="replace") if body else ""
            jsf_debug.append({
                "method": request.method,
                "url": request.url[-80:],
                "status": response.status,
                "ct": ct[:60],
                "cd": cd[:60],
                "size": len(body),
                "preview": preview[:80],
            })
            # Capturar si es el archivo real: adjunto, o texto plano no-JSON >100 bytes
            # NOTA: NO chequeamos "No existen datos" en el HTML porque esa cadena
            # aparece como template JS incluso cuando SÍ hay resultados (falso positivo).
            if "attachment" in cd:
                jsf_bodies.append(body)
            elif "text/plain" in ct and len(body) > 100 and not preview.strip().startswith("{"):
                jsf_bodies.append(body)
            await route.fulfill(response=response)
        except Exception as e:
            log.warning("jsf_route_catch_error", error=str(e))
            await route.continue_()

    try:
        await page.route("**/*.jsf", _catch_jsf)
        await download_btn.click()
        await page.wait_for_timeout(12_000)
    finally:
        try:
            await page.unroute("**/*.jsf", _catch_jsf)
        except Exception:
            pass

    for entry in jsf_debug:
        log.debug("jsf_route_response", **entry)

    if jsf_bodies:
        body_to_use = jsf_bodies[-1]
        if body_to_use == b"__EMPTY__":
            log.info("download_jsf_confirmed_empty")
            return None  # Sin datos → tratar como "no comprobantes"
        dest.write_bytes(body_to_use)
        if dest.stat().st_size > 0:
            log.info("download_via_jsf_route", path=str(dest), size_bytes=dest.stat().st_size)
            return dest

    # ── Estrategia 6: fetch directo con httpx + cookies (fallback si href real) ──
    if btn_href and btn_href.startswith("http"):
        log.info("download_trying_direct_fetch", href=btn_href)
        try:
            cookies_list = await page.context.cookies()
            cookies_dict = {c["name"]: c["value"] for c in cookies_list}
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Referer": page.url,
                "Accept": "text/plain,*/*",
            }
            async with httpx.AsyncClient(
                cookies=cookies_dict,
                headers=headers,
                follow_redirects=True,
                timeout=30.0,
            ) as client:
                resp = await client.get(btn_href)
                if resp.status_code == 200 and resp.content:
                    dest.write_bytes(resp.content)
                    if dest.stat().st_size > 0:
                        log.info(
                            "download_via_direct_fetch",
                            path=str(dest),
                            size_bytes=dest.stat().st_size,
                        )
                        return dest
        except Exception as e:
            log.warning("direct_fetch_failed", error=str(e))

    # ── Diagnóstico final ──────────────────────────────────────────────────────
    screenshot_path = str(config.logs_dir / "download_timeout.png")
    await page.screenshot(path=screenshot_path, full_page=True)
    raise DownloadError(
        f"No se pudo capturar la descarga del TXT tras siete estrategias (DOM+6). "
        f"Screenshot: {screenshot_path}"
    )
