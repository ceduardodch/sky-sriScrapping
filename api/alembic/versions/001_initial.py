"""initial schema

Revision ID: 001
Revises:
Create Date: 2026-03-10
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("nombre", sa.Text(), nullable=False),
        sa.Column("ruc", sa.String(13), nullable=False, unique=True),
        sa.Column("sri_password_enc", sa.Text(), nullable=False),
        sa.Column("api_key_hash", sa.String(64), nullable=False),
        sa.Column("ambiente", sa.String(20), server_default="PRODUCCION"),
        sa.Column("active", sa.Boolean(), server_default="true"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
    )

    op.create_table(
        "comprobantes",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("clave_acceso", sa.String(49), nullable=False),
        sa.Column("numero_autorizacion", sa.String(49)),
        sa.Column("estado", sa.String(20)),
        sa.Column("ambiente", sa.String(20)),
        sa.Column("fecha_autorizacion", sa.DateTime(timezone=True)),
        sa.Column("tipo_comprobante", sa.String(30)),
        sa.Column("cod_doc", sa.String(2)),
        sa.Column("ruc_emisor", sa.String(13)),
        sa.Column("razon_social_emisor", sa.Text()),
        sa.Column("nombre_comercial", sa.Text()),
        sa.Column("estab", sa.String(3)),
        sa.Column("pto_emi", sa.String(3)),
        sa.Column("secuencial", sa.String(9)),
        sa.Column("identificacion_receptor", sa.String(20)),
        sa.Column("razon_social_receptor", sa.Text()),
        sa.Column("fecha_emision", sa.Date()),
        sa.Column("total_sin_impuestos", sa.Numeric(12, 4)),
        sa.Column("iva", sa.Numeric(12, 4)),
        sa.Column("importe_total", sa.Numeric(12, 4)),
        sa.Column("detalles", postgresql.JSONB()),
        sa.Column("xml_raw", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("tenant_id", "clave_acceso", name="uq_tenant_clave"),
    )
    op.create_index("ix_comprobantes_tenant_fecha", "comprobantes", ["tenant_id", "fecha_emision"])
    op.create_index("ix_comprobantes_ruc_emisor", "comprobantes", ["tenant_id", "ruc_emisor"])

    op.create_table(
        "scrape_logs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("fecha_reporte", sa.Date()),
        sa.Column("status", sa.String(20), server_default="running"),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("comprobantes_nuevos", sa.Integer(), server_default="0"),
        sa.Column("error_message", sa.Text()),
    )


def downgrade() -> None:
    op.drop_table("scrape_logs")
    op.drop_index("ix_comprobantes_ruc_emisor")
    op.drop_index("ix_comprobantes_tenant_fecha")
    op.drop_table("comprobantes")
    op.drop_table("tenants")
