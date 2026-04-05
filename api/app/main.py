from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from sqlalchemy import text

from .database import control_session_context, dispose_engines, get_control_engine, get_default_data_engine
from .middleware import RequestIDMiddleware
from .routers import admin, clientes, comprobantes
from .schemas import HealthOut

tags_metadata = [
    {"name": "admin", "description": "Gestión de clientes/tenants y operación del scraper."},
    {"name": "comprobantes", "description": "Consulta e inserción interna de comprobantes electrónicos."},
    {"name": "clientes", "description": "Consulta de datos de clientes desde base maestra para facturación."},
    {"name": "health", "description": "Estado de la API y conectividad a bases de datos."},
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with get_control_engine().begin() as conn:
        await conn.execute(text("SELECT 1"))
    async with get_default_data_engine().begin() as conn:
        await conn.execute(text("SELECT 1"))
    yield
    await dispose_engines()


app = FastAPI(
    title="SRI Comprobantes API",
    description=(
        "API multi-cliente para comprobantes electrónicos SRI Ecuador.\n\n"
        "**Admin**: `X-API-Key: <ADMIN_API_KEY>` — gestión de clientes, corridas y trigger manual.\n\n"
        "**Clientes**: `X-API-Key: <api_key_del_tenant>` — consulta aislada de comprobantes."
    ),
    version="1.1.0",
    lifespan=lifespan,
    openapi_tags=tags_metadata,
    contact={"name": "Soporte Sky Tech", "email": "soporte@skytech.ec"},
    license_info={"name": "Propietario — uso interno"},
    servers=[
        {"url": "http://127.0.0.1:8000", "description": "Local / Docker"},
        {"url": "http://192.168.1.9:8000", "description": "Producción LAN"},
        {"url": "http://192.168.1.12:18000", "description": "Staging nativo"},
    ],
)

# RequestIDMiddleware debe agregarse después de CORS para ser el wrapper externo
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["*"],
)
app.add_middleware(RequestIDMiddleware)

app.include_router(admin.router)
app.include_router(comprobantes.router)
app.include_router(clientes.router)


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
        tags=tags_metadata,
    )
    schema.setdefault("components", {})
    schema["components"]["securitySchemes"] = {
        "AdminKey": {
            "type": "apiKey",
            "in": "header",
            "name": "X-API-Key",
            "description": "Clave admin — variable de entorno `ADMIN_API_KEY`",
        },
        "TenantKey": {
            "type": "apiKey",
            "in": "header",
            "name": "X-API-Key",
            "description": "Clave del tenant — devuelta al crear el cliente o al rotar la key",
        },
    }
    app.openapi_schema = schema
    return app.openapi_schema


app.openapi = custom_openapi


@app.get("/health", tags=["health"], response_model=HealthOut, summary="Estado de API y bases")
async def health() -> HealthOut:
    control_db = "ok"
    default_data_db = "ok"

    try:
        async with control_session_context() as session:
            await session.execute(text("SELECT 1"))
    except Exception as e:
        control_db = str(e)

    try:
        async with get_default_data_engine().begin() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as e:
        default_data_db = str(e)

    status_value = "ok" if control_db == "ok" and default_data_db == "ok" else "degraded"
    return HealthOut(
        status=status_value,
        db=control_db,
        control_db=control_db,
        default_data_db=default_data_db,
    )
