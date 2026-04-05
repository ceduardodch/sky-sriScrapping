#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${REPO_DIR:-/home/b2b/apps/sky-sriScrapping}"
NATIVE_CLUSTER="${NATIVE_CLUSTER:-sky}"
NATIVE_PORT="${NATIVE_PORT:-15432}"

detect_pg_major() {
  local bin_dir
  bin_dir="$(find /usr/lib/postgresql -mindepth 1 -maxdepth 1 -type d | sort | tail -n 1)"
  basename "$bin_dir"
}

upsert_env_value() {
  local key="$1"
  local value="$2"
  python3 - "$REPO_DIR/.env" "$key" "$value" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
key = sys.argv[2]
value = sys.argv[3]
lines = path.read_text(encoding="utf-8").splitlines()
updated = False
new_lines = []
for line in lines:
    if line.startswith(f"{key}="):
        new_lines.append(f"{key}={value}")
        updated = True
    else:
        new_lines.append(line)
if not updated:
    new_lines.append(f"{key}={value}")
path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
PY
}

read_env_value() {
  local key="$1"
  python3 - "$REPO_DIR/.env" "$key" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
key = sys.argv[2]

for raw_line in path.read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    name, value = line.split("=", 1)
    if name == key:
        print(value)
        break
PY
}

main() {
  cd "$REPO_DIR"

  local db_password
  db_password="$(read_env_value DB_PASSWORD)"
  if [[ -z "$db_password" ]]; then
    echo "DB_PASSWORD no está configurado en $REPO_DIR/.env" >&2
    exit 1
  fi

  local pg_major
  pg_major="$(detect_pg_major)"

  if ! sudo pg_lsclusters | awk '{print $2}' | grep -qx "$NATIVE_CLUSTER"; then
    sudo pg_createcluster "$pg_major" "$NATIVE_CLUSTER" --port "$NATIVE_PORT"
  fi

  sudo sed -ri "s/^#?listen_addresses =.*/listen_addresses = '127.0.0.1'/" "/etc/postgresql/$pg_major/$NATIVE_CLUSTER/postgresql.conf"
  sudo sed -ri "s/^port = .*/port = $NATIVE_PORT/" "/etc/postgresql/$pg_major/$NATIVE_CLUSTER/postgresql.conf"
  sudo pg_ctlcluster "$pg_major" "$NATIVE_CLUSTER" restart

  sudo -u postgres psql -p "$NATIVE_PORT" -tc "SELECT 1 FROM pg_roles WHERE rolname = 'sri'" | grep -q 1 \
    || sudo -u postgres psql -p "$NATIVE_PORT" -c "CREATE ROLE sri LOGIN PASSWORD '${db_password}';"
  sudo -u postgres psql -p "$NATIVE_PORT" -tc "SELECT 1 FROM pg_database WHERE datname = 'sri_db'" | grep -q 1 \
    || sudo -u postgres psql -p "$NATIVE_PORT" -c "CREATE DATABASE sri_db OWNER sri;"

  DOCKER_HOST=unix:///var/run/docker.sock /usr/bin/docker exec -i sky-sriscrapping-db-1 \
    pg_dump -U sri -d sri_db --clean --if-exists --no-owner --no-privileges \
    | PGPASSWORD="${db_password}" psql -h 127.0.0.1 -p "$NATIVE_PORT" -U sri -d sri_db

  upsert_env_value DATABASE_URL "postgresql+asyncpg://sri:${db_password}@127.0.0.1:${NATIVE_PORT}/sri_db"
  upsert_env_value INTERNAL_API_URL "http://127.0.0.1:18000"
  upsert_env_value BROWSER_CHANNEL "chrome"
  upsert_env_value BROWSER_EXECUTABLE_PATH ""
  upsert_env_value WORKER_RUNTIME_ROOT "$REPO_DIR/runtime"

  source "$REPO_DIR/.venv-native/bin/activate"
  (
    cd "$REPO_DIR/api"
    DATABASE_URL="postgresql+asyncpg://sri:${db_password}@127.0.0.1:${NATIVE_PORT}/sri_db" alembic upgrade head
  )

  sudo systemctl restart sky-sri-api.service
  for _attempt in $(seq 1 15); do
    if curl --fail --silent http://127.0.0.1:18000/health >/dev/null; then
      break
    fi
    sleep 2
  done
  curl --fail --silent http://127.0.0.1:18000/health

  echo "PostgreSQL nativo listo en 127.0.0.1:${NATIVE_PORT}"
  echo "API nativa validada en http://127.0.0.1:18000/health"
}

main "$@"
