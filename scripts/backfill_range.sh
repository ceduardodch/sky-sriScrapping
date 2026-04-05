#!/usr/bin/env bash

set -uo pipefail

usage() {
  cat <<'EOF'
Uso:
  bash scripts/backfill_range.sh --tenant-id 1 --start 2026-03-03 --end 2026-03-24

Requisitos:
  - Ejecutar desde la raíz del repo
  - Tener el entorno Python activado
  - En Linux nativo, si HEADLESS=false, requiere xvfb-run disponible
EOF
}

TENANT_ID=""
START_DATE=""
END_DATE=""
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-1500}"
HEADLESS_VALUE="${HEADLESS:-false}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tenant-id)
      TENANT_ID="$2"
      shift 2
      ;;
    --start)
      START_DATE="$2"
      shift 2
      ;;
    --end)
      END_DATE="$2"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Argumento no reconocido: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ -z "$TENANT_ID" || -z "$START_DATE" || -z "$END_DATE" ]]; then
  usage >&2
  exit 1
fi

if [[ ! -f "run.py" ]]; then
  echo "Este script debe ejecutarse desde la raíz del repo." >&2
  exit 1
fi

if [[ -z "${VIRTUAL_ENV:-}" && -f ".venv-native/bin/activate" ]]; then
  # Permite usar el script directo en el server sin activar el entorno a mano.
  # shellcheck disable=SC1091
  source .venv-native/bin/activate
fi

RUNTIME_DIR="${WORKER_RUNTIME_ROOT:-runtime}"
mkdir -p "$RUNTIME_DIR"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG_FILE="$RUNTIME_DIR/backfill_tenant${TENANT_ID}_${START_DATE//-/}_${END_DATE//-/}_${STAMP}.log"
PID_FILE="${LOG_FILE%.log}.pid"
echo "$$" > "$PID_FILE"
trap 'rm -f "$PID_FILE"' EXIT

mapfile -t DATES < <(
  python - "$START_DATE" "$END_DATE" <<'PY'
from __future__ import annotations

import sys
from datetime import date, timedelta

start = date.fromisoformat(sys.argv[1])
end = date.fromisoformat(sys.argv[2])

current = start
while current <= end:
    print(current.isoformat())
    current += timedelta(days=1)
PY
)

if [[ ${#DATES[@]} -eq 0 ]]; then
  echo "No se generaron fechas para procesar." >&2
  exit 1
fi

echo "log_file=$LOG_FILE"

run_worker() {
  local day="$1"
  local rc=0
  local total_line=""

  {
    echo "===== $day ====="
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) starting tenant_id=$TENANT_ID timeout_seconds=$TIMEOUT_SECONDS"
  } >>"$LOG_FILE"

  if [[ "$HEADLESS_VALUE" == "false" ]]; then
    REPORT_DATE="$day" HEADLESS=false timeout -k 30s "${TIMEOUT_SECONDS}s" \
      xvfb-run -a python -m api.app.worker --tenant-id "$TENANT_ID" --once >>"$LOG_FILE" 2>&1
    rc=$?
  else
    REPORT_DATE="$day" HEADLESS="$HEADLESS_VALUE" timeout -k 30s "${TIMEOUT_SECONDS}s" \
      python -m api.app.worker --tenant-id "$TENANT_ID" --once >>"$LOG_FILE" 2>&1
    rc=$?
  fi

  total_line="$(
    python - "$TENANT_ID" <<'PY'
from __future__ import annotations

import asyncio
import sys

from sqlalchemy import text

from api.app.database import get_control_engine, get_default_data_engine


async def main(tenant_id: int) -> None:
    async with get_default_data_engine().begin() as conn:
        total = (
            await conn.execute(
                text("select count(*) from comprobantes where tenant_id = :tenant_id"),
                {"tenant_id": tenant_id},
            )
        ).scalar_one()

    async with get_control_engine().begin() as conn:
        row = (
            await conn.execute(
                text(
                    """
                    select id, status, comprobantes_nuevos, error_message
                    from scrape_logs
                    where tenant_id = :tenant_id
                    order by id desc
                    limit 1
                    """
                ),
                {"tenant_id": tenant_id},
            )
        ).mappings().first()

    if row is None:
        print(f"tenant_total={total} latest_log=none")
        return

    print(
        "tenant_total={total} latest_log_id={id} latest_status={status} "
        "latest_nuevos={nuevos} latest_error={error}".format(
            total=total,
            id=row["id"],
            status=row["status"],
            nuevos=row["comprobantes_nuevos"],
            error=(row["error_message"] or "-").replace("\n", " | "),
        )
    )


asyncio.run(main(int(sys.argv[1])))
PY
  )"

  {
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) worker_exit_code=$rc date=$day"
    echo "$total_line"
  } >>"$LOG_FILE"

  if [[ "$rc" -eq 124 ]]; then
    {
      echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) timeout_cleanup_started date=$day"
    } >>"$LOG_FILE"
    pkill -TERM -f "python -m api.app.worker --tenant-id $TENANT_ID --once" || true
    pkill -TERM -f "runtime/tenant_${TENANT_ID}/state/chrome_profile" || true
    sleep 5
    pkill -KILL -f "python -m api.app.worker --tenant-id $TENANT_ID --once" || true
    pkill -KILL -f "runtime/tenant_${TENANT_ID}/state/chrome_profile" || true
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) timeout_cleanup_done date=$day" >>"$LOG_FILE"
  fi
}

for day in "${DATES[@]}"; do
  run_worker "$day"
  sleep 5
done

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) backfill_range_done tenant_id=$TENANT_ID" >>"$LOG_FILE"
