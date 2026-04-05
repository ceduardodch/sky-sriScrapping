"""
Navegación post-login dentro del portal SRI.

Lleva la página desde el dashboard hasta la sección
"Comprobantes electrónicos recibidos" en tuportal-internet.

DOM real del portal (PrimeNG p-panelMenu):
  - Secciones: .ui-panelmenu-header-link
  - Sub-items:  .ui-menuitem-link
  - Sección objetivo: "FACTURACIÓN ELECTRÓNICA" (índice 3)
  - Sub-item objetivo: "Comprobantes electrónicos recibidos"
    href: tuportal-internet/accederAplicacion.jspa?redireccion=57&idGrupo=55
"""

from __future__ import annotations

import structlog
from patchright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from .browser import human_delay
from .exceptions import NavigationError
from .login import assert_authenticated

log = structlog.get_logger(__name__)

TUPORTAL_RECIBIDOS_URL = (
    "https://srienlinea.sri.gob.ec/tuportal-internet/"
    "accederAplicacion.jspa?redireccion=57&idGrupo=55"
)
COMPROBANTES_RECIBIDOS_PATH = "comprobantes-electronicos-internet"
# Keep old constant for backwards compatibility
TUPORTAL_RECIBIDOS_PARAM = "redireccion=57"


async def go_to_comprobantes_recibidos(
    page: Page,
    timeout_ms: int,
    *,
    prefer_menu_first: bool = False,
) -> None:
    """
    Navega desde el dashboard a Comprobantes electrónicos recibidos.

    Estrategia:
      1. Si ya estamos en la página de comprobantes, no hace nada.
      2. Navega directamente via page.goto() al tuportal — el servidor redirige
         al portal JSF de comprobantes electrónicos recibidos con el token de sesión.
      3. Fallback: intenta navegar via menú (portal antiguo).

    Raises:
        SessionExpiredError: Si se detecta redirect al login.
        NavigationError: Si no se puede llegar a la sección.
    """
    await assert_authenticated(page)
    log.info("navigation_started", target="comprobantes_recibidos", url=page.url)

    # Si ya estamos en la página de comprobantes, no hacer nada
    if COMPROBANTES_RECIBIDOS_PATH in page.url:
        log.info("navigation_skipped", reason="already_in_section")
        return

    if prefer_menu_first:
        log.info("navigation_menu_first_enabled")
        try:
            await _wait_for_portal_ready(page, timeout_ms)
            await human_delay(1200, 2400)
            await _navigate_via_menu(page, timeout_ms)
            log.info("navigation_success", url=page.url, strategy="menu_first")
            return
        except Exception as e:
            log.warning("navigation_menu_first_failed", error=str(e), url=page.url)

    # Estrategia 1: Navegación directa (portal nuevo — redirige correctamente)
    try:
        log.info("navigation_direct_goto", url=TUPORTAL_RECIBIDOS_URL)
        await page.goto(TUPORTAL_RECIBIDOS_URL, wait_until="domcontentloaded", timeout=timeout_ms)
        await human_delay(1200, 2400)

        if COMPROBANTES_RECIBIDOS_PATH in page.url:
            log.info("navigation_success", url=page.url)
            return

        # Verificar si el contenido de la página indica éxito
        content = await page.content()
        if "Comprobantes electrónicos recibidos" in content or "comprobantesRecibidos" in page.url:
            log.info("navigation_success_by_content", url=page.url)
            return
    except Exception as e:
        log.warning("navigation_direct_failed", error=str(e))

    # Estrategia 2: Via menú (portal con sidebar)
    log.info("navigation_via_menu_fallback")
    await _wait_for_portal_ready(page, timeout_ms)
    await human_delay(1200, 2400)
    await _navigate_via_menu(page, timeout_ms)
    log.info("navigation_success", url=page.url)


async def _wait_for_portal_ready(page: Page, timeout_ms: int) -> None:
    """
    Espera a que la SPA Angular del SRI termine de cargar.

    Después del login el portal muestra "Espere por favor" mientras
    inicializa. Hay que esperar a que desaparezca antes de buscar el menú.
    """
    log.debug("waiting_for_portal_ready")

    loading_selectors = [
        "text=Espere por favor",
        "[class*='loading']",
        "[class*='spinner']",
        "[class*='loader']",
    ]
    for sel in loading_selectors:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=2_000):
                log.debug("loading_spinner_detected", selector=sel)
                await el.wait_for(state="hidden", timeout=timeout_ms)
                log.debug("loading_spinner_gone")
                break
        except PlaywrightTimeoutError:
            continue

    # Espera adicional para que Angular renderice el menú PrimeNG
    await human_delay(1500, 2500)
    log.debug("portal_ready")


async def _navigate_via_menu(page: Page, timeout_ms: int) -> None:
    """
    Navega al portal de Comprobantes Electrónicos Recibidos.

    Estrategia en cascada:
      1. Buscar link directo con href que contenga el parámetro de comprobantes recibidos
      2. Expandir sección "FACTURACIÓN ELECTRÓNICA" en el sidebar y clickear sub-item
      3. Abrir sidebar y volver a intentar estrategias anteriores
    """

    # ── Estrategia 1: Link directo en sidebar (nuevo portal con íconos) ───────
    direct_selectors = [
        f"a[href*='{TUPORTAL_RECIBIDOS_PARAM}']",
        "a[href*='accederAplicacion'][href*='redireccion=57']",
        "a[href*='comprobantes-electronicos'][href*='recibidos']",
    ]

    for sel in direct_selectors:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=3_000):
                log.debug("direct_link_found", selector=sel)
                await el.click()
                arrived = await _wait_for_tuportal(page, timeout_ms // 2)
                if arrived:
                    return
        except PlaywrightTimeoutError:
            continue

    # ── Estrategia 2: Abrir sidebar y buscar con texto ─────────────────────────
    await _ensure_sidebar_open(page, timeout_ms)
    await human_delay(600, 1000)

    # Intentar link directo otra vez después de abrir sidebar
    for sel in direct_selectors:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=3_000):
                log.debug("direct_link_found_after_sidebar", selector=sel)
                await el.click()
                arrived = await _wait_for_tuportal(page, timeout_ms // 2)
                if arrived:
                    return
        except PlaywrightTimeoutError:
            continue

    # ── Estrategia 3: Menú PrimeNG (portal antiguo) ────────────────────────────
    selectors_seccion = [
        "a.ui-panelmenu-header-link:has-text('FACTURACIÓN ELECTRÓNICA')",
        "a.ui-panelmenu-header-link:has-text('ELECTR')",
        ".ui-panelmenu-header-link:has-text('FACTUR')",
        "text=FACTURACIÓN ELECTRÓNICA",
    ]

    seccion = None
    for sel in selectors_seccion:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=3_000):
                seccion = el
                log.debug("menu_seccion_found", selector=sel)
                break
        except PlaywrightTimeoutError:
            continue

    if seccion is None:
        screenshot_path = "logs/nav_debug_menu.png"
        await page.screenshot(path=screenshot_path)
        raise NavigationError(
            f"No se encontró la sección 'FACTURACIÓN ELECTRÓNICA' en el menú. "
            f"Screenshot: {screenshot_path}. URL: {page.url}"
        )

    await seccion.click()
    await human_delay(600, 1200)

    selectors_subitem = [
        f"a[href*='{TUPORTAL_RECIBIDOS_PARAM}']",
        "a.ui-menuitem-link:has-text('recibidos')",
        ".ui-menuitem-link:has-text('Comprobantes electrónicos recibidos')",
        "text=Comprobantes electrónicos recibidos",
        "a:has-text('recibidos')",
    ]

    subitem = None
    for sel in selectors_subitem:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=5_000):
                subitem = el
                log.debug("menu_subitem_found", selector=sel)
                break
        except PlaywrightTimeoutError:
            continue

    if subitem is None:
        screenshot_path = "logs/nav_debug_submenu.png"
        await page.screenshot(path=screenshot_path)
        raise NavigationError(
            f"No se encontró el sub-item 'Comprobantes electrónicos recibidos'. "
            f"Screenshot: {screenshot_path}. URL: {page.url}"
        )

    await subitem.click()
    await human_delay(1500, 2500)

    # ── Paso 3: Esperar carga de tuportal-internet ────────────────────────────
    arrived = await _wait_for_tuportal(page, timeout_ms // 2)
    if not arrived:
        screenshot_path = "logs/nav_debug_tuportal.png"
        await page.screenshot(path=screenshot_path)
        raise NavigationError(
            f"El click en 'Comprobantes recibidos' no cargó tuportal-internet. "
            f"URL actual: {page.url}. Screenshot: {screenshot_path}"
        )


async def _ensure_sidebar_open(page: Page, timeout_ms: int) -> None:
    """
    Abre el sidebar si está colapsado.

    El portal usa w3-sidebar (#mySidebar). Por defecto está cerrado y necesita
    que el usuario haga click en el ícono hamburguesa para expandirlo.
    Los items del menú PrimeNG solo son visibles cuando el sidebar está abierto.
    """
    # Verificar si el menú ya está visible (sidebar abierto)
    try:
        el = page.locator(".ui-panelmenu-header-link").first
        if await el.is_visible(timeout=2_000):
            log.debug("sidebar_already_open")
            return
    except PlaywrightTimeoutError:
        pass

    # Buscar y clickear el botón hamburguesa
    hamburger_selectors = [
        "[class*='hamburguesa']",
        "span.tamano-icono-hamburguesa",
        "[class*='hamburger']",
        "[class*='menu-icon']",
        "#mySidebar ~ * [class*='icon']",
        # Fallback: cualquier span/button que abra el sidebar
        "span[class*='sri-menu-icon']",
    ]

    hamburger = None
    for sel in hamburger_selectors:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=2_000):
                hamburger = el
                log.debug("hamburger_found", selector=sel)
                break
        except PlaywrightTimeoutError:
            continue

    if hamburger is None:
        log.warning("hamburger_not_found", url=page.url)
        # Intentar click en el sidebar directamente para activarlo
        try:
            await page.locator("#mySidebar").click(timeout=2_000)
        except Exception:
            pass
        return

    # Esperar a que cualquier overlay de carga desaparezca antes del click
    try:
        await page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        pass

    try:
        await hamburger.click(timeout=10_000)
    except Exception:
        log.warning("hamburger_click_failed_trying_force")
        try:
            await hamburger.click(force=True, timeout=5_000)
        except Exception:
            # Último recurso: click via JavaScript
            try:
                await hamburger.evaluate("el => el.click()")
            except Exception as e:
                log.warning("hamburger_js_click_failed", error=str(e))
    await human_delay(500, 900)

    # Esperar a que los items del menú sean visibles
    try:
        await page.locator(".ui-panelmenu-header-link").first.wait_for(
            state="visible", timeout=5_000
        )
        log.debug("sidebar_opened")
    except PlaywrightTimeoutError:
        log.warning("sidebar_open_timeout")


async def _wait_for_tuportal(page: Page, timeout_ms: int) -> bool:
    """
    Espera hasta que tuportal-internet esté visible.

    El portal tuportal-internet es una aplicación JSP legacy distinta de la
    SPA Angular. Tiene su propio HTML con filtros de fecha y tabla de resultados.

    Retorna True si se detecta, False si expira el timeout.
    """
    # Indicadores de que estamos en tuportal-internet con comprobantes recibidos
    indicators = [
        # URL con el parámetro correcto
        f"**{TUPORTAL_RECIBIDOS_PARAM}**",
    ]

    # Primero verificar por URL (dos patrones posibles según versión del portal)
    url_patterns = [
        f"**{TUPORTAL_RECIBIDOS_PARAM}**",
        f"**{COMPROBANTES_RECIBIDOS_PATH}**",
        "**comprobantesRecibidos**",
    ]
    for pattern in url_patterns:
        try:
            await page.wait_for_url(pattern, timeout=timeout_ms // len(url_patterns))
            log.debug("tuportal_url_confirmed", url=page.url)
            return True
        except PlaywrightTimeoutError:
            continue

    # Fallback: verificar elementos de la página
    page_indicators = [
        # Elementos típicos del portal de comprobantes recibidos
        "text=Comprobantes Recibidos",
        "text=Fecha Inicio",
        "text=Fecha Fin",
        "input[name*='fecha']",
        "input[name*='Fecha']",
        "select[name*='tipo']",
        "button:has-text('Consultar')",
        "button:has-text('Buscar')",
        # Tabla de resultados
        "table.ui-datatable",
        "#tablaComprobantes",
        ".datatable",
    ]

    per_indicator = max(2_000, timeout_ms // len(page_indicators))
    for sel in page_indicators:
        try:
            el = page.locator(sel).first
            await el.wait_for(state="visible", timeout=per_indicator)
            log.debug("tuportal_element_confirmed", indicator=sel)
            return True
        except PlaywrightTimeoutError:
            continue

    return False
