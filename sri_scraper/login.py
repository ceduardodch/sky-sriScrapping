"""
Login al portal SRI en Línea.

Maneja autenticación, detección de sesión activa y persistencia de cookies.
Los selectores están ajustados a la SPA Angular del SRI (probados en el portal
real en modo headed — calibrar si el SRI actualiza el frontend).
"""

from __future__ import annotations

import structlog
from patchright.async_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .browser import human_delay, human_page_dwell, type_humanlike
from .config import SRIConfig
from .exceptions import LoginError, MaintenanceError, SessionExpiredError

log = structlog.get_logger(__name__)

SRI_BASE_URL = "https://srienlinea.sri.gob.ec/sri-en-linea/"
SRI_AUTH_ZONE = "/contribuyente/"

# Sub-aplicaciones del SRI a las que solo se puede acceder autenticado
SRI_AUTHENTICATED_ZONES = [
    "/contribuyente/",           # Angular SPA dashboard
    "tuportal-internet/",        # Portal legado JSP
    "comprobantes-electronicos-internet/",  # Portal JSF de comprobantes
]

# Frases que SOLO aparecen en pantallas de mantenimiento real (no en el portal normal)
MAINTENANCE_PHRASES = [
    "estamos realizando una actualización",
    "estamos en mantenimiento",
    "sistema en mantenimiento",
    "temporalmente fuera de servicio",
    "el servicio no está disponible temporalmente",
    "scheduled maintenance",
]


async def _is_maintenance(page: Page, logs_dir=None) -> bool:
    """
    Detecta si el portal está en ventana de mantenimiento.
    Usa frases largas para evitar falsos positivos con palabras sueltas
    como 'mantenimiento' que pueden aparecer en menus o footers normales.
    """
    try:
        content = (await page.content()).lower()
        detected = any(phrase in content for phrase in MAINTENANCE_PHRASES)
        if detected and logs_dir:
            # Guardar screenshot para diagnóstico
            import pathlib
            shot = str(pathlib.Path(logs_dir) / "maintenance_detected.png")
            try:
                await page.screenshot(path=shot)
                log.info("maintenance_screenshot_saved", path=shot)
            except Exception:
                pass
        return detected
    except Exception:
        return False


async def _is_authenticated(page: Page) -> bool:
    """Verifica si la página actual está en zona autenticada."""
    return any(zone in page.url for zone in SRI_AUTHENTICATED_ZONES)


async def assert_authenticated(page: Page) -> None:
    """Lanza SessionExpiredError si la página actual es el login."""
    if not await _is_authenticated(page):
        current = page.url
        raise SessionExpiredError(
            f"Sesión expirada — redirigido fuera de zona autenticada. URL actual: {current}"
        )


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=5, max=20),
    retry=retry_if_exception_type(PlaywrightTimeoutError),
    reraise=True,
)
async def _navigate_to_portal(page: Page, timeout_ms: int) -> None:
    """Navega al portal SRI con reintentos ante timeout."""
    await page.goto(SRI_BASE_URL, wait_until="domcontentloaded", timeout=timeout_ms)


async def login(context: BrowserContext, config: SRIConfig) -> Page:
    """
    Abre una página y realiza el login al portal SRI si es necesario.

    Si ya hay una sesión activa (cookies del perfil Chrome), omite el login.
    Retorna la Page autenticada lista para navegación posterior.

    Raises:
        LoginError: Si las credenciales son incorrectas o el formulario no responde.
        MaintenanceError: Si el portal muestra página de mantenimiento.
        SessionExpiredError: Si la sesión guardada expiró y el re-login falló.
    """
    ruc = config.sri_ruc
    password = config.sri_password.get_secret_value()

    log.info("login_attempt", ruc=ruc[:4] + "***")

    page = await context.new_page()
    page.set_default_timeout(config.page_timeout_ms)

    # ── Paso 1: Navegar al portal ─────────────────────────────────────────────
    await _navigate_to_portal(page, config.page_timeout_ms)
    await human_delay(800, 1500)
    await human_page_dwell(page, rounds=2)

    # ── Paso 2: Verificar mantenimiento ───────────────────────────────────────
    if await _is_maintenance(page, logs_dir=config.logs_dir):
        raise MaintenanceError("El portal SRI está en mantenimiento. Intenta más tarde.")

    # ── Paso 3: ¿Ya autenticado? ──────────────────────────────────────────────
    if await _is_authenticated(page):
        log.info("login_skipped", reason="session_already_active", url=page.url)
        return page

    # ── Paso 4: Buscar y hacer click en "Ingresar" ────────────────────────────
    await human_delay(500, 1000)
    ingresar_btn = None

    # Intentar múltiples selectores — el SRI puede cambiar el frontend
    selectors_ingresar = [
        "a:has-text('Ingresar')",
        "button:has-text('Ingresar')",
        "[href*='iniciar'], [href*='login']",
        "a:has-text('Iniciar sesión')",
        "button:has-text('Iniciar sesión')",
    ]

    for sel in selectors_ingresar:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=3_000):
                ingresar_btn = el
                log.debug("ingresar_btn_found", selector=sel)
                break
        except PlaywrightTimeoutError:
            continue

    if ingresar_btn is None:
        # Sesión guardada expirada o portal redirigió a página inesperada.
        # Limpiar cookies del contexto y reintentar desde cero.
        log.warning("ingresar_btn_not_found_clearing_session", url=page.url)
        await context.clear_cookies()

        # También eliminar el auth_state.json guardado
        import pathlib
        auth_state_path = pathlib.Path(config.state_dir) / "auth_state.json"
        try:
            if auth_state_path.exists():
                auth_state_path.unlink()
                log.info("auth_state_cleared", path=str(auth_state_path))
        except Exception:
            pass

        # Navegar de nuevo al portal con sesión limpia
        await page.goto(SRI_BASE_URL, wait_until="domcontentloaded", timeout=config.page_timeout_ms)
        await human_delay(1000, 2000)

        for sel in selectors_ingresar:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=3_000):
                    ingresar_btn = el
                    log.debug("ingresar_btn_found_after_clear", selector=sel)
                    break
            except PlaywrightTimeoutError:
                continue

        if ingresar_btn is None:
            log.warning("ingresar_btn_still_not_found", url=page.url)

    if ingresar_btn is not None:
        await ingresar_btn.hover()
        await human_delay(300, 700)
        await ingresar_btn.click()
        await human_delay(1000, 2000)

    # ── Paso 5: Esperar y rellenar el formulario de credenciales ──────────────
    # El formulario puede estar en la misma página o en una nueva tras el click
    await page.wait_for_load_state("domcontentloaded")

    # Selectores para el campo RUC/usuario (Angular Material — IDs son dinámicos)
    user_field = None
    selectors_ruc = [
        "input[id*='ruc']",
        "input[id*='usuario']",
        "input[placeholder*='RUC']",
        "input[placeholder*='Usuario']",
        "input[formcontrolname*='ruc']",
        "input[formcontrolname*='usuario']",
        "input[type='text']:visible",
    ]

    for sel in selectors_ruc:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=5_000):
                user_field = el
                log.debug("ruc_field_found", selector=sel)
                break
        except PlaywrightTimeoutError:
            continue

    if user_field is None:
        screenshot_path = str(config.logs_dir / "login_debug.png")
        await page.screenshot(path=screenshot_path)
        raise LoginError(
            f"No se encontró el campo de RUC/usuario. "
            f"Screenshot guardado en: {screenshot_path}"
        )

    # Rellenar RUC con pulsaciones humanas
    await type_humanlike(user_field, ruc)
    await human_delay(300, 700)

    # Selector para el campo de contraseña
    pass_field = None
    selectors_pass = [
        "input[type='password']",
        "input[id*='contrasena']",
        "input[id*='password']",
        "input[formcontrolname*='contrasena']",
        "input[formcontrolname*='password']",
    ]

    for sel in selectors_pass:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=3_000):
                pass_field = el
                log.debug("pass_field_found", selector=sel)
                break
        except PlaywrightTimeoutError:
            continue

    if pass_field is None:
        screenshot_path = str(config.logs_dir / "login_debug_pass.png")
        await page.screenshot(path=screenshot_path)
        raise LoginError(
            f"No se encontró el campo de contraseña. "
            f"Screenshot guardado en: {screenshot_path}"
        )

    await type_humanlike(pass_field, password)
    await human_delay(500, 1000)

    # ── Paso 6: Submit ────────────────────────────────────────────────────────
    submit_btn = None
    selectors_submit = [
        "button[type='submit']",
        "button:has-text('Ingresar')",
        "button:has-text('Iniciar sesión')",
        "input[type='submit']",
    ]

    for sel in selectors_submit:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=2_000):
                submit_btn = el
                break
        except PlaywrightTimeoutError:
            continue

    if submit_btn:
        await submit_btn.hover()
        await human_delay(300, 700)
        await submit_btn.click()
    else:
        # Fallback: Enter en el campo de contraseña
        await pass_field.press("Enter")

    # ── Paso 7: Esperar redirección a zona autenticada ────────────────────────
    try:
        await page.wait_for_url(
            f"**{SRI_AUTH_ZONE}**",
            timeout=config.page_timeout_ms,
        )
    except PlaywrightTimeoutError:
        # No hubo redirección — verificar si hay mensaje de error
        error_selectors = [
            "text=Credenciales incorrectas",
            "text=Usuario o contraseña incorrectos",
            "text=Usuario no encontrado",
            "[class*='error']:visible",
            "[class*='alert']:visible",
        ]
        error_msg = "Credenciales incorrectas o timeout esperando redirección"
        for sel in error_selectors:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=2_000):
                    error_msg = await el.inner_text()
                    break
            except Exception:
                continue

        screenshot_path = str(config.logs_dir / "login_failed.png")
        await page.screenshot(path=screenshot_path)
        raise LoginError(f"Login fallido: {error_msg.strip()} — screenshot: {screenshot_path}")

    log.info("login_success", url=page.url, ruc=ruc[:4] + "***")
    return page
