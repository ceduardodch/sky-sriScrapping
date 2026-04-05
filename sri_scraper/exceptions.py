"""Jerarquía de excepciones del scraper SRI."""


class SRIScraperError(Exception):
    """Base — todos los errores del scraper heredan de aquí."""


class LoginError(SRIScraperError):
    """Autenticación fallida (credenciales incorrectas o forma de login inesperada)."""


class SessionExpiredError(SRIScraperError):
    """La sesión guardada ya no es válida — hay que hacer login de nuevo."""


class NavigationError(SRIScraperError):
    """No se pudo navegar a la sección esperada del portal."""


class DownloadError(SRIScraperError):
    """La descarga del reporte TXT no pudo completarse."""


class CaptchaChallengeError(DownloadError):
    """El portal rechazó la consulta por CAPTCHA y la respuesta no es confiable."""


class ParseError(SRIScraperError):
    """El archivo TXT descargado no pudo parsearse."""


class MaintenanceError(SRIScraperError):
    """El portal SRI está en mantenimiento — reintentar más tarde."""
