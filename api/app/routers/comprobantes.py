"""
Endpoints para:
  - POST /api/v1/comprobantes         (worker interno)
  - GET  /api/v1/comprobantes         (clientes)
  - GET  /api/v1/comprobantes/{clave} (clientes)
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import verify_admin, verify_tenant
from ..database import data_session_context, get_control_session
from ..models import Comprobante, Tenant
from ..schemas import (
    ComprobanteOut,
    ComprobantePush,
    ComprobantesListOut,
    ErrorResponse,
    EmisorOut,
    ReceptorOut,
    TotalesOut,
)
from ..xml_parser import parse_xml

router = APIRouter(prefix="/api/v1", tags=["comprobantes"])


def _to_out(c: Comprobante) -> ComprobanteOut:
    serie = (
        f"{c.estab}-{c.pto_emi}-{c.secuencial}"
        if c.estab and c.pto_emi and c.secuencial
        else None
    )
    return ComprobanteOut(
        clave_acceso=c.clave_acceso,
        tipo_comprobante=c.tipo_comprobante,
        estado=c.estado,
        ambiente=c.ambiente,
        fecha_emision=c.fecha_emision,
        fecha_autorizacion=c.fecha_autorizacion,
        emisor=EmisorOut(
            ruc=c.ruc_emisor,
            razon_social=c.razon_social_emisor,
            nombre_comercial=c.nombre_comercial,
            serie=serie,
        ),
        receptor=ReceptorOut(
            identificacion=c.identificacion_receptor,
            razon_social=c.razon_social_receptor,
        ),
        totales=TotalesOut(
            subtotal=float(c.total_sin_impuestos) if c.total_sin_impuestos else None,
            iva=float(c.iva) if c.iva else None,
            total=float(c.importe_total) if c.importe_total else None,
        ),
        detalles=c.detalles or [],
        xml_raw=c.xml_raw,
        created_at=c.created_at,
    )


def _parse_fecha_autorizacion(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        from dateutil.parser import parse as _parse

        parsed = _parse(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


def _populate_comprobante(comp: Comprobante, body: ComprobantePush, parsed: dict) -> None:
    comp.numero_autorizacion = body.numero_autorizacion or None
    comp.estado = body.estado
    comp.ambiente = body.ambiente
    comp.fecha_autorizacion = _parse_fecha_autorizacion(body.fecha_autorizacion)
    comp.xml_raw = body.xml_comprobante
    comp.tipo_comprobante = parsed["tipo_comprobante"]
    comp.cod_doc = parsed["cod_doc"]
    comp.ruc_emisor = parsed["ruc_emisor"]
    comp.razon_social_emisor = parsed["razon_social_emisor"]
    comp.nombre_comercial = parsed["nombre_comercial"]
    comp.estab = parsed["estab"]
    comp.pto_emi = parsed["pto_emi"]
    comp.secuencial = parsed["secuencial"]
    comp.identificacion_receptor = parsed["identificacion_receptor"]
    comp.razon_social_receptor = parsed["razon_social_receptor"]
    comp.fecha_emision = parsed["fecha_emision"]
    comp.total_sin_impuestos = parsed["total_sin_impuestos"]
    comp.iva = parsed["iva"]
    comp.importe_total = parsed["importe_total"]
    comp.detalles = parsed["detalles"] or []


async def _find_existing_comprobante(
    session: AsyncSession,
    *,
    tenant_id: int,
    clave_acceso: str,
) -> Comprobante | None:
    result = await session.execute(
        select(Comprobante).where(
            Comprobante.tenant_id == tenant_id,
            Comprobante.clave_acceso == clave_acceso,
        )
    )
    return result.scalar_one_or_none()


_TENANT_ERRORS = {
    403: {"model": ErrorResponse, "description": "API key de tenant inválida o inactiva"},
    422: {"model": ErrorResponse, "description": "Parámetros inválidos"},
}


@router.post(
    "/comprobantes",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(verify_admin)],
    summary="[Interno] Recibir XML del worker",
    description="Inserta o actualiza un comprobante en el data store del cliente.",
    responses={
        200: {"description": "Comprobante ya existente — actualizado"},
        404: {"model": ErrorResponse, "description": "Tenant no encontrado"},
        422: {"model": ErrorResponse, "description": "XML inválido o parámetros incorrectos"},
        403: {"model": ErrorResponse, "description": "API key admin inválida"},
    },
)
async def push_comprobante(
    body: ComprobantePush,
    response: Response,
    control_session: AsyncSession = Depends(get_control_session),
) -> dict:
    try:
        parsed = parse_xml(body.xml_comprobante)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"XML inválido: {e}")

    tenant = await control_session.get(Tenant, body.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant no encontrado")

    async with data_session_context(tenant.storage_key) as data_session:
        comp = await _find_existing_comprobante(
            data_session,
            tenant_id=body.tenant_id,
            clave_acceso=body.clave_acceso,
        )
        created = comp is None

        if comp is None:
            comp = Comprobante(
                tenant_id=body.tenant_id,
                clave_acceso=body.clave_acceso,
            )
            data_session.add(comp)

        _populate_comprobante(comp, body, parsed)
        try:
            await data_session.commit()
        except IntegrityError:
            # Si dos procesos intentan insertar la misma clave al mismo tiempo,
            # la restricción única gana. Releemos y actualizamos el registro.
            await data_session.rollback()
            comp = await _find_existing_comprobante(
                data_session,
                tenant_id=body.tenant_id,
                clave_acceso=body.clave_acceso,
            )
            if comp is None:
                raise
            created = False
            _populate_comprobante(comp, body, parsed)
            await data_session.commit()

    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return {"ok": True, "created": created, "clave_acceso": body.clave_acceso}


@router.get(
    "/comprobantes",
    response_model=ComprobantesListOut,
    summary="Listar comprobantes del cliente autenticado",
    responses={**_TENANT_ERRORS},
    description=(
        "Consulta solo los comprobantes del tenant asociado a la API key enviada.\n\n"
        "Ejemplo:\n"
        "```bash\n"
        "curl 'http://127.0.0.1:8000/api/v1/comprobantes?fecha_desde=2026-03-23&limit=20' \\\n"
        "  -H 'X-API-Key: <TENANT_API_KEY>'\n"
        "```"
    ),
)
async def list_comprobantes(
    fecha_desde: date | None = Query(None),
    fecha_hasta: date | None = Query(None),
    tipo: str | None = Query(None, description="factura, notaCredito, comprobanteRetencion..."),
    ruc_emisor: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    tenant: Tenant = Depends(verify_tenant),
) -> ComprobantesListOut:
    async with data_session_context(tenant.storage_key) as session:
        q = select(Comprobante).where(Comprobante.tenant_id == tenant.id)

        if fecha_desde:
            q = q.where(Comprobante.fecha_emision >= fecha_desde)
        if fecha_hasta:
            q = q.where(Comprobante.fecha_emision <= fecha_hasta)
        if tipo:
            q = q.where(Comprobante.tipo_comprobante == tipo)
        if ruc_emisor:
            q = q.where(Comprobante.ruc_emisor == ruc_emisor)

        count_q = select(func.count()).select_from(q.subquery())
        total = (await session.execute(count_q)).scalar_one()

        items_q = q.order_by(Comprobante.fecha_emision.desc()).offset(offset).limit(limit)
        items = (await session.execute(items_q)).scalars().all()

    return ComprobantesListOut(
        total=total,
        limit=limit,
        offset=offset,
        items=[_to_out(c) for c in items],
    )


@router.get(
    "/comprobantes/{clave_acceso}",
    response_model=ComprobanteOut,
    summary="Obtener comprobante por clave de acceso",
    responses={
        404: {"model": ErrorResponse, "description": "Comprobante no encontrado"},
        **_TENANT_ERRORS,
    },
)
async def get_comprobante(
    clave_acceso: str,
    tenant: Tenant = Depends(verify_tenant),
) -> ComprobanteOut:
    async with data_session_context(tenant.storage_key) as session:
        result = await session.execute(
            select(Comprobante).where(
                Comprobante.tenant_id == tenant.id,
                Comprobante.clave_acceso == clave_acceso,
            )
        )
        comp = result.scalar_one_or_none()

    if not comp:
        raise HTTPException(status_code=404, detail="Comprobante no encontrado")
    return _to_out(comp)
