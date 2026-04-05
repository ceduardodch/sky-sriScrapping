"""
Endpoints de administración: gestión de tenants y trigger manual de scraping.
Solo accesibles con X-API-Key: ADMIN_API_KEY
"""
from __future__ import annotations

import hashlib
import secrets
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import verify_admin
from ..crypto import encrypt
from ..database import DatabaseRouterError, get_control_session, get_database_router
from ..models import ScrapeLog, ScrapeLogStatus, Tenant
from ..schemas import (
    ErrorResponse,
    ScrapeLogOut,
    TenantApiKeyRotated,
    TenantCreate,
    TenantCreated,
    TenantOut,
    TenantUpdate,
    TriggerOut,
)

router = APIRouter(prefix="/admin", tags=["admin"])

_ADMIN_ERRORS = {
    403: {"model": ErrorResponse, "description": "API key admin inválida"},
    422: {"model": ErrorResponse, "description": "Datos de entrada inválidos"},
}


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _issue_api_key() -> str:
    return secrets.token_urlsafe(32)


def _normalize_ambiente(value: str | None) -> str | None:
    return value.upper() if value is not None else None


def _ensure_storage_key_exists(storage_key: str) -> str:
    try:
        get_database_router().get_data_engine(storage_key)
    except DatabaseRouterError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return storage_key


async def _get_tenant_or_404(session: AsyncSession, tenant_id: int) -> Tenant:
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant no encontrado")
    return tenant


@router.post(
    "/tenants",
    response_model=TenantCreated,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(verify_admin)],
    summary="Registrar nuevo cliente",
    description=(
        "Crea un cliente/tenant con sus credenciales SRI. "
        "La `api_key` se devuelve una sola vez.\n\n"
        "Ejemplo:\n"
        "```bash\n"
        "curl -X POST http://127.0.0.1:8000/admin/tenants \\\n"
        "  -H 'X-API-Key: <ADMIN_API_KEY>' \\\n"
        "  -H 'Content-Type: application/json' \\\n"
        "  -d '{\n"
        "    \"nombre\": \"Cliente Demo\",\n"
        "    \"ruc\": \"1790012345001\",\n"
        "    \"sri_password\": \"clave-sri\",\n"
        "    \"ambiente\": \"PRODUCCION\",\n"
        "    \"storage_key\": \"default\"\n"
        "  }'\n"
        "```"
    ),
    responses={
        409: {"model": ErrorResponse, "description": "RUC ya registrado"},
        400: {"model": ErrorResponse, "description": "storage_key no configurado"},
        **_ADMIN_ERRORS,
    },
)
async def create_tenant(
    body: TenantCreate,
    session: AsyncSession = Depends(get_control_session),
) -> TenantCreated:
    existing = await session.execute(select(Tenant).where(Tenant.ruc == body.ruc))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"RUC {body.ruc} ya registrado")

    storage_key = _ensure_storage_key_exists(body.storage_key)
    api_key = _issue_api_key()
    tenant = Tenant(
        nombre=body.nombre,
        ruc=body.ruc,
        sri_password_enc=encrypt(body.sri_password),
        api_key_hash=_sha256(api_key),
        ambiente=_normalize_ambiente(body.ambiente) or "PRODUCCION",
        storage_key=storage_key,
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
        storage_key=tenant.storage_key,
        created_at=tenant.created_at,
        api_key=api_key,
    )


@router.get(
    "/tenants",
    response_model=List[TenantOut],
    dependencies=[Depends(verify_admin)],
    summary="Listar clientes",
    responses={**_ADMIN_ERRORS},
)
async def list_tenants(
    session: AsyncSession = Depends(get_control_session),
) -> List[TenantOut]:
    result = await session.execute(select(Tenant).order_by(Tenant.id))
    return [TenantOut.model_validate(t) for t in result.scalars()]


@router.get(
    "/tenants/{tenant_id}",
    response_model=TenantOut,
    dependencies=[Depends(verify_admin)],
    summary="Obtener cliente por ID",
    responses={
        404: {"model": ErrorResponse, "description": "Tenant no encontrado"},
        **_ADMIN_ERRORS,
    },
)
async def get_tenant(
    tenant_id: int,
    session: AsyncSession = Depends(get_control_session),
) -> TenantOut:
    tenant = await _get_tenant_or_404(session, tenant_id)
    return TenantOut.model_validate(tenant)


@router.patch(
    "/tenants/{tenant_id}",
    response_model=TenantOut,
    dependencies=[Depends(verify_admin)],
    summary="Actualizar cliente",
    responses={
        404: {"model": ErrorResponse, "description": "Tenant no encontrado"},
        400: {"model": ErrorResponse, "description": "storage_key no configurado"},
        **_ADMIN_ERRORS,
    },
)
async def update_tenant(
    tenant_id: int,
    body: TenantUpdate,
    session: AsyncSession = Depends(get_control_session),
) -> TenantOut:
    tenant = await _get_tenant_or_404(session, tenant_id)

    payload = body.model_dump(exclude_unset=True)
    if "nombre" in payload:
        tenant.nombre = payload["nombre"]
    if "sri_password" in payload:
        tenant.sri_password_enc = encrypt(payload["sri_password"])
    if "ambiente" in payload:
        tenant.ambiente = _normalize_ambiente(payload["ambiente"]) or tenant.ambiente
    if "active" in payload:
        tenant.active = payload["active"]
    if "storage_key" in payload:
        tenant.storage_key = _ensure_storage_key_exists(payload["storage_key"])

    await session.commit()
    await session.refresh(tenant)
    return TenantOut.model_validate(tenant)


@router.post(
    "/tenants/{tenant_id}/rotate-api-key",
    response_model=TenantApiKeyRotated,
    dependencies=[Depends(verify_admin)],
    summary="Rotar API key del cliente",
    description=(
        "Genera una nueva API key para el tenant y la devuelve una sola vez.\n\n"
        "Ejemplo:\n"
        "```bash\n"
        "curl -X POST http://127.0.0.1:8000/admin/tenants/1/rotate-api-key \\\n"
        "  -H 'X-API-Key: <ADMIN_API_KEY>'\n"
        "```"
    ),
    responses={
        404: {"model": ErrorResponse, "description": "Tenant no encontrado"},
        **_ADMIN_ERRORS,
    },
)
async def rotate_api_key(
    tenant_id: int,
    session: AsyncSession = Depends(get_control_session),
) -> TenantApiKeyRotated:
    tenant = await _get_tenant_or_404(session, tenant_id)

    api_key = _issue_api_key()
    tenant.api_key_hash = _sha256(api_key)
    await session.commit()

    return TenantApiKeyRotated(
        id=tenant.id,
        nombre=tenant.nombre,
        ruc=tenant.ruc,
        api_key=api_key,
    )


@router.post(
    "/tenants/{tenant_id}/trigger",
    response_model=TriggerOut,
    dependencies=[Depends(verify_admin)],
    summary="Disparar scraping inmediato para un cliente",
    description=(
        "Crea un `scrape_log` en estado `pending`; el worker lo recogerá en el siguiente ciclo.\n\n"
        "Ejemplo:\n"
        "```bash\n"
        "curl -X POST http://127.0.0.1:8000/admin/tenants/1/trigger \\\n"
        "  -H 'X-API-Key: <ADMIN_API_KEY>'\n"
        "```"
    ),
    responses={
        404: {"model": ErrorResponse, "description": "Tenant no encontrado"},
        400: {"model": ErrorResponse, "description": "Tenant inactivo"},
        **_ADMIN_ERRORS,
    },
)
async def trigger_scrape(
    tenant_id: int,
    session: AsyncSession = Depends(get_control_session),
) -> TriggerOut:
    tenant = await _get_tenant_or_404(session, tenant_id)
    if not tenant.active:
        raise HTTPException(status_code=400, detail="Tenant inactivo")

    log = ScrapeLog(tenant_id=tenant_id, status=ScrapeLogStatus.PENDING.value)
    session.add(log)
    await session.commit()
    await session.refresh(log)

    return TriggerOut(
        message="Scraping encolado — el worker lo ejecutará en segundos",
        tenant_id=tenant_id,
        log_id=log.id,
    )


@router.get(
    "/tenants/{tenant_id}/scrape-logs",
    response_model=List[ScrapeLogOut],
    dependencies=[Depends(verify_admin)],
    summary="Listar corridas de scraping del cliente",
    responses={
        404: {"model": ErrorResponse, "description": "Tenant no encontrado"},
        **_ADMIN_ERRORS,
    },
)
async def list_scrape_logs(
    tenant_id: int,
    limit: int = Query(20, ge=1, le=200),
    session: AsyncSession = Depends(get_control_session),
) -> List[ScrapeLogOut]:
    await _get_tenant_or_404(session, tenant_id)
    result = await session.execute(
        select(ScrapeLog)
        .where(ScrapeLog.tenant_id == tenant_id)
        .order_by(ScrapeLog.id.desc())
        .limit(limit)
    )
    return [ScrapeLogOut.model_validate(item) for item in result.scalars()]
