from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field


# ── Admin: Tenants ─────────────────────────────────────────────────────────────

class TenantCreate(BaseModel):
    nombre: str
    ruc: str = Field(pattern=r"^\d{10,13}$")
    sri_password: str
    ambiente: str = "PRODUCCION"


class TenantCreated(BaseModel):
    id: int
    nombre: str
    ruc: str
    ambiente: str
    active: bool
    created_at: datetime
    api_key: str  # Solo en la respuesta de creación, luego nunca más

    model_config = {"from_attributes": True}


class TenantOut(BaseModel):
    id: int
    nombre: str
    ruc: str
    ambiente: str
    active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Worker → API: push de XML ──────────────────────────────────────────────────

class ComprobantePush(BaseModel):
    tenant_id: int
    clave_acceso: str = Field(min_length=49, max_length=49)
    xml_comprobante: str
    estado: str = "AUTORIZADO"
    numero_autorizacion: str = ""
    fecha_autorizacion: str | None = None
    ambiente: str = "PRODUCCION"


# ── Clientes: respuesta comprobante ───────────────────────────────────────────

class EmisorOut(BaseModel):
    ruc: str | None
    razon_social: str | None
    nombre_comercial: str | None
    serie: str | None  # "002-902-000016115"


class ReceptorOut(BaseModel):
    identificacion: str | None
    razon_social: str | None


class TotalesOut(BaseModel):
    subtotal: float | None
    iva: float | None
    total: float | None


class ComprobanteOut(BaseModel):
    clave_acceso: str
    tipo_comprobante: str | None
    estado: str | None
    ambiente: str | None
    fecha_emision: date | None
    fecha_autorizacion: datetime | None
    emisor: EmisorOut
    receptor: ReceptorOut
    totales: TotalesOut
    detalles: list[dict[str, Any]] = []
    xml_raw: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ComprobantesListOut(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[ComprobanteOut]


# ── Trigger response ───────────────────────────────────────────────────────────

class TriggerOut(BaseModel):
    message: str
    tenant_id: int
    log_id: int
