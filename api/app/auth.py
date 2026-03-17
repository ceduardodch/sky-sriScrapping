"""
Dos dependencias de autenticación:
  - verify_admin  → compara header X-API-Key con ADMIN_API_KEY del env
  - verify_tenant → busca el tenant cuyo api_key_hash coincide con el header
"""
from __future__ import annotations

import hashlib

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .database import get_session
from .models import Tenant

_key_header = APIKeyHeader(name="X-API-Key", auto_error=True)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


async def verify_admin(api_key: str = Security(_key_header)) -> None:
    if api_key != settings.admin_api_key:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin key inválida")


async def verify_tenant(
    api_key: str = Security(_key_header),
    session: AsyncSession = Depends(get_session),
) -> Tenant:
    key_hash = _sha256(api_key)
    result = await session.execute(
        select(Tenant).where(Tenant.api_key_hash == key_hash, Tenant.active.is_(True))
    )
    tenant = result.scalar_one_or_none()
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="API key inválida")
    return tenant
