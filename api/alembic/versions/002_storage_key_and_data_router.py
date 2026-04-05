"""add storage key and decouple comprobantes from control db

Revision ID: 002
Revises: 001
Create Date: 2026-03-25
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("storage_key", sa.String(length=50), nullable=False, server_default="default"),
    )

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for fk in inspector.get_foreign_keys("comprobantes"):
        if fk.get("referred_table") == "tenants" and fk.get("constrained_columns") == ["tenant_id"]:
            op.drop_constraint(fk["name"], "comprobantes", type_="foreignkey")


def downgrade() -> None:
    op.create_foreign_key(
        "comprobantes_tenant_id_fkey",
        "comprobantes",
        "tenants",
        ["tenant_id"],
        ["id"],
    )
    op.drop_column("tenants", "storage_key")
