"""
Endpoints para:
  - POST /api/v1/comprobantes       (worker interno)
  - GET  /api/v1/comprobantes       (clientes)
  - GET  /api/v1/comprobantes/{clave} (clientes)
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import verify_admin, verify_tenant
from ..database import get_session
from ..models import Comprobante, Tenant
from ..schemas import (
    ComprobantePush,
    ComprobanteOut,
    ComprobantesListOut,
    EmisorOut,
    ReceptorOut,
    TotalesOut,
)
from ..xml_parser import parse_xml

router = APIRouter(prefix="/api/v1", tags=["comprobantes"])


# ── Conversión ORM → schema ────────────────────────────────────────────────────

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


# ── Worker → API: recibir XML ──────────────────────────────────────────────────

@router.post(
    "/comprobantes",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(verify_admin)],
    summary="[Interno] Recibir XML del worker",
    include_in_schema=True,
)
async def push_comprobante(
    body: ComprobantePush,
    session: AsyncSession = Depends(get_session),
) -> dict:
    # Parsear XML
    try:
        parsed = parse_xml(body.xml_comprobante)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"XML inválido: {e}")

    # Parsear fecha_autorizacion
    fecha_auth: datetime | None = None
    if body.fecha_autorizacion:
        try:
            from dateutil.parser import parse as _parse
            fecha_auth = _parse(body.fecha_autorizacion)
            if fecha_auth.tzinfo is None:
                fecha_auth = fecha_auth.replace(tzinfo=timezone.utc)
        except Exception:
            pass

    # Upsert (INSERT ... ON CONFLICT DO NOTHING + update)
    existing = await session.execute(
        select(Comprobante).where(
            Comprobante.tenant_id == body.tenant_id,
            Comprobante.clave_acceso == body.clave_acceso,
        )
    )
    comp = existing.scalar_one_or_none()

    if comp is None:
        comp = Comprobante(
            tenant_id=body.tenant_id,
            clave_acceso=body.clave_acceso,
        )
        session.add(comp)

    comp.numero_autorizacion = body.numero_autorizacion or None
    comp.estado = body.estado
    comp.ambiente = body.ambiente
    comp.fecha_autorizacion = fecha_auth
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

    await session.commit()
    return {"ok": True, "clave_acceso": body.clave_acceso}


# ── Clientes: GET comprobantes ─────────────────────────────────────────────────

@router.get(
    "/comprobantes",
    response_model=ComprobantesListOut,
    summary="Listar comprobantes del tenant autenticado",
)
async def list_comprobantes(
    fecha_desde: date | None = Query(None),
    fecha_hasta: date | None = Query(None),
    tipo: str | None = Query(None, description="factura, notaCredito, comprobanteRetencion..."),
    ruc_emisor: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    tenant: Tenant = Depends(verify_tenant),
    session: AsyncSession = Depends(get_session),
) -> ComprobantesListOut:
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
)
async def get_comprobante(
    clave_acceso: str,
    tenant: Tenant = Depends(verify_tenant),
    session: AsyncSession = Depends(get_session),
) -> ComprobanteOut:
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
