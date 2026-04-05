#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import text

from api.app.config import settings
from api.app.database import get_control_engine, get_default_data_engine

_DATE_BANNER_RE = re.compile(r"^===== (?P<date>\d{4}-\d{2}-\d{2}) =====$")


@dataclass
class BackfillStatus:
    log_path: str | None
    pidfile_path: str | None
    pid: int | None
    pid_alive: bool
    log_size_bytes: int | None
    log_age_sec: int | None
    current_date: str | None
    last_line: str | None
    total_comprobantes: int | None
    recent_scrape_logs: list[dict[str, Any]]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Muestra el estado del backfill manual.")
    parser.add_argument("--tenant-id", type=int, default=1)
    parser.add_argument(
        "--pattern",
        default="backfill_tenant*.log",
        help="Glob relativo a WORKER_RUNTIME_ROOT para localizar el log.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Imprime el resultado como JSON.",
    )
    return parser.parse_args()


def _find_latest_log(runtime_root: Path, pattern: str) -> Path | None:
    matches = sorted(runtime_root.glob(pattern))
    return matches[-1] if matches else None


def _load_log_details(log_path: Path | None) -> tuple[int | None, int | None, str | None, str | None]:
    if log_path is None or not log_path.exists():
        return None, None, None, None

    stat = log_path.stat()
    lines = log_path.read_text(errors="replace").splitlines()
    current_date = None
    last_line = None

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        last_line = line
        match = _DATE_BANNER_RE.match(line)
        if match:
            current_date = match.group("date")

    return stat.st_size, int(time.time() - stat.st_mtime), current_date, last_line


def _load_pid(pidfile_path: Path | None) -> tuple[int | None, bool]:
    if pidfile_path is None or not pidfile_path.exists():
        return None, False

    try:
        pid = int(pidfile_path.read_text().strip())
    except ValueError:
        return None, False

    return pid, Path(f"/proc/{pid}").exists()


async def _query_database(tenant_id: int) -> tuple[int | None, list[dict[str, Any]]]:
    async with get_default_data_engine().begin() as conn:
        total = (
            await conn.execute(
                text("select count(*) from comprobantes where tenant_id = :tenant_id"),
                {"tenant_id": tenant_id},
            )
        ).scalar_one()

    async with get_control_engine().begin() as conn:
        rows = (
            await conn.execute(
                text(
                    """
                    select
                        id,
                        status,
                        started_at,
                        completed_at,
                        comprobantes_nuevos,
                        error_message
                    from scrape_logs
                    where tenant_id = :tenant_id
                    order by id desc
                    limit 5
                    """
                ),
                {"tenant_id": tenant_id},
            )
        ).mappings().all()

    recent_logs: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        for key in ("started_at", "completed_at"):
            value = item.get(key)
            if isinstance(value, datetime):
                item[key] = value.astimezone(timezone.utc).isoformat()
        recent_logs.append(item)

    return total, recent_logs


def _render_human(status: BackfillStatus) -> str:
    lines = [
        f"log: {status.log_path or '-'}",
        f"pid: {status.pid or '-'} ({'alive' if status.pid_alive else 'dead'})",
        f"log age: {status.log_age_sec if status.log_age_sec is not None else '-'}s",
        f"log size: {status.log_size_bytes if status.log_size_bytes is not None else '-'} bytes",
        f"current date: {status.current_date or '-'}",
        f"last line: {status.last_line or '-'}",
        f"tenant total: {status.total_comprobantes if status.total_comprobantes is not None else '-'}",
        "recent scrape_logs:",
    ]
    if status.recent_scrape_logs:
        for item in status.recent_scrape_logs:
            lines.append(
                f"  - id={item['id']} status={item['status']} nuevos={item['comprobantes_nuevos']} "
                f"started_at={item['started_at']} completed_at={item['completed_at']}"
            )
            if item.get("error_message"):
                lines.append(f"    error={item['error_message']}")
    else:
        lines.append("  - none")
    return "\n".join(lines)


async def _build_status(tenant_id: int, pattern: str) -> BackfillStatus:
    runtime_root = settings.worker_runtime_root
    log_path = _find_latest_log(runtime_root, pattern)
    pidfile_path = None
    if log_path is not None:
        pidfile_path = log_path.with_suffix(".pid")

    log_size_bytes, log_age_sec, current_date, last_line = _load_log_details(log_path)
    pid, pid_alive = _load_pid(pidfile_path)
    total_comprobantes, recent_scrape_logs = await _query_database(tenant_id)

    return BackfillStatus(
        log_path=str(log_path) if log_path else None,
        pidfile_path=str(pidfile_path) if pidfile_path else None,
        pid=pid,
        pid_alive=pid_alive,
        log_size_bytes=log_size_bytes,
        log_age_sec=log_age_sec,
        current_date=current_date,
        last_line=last_line,
        total_comprobantes=total_comprobantes,
        recent_scrape_logs=recent_scrape_logs,
    )


def main() -> None:
    args = _parse_args()
    status = asyncio.run(_build_status(args.tenant_id, args.pattern))
    if args.json:
        print(json.dumps(asdict(status), ensure_ascii=True, indent=2))
        return
    print(_render_human(status))


if __name__ == "__main__":
    main()
