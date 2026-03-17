"""
Cliente HTTP para pushear XMLs autorizados a la SRI Comprobantes API.
"""
from __future__ import annotations

import httpx
import structlog

from .soap_client import AutorizacionResult

log = structlog.get_logger(__name__)


async def push_xml_to_api(
    result: AutorizacionResult,
    tenant_id: int,
    api_url: str,
    admin_api_key: str,
) -> bool:
    """
    Envía el XML de un comprobante autorizado a la API.

    Returns:
        True si se guardó correctamente (201 o ya existía 200/409).
    """
    if not result.tiene_xml:
        return False

    payload = {
        "tenant_id": tenant_id,
        "clave_acceso": result.clave_acceso,
        "xml_comprobante": result.xml_comprobante,
        "estado": result.estado,
        "numero_autorizacion": result.numero_autorizacion or "",
        "fecha_autorizacion": result.fecha_autorizacion,
        "ambiente": result.ambiente or "PRODUCCION",
    }

    try:
        async with httpx.AsyncClient(base_url=api_url, timeout=30) as client:
            resp = await client.post(
                "/api/v1/comprobantes",
                json=payload,
                headers={"X-API-Key": admin_api_key},
            )

        if resp.status_code in (200, 201):
            log.info("api_push_ok", clave=result.clave_acceso)
            return True
        elif resp.status_code == 409:
            log.debug("api_push_already_exists", clave=result.clave_acceso)
            return True
        else:
            log.warning("api_push_failed", clave=result.clave_acceso, status=resp.status_code, body=resp.text[:200])
            return False

    except httpx.RequestError as e:
        log.error("api_push_error", clave=result.clave_acceso, error=str(e))
        return False
