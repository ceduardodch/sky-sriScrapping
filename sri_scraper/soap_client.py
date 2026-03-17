"""
Cliente SOAP para el WS de autorización de comprobantes electrónicos del SRI Ecuador.

Endpoints oficiales:
  Producción: https://cel.sri.gob.ec/comprobantes-electronicos-ws/AutorizacionComprobantesOffline?wsdl
  Pruebas:    https://celcer.sri.gob.ec/comprobantes-electronicos-ws/AutorizacionComprobantesOffline?wsdl

Método SOAP utilizado: autorizacionComprobante(claveAccesoComprobante: str)
El servicio devuelve el XML autorizado del comprobante electrónico.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log = structlog.get_logger(__name__)

# ── Endpoints oficiales SRI ────────────────────────────────────────────────────
WSDL_PRODUCCION = (
    "https://cel.sri.gob.ec/comprobantes-electronicos-ws/"
    "AutorizacionComprobantesOffline?wsdl"
)
WSDL_PRUEBAS = (
    "https://celcer.sri.gob.ec/comprobantes-electronicos-ws/"
    "AutorizacionComprobantesOffline?wsdl"
)


# ── Modelo de resultado ────────────────────────────────────────────────────────

@dataclass
class AutorizacionResult:
    """
    Resultado de la consulta de autorización de un comprobante electrónico.

    Campos mapeados desde la respuesta SOAP del SRI:
        estado              → "AUTORIZADO" | "NO AUTORIZADO" | "EN PROCESO" | "ERROR"
        numero_autorizacion → número de autorización asignado por el SRI
        fecha_autorizacion  → fecha/hora en que el SRI autorizó el comprobante
        ambiente            → "PRODUCCION" | "PRUEBAS"
        xml_comprobante     → XML completo del comprobante electrónico
        mensajes            → lista de mensajes/errores del SRI
        error               → descripción del error si estado == "ERROR"
    """
    clave_acceso: str
    estado: str                         # "AUTORIZADO", "NO AUTORIZADO", ...
    numero_autorizacion: str = ""
    fecha_autorizacion: Optional[str] = None
    ambiente: str = ""
    xml_comprobante: Optional[str] = None
    mensajes: list[dict] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def autorizado(self) -> bool:
        """True si el SRI autorizó el comprobante."""
        return self.estado == "AUTORIZADO"

    @property
    def tiene_xml(self) -> bool:
        """True si la respuesta contiene XML del comprobante."""
        return bool(self.xml_comprobante and self.xml_comprobante.strip())


# ── Cliente SOAP ───────────────────────────────────────────────────────────────

class SRISOAPClient:
    """
    Cliente SOAP liviano para el servicio de autorización de comprobantes SRI.

    Usa zeep para el transporte SOAP y tenacity para reintentos automáticos
    ante errores de transporte (timeouts, conexión caída, etc.).

    Ejemplo de uso::

        client = SRISOAPClient(ambiente="produccion")
        result = client.autorizar_comprobante("1234567890...")
        if result.autorizado:
            print(result.xml_comprobante)
    """

    def __init__(self, ambiente: str = "produccion", timeout: int = 30) -> None:
        """
        Args:
            ambiente: "produccion" o "pruebas". Determina el endpoint WSDL.
            timeout:  Timeout en segundos para la llamada HTTP al WS.
        """
        import zeep
        import zeep.transports

        wsdl = WSDL_PRODUCCION if ambiente == "produccion" else WSDL_PRUEBAS
        self.ambiente = ambiente
        self.timeout = timeout

        log.info("soap_client_init", wsdl=wsdl, ambiente=ambiente, timeout=timeout)

        settings = zeep.Settings(strict=False, xml_huge_tree=True)
        transport = zeep.transports.Transport(
            timeout=timeout,
            operation_timeout=timeout,
        )
        self._client = zeep.Client(wsdl=wsdl, settings=settings, transport=transport)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((ConnectionError, TimeoutError, OSError)),
        reraise=True,
    )
    def autorizar_comprobante(self, clave_acceso: str) -> AutorizacionResult:
        """
        Consulta la autorización de un comprobante por su clave de acceso.

        Llama a ``autorizacionComprobante(claveAccesoComprobante)`` en el WS
        del SRI y convierte la respuesta en un :class:`AutorizacionResult`.

        Args:
            clave_acceso: Clave de acceso de 49 dígitos numéricos.

        Returns:
            :class:`AutorizacionResult` con el estado y el XML del comprobante.
        """
        import zeep.exceptions

        log.info("soap_request_start", clave_prefix=clave_acceso[:15])

        try:
            respuesta = self._client.service.autorizacionComprobante(
                claveAccesoComprobante=clave_acceso
            )
        except zeep.exceptions.Fault as e:
            log.error("soap_fault", clave_prefix=clave_acceso[:15], fault=str(e))
            return AutorizacionResult(
                clave_acceso=clave_acceso,
                estado="ERROR",
                error=f"SOAP Fault: {e}",
            )
        except Exception as e:
            log.error("soap_exception", clave_prefix=clave_acceso[:15], error=str(e))
            return AutorizacionResult(
                clave_acceso=clave_acceso,
                estado="ERROR",
                error=f"{type(e).__name__}: {e}",
            )

        result = _parse_respuesta(clave_acceso, respuesta)
        log.info(
            "soap_request_done",
            clave_prefix=clave_acceso[:15],
            estado=result.estado,
            tiene_xml=result.tiene_xml,
        )
        return result


# ── Parsing de la respuesta SOAP ──────────────────────────────────────────────

def _parse_respuesta(clave_acceso: str, respuesta) -> AutorizacionResult:
    """
    Convierte el objeto zeep de respuesta en un :class:`AutorizacionResult`.

    La respuesta SOAP del SRI tiene la siguiente estructura::

        RespuestaAutorizacionComprobante
          claveAccesoConsultada: str
          numeroComprobantes:    str
          autorizaciones:
            autorizacion[]:
              estado:              str
              numeroAutorizacion:  str
              fechaAutorizacion:   datetime
              ambiente:            str
              comprobante:         str   ← XML completo
              mensajes:
                mensaje[]:
                  identificador:        str
                  mensaje:              str
                  informacionAdicional: str
                  tipo:                 str
    """
    try:
        autorizaciones = getattr(respuesta, "autorizaciones", None)
        if not autorizaciones:
            return AutorizacionResult(
                clave_acceso=clave_acceso,
                estado="SIN_AUTORIZACION",
                error="La respuesta no contiene el campo 'autorizaciones'",
            )

        lista = getattr(autorizaciones, "autorizacion", None)
        if not lista:
            return AutorizacionResult(
                clave_acceso=clave_acceso,
                estado="SIN_AUTORIZACION",
                error="La respuesta no contiene autorizaciones",
            )

        # El SRI siempre retorna una sola autorización por clave consultada
        auth = lista[0]

        # ── Mensajes ──────────────────────────────────────────────────────────
        mensajes: list[dict] = []
        msgs_wrapper = getattr(auth, "mensajes", None)
        if msgs_wrapper:
            msgs_lista = getattr(msgs_wrapper, "mensaje", None) or []
            for msg in msgs_lista:
                mensajes.append(
                    {
                        "identificador": str(getattr(msg, "identificador", "") or ""),
                        "mensaje": str(getattr(msg, "mensaje", "") or ""),
                        "informacionAdicional": str(
                            getattr(msg, "informacionAdicional", "") or ""
                        ),
                        "tipo": str(getattr(msg, "tipo", "") or ""),
                    }
                )

        # ── Fecha de autorización (puede ser datetime o str) ──────────────────
        fecha_auth = getattr(auth, "fechaAutorizacion", None)
        fecha_str = str(fecha_auth) if fecha_auth is not None else None

        # ── XML del comprobante ───────────────────────────────────────────────
        comprobante = getattr(auth, "comprobante", None)
        xml_str = str(comprobante).strip() if comprobante else None

        return AutorizacionResult(
            clave_acceso=clave_acceso,
            estado=str(getattr(auth, "estado", "DESCONOCIDO") or "DESCONOCIDO"),
            numero_autorizacion=str(
                getattr(auth, "numeroAutorizacion", "") or ""
            ),
            fecha_autorizacion=fecha_str,
            ambiente=str(getattr(auth, "ambiente", "") or ""),
            xml_comprobante=xml_str,
            mensajes=mensajes,
        )

    except Exception as e:
        log.error(
            "parse_respuesta_error",
            clave_prefix=clave_acceso[:15],
            error=str(e),
        )
        return AutorizacionResult(
            clave_acceso=clave_acceso,
            estado="ERROR",
            error=f"Error al parsear respuesta SOAP: {e}",
        )


# ── Guardar XML en disco ───────────────────────────────────────────────────────

def save_xml(result: AutorizacionResult, output_dir: Path) -> Optional[Path]:
    """
    Guarda el XML del comprobante en ``output_dir/{clave_acceso}.xml``.

    El XML se guarda con declaración UTF-8 si no la tiene ya.

    Args:
        result:     Resultado de la autorización (debe tener ``xml_comprobante``).
        output_dir: Directorio destino. Se crea si no existe.

    Returns:
        :class:`Path` del archivo creado, o ``None`` si no hay XML que guardar.
    """
    if not result.tiene_xml:
        log.warning(
            "save_xml_skipped",
            clave_prefix=result.clave_acceso[:15],
            razon="sin XML en la respuesta",
        )
        return None

    output_dir.mkdir(parents=True, exist_ok=True)
    xml_path = output_dir / f"{result.clave_acceso}.xml"

    xml_content = result.xml_comprobante  # type: ignore[assignment]

    # Agregar declaración XML si el SRI la omitió
    if not xml_content.startswith("<?xml"):
        xml_content = '<?xml version="1.0" encoding="UTF-8"?>\n' + xml_content

    xml_path.write_text(xml_content, encoding="utf-8")
    log.info(
        "xml_saved",
        path=str(xml_path),
        bytes=len(xml_content.encode("utf-8")),
        clave_prefix=result.clave_acceso[:15],
    )
    return xml_path
