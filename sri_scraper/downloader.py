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
from patchright.async_api import Locator, Page, TimeoutError as PlaywrightTimeoutError

from .browser import USER_AGENT, human_delay, human_page_dwell
from .config import SRIConfig
from .diagnostics import (
    artifact_stem,
    classify_payload,
    persist_binary_artifact,
    write_json_artifact,
    write_text_artifact,
)
from .exceptions import CaptchaChallengeError, DownloadError
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

    # Guardar URL de comprobantes para recargar entre tipos y limpiar estado CAPTCHA
    comprobantes_url = page.url

    for tipo_idx, (tipo_val, tipo_label) in enumerate(tipo_options):
        slug = tipo_label.lower().replace(" ", "_").replace("/", "-").replace("\\", "-")
        temp_dest = config.downloads_dir / f"sri_recibidos_{target_date.strftime('%Y%m%d')}_{slug}.txt"

        # A partir del segundo tipo, recargar la página para limpiar mensajes
        # de error de CAPTCHA que persisten del tipo anterior en el DOM de JSF.
        if tipo_idx > 0:
            log.info("page_reload_between_tipos", tipo=tipo_label)
            try:
                await page.goto(comprobantes_url, wait_until="domcontentloaded", timeout=15_000)
                await human_delay(1000, 2000)
            except Exception as e:
                log.warning("page_reload_failed", error=str(e))

            # Re-establecer período tras el reload (JSF puede resetear filtros).
            await _set_periodo_selects(page, target_date)
            await human_delay(200, 400)

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


def _is_jsf_post_response(response) -> bool:
    """Reconoce postbacks JSF del módulo de comprobantes recibidos."""
    try:
        return (
            "comprobantesRecibidos.jsf" in response.url
            and response.request.method == "POST"
        )
    except Exception:
        return False


async def _read_select_state(select: Locator) -> dict[str, str]:
    """Lee value + label seleccionados de un <select>."""
    return await select.evaluate(
        """
        el => {
            const opt = el.options[el.selectedIndex];
            return {
                value: el.value || '',
                label: opt ? opt.text.trim() : '',
            };
        }
        """
    )


async def _pick_periodo_selects(page: Page) -> tuple[Locator, Locator, Locator]:
    """
    Ubica los selects de año, mes y día.

    Prefiere los IDs JSF estables del portal y cae al heurístico por contenido
    solo si el DOM cambia.
    """
    direct_selectors = {
        "year": "#frmPrincipal\\:ano",
        "month": "#frmPrincipal\\:mes",
        "day": "#frmPrincipal\\:dia",
    }
    located: dict[str, Locator] = {}
    for key, selector in direct_selectors.items():
        locator = page.locator(selector)
        if await locator.count() > 0:
            located[key] = locator.first

    if len(located) == 3:
        return located["year"], located["month"], located["day"]

    selects = page.locator("select")
    count = await selects.count()
    year_idx = month_idx = day_idx = None

    for i in range(count):
        try:
            html = await selects.nth(i).inner_html(timeout=2_000)
        except Exception:
            continue

        if year_idx is None and ">2026<" in html:
            year_idx = i
        elif month_idx is None and "Enero" in html and "Diciembre" in html:
            month_idx = i
        elif day_idx is None and ">31<" in html and ">1<" in html:
            day_idx = i

    if year_idx is None:
        year_idx = 0
    if month_idx is None:
        month_idx = 1
    if day_idx is None:
        day_idx = 2

    return selects.nth(year_idx), selects.nth(month_idx), selects.nth(day_idx)


async def _pick_tipo_select(page: Page) -> Locator:
    """Ubica el select del tipo de comprobante."""
    direct = page.locator("#frmPrincipal\\:cmbTipoComprobante")
    if await direct.count() > 0:
        return direct.first

    selects = page.locator("select")
    count = await selects.count()
    if count == 0:
        raise DownloadError("No se encontró ningún select en la pantalla de consulta")
    return selects.nth(count - 1)


async def _select_option_stably(
    page: Page,
    select: Locator,
    *,
    field_name: str,
    value: Optional[str] = None,
    label: Optional[str] = None,
    expect_jsf_post: bool = False,
) -> dict[str, str]:
    """
    Selecciona una opción y espera el posible postback AJAX del portal.

    RichFaces/JSF puede re-renderizar selects después de cambiar año/mes; por
    eso esperamos el POST cuando aplica y luego leemos el estado final.
    """
    async def _apply() -> None:
        if value is not None:
            await select.select_option(value=value)
        elif label is not None:
            await select.select_option(label=label)
        else:
            raise ValueError("Se requiere value o label para seleccionar una opción")

    response_status: Optional[int] = None
    if expect_jsf_post:
        try:
            async with page.expect_response(_is_jsf_post_response, timeout=4_000) as info:
                await _apply()
            response = await info.value
            response_status = response.status
        except PlaywrightTimeoutError:
            log.debug("select_option_no_jsf_post", field=field_name)
        except Exception:
            raise
    else:
        await _apply()

    await human_delay(300, 600)
    current = await _read_select_state(select)
    log.debug(
        "select_option_applied",
        field=field_name,
        requested_value=value,
        requested_label=label,
        response_status=response_status,
        current=current,
    )
    return current


async def _read_current_filters(page: Page) -> dict[str, str]:
    """Lee el estado actual de filtros visibles del formulario JSF."""
    current = {
        "ruc_value": "",
        "year_value": "",
        "month_value": "",
        "month_label": "",
        "day_value": "",
        "day_label": "",
        "tipo_value": "",
        "tipo_label": "",
    }

    try:
        ruc_input = page.locator("#frmPrincipal\\:txtParametro")
        if await ruc_input.count() > 0:
            current["ruc_value"] = await ruc_input.first.input_value()
    except Exception:
        pass

    try:
        year_select, month_select, day_select = await _pick_periodo_selects(page)
        year_state = await _read_select_state(year_select)
        month_state = await _read_select_state(month_select)
        day_state = await _read_select_state(day_select)
        current["year_value"] = year_state["value"]
        current["month_value"] = month_state["value"]
        current["month_label"] = month_state["label"]
        current["day_value"] = day_state["value"]
        current["day_label"] = day_state["label"]
    except Exception:
        pass

    try:
        tipo_state = await _read_select_state(await _pick_tipo_select(page))
        current["tipo_value"] = tipo_state["value"]
        current["tipo_label"] = tipo_state["label"]
    except Exception:
        pass

    return current


def _filters_match_target_date(current: dict[str, str], target_date: date) -> bool:
    """Retorna True si los filtros visibles ya apuntan a la fecha objetivo."""
    return (
        current.get("year_value") == str(target_date.year)
        and current.get("month_label") == MESES_ES[target_date.month]
        and current.get("day_value") == str(target_date.day)
    )


def _tipo_matches_current(current: dict[str, str], tipo_value: str, tipo_label: str) -> bool:
    """Retorna True si el tipo visible ya coincide con el solicitado."""
    current_value = current.get("tipo_value", "")
    current_label = current.get("tipo_label", "")
    if tipo_value and current_value == tipo_value:
        return True
    if tipo_label and current_label == tipo_label:
        return True
    return False


async def _select_tipo(page: Page, tipo_value: str, tipo_label: str) -> None:
    """Selecciona el tipo de comprobante en el último select de la página."""
    current = await _read_current_filters(page)
    if _tipo_matches_current(current, tipo_value, tipo_label):
        log.debug("tipo_already_selected", value=tipo_value, label=tipo_label, current=current)
        return

    try:
        for attempt in range(3):
            tipo_select = await _pick_tipo_select(page)
            current = await _select_option_stably(
                page,
                tipo_select,
                field_name="tipo_comprobante",
                value=tipo_value or None,
                label=None if tipo_value else tipo_label,
            )
            if current["value"] == tipo_value or current["label"] == tipo_label:
                log.debug(
                    "tipo_selected",
                    attempt=attempt + 1,
                    value=tipo_value,
                    label=tipo_label,
                    current=current,
                )
                return
            log.warning(
                "tipo_selection_mismatch",
                attempt=attempt + 1,
                expected_value=tipo_value,
                expected_label=tipo_label,
                current=current,
            )
            await page.wait_for_timeout(1_000)
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
    current_filters = await _read_current_filters(page)
    if not _filters_match_target_date(current_filters, target_date):
        log.warning("periodo_drift_detected_before_consultar", current=current_filters)
        await _set_periodo_selects(page, target_date)
        await human_delay(300, 500)
        current_filters = await _read_current_filters(page)

    if tipo_label != "default" and not _tipo_matches_current(current_filters, tipo_value, tipo_label):
        await _select_tipo(page, tipo_value, tipo_label)
        await human_delay(300, 500)

    # Consultar
    await _click_consultar(
        page,
        config,
        target_date=target_date,
        tipo_label=tipo_label,
    )
    log.info("consultar_clicked", tipo=tipo_label)

    # Esperar AJAX (SRI tarda 5-15 s)
    await human_delay(6000, 10000)

    artifacts = await _persist_page_artifacts(
        page,
        config,
        target_date,
        tipo_label,
        stage="post_consultar",
        classification="post_consultar_snapshot",
    )
    log.debug("post_consultar_screenshot", path=artifacts["screenshot"], tipo=tipo_label)

    if await _is_empty_result(page):
        await _persist_page_artifacts(
            page,
            config,
            target_date,
            tipo_label,
            stage="empty_result",
            classification="empty_result_detected",
        )
        log.info("no_comprobantes_for_tipo", tipo=tipo_label, date=str(target_date))
        return None

    return await _do_download(page, config, target_date, tipo_label, dest)


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

    current = await _read_current_filters(page)
    if _filters_match_target_date(current, target_date):
        log.info("periodo_already_selected", current=current)
        return

    for attempt in range(3):
        try:
            year_select, month_select, day_select = await _pick_periodo_selects(page)
            await _select_option_stably(
                page,
                year_select,
                field_name="periodo_ano",
                value=year_str,
                expect_jsf_post=False,
            )
            await _select_option_stably(
                page,
                month_select,
                field_name="periodo_mes",
                label=month_str,
                expect_jsf_post=True,
            )
            await page.wait_for_timeout(800)
            year_select, month_select, day_select = await _pick_periodo_selects(page)
            await _select_option_stably(
                page,
                day_select,
                field_name="periodo_dia",
                value=day_str,
                expect_jsf_post=False,
            )
            await page.wait_for_timeout(1_000)

            current = await _read_current_filters(page)
            if _filters_match_target_date(current, target_date):
                log.info("periodo_selected_confirmed", attempt=attempt + 1, current=current)
                return

            log.warning(
                "periodo_selection_mismatch",
                attempt=attempt + 1,
                expected={"year": year_str, "month": month_str, "day": day_str},
                current=current,
            )
        except Exception as e:
            log.warning("periodo_select_failed", attempt=attempt + 1, error=str(e))

        await page.wait_for_timeout(1_500)

    log.warning(
        "periodo_selection_unconfirmed",
        expected={"year": year_str, "month": month_str, "day": day_str},
        current=await _read_current_filters(page),
    )


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
        task_variants = [
            (
                "v2_enterprise_invisible",
                {
                    "type": "RecaptchaV2EnterpriseTaskProxyless",
                    "websiteURL": page_url,
                    "websiteKey": _RECAPTCHA_SITE_KEY,
                    "isInvisible": True,
                    "userAgent": USER_AGENT,
                },
            ),
            (
                "v3_enterprise",
                {
                    "type": "RecaptchaV3TaskProxyless",
                    "websiteURL": page_url,
                    "websiteKey": _RECAPTCHA_SITE_KEY,
                    "pageAction": _RECAPTCHA_ACTION,
                    "isEnterprise": True,
                    "minScore": 0.5,
                },
            ),
        ]

        for variant_name, task_payload in task_variants:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    "https://api.2captcha.com/createTask",
                    json={
                        "clientKey": api_key,
                        "task": task_payload,
                    },
                )
            data = resp.json()
            if data.get("errorId", 1) != 0:
                log.warning(
                    "2captcha_create_failed",
                    variant=variant_name,
                    error=data.get("errorDescription"),
                    code=data.get("errorCode"),
                )
                continue

            task_id = data["taskId"]
            log.info("2captcha_task_created", task_id=task_id, variant=variant_name)

            # Polling para resultado (máx 2 minutos)
            for attempt in range(24):
                await asyncio.sleep(5)
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.post(
                        "https://api.2captcha.com/getTaskResult",
                        json={"clientKey": api_key, "taskId": task_id},
                    )
                try:
                    result = resp.json()
                except Exception:
                    # A veces la API devuelve JSON con trailing bytes — parsear manualmente
                    import json as _json
                    raw_text = resp.text.strip()
                    try:
                        result = _json.loads(raw_text)
                    except Exception:
                        log.warning("2captcha_json_parse_error", raw=raw_text[:80], variant=variant_name)
                        continue
                status = result.get("status")
                if status == "ready":
                    token = result["solution"]["gRecaptchaResponse"]
                    log.info(
                        "2captcha_solved",
                        attempts=attempt + 1,
                        token_prefix=token[:20],
                        variant=variant_name,
                    )
                    return token
                if status == "processing":
                    log.debug("2captcha_still_processing", attempt=attempt + 1, variant=variant_name)
                    continue

                log.warning("2captcha_unexpected_status", status=status, data=result, variant=variant_name)
                break

        log.warning("2captcha_timeout_2min")
        return None

    except Exception as e:
        log.warning("2captcha_exception", error=str(e))
        return None


async def _inject_and_submit_with_token(page: Page, token: str) -> None:
    """
    Inyecta el token de reCAPTCHA en el formulario y dispara el AJAX
    JSF/PrimeFaces que usa el portal para consultar comprobantes.

    El flujo del SRI define `rcBuscar()` con un `source` JSF dinámico
    (`frmPrincipal:j_idtXX`). Llamar `onSubmit()` directamente resultó
    inestable en automatización nativa; por eso extraemos el `source`
    y ejecutamos `PrimeFaces.ab(...)` de forma explícita.
    """
    def _is_jsf_post_response(response) -> bool:
        return (
            response.request.method == "POST"
            and "comprobantesRecibidos.jsf" in response.url
        )

    async def _read_submit_meta() -> dict[str, object]:
        raw_meta = await page.evaluate(
            """
            () => document.documentElement.getAttribute('data-codex-submit-meta')
            """
        )
        if not raw_meta:
            return {}

        import json as _json

        try:
            parsed = _json.loads(raw_meta)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {"raw_meta": raw_meta}
        return {}

    await page.evaluate(
        """
        (token) => {
            document.documentElement.removeAttribute('data-codex-submit-meta');
            const tokenLiteral = JSON.stringify(token);
            const script = document.createElement('script');
            script.text = `
                (function() {
                    const token = ${tokenLiteral};
                    const root = document.documentElement;

                    const writeMeta = (meta) => {
                        try {
                            root.setAttribute('data-codex-submit-meta', JSON.stringify(meta));
                        } catch (err) {
                            root.setAttribute('data-codex-submit-meta', JSON.stringify({
                                strategy: 'meta_write_failed',
                                error: String(err),
                            }));
                        }
                    };

                    const fillTokenFields = () => {
                        const fields = Array.from(
                            document.querySelectorAll('[name="g-recaptcha-response"]')
                        );
                        fields.forEach((el) => {
                            el.value = token;
                            el.textContent = token;
                            el.innerHTML = token;
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                        });
                        return fields.length;
                    };

                    const trySubmit = () => {
                        const filled = fillTokenFields();
                        const rcBuscarSource = typeof rcBuscar === 'function'
                            ? String(rcBuscar)
                            : '';
                        const sourceMatch = rcBuscarSource.match(/source:'([^']+)'/);
                        const updateMatch = rcBuscarSource.match(/update:'([^']+)'/);
                        const source = sourceMatch ? sourceMatch[1] : null;
                        const update = updateMatch ? updateMatch[1] : null;

                        if (typeof onSubmit === 'function') {
                            const btn = document.getElementById('frmPrincipal:btnBuscar');
                            if (btn && typeof deshabilitarBoton === 'function') {
                                deshabilitarBoton(btn);
                            }
                            onSubmit();
                            writeMeta({
                                strategy: 'onSubmit',
                                filled,
                                source,
                                update,
                                has_execute_recaptcha: typeof executeRecaptcha === 'function',
                                has_on_submit: true,
                                has_rc_buscar: false,
                                has_primefaces: typeof PrimeFaces !== 'undefined'
                                    && PrimeFaces
                                    && typeof PrimeFaces.ab === 'function',
                            });
                            return true;
                        }

                        if (typeof rcBuscar === 'function') {
                            const btn = document.getElementById('frmPrincipal:btnBuscar');
                            if (btn && typeof deshabilitarBoton === 'function') {
                                deshabilitarBoton(btn);
                            }
                            rcBuscar();
                            writeMeta({
                                strategy: 'rcBuscar_no_params',
                                filled,
                                source,
                                update,
                                has_execute_recaptcha: typeof executeRecaptcha === 'function',
                                has_on_submit: typeof onSubmit === 'function',
                                has_rc_buscar: true,
                                has_primefaces: typeof PrimeFaces !== 'undefined'
                                    && PrimeFaces
                                    && typeof PrimeFaces.ab === 'function',
                            });
                            return true;
                        }

                        if (
                            typeof PrimeFaces !== 'undefined'
                            && PrimeFaces
                            && typeof PrimeFaces.ab === 'function'
                        ) {
                            PrimeFaces.ab({
                                source: source || 'frmPrincipal:btnBuscar',
                                formId: 'frmPrincipal',
                                process: '@all',
                                update: update || undefined,
                                params: [{ name: 'g-recaptcha-response', value: token }],
                            });
                            writeMeta({
                                strategy: 'primefaces_ab',
                                filled,
                                source: source || 'frmPrincipal:btnBuscar',
                                update,
                                has_execute_recaptcha: typeof executeRecaptcha === 'function',
                                has_on_submit: typeof onSubmit === 'function',
                                has_rc_buscar: false,
                                has_primefaces: true,
                            });
                            return true;
                        }

                        return false;
                    };

                    let attempts = 0;
                    const timer = setInterval(() => {
                        attempts += 1;
                        if (trySubmit()) {
                            clearInterval(timer);
                            return;
                        }

                        if (attempts >= 40) {
                            clearInterval(timer);
                            writeMeta({
                                strategy: 'no_handlers',
                                attempts,
                                has_execute_recaptcha: typeof executeRecaptcha === 'function',
                                has_on_submit: typeof onSubmit === 'function',
                                has_rc_buscar: typeof rcBuscar === 'function',
                                has_primefaces: typeof PrimeFaces !== 'undefined'
                                    && PrimeFaces
                                    && typeof PrimeFaces.ab === 'function',
                                rc_buscar_source: typeof rcBuscar === 'function' ? String(rcBuscar) : null,
                            });
                        }
                    }, 500);
                })();
            `;
            document.documentElement.appendChild(script);
            script.remove();
        }
        """,
        token,
    )

    try:
        async with page.expect_response(_is_jsf_post_response, timeout=20_000) as resp_info:
            await human_delay(50, 150)
        response = await resp_info.value
        response_status = response.status
    except PlaywrightTimeoutError:
        submit_meta = await _read_submit_meta()
        if not submit_meta:
            submit_meta = {"strategy": "timeout_no_request"}
        log.warning("2captcha_submit_no_jsf_response", **submit_meta)
        return

    submit_meta = await _read_submit_meta()
    log.info(
        "2captcha_token_injected_and_submitted",
        response_status=response_status,
        **submit_meta,
    )


async def _humanize_before_consultar(page: Page, config: Optional["SRIConfig"] = None) -> None:
    """
    Simula comportamiento humano antes de hacer click en Consultar.
    Mejora el score de reCAPTCHA v3 Enterprise para el modo sin solver.
    """
    rounds = 4 if config and not config.headless else 3
    await human_page_dwell(page, rounds=rounds)

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

    for selector in (
        "#frmPrincipal\\:ano",
        "#frmPrincipal\\:mes",
        "#frmPrincipal\\:dia",
        "#frmPrincipal\\:cmbTipoComprobante",
        "#frmPrincipal\\:btnBuscar",
    ):
        try:
            el = page.locator(selector).first
            if await el.is_visible(timeout=1_500):
                await el.hover()
                await human_delay(250, 700)
        except Exception:
            continue

    try:
        await page.locator("body").click(position={"x": 120, "y": 120}, timeout=1_500)
        await human_delay(500, 1100)
    except Exception:
        pass

    # En modo headed damos más tiempo para que la sesión "madure" antes
    # de accionar el botón sensible de consulta.
    if config and not config.headless:
        await human_delay(8000, 14000)
    else:
        await human_delay(4000, 7000)


async def _humanize_after_captcha_failure(
    page: Page,
    config: SRIConfig,
    *,
    attempt: int,
) -> None:
    """
    Hace una pausa más creíble tras un captcha fallido.

    Evita el patrón robótico de click-ear inmediatamente otra vez y mete
    lectura/scroll/hover antes del siguiente intento.
    """
    await human_page_dwell(page, rounds=3 + attempt)

    for selector in (
        "#frmPrincipal\\:ano",
        "#frmPrincipal\\:mes",
        "#frmPrincipal\\:dia",
        "#frmPrincipal\\:cmbTipoComprobante",
        "#frmPrincipal\\:btnBuscar",
    ):
        try:
            el = page.locator(selector).first
            if await el.is_visible(timeout=1_500):
                await el.hover()
                await human_delay(250, 800)
        except Exception:
            continue

    try:
        await page.evaluate("window.scrollTo(0, 260)")
        await human_delay(700, 1400)
        await page.evaluate("window.scrollTo(0, 0)")
        await human_delay(600, 1200)
    except Exception:
        pass

    headed_ranges = [
        (15000, 22000),
        (26000, 36000),
        (38000, 52000),
    ]
    headless_ranges = [
        (10000, 15000),
        (18000, 26000),
        (26000, 34000),
    ]
    wait_ranges = headed_ranges if not config.headless else headless_ranges
    low_ms, high_ms = wait_ranges[min(attempt, len(wait_ranges) - 1)]
    log.info(
        "consultar_retry_after_captcha",
        attempt=attempt + 1,
        wait_ms=low_ms,
        wait_ms_max=high_ms,
    )
    await human_delay(low_ms, high_ms)


async def _persist_captcha_failure_artifacts(
    page: Page,
    config: SRIConfig,
    *,
    target_date: date,
    tipo_label: str,
) -> None:
    try:
        artifacts = await _persist_page_artifacts(
            page,
            config,
            target_date,
            tipo_label,
            stage="captcha_failed",
            classification="captcha_failed_after_consultar",
        )
        log.warning("consultar_captcha_artifacts_saved", **artifacts)
    except Exception as exc:
        log.warning("consultar_captcha_artifacts_failed", error=str(exc))


async def _click_consultar(
    page: Page,
    config: SRIConfig,
    *,
    target_date: date,
    tipo_label: str,
) -> None:
    """
    Hace click en el botón "Consultar" del portal de comprobantes.

    Estrategia 1 (si TWOCAPTCHA_API_KEY configurado):
      - Resuelve reCAPTCHA Enterprise via 2captcha
      - Inyecta token en g-recaptcha-response
      - Dispara onSubmit() directamente

    Estrategia 2 (fallback sin solver):
      - Humanización + click normal
      - 3 reintentos con delays crecientes
      - Si sigue fallando, aborta la consulta para no tratar CAPTCHA como vacío

    Raises:
        DownloadError: Si el botón no se encuentra.
    """
    log.info("consultar_filters_before_click", current=await _read_current_filters(page))
    await _humanize_before_consultar(page, config)

    # En modo headed preferimos primero el flujo natural del navegador real.
    prefer_natural = not config.headless

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

    async def _natural_click_attempts(max_attempts: int) -> bool:
        for attempt in range(max_attempts):
            try:
                await consultar_btn.hover()
                await human_delay(250, 700)
            except Exception:
                pass

            await consultar_btn.click()
            log.debug("consultar_btn_clicked", attempt=attempt + 1)
            await human_delay(5000, 8000)

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
                log.info("consultar_clicked_naturally", attempt=attempt + 1)
                return True

            if attempt < max_attempts - 1:
                await _humanize_after_captcha_failure(page, config, attempt=attempt)
        return False

    if prefer_natural:
        if await _natural_click_attempts(3):
            return
        # En modo headed preferimos cortar aquí antes que caer a una segunda
        # ronda de clicks más mecánicos que delatan automatización.
        if not config.twocaptcha_api_key:
            await _persist_captcha_failure_artifacts(
                page,
                config,
                target_date=target_date,
                tipo_label=tipo_label,
            )
            log.warning("consultar_captcha_exhausted_retries")
            raise CaptchaChallengeError("Captcha incorrecta persistente al consultar el SRI")

    # ── Estrategia 2: 2captcha solver ────────────────────────────────────────
    if config.twocaptcha_api_key:
        token = await _solve_recaptcha_2captcha(page.url, config.twocaptcha_api_key)
        if token:
            await _inject_and_submit_with_token(page, token)
            log.info("consultar_via_2captcha")
            return
        log.warning("2captcha_failed_falling_back_to_click")

    # ── Estrategia 3: click normal con reintentos ─────────────────────────────
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
            await _humanize_after_captcha_failure(page, config, attempt=attempt)

    await _persist_captcha_failure_artifacts(
        page,
        config,
        target_date=target_date,
        tipo_label=tipo_label,
    )
    log.warning("consultar_captcha_exhausted_retries")
    raise CaptchaChallengeError("Captcha incorrecta persistente al consultar el SRI")


async def _is_empty_result(page: Page) -> bool:
    """
    Detecta si la consulta no retornó comprobantes (día sin actividad = normal).

    Retorna True si:
    - El portal muestra texto de "sin resultados"
    - No hay tabla de resultados con filas de datos (RichFaces / tabla JSF)

    Raises:
        CaptchaChallengeError: Si el portal sigue reportando CAPTCHA incorrecto.
    """
    # ── Verificación robusta via JS (innerText solo incluye texto visible) ───────
    # Usamos JS en lugar de Playwright locators para evitar problemas con
    # tildes/acentos (á, é, ó) que confunden el selector `text=`.
    empty_keywords = [
        "no se encontraron resultados",
        "no existen comprobantes",
        "no hay comprobantes",
        "sin registros",
        "0 registros",
        "no existen resultados",
        "no se encontraron",
        "no existe informaci",
        "sin resultados",
        "no existen datos para los par",   # "No existen datos para los parámetros"
        "no existen datos",
    ]
    try:
        visible_text: str = await page.evaluate("() => document.body.innerText.toLowerCase()")
        for kw in empty_keywords:
            if kw in visible_text:
                log.info("empty_result_detected_js", keyword=kw)
                return True
    except Exception:
        pass

    # Si el CAPTCHA sigue fallando después del retry, abortar.
    captcha_error_selectors = [
        "text=Captcha incorrecta",
        "text=Captcha error",
    ]
    for sel in captcha_error_selectors:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=1_000):
                log.warning("captcha_still_failing_after_consultar", selector=sel)
                raise CaptchaChallengeError(
                    "Captcha incorrecta persistente luego de consultar el SRI"
                )
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


async def _persist_page_artifacts(
    page: Page,
    config: SRIConfig,
    target_date: date,
    tipo_label: str,
    stage: str,
    classification: str,
    extra: Optional[dict] = None,
) -> dict[str, str]:
    """Guarda screenshot, HTML, innerText y metadata clasificando el estado actual."""
    stem = artifact_stem(target_date, tipo_label, stage)
    screenshot_path = config.logs_dir / f"{stem}.png"
    html_path = config.logs_dir / f"{stem}.html"
    text_path = config.logs_dir / f"{stem}_innerText.txt"
    meta_path = config.logs_dir / f"{stem}.json"

    await page.screenshot(path=str(screenshot_path), full_page=True)
    html = await page.content()
    inner_text = await page.evaluate("() => document.body.innerText")
    filters = await _read_current_filters(page)

    write_text_artifact(html_path, html)
    write_text_artifact(text_path, inner_text)
    write_json_artifact(
        meta_path,
        {
            "classification": classification,
            "stage": stage,
            "tipo_label": tipo_label,
            "target_date": str(target_date),
            "page_url": page.url,
            "artifacts": {
                "screenshot": str(screenshot_path),
                "html": str(html_path),
                "inner_text": str(text_path),
            },
            "filters": filters,
            **(extra or {}),
        },
    )
    return {
        "screenshot": str(screenshot_path),
        "html": str(html_path),
        "inner_text": str(text_path),
        "metadata": str(meta_path),
    }


def _persist_jsf_debug_artifacts(
    config: SRIConfig,
    target_date: date,
    tipo_label: str,
    responses: list[dict],
) -> list[str]:
    stem = artifact_stem(target_date, tipo_label, "jsf_route")
    meta_path = config.logs_dir / f"{stem}.json"
    saved_files: list[str] = []
    serializable: list[dict] = []

    for idx, entry in enumerate(responses, start=1):
        entry_copy = {k: v for k, v in entry.items() if k != "body"}
        body: bytes = entry["body"]
        suffix = ".txt"
        if entry["classification"] == "html":
            suffix = ".html"
        elif entry["classification"] == "binary":
            suffix = ".bin"
        artifact_path = config.logs_dir / f"{stem}_{idx:02d}{suffix}"
        persist_binary_artifact(artifact_path, body)
        entry_copy["artifact_path"] = str(artifact_path)
        serializable.append(entry_copy)
        saved_files.append(str(artifact_path))

    write_json_artifact(meta_path, {"responses": serializable})
    saved_files.append(str(meta_path))
    return saved_files


_CLAVE_RE = re.compile(r"(?<!\d)(\d{49})(?!\d)")

# Regex para 49-digit access keys used in DOM scraping strategy


async def _extract_claves_from_dom(
    page: Page,
    config: SRIConfig,
    target_date: date,
    tipo_label: str,
    dest: Path,
    stage: str,
) -> Optional[Path]:
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
        long_digits = re.findall(r"\d{10,}", content)
        table_digits = re.findall(r"\d{10,}", table_text)
        await _persist_page_artifacts(
            page,
            config,
            target_date,
            tipo_label,
            stage=stage,
            classification="dom_missing_claves",
            extra={
                "html_size": len(content),
                "long_digit_seqs_html": len(long_digits),
                "long_digit_seqs_table": len(table_digits),
                "sample_long": long_digits[:3],
            },
        )
        log.debug(
            "dom_debug_info",
            html_size=len(content),
            long_digit_seqs_html=len(long_digits),
            long_digit_seqs_table=len(table_digits),
            sample_long=long_digits[:3] if long_digits else [],
        )

        return None

    except Exception as e:
        log.warning("dom_extraction_failed", error=str(e))
        return None


async def _do_download(
    page: Page,
    config: SRIConfig,
    target_date: date,
    tipo_label: str,
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
    dom_result = await _extract_claves_from_dom(
        page,
        config,
        target_date,
        tipo_label,
        dest,
        stage="dom_probe_00",
    )
    if dom_result is not None:
        return dom_result

    # El AJAX de Consultar puede tardar más que el wait inicial en _download_for_tipo.
    # Esperamos hasta 15 s adicionales para que el portal muestre los resultados
    # o el banner de "sin datos". Revisamos cada 3 s.
    log.info("dom_no_claves_waiting_ajax_settle")
    for _wait_round in range(5):
        await page.wait_for_timeout(3_000)
        # Intentar extracción de nuevo (puede aparecer la tabla con retraso)
        dom_result = await _extract_claves_from_dom(
            page,
            config,
            target_date,
            tipo_label,
            dest,
            stage=f"dom_probe_{_wait_round + 1:02d}",
        )
        if dom_result is not None:
            return dom_result
        # Si el portal ya muestra mensaje visible de "sin datos" → salir limpio
        if await _is_empty_result(page):
            await _persist_page_artifacts(
                page,
                config,
                target_date,
                tipo_label,
                stage=f"empty_after_wait_{_wait_round + 1:02d}",
                classification="empty_result_detected_after_ajax_wait",
                extra={"wait_round": _wait_round + 1},
            )
            log.info("download_empty_confirmed_after_ajax_wait", round=_wait_round + 1)
            return None

    # Espera final de 8s antes de buscar el botón de descarga.
    # El banner "No existen datos para los parámetros ingresados" puede tardar
    # hasta 25s en aparecer — si se detecta ahora evitamos los ~60s de botón.
    await page.wait_for_timeout(8_000)
    if await _is_empty_result(page):
        await _persist_page_artifacts(
            page,
            config,
            target_date,
            tipo_label,
            stage="empty_pre_button",
            classification="empty_result_detected_pre_button",
        )
        log.info("download_empty_confirmed_pre_button")
        return None

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
        artifacts = await _persist_page_artifacts(
            page,
            config,
            target_date,
            tipo_label,
            stage="missing_download_button",
            classification="download_button_not_found",
        )
        raise DownloadError(
            f"No se encontró el botón 'Descargar reporte'. "
            f"Screenshot: {artifacts['screenshot']}"
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
            classification = classify_payload(ct, cd, preview)
            jsf_debug.append({
                "method": request.method,
                "url": request.url[-80:],
                "status": response.status,
                "ct": ct[:60],
                "cd": cd[:60],
                "size": len(body),
                "preview": preview[:80],
                "classification": classification,
                "body": body,
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
        log.debug("jsf_route_response", **{k: v for k, v in entry.items() if k != "body"})

    if jsf_debug:
        artifact_paths = _persist_jsf_debug_artifacts(config, target_date, tipo_label, jsf_debug)
        log.info("jsf_route_artifacts_saved", tipo=tipo_label, files=artifact_paths)

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
    # Si el DOM tampoco tenía claves (estrategia 0 falló), lo más probable
    # es que este tipo de comprobante realmente no tenga datos para la fecha
    # consultada (el portal no muestra mensaje visible cuando el AJAX devuelve
    # 0 resultados para un tipo específico).
    # Retornar None permite que el bucle de tipos continúe con el siguiente.
    artifacts = await _persist_page_artifacts(
        page,
        config,
        target_date,
        tipo_label,
        stage="download_all_failed",
        classification="download_all_strategies_failed",
    )
    log.warning(
        "download_all_strategies_failed",
        note="tratando como tipo sin comprobantes para esta fecha",
        screenshot=artifacts["screenshot"],
    )
    return None
