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

import argparse
import asyncio
from datetime import datetime, timezone
from typing import Iterable, List

import structlog
from apscheduler.triggers.cron import CronTrigger

log = structlog.get_logger(__name__)

# ── Pipeline del scraper ───────────────────────────────────────────────────────


def _extract_clave_value(clave_info) -> str:
    """
    Normaliza una clave parseada para la fase SOAP.

    `extract_claves_from_file()` retorna instancias de `ClaveDeAcceso`, pero en
    algunos flujos antiguos también se manejaron dicts serializados. Si aquí
    usamos `str(clave_info)` sobre el dataclass, terminamos enviando al SOAP el
    repr completo del objeto en vez de los 49 dígitos de la clave.
    """
    if isinstance(clave_info, dict):
        return str(clave_info["clave"])

    raw_value = getattr(clave_info, "raw", None)
    if raw_value:
        return str(raw_value)

    return str(clave_info)


def _dedupe_claves(claves: Iterable[object]) -> List[object]:
    """Elimina claves repetidas dentro de una misma corrida conservando el orden."""
    uniques: List[object] = []
    seen: set[str] = set()

    for clave_info in claves:
        clave = _extract_clave_value(clave_info)
        if clave in seen:
            continue
        seen.add(clave)
        uniques.append(clave_info)

    return uniques


def _build_scrape_trigger(*, hour: int, minute: int, tz) -> CronTrigger:
    """
    Construye el cron del worker con zona horaria explícita.

    En algunos hosts Linux, `CronTrigger(hour=1, minute=0)` sin `timezone`
    termina interpretándose en UTC aunque el scheduler viva en
    `America/Guayaquil`, disparando a las 20:00 hora Ecuador del día previo.
    """
    return CronTrigger(hour=hour, minute=minute, timezone=tz)

async def run_scrape_for_tenant(tenant_id: int, log_id: int) -> None:
    """
    Ejecuta el pipeline completo del scraper para un tenant:
      1. Descifra contraseña SRI del tenant
      2. Login + descarga TXT del día anterior
      3. SOAP: obtiene XML por cada clave de acceso
      4. POST de cada XML a la API (localhost)

    Actualiza scrape_logs al terminar.
    """
    from .database import control_session_context
    from .models import ScrapeLog, Tenant
    from .crypto import decrypt
    from .config import settings

    async with control_session_context() as session:
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
    async with control_session_context() as session:
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

    Usa la configuración nativa del host o los defaults de Docker según el entorno.
    """
    from .config import settings
    from sri_scraper.config import SRIConfig
    from sri_scraper.pipeline import scrape_recibidos
    from sri_scraper.soap_client import SRISOAPClient

    run_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tenant_root = settings.worker_runtime_root / f"tenant_{tenant_id}"

    config = SRIConfig(
        sri_ruc=ruc,
        sri_password=sri_password,
        headless=settings.headless,
        browser_channel=settings.browser_channel,
        browser_executable_path=settings.browser_executable_path,
        downloads_dir=tenant_root / "downloads" / run_stamp,
        state_dir=tenant_root / "state",
        logs_dir=tenant_root / "logs" / run_stamp,
    )
    config.ensure_dirs()
    target_date = config.effective_report_date

    scrape_result = await scrape_recibidos(config)
    if scrape_result.txt_path is None:
        log.info("worker_no_comprobantes", tenant_id=tenant_id, date=str(target_date))
        return 0

    claves = _dedupe_claves(scrape_result.claves)
    if not claves:
        return 0
    if len(claves) != len(scrape_result.claves):
        log.info(
            "worker_claves_deduplicated",
            tenant_id=tenant_id,
            before=len(scrape_result.claves),
            after=len(claves),
        )

    # ── Fase 3: SOAP + push a API ──────────────────────────────────────────────
    import httpx

    soap = SRISOAPClient(ambiente=ambiente.lower())
    nuevos = 0

    async with httpx.AsyncClient(base_url=api_url, timeout=30) as client:
        for clave_info in claves:
            clave = _extract_clave_value(clave_info)
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
            elif resp.status_code in (200, 409):
                pass
            else:
                log.warning("worker_push_failed", clave=clave, status=resp.status_code)

    return nuevos


async def _run_once(tenant_id: int) -> None:
    """Ejecuta un scrape inmediato para un tenant sin iniciar el scheduler."""
    from .database import control_session_context
    from .models import ScrapeLog

    async with control_session_context() as session:
        scrape_log = ScrapeLog(tenant_id=tenant_id, status="running")
        session.add(scrape_log)
        await session.commit()
        await session.refresh(scrape_log)
        log_id = scrape_log.id

    log.info("worker_manual_run_started", tenant_id=tenant_id, log_id=log_id)
    await run_scrape_for_tenant(tenant_id, log_id)


# ── APScheduler (solo activo cuando el proceso es el worker) ──────────────────

def start_scheduler() -> None:
    """
    Inicia APScheduler con un job por tenant, escalonado 20 min entre cada uno.
    Corre de forma bloqueante (event loop propio).
    """
    import asyncio
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    async def _main() -> None:
        import pytz
        _tz = pytz.timezone("America/Guayaquil")
        scheduler = AsyncIOScheduler(timezone=_tz)

        async def run_pending_scrapes() -> None:
            """Ejecuta inmediatamente cualquier ScrapeLog con status='pending'."""
            from .database import control_session_context
            from .models import ScrapeLog
            from sqlalchemy import select
            async with control_session_context() as session:
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
            from .database import control_session_context
            from .models import Tenant
            from sqlalchemy import select

            async with control_session_context() as session:
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
                    from .database import control_session_context
                    from .models import ScrapeLog
                    async with control_session_context() as session:
                        scrape_log = ScrapeLog(tenant_id=tid, status="running")
                        session.add(scrape_log)
                        await session.commit()
                        await session.refresh(scrape_log)
                        log_id = scrape_log.id
                    await run_scrape_for_tenant(tid, log_id)

                scheduler.add_job(
                    _run,
                    _build_scrape_trigger(hour=hour, minute=minute, tz=_tz),
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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Worker APScheduler / ejecución manual del scraper.")
    parser.add_argument("--tenant-id", type=int, help="Tenant a ejecutar manualmente.")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Corre una sola ejecución para el tenant indicado en vez de iniciar el scheduler.",
    )
    args = parser.parse_args(argv)

    if args.once:
        if args.tenant_id is None:
            parser.error("--once requiere --tenant-id")
        asyncio.run(_run_once(args.tenant_id))
        return

    if args.tenant_id is not None:
        parser.error("--tenant-id solo se puede usar junto con --once")

    start_scheduler()


if __name__ == "__main__":
    main()
