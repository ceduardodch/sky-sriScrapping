from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator

from .models import ScrapeLogStatus


class TenantCreate(BaseModel):
    nombre: str = Field(examples=["Cliente Demo"])
    ruc: str = Field(pattern=r"^\d{10,13}$", examples=["1790012345001"])
    sri_password: str = Field(examples=["clave-segura-sri"])
    ambiente: str = Field(default="PRODUCCION", examples=["PRODUCCION"])
    storage_key: str = Field(default="default", min_length=1, max_length=50, examples=["default"])

    model_config = {
        "json_schema_extra": {
            "example": {
                "nombre": "Cliente Demo",
                "ruc": "1790012345001",
                "sri_password": "clave-segura-sri",
                "ambiente": "PRODUCCION",
                "storage_key": "default",
            }
        }
    }


class TenantUpdate(BaseModel):
    nombre: Optional[str] = None
    sri_password: Optional[str] = None
    ambiente: Optional[str] = None
    active: Optional[bool] = None
    storage_key: Optional[str] = Field(default=None, min_length=1, max_length=50)

    @model_validator(mode="after")
    def validate_at_least_one_field(self) -> TenantUpdate:
        if not any(getattr(self, field) is not None for field in self.__class__.model_fields):
            raise ValueError("Debe enviarse al menos un campo para actualizar")
        return self

    model_config = {
        "json_schema_extra": {
            "example": {
                "nombre": "Cliente Demo Actualizado",
                "active": True,
                "storage_key": "default",
            }
        }
    }


class TenantOut(BaseModel):
    id: int
    nombre: str
    ruc: str
    ambiente: str
    active: bool
    storage_key: str
    created_at: datetime

    model_config = {"from_attributes": True}


class TenantCreated(TenantOut):
    api_key: str


class TenantApiKeyRotated(BaseModel):
    id: int
    nombre: str
    ruc: str
    api_key: str


class ScrapeLogOut(BaseModel):
    id: int
    tenant_id: int
    fecha_reporte: Optional[date]
    status: ScrapeLogStatus
    started_at: datetime
    completed_at: Optional[datetime]
    comprobantes_nuevos: int
    error_message: Optional[str]

    model_config = {"from_attributes": True}


class ComprobantePush(BaseModel):
    tenant_id: int
    clave_acceso: str = Field(min_length=49, max_length=49, examples=["2303202601179071031900129300010000138985658032315"])
    xml_comprobante: str
    estado: str = "AUTORIZADO"
    numero_autorizacion: str = ""
    fecha_autorizacion: Optional[str] = None
    ambiente: str = "PRODUCCION"

    model_config = {
        "json_schema_extra": {
            "example": {
                "tenant_id": 1,
                "clave_acceso": "2303202601179071031900129300010000138985658032315",
                "xml_comprobante": "<factura>...</factura>",
                "estado": "AUTORIZADO",
                "numero_autorizacion": "2303202601179071031900129300010000138985658032315",
                "fecha_autorizacion": "2026-03-23T10:15:00-05:00",
                "ambiente": "PRODUCCION",
            }
        }
    }


class EmisorOut(BaseModel):
    ruc: Optional[str]
    razon_social: Optional[str]
    nombre_comercial: Optional[str]
    serie: Optional[str]


class ReceptorOut(BaseModel):
    identificacion: Optional[str]
    razon_social: Optional[str]


class TotalesOut(BaseModel):
    subtotal: Optional[float]
    iva: Optional[float]
    total: Optional[float]


class ComprobanteOut(BaseModel):
    clave_acceso: str
    tipo_comprobante: Optional[str]
    estado: Optional[str]
    ambiente: Optional[str]
    fecha_emision: Optional[date]
    fecha_autorizacion: Optional[datetime]
    emisor: EmisorOut
    receptor: ReceptorOut
    totales: TotalesOut
    detalles: List[Dict[str, Any]] = Field(default_factory=list)
    xml_raw: Optional[str]
    created_at: datetime

    model_config = {"from_attributes": True}


class ComprobantesListOut(BaseModel):
    total: int
    limit: int
    offset: int
    items: List[ComprobanteOut]


class TriggerOut(BaseModel):
    message: str
    tenant_id: int
    log_id: int

    model_config = {
        "json_schema_extra": {
            "example": {
                "message": "Scraping encolado — el worker lo ejecutará en segundos",
                "tenant_id": 1,
                "log_id": 42,
            }
        }
    }


class HealthOut(BaseModel):
    status: str
    db: str
    control_db: str
    default_data_db: str


class ClienteFacturacionOut(BaseModel):
    """Datos básicos de un cliente para emisión de comprobantes electrónicos."""

    identity: str = Field(description="Cédula o RUC principal")
    full_name: Optional[str] = Field(default=None, description="Nombre completo")
    names: Optional[str] = Field(default=None, description="Nombres")
    lastnames: Optional[str] = Field(default=None, description="Apellidos")
    rucs: Optional[List[str]] = Field(default=None, description="RUCs asociados")
    emails: Optional[List[str]] = Field(default=None, description="Correos electrónicos")
    phones: Optional[List[str]] = Field(default=None, description="Teléfonos")
    latest_address: Optional[str] = Field(default=None, description="Dirección más reciente")
    latest_postcode: Optional[str] = Field(default=None, description="Código postal")
    province_code: Optional[str] = Field(default=None, description="Código de provincia")
    canton_code: Optional[str] = Field(default=None, description="Código de cantón")
    nationality: Optional[str] = Field(default=None, description="Nacionalidad")

    model_config = {
        "json_schema_extra": {
            "example": {
                "identity": "1713209771001",
                "full_name": "PÉREZ GÓMEZ JUAN CARLOS",
                "names": "JUAN CARLOS",
                "lastnames": "PÉREZ GÓMEZ",
                "rucs": ["1713209771001"],
                "emails": ["juan.perez@empresa.ec"],
                "phones": ["0991234567"],
                "latest_address": "AV. AMAZONAS N12-34 Y PATRIA",
                "latest_postcode": "170150",
                "province_code": "17",
                "canton_code": "1701",
                "nationality": "ECUATORIANA",
            }
        }
    }


class ErrorResponse(BaseModel):
    """Envelope estándar para errores de la API."""

    detail: str = Field(description="Mensaje de error legible")
    request_id: Optional[str] = Field(default=None, description="ID de correlación (X-Request-ID)")

    model_config = {
        "json_schema_extra": {
            "example": {
                "detail": "Tenant no encontrado",
                "request_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
            }
        }
    }
