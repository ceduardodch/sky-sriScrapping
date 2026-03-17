from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from .database import AsyncSessionLocal, engine
from .routers import admin, comprobantes


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: verificar conexión a DB
    async with engine.begin() as conn:
        await conn.execute(text("SELECT 1"))
    yield
    await engine.dispose()


app = FastAPI(
    title="SRI Comprobantes API",
    description=(
        "API multi-tenant para comprobantes electrónicos SRI Ecuador.\n\n"
        "**Admin**: `X-API-Key: <ADMIN_API_KEY>` — gestión de tenants.\n\n"
        "**Clientes**: `X-API-Key: <tu-api-key>` — consulta de comprobantes."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.include_router(admin.router)
app.include_router(comprobantes.router)


@app.get("/health", tags=["health"], include_in_schema=False)
async def health() -> dict:
    try:
        async with AsyncSessionLocal() as s:
            await s.execute(text("SELECT 1"))
        db_status = "ok"
    except Exception as e:
        db_status = str(e)
    return {"status": "ok", "db": db_status}
