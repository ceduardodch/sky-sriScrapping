from __future__ import annotations
import enum

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import JSON as SAJSON
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import ControlBase, DataBase

_JSON_DETAILS = SAJSON().with_variant(JSONB, "postgresql")
_BIGINT_PK = BigInteger().with_variant(Integer, "sqlite")


class Tenant(ControlBase):
    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    nombre: Mapped[str] = mapped_column(Text, nullable=False)
    ruc: Mapped[str] = mapped_column(String(13), unique=True, nullable=False)
    sri_password_enc: Mapped[str] = mapped_column(Text, nullable=False)
    api_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    ambiente: Mapped[str] = mapped_column(String(20), default="PRODUCCION")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    storage_key: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="default",
        server_default="default",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    scrape_logs: Mapped[list[ScrapeLog]] = relationship(back_populates="tenant")


class Comprobante(DataBase):
    __tablename__ = "comprobantes"
    __table_args__ = (
        UniqueConstraint("tenant_id", "clave_acceso", name="uq_tenant_clave"),
        Index("ix_comprobantes_tenant_fecha", "tenant_id", "fecha_emision"),
        Index("ix_comprobantes_ruc_emisor", "tenant_id", "ruc_emisor"),
    )

    id: Mapped[int] = mapped_column(_BIGINT_PK, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False)

    # Identificación SRI
    clave_acceso: Mapped[str] = mapped_column(String(49), nullable=False)
    numero_autorizacion: Mapped[str | None] = mapped_column(String(49))
    estado: Mapped[str | None] = mapped_column(String(20))
    ambiente: Mapped[str | None] = mapped_column(String(20))
    fecha_autorizacion: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Tipo
    tipo_comprobante: Mapped[str | None] = mapped_column(String(30))
    cod_doc: Mapped[str | None] = mapped_column(String(2))

    # Emisor
    ruc_emisor: Mapped[str | None] = mapped_column(String(13))
    razon_social_emisor: Mapped[str | None] = mapped_column(Text)
    nombre_comercial: Mapped[str | None] = mapped_column(Text)
    estab: Mapped[str | None] = mapped_column(String(3))
    pto_emi: Mapped[str | None] = mapped_column(String(3))
    secuencial: Mapped[str | None] = mapped_column(String(9))

    # Receptor
    identificacion_receptor: Mapped[str | None] = mapped_column(String(20))
    razon_social_receptor: Mapped[str | None] = mapped_column(Text)

    # Valores
    fecha_emision: Mapped[date | None] = mapped_column(Date)
    total_sin_impuestos: Mapped[float | None] = mapped_column(Numeric(12, 4))
    iva: Mapped[float | None] = mapped_column(Numeric(12, 4))
    importe_total: Mapped[float | None] = mapped_column(Numeric(12, 4))

    # Detalles (líneas de la factura)
    detalles: Mapped[list | None] = mapped_column(_JSON_DETAILS)

    # XML original
    xml_raw: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )




class ScrapeLogStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
class ScrapeLog(ControlBase):
    __tablename__ = "scrape_logs"

    id: Mapped[int] = mapped_column(_BIGINT_PK, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    fecha_reporte: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="running")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    comprobantes_nuevos: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)

    tenant: Mapped[Tenant] = relationship(back_populates="scrape_logs")
