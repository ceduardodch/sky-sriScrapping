from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import func, select

os.environ.setdefault("ADMIN_API_KEY", "admin-import-key")
os.environ.setdefault("FERNET_KEY", Fernet.generate_key().decode())

from api.app import crypto, database
from api.app.config import settings
from api.app.main import app
from api.app.models import Comprobante, ControlBase, DataBase

CLAVE_ACCESO = "2303202601179071031900129300010000138985658032315"
XML_FACTURA = f"""<?xml version="1.0" encoding="UTF-8"?>
<factura id="comprobante" version="1.1.0">
  <infoTributaria>
    <ambiente>2</ambiente>
    <tipoEmision>1</tipoEmision>
    <razonSocial>Proveedor Demo</razonSocial>
    <nombreComercial>Proveedor Demo</nombreComercial>
    <ruc>1790710319001</ruc>
    <claveAcceso>{CLAVE_ACCESO}</claveAcceso>
    <codDoc>01</codDoc>
    <estab>930</estab>
    <ptoEmi>001</ptoEmi>
    <secuencial>000013898</secuencial>
  </infoTributaria>
  <infoFactura>
    <fechaEmision>23/03/2026</fechaEmision>
    <razonSocialComprador>Cliente Demo</razonSocialComprador>
    <identificacionComprador>0999999999</identificacionComprador>
    <totalSinImpuestos>100.00</totalSinImpuestos>
    <importeTotal>112.00</importeTotal>
  </infoFactura>
  <totalConImpuestos>
    <totalImpuesto>
      <codigo>2</codigo>
      <valor>12.00</valor>
    </totalImpuesto>
  </totalConImpuestos>
  <detalles>
    <detalle>
      <codigoPrincipal>SKU-1</codigoPrincipal>
      <descripcion>Servicio demo</descripcion>
      <cantidad>1</cantidad>
      <precioUnitario>100.00</precioUnitario>
      <descuento>0.00</descuento>
      <precioTotalSinImpuesto>100.00</precioTotalSinImpuesto>
      <impuestos>
        <impuesto>
          <tarifa>12</tarifa>
          <valor>12.00</valor>
        </impuesto>
      </impuestos>
    </detalle>
  </detalles>
</factura>
"""


def _run(coro):
    return asyncio.run(coro)


async def _create_schema_for_url(storage_key: str) -> None:
    async with database.get_database_router().get_data_engine(storage_key).begin() as conn:
        await conn.run_sync(DataBase.metadata.create_all)


async def _create_control_schema() -> None:
    async with database.get_control_engine().begin() as conn:
        await conn.run_sync(ControlBase.metadata.create_all)


async def _count_comprobantes(storage_key: str) -> int:
    async with database.data_session_context(storage_key) as session:
        result = await session.execute(select(func.count()).select_from(Comprobante))
        return result.scalar_one()


@pytest.fixture
def app_env(tmp_path: Path):
    original = {
        "database_url": settings.database_url,
        "data_database_urls": dict(settings.data_database_urls),
        "admin_api_key": settings.admin_api_key,
        "fernet_key": settings.fernet_key,
    }

    control_url = f"sqlite+aiosqlite:///{tmp_path / 'control.db'}"
    default_data_url = f"sqlite+aiosqlite:///{tmp_path / 'data_default.db'}"
    secondary_data_url = f"sqlite+aiosqlite:///{tmp_path / 'data_secondary.db'}"

    settings.database_url = control_url
    settings.data_database_urls = {
        "default": default_data_url,
        "secondary": secondary_data_url,
    }
    settings.admin_api_key = "admin-test-key"
    settings.fernet_key = Fernet.generate_key().decode()
    crypto._fernet = None

    _run(
        database.reconfigure_database_router(
            control_url=control_url,
            data_urls=settings.resolved_data_database_urls(),
        )
    )
    _run(_create_control_schema())
    _run(_create_schema_for_url("default"))
    _run(_create_schema_for_url("secondary"))

    with TestClient(app) as client:
        yield {
            "client": client,
            "admin_headers": {"X-API-Key": settings.admin_api_key},
        }

    _run(database.dispose_engines())
    settings.database_url = original["database_url"]
    settings.data_database_urls = original["data_database_urls"]
    settings.admin_api_key = original["admin_api_key"]
    settings.fernet_key = original["fernet_key"]
    crypto._fernet = None
    _run(
        database.reconfigure_database_router(
            control_url=settings.database_url,
            data_urls=settings.resolved_data_database_urls(),
        )
    )


def _create_tenant(client: TestClient, headers: dict[str, str], **overrides) -> dict:
    payload = {
        "nombre": "Cliente Demo",
        "ruc": overrides.pop("ruc", "1790012345001"),
        "sri_password": "clave-sri",
        "ambiente": "PRODUCCION",
        "storage_key": "default",
    }
    payload.update(overrides)
    response = client.post("/admin/tenants", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def test_database_router_resolves_default_and_named_urls() -> None:
    router = database.DatabaseRouter(
        control_url="sqlite+aiosqlite:///control.db",
        data_urls={"secondary": "sqlite+aiosqlite:///secondary.db"},
    )
    assert str(router.get_control_engine().url) == "sqlite+aiosqlite:///control.db"
    assert str(router.get_data_engine("default").url) == "sqlite+aiosqlite:///control.db"
    assert str(router.get_data_engine("secondary").url) == "sqlite+aiosqlite:///secondary.db"
    _run(router.dispose())


def test_swagger_exposes_admin_cliente_and_health_paths(app_env) -> None:
    client = app_env["client"]

    response = client.get("/openapi.json")
    assert response.status_code == 200
    payload = response.json()
    security_schemes = payload["components"]["securitySchemes"]

    assert "/admin/tenants" in payload["paths"]
    assert "/admin/tenants/{tenant_id}/rotate-api-key" in payload["paths"]
    assert "/api/v1/comprobantes" in payload["paths"]
    assert "/health" in payload["paths"]
    assert security_schemes
    assert any(
        scheme.get("type") == "apiKey"
        and scheme.get("name") == "X-API-Key"
        and scheme.get("in") == "header"
        for scheme in security_schemes.values()
    )
    assert payload["paths"]["/admin/tenants"]["post"]["security"]
    assert payload["paths"]["/api/v1/comprobantes"]["get"]["security"]


def test_create_and_patch_tenant_with_storage_key(app_env) -> None:
    client = app_env["client"]
    admin_headers = app_env["admin_headers"]

    created = _create_tenant(client, admin_headers)
    assert created["storage_key"] == "default"

    updated = client.patch(
        f"/admin/tenants/{created['id']}",
        json={"nombre": "Cliente Ajustado", "storage_key": "secondary", "active": False},
        headers=admin_headers,
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["nombre"] == "Cliente Ajustado"
    assert body["storage_key"] == "secondary"
    assert body["active"] is False

    empty_patch = client.patch(
        f"/admin/tenants/{created['id']}",
        json={},
        headers=admin_headers,
    )
    assert empty_patch.status_code == 422

    invalid = client.patch(
        f"/admin/tenants/{created['id']}",
        json={"storage_key": "missing"},
        headers=admin_headers,
    )
    assert invalid.status_code == 400


def test_admin_and_tenant_keys_are_scoped(app_env) -> None:
    client = app_env["client"]
    admin_headers = app_env["admin_headers"]
    created = _create_tenant(client, admin_headers)

    tenant_admin = client.get(
        "/admin/tenants",
        headers={"X-API-Key": created["api_key"]},
    )
    assert tenant_admin.status_code == 403

    admin_cliente = client.get(
        "/api/v1/comprobantes",
        headers=admin_headers,
    )
    assert admin_cliente.status_code == 403


def test_rotate_api_key_invalidates_previous_key(app_env) -> None:
    client = app_env["client"]
    admin_headers = app_env["admin_headers"]

    created = _create_tenant(client, admin_headers)
    old_key = created["api_key"]

    rotated = client.post(
        f"/admin/tenants/{created['id']}/rotate-api-key",
        headers=admin_headers,
    )
    assert rotated.status_code == 200, rotated.text
    new_key = rotated.json()["api_key"]
    assert new_key != old_key

    old_resp = client.get("/api/v1/comprobantes", headers={"X-API-Key": old_key})
    assert old_resp.status_code == 403

    new_resp = client.get("/api/v1/comprobantes", headers={"X-API-Key": new_key})
    assert new_resp.status_code == 200
    assert new_resp.json()["total"] == 0


def test_trigger_and_scrape_logs_are_visible(app_env) -> None:
    client = app_env["client"]
    admin_headers = app_env["admin_headers"]

    created = _create_tenant(client, admin_headers)
    triggered = client.post(
        f"/admin/tenants/{created['id']}/trigger",
        headers=admin_headers,
    )
    assert triggered.status_code == 200, triggered.text

    logs = client.get(
        f"/admin/tenants/{created['id']}/scrape-logs",
        headers=admin_headers,
    )
    assert logs.status_code == 200
    items = logs.json()
    assert len(items) == 1
    assert items[0]["status"] == "pending"
    assert items[0]["tenant_id"] == created["id"]


def test_multi_tenant_isolation_and_same_clave_across_storages(app_env) -> None:
    client = app_env["client"]
    admin_headers = app_env["admin_headers"]

    tenant_default = _create_tenant(
        client,
        admin_headers,
        nombre="Cliente Default",
        ruc="1790012345001",
        storage_key="default",
    )
    tenant_secondary = _create_tenant(
        client,
        admin_headers,
        nombre="Cliente Secondary",
        ruc="1790012345002",
        storage_key="secondary",
    )

    for tenant in (tenant_default, tenant_secondary):
        response = client.post(
            "/api/v1/comprobantes",
            json={
                "tenant_id": tenant["id"],
                "clave_acceso": CLAVE_ACCESO,
                "xml_comprobante": XML_FACTURA,
                "estado": "AUTORIZADO",
                "numero_autorizacion": CLAVE_ACCESO,
                "fecha_autorizacion": "2026-03-23T10:15:00-05:00",
                "ambiente": "PRODUCCION",
            },
            headers=admin_headers,
        )
        assert response.status_code == 201, response.text

    assert _run(_count_comprobantes("default")) == 1
    assert _run(_count_comprobantes("secondary")) == 1

    list_default = client.get(
        "/api/v1/comprobantes",
        headers={"X-API-Key": tenant_default["api_key"]},
    )
    assert list_default.status_code == 200
    body_default = list_default.json()
    assert body_default["total"] == 1
    assert body_default["items"][0]["clave_acceso"] == CLAVE_ACCESO

    list_secondary = client.get(
        "/api/v1/comprobantes",
        headers={"X-API-Key": tenant_secondary["api_key"]},
    )
    assert list_secondary.status_code == 200
    body_secondary = list_secondary.json()
    assert body_secondary["total"] == 1
    assert body_secondary["items"][0]["clave_acceso"] == CLAVE_ACCESO

    get_single = client.get(
        f"/api/v1/comprobantes/{CLAVE_ACCESO}",
        headers={"X-API-Key": tenant_secondary["api_key"]},
    )
    assert get_single.status_code == 200
    assert get_single.json()["emisor"]["ruc"] == "1790710319001"
