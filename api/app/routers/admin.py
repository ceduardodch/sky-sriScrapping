"""
Endpoints de administración: gestión de tenants y trigger manual de scraping.
Solo accesibles con X-API-Key: ADMIN_API_KEY
"""
from __future__ import annotations

import hashlib
import secrets

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import verify_admin
from ..crypto import encrypt
from ..database import get_session
from ..models import ScrapeLog, Tenant
from ..schemas import TenantCreate, TenantCreated, TenantOut, TriggerOut

router = APIRouter(prefix="/admin", tags=["admin"])


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@router.post(
    "/tenants",
    response_model=TenantCreated,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(verify_admin)],
    summary="Registrar nuevo tenant",
    description=(
        "Crea un tenant con sus credenciales SRI. "
        "El api_key se devuelve **una sola vez** — guárdalo, no se puede recuperar."
    ),
)
async def create_tenant(
    body: TenantCreate,
    session: AsyncSession = Depends(get_session),
) -> TenantCreated:
    # Verificar RUC no duplicado
    existing = await session.execute(select(Tenant).where(Tenant.ruc == body.ruc))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"RUC {body.ruc} ya registrado")

    api_key = secrets.token_urlsafe(32)
    tenant = Tenant(
        nombre=body.nombre,
        ruc=body.ruc,
        sri_password_enc=encrypt(body.sri_password),
        api_key_hash=_sha256(api_key),
        ambiente=body.ambiente,
    )
    session.add(tenant)
    await session.commit()
    await session.refresh(tenant)

    return TenantCreated(
        id=tenant.id,
        nombre=tenant.nombre,
        ruc=tenant.ruc,
        ambiente=tenant.ambiente,
        active=tenant.active,
        created_at=tenant.created_at,
        api_key=api_key,
    )


@router.get(
    "/tenants",
    response_model=list[TenantOut],
    dependencies=[Depends(verify_admin)],
    summary="Listar tenants",
)
async def list_tenants(session: AsyncSession = Depends(get_session)) -> list[TenantOut]:
    result = await session.execute(select(Tenant).order_by(Tenant.id))
    return [TenantOut.model_validate(t) for t in result.scalars()]


@router.post(
    "/tenants/{tenant_id}/trigger",
    response_model=TriggerOut,
    dependencies=[Depends(verify_admin)],
    summary="Disparar scraping inmediato para un tenant",
)
async def trigger_scrape(
    tenant_id: int,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> TriggerOut:
    tenant = await session.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant no encontrado")
    if not tenant.active:
        raise HTTPException(status_code=400, detail="Tenant inactivo")

    # Crear registro pendiente — el worker lo ejecuta en su próximo ciclo
    log = ScrapeLog(tenant_id=tenant_id, status="pending")
    session.add(log)
    await session.commit()
    await session.refresh(log)

    return TriggerOut(
        message="Scraping encolado — el worker lo ejecutará en segundos",
        tenant_id=tenant_id,
        log_id=log.id,
    )
