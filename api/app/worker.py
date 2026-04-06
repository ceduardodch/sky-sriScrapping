"""
Worker APScheduler — ejecuta el scraper por cada tenant activo.

Cron: cada día a las 01:00 hora Ecuador, escalonado 20 min por tenant.
  Tenant 1 → 01:00
  Tenant 2 → 01:20
  Tenant 3 → 01:40  (etc.)

El worker también puede ejecutarse manualmente via POST /admin/tenants/{id}/trigger.

NOTA: Este módulo se importa tanto por el proceso worker como por FastAPI
(para el trigger manual via BackgroundTasks). Cuando corre como worker
independiente, el APScheduler está activo. Cuando corre como API, solo
se usa run_scrape_for_tenant().
"""
from __future__ import annotations

import asyncio
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

# ── Pipeline del scraper ───────────────────────────────────────────────────────

async def run_scrape_for_tenant(tenant_id: int, log_id: int) -> None:
    """
    Ejecuta el pipeline completo del scraper para un tenant:
      1. Descifra contraseña SRI del tenant
      2. Login + descarga TXT del día anterior
      3. SOAP: obtiene XML por cada clave de acceso
      4. POST de cada XML a la API (localhost)

    Actualiza scrape_logs al terminar.
    """
    from .database import AsyncSessionLocal
    from .models import ScrapeLog, Tenant
    from .crypto import decrypt
    from .config import settings

    async with AsyncSessionLocal() as session:
        tenant = await session.get(Tenant, tenant_id)
        if not tenant:
            log.error("worker_tenant_not_found", tenant_id=tenant_id)
            return

        sri_password = decrypt(tenant.sri_password_enc)
        log.info("worker_started", tenant_id=tenant_id, ruc=tenant.ruc)

    comprobantes_nuevos = 0
    error_msg: str | None = None

    try:
        comprobantes_nuevos = await _scrape_pipeline(
            tenant_id=tenant_id,
            ruc=tenant.ruc,
            sri_password=sri_password,
            ambiente=tenant.ambiente,
            api_url=settings.internal_api_url,
            admin_api_key=settings.admin_api_key,
        )
        status = "success"
    except Exception as e:
        log.error("worker_failed", tenant_id=tenant_id, error=str(e))
        error_msg = str(e)
        status = "failed"

    # Actualizar log
    async with AsyncSessionLocal() as session:
        scrape_log = await session.get(ScrapeLog, log_id)
        if scrape_log:
            scrape_log.status = status
            scrape_log.completed_at = datetime.now(timezone.utc)
            scrape_log.comprobantes_nuevos = comprobantes_nuevos
            scrape_log.error_message = error_msg
            await session.commit()

    log.info("worker_done", tenant_id=tenant_id, status=status, nuevos=comprobantes_nuevos)


async def _scrape_pipeline(
    tenant_id: int,
    ruc: str,
    sri_password: str,
    ambiente: str,
    api_url: str,
    admin_api_key: str,
) -> int:
    """
    Pipeline scraper → API. Retorna la cantidad de comprobantes nuevos.

    Importa sri_scraper que debe estar en el PYTHONPATH del worker.
    """
    # sri_scraper debe estar en el path (ver Dockerfile.worker)
    sys.path.insert(0, "/app")

    from sri_scraper.config import SRIConfig
    from sri_scraper.browser import browser_context
    from sri_scraper.login import login
    from sri_scraper.navigator import go_to_comprobantes_recibidos
    from sri_scraper.downloader import download_report
    from sri_scraper.parser import extract_claves_from_file
    from sri_scraper.soap_client import SRISOAPClient

    target_date = date.today()  # descarga el día de ayer (config lo maneja)

    config = SRIConfig(
        sri_ruc=ruc,
        sri_password=sri_password,
        headless=True,
        browser_channel="",   # Chromium bundled (en Docker)
        downloads_dir=Path(f"/tmp/sri_{tenant_id}_{target_date}"),
        state_dir=Path(f"/tmp/sri_state_{tenant_id}"),
        logs_dir=Path(f"/tmp/sri_logs_{tenant_id}"),
    )
    config.ensure_dirs()

    # ── Fase 1: Browser → TXT ──────────────────────────────────────────────────
    txt_path: Path | None = None
    async with browser_context(config) as ctx:
        page = await login(ctx, config)
        await go_to_comprobantes_recibidos(page, config.page_timeout_ms)
        txt_path = await download_report(page, config)

    if txt_path is None:
        log.info("worker_no_comprobantes", tenant_id=tenant_id, date=str(target_date))
        return 0

    # ── Fase 2: Parse claves ───────────────────────────────────────────────────
    claves = extract_claves_from_file(txt_path)
    if not claves:
        return 0

    # ── Fase 3: SOAP + push a API ──────────────────────────────────────────────
    import httpx

    soap = SRISOAPClient(ambiente=ambiente.lower())
    nuevos = 0

    async with httpx.AsyncClient(base_url=api_url, timeout=30) as client:
        for clave_info in claves:
            clave = clave_info["clave"] if isinstance(clave_info, dict) else str(clave_info)
            result = soap.autorizar_comprobante(clave)

            if not result.tiene_xml:
                log.warning("worker_no_xml", clave=clave, estado=result.estado)
                continue

            resp = await client.post(
                "/api/v1/comprobantes",
                json={
                    "tenant_id": tenant_id,
                    "clave_acceso": result.clave_acceso,
                    "xml_comprobante": result.xml_comprobante,
                    "estado": result.estado,
                    "numero_autorizacion": result.numero_autorizacion,
                    "fecha_autorizacion": result.fecha_autorizacion,
                    "ambiente": result.ambiente,
                },
                headers={"X-API-Key": admin_api_key},
            )
            if resp.status_code == 201:
                nuevos += 1
            elif resp.status_code == 409:
                pass  # ya existía
            else:
                log.warning("worker_push_failed", clave=clave, status=resp.status_code)

    return nuevos


# ── APScheduler (solo activo cuando el proceso es el worker) ──────────────────

def start_scheduler() -> None:
    """
    Inicia APScheduler con un job por tenant, escalonado 20 min entre cada uno.
    Corre de forma bloqueante (event loop propio).
    """
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger

    async def _main() -> None:
        import pytz
        _tz = pytz.timezone("America/Guayaquil")
        scheduler = AsyncIOScheduler(timezone=_tz)

        async def run_pending_scrapes() -> None:
            """Ejecuta inmediatamente cualquier ScrapeLog con status='pending'."""
            from .database import AsyncSessionLocal
            from .models import ScrapeLog
            from sqlalchemy import select
            async with AsyncSessionLocal() as session:
                pending = (await session.execute(
                    select(ScrapeLog).where(ScrapeLog.status == "pending")
                )).scalars().all()
                for scrape_log in pending:
                    scrape_log.status = "running"
                await session.commit()
            for scrape_log in pending:
                log.info("worker_trigger_pending", log_id=scrape_log.id, tenant_id=scrape_log.tenant_id)
                await run_scrape_for_tenant(scrape_log.tenant_id, scrape_log.id)

        async def schedule_tenants() -> None:
            """Reagenda jobs al inicio y cada 6 horas para detectar nuevos tenants."""
            from .database import AsyncSessionLocal
            from .models import Tenant
            from sqlalchemy import select

            async with AsyncSessionLocal() as session:
                tenants = (await session.execute(
                    select(Tenant).where(Tenant.active.is_(True)).order_by(Tenant.id)
                )).scalars().all()

            # Limpiar jobs anteriores de scraping
            for job in scheduler.get_jobs():
                if job.id.startswith("scrape_"):
                    job.remove()

            for i, tenant in enumerate(tenants):
                hour = 1
                minute = i * 20
                if minute >= 60:
                    hour += minute // 60
                    minute = minute % 60

                async def _run(tid=tenant.id):
                    from .database import AsyncSessionLocal
                    from .models import ScrapeLog
                    async with AsyncSessionLocal() as session:
                        scrape_log = ScrapeLog(tenant_id=tid, status="running")
                        session.add(scrape_log)
                        await session.commit()
                        await session.refresh(scrape_log)
                        log_id = scrape_log.id
                    await run_scrape_for_tenant(tid, log_id)

                scheduler.add_job(
                    _run,
                    CronTrigger(hour=hour, minute=minute),
                    id=f"scrape_{tenant.id}",
                    replace_existing=True,
                )
                log.info("job_scheduled", tenant_id=tenant.id, at=f"{hour:02d}:{minute:02d}")

        # Reagendar tenants al inicio y cada 6 horas
        scheduler.add_job(schedule_tenants, "interval", hours=6, id="reschedule", next_run_time=datetime.now(_tz))
        # Polling de scrapes pendientes (trigger manual via API)
        scheduler.add_job(run_pending_scrapes, "interval", seconds=30, id="pending_poll", next_run_time=datetime.now(_tz))
        scheduler.start()

        try:
            await asyncio.Event().wait()
        except (KeyboardInterrupt, SystemExit):
            scheduler.shutdown()

    asyncio.run(_main())


if __name__ == "__main__":
    start_scheduler()
