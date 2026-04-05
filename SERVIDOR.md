# Conexión al Servidor sky-sriScrapping

## SSH (sin contraseña — llave configurada)

```bash
ssh b2b@192.168.1.9
```

La llave privada está en `~/.ssh/id_ed25519` (tu Mac). No pedirá contraseña.

---

## Stack Docker

Ubicación del proyecto en el servidor: `~/apps/sky-sriScrapping`

> **NOTA**: Usar `DOCKER_HOST=unix:///var/run/docker.sock /usr/bin/docker` porque Docker Desktop coexiste en el servidor y puede interceptar el comando `docker` del PATH.

### Comandos básicos

```bash
# Alias recomendado (agregar a ~/.bashrc del servidor)
alias dk='DOCKER_HOST=unix:///var/run/docker.sock /usr/bin/docker'

# Ver estado de los contenedores
dk ps

# Ver logs en tiempo real
dk logs -f sky-sriscrapping-api-1
dk logs -f sky-sriscrapping-worker-1

# Parar un contenedor
dk stop sky-sriscrapping-worker-1

# Iniciar un contenedor parado
dk start sky-sriscrapping-worker-1
```

### Servicios activos

| Servicio | Puerto | Descripción |
|---------|--------|-------------|
| `db`     | 5432   | PostgreSQL 16 |
| `api`    | 8000   | FastAPI REST |
| `worker` | —      | Scraper APScheduler (01:00 AM Ecuador) |

---

## API REST

Base URL: `http://192.168.1.9:8000`

Documentación Swagger: `http://192.168.1.9:8000/docs`

### Endpoints

| Método | Ruta | Auth | Descripción |
|--------|------|------|-------------|
| GET | `/docs` | — | Swagger UI |
| GET | `/admin/tenants` | X-API-Key | Listar tenants |
| POST | `/admin/tenants` | X-API-Key | Crear tenant |
| POST | `/admin/tenants/{id}/trigger` | X-API-Key | Ejecutar scrape manual |
| GET | `/api/v1/comprobantes` | X-API-Key tenant | Listar comprobantes |

### ADMIN_API_KEY

```
5b0hQO6fb--7SaIZGYpO72D_sM8Pq1mn4dINyQOLK44
```

Ejemplo:
```bash
curl -H "X-API-Key: 5b0hQO6fb--7SaIZGYpO72D_sM8Pq1mn4dINyQOLK44" \
  http://192.168.1.9:8000/admin/tenants
```

---

## PostgreSQL (pgAdmin / DBeaver)

Conéctate directamente desde tu Mac:

| Campo | Valor |
|-------|-------|
| Host | `192.168.1.9` |
| Puerto | `5432` |
| Base de datos | `sri_db` |
| Usuario | `sri` |
| Contraseña | `TestPass123` |

El puerto 5432 está abierto en el servidor (expuesto en docker-compose).

---

## Variables de entorno (.env en el servidor)

Archivo: `~/apps/sky-sriScrapping/.env`

```bash
# Ver el contenido
cat ~/apps/sky-sriScrapping/.env

# Editar
nano ~/apps/sky-sriScrapping/.env
```

### Variables clave

| Variable | Valor |
|---------|-------|
| `DB_PASSWORD` | `TestPass123` |
| `ADMIN_API_KEY` | `5b0hQO6fb--7SaIZGYpO72D_sM8Pq1mn4dINyQOLK44` |
| `FERNET_KEY` | `TpIGiSzj2OnggLiMIw9S4UGWnqomESa9nKKQkMCZJh8=` |
| `SRI_RUC` | `1713209771001` |
| `HEADLESS` | `true` |

---

## Monitoreo del Worker (scraping)

```bash
alias dk='DOCKER_HOST=unix:///var/run/docker.sock /usr/bin/docker'

# Ver si el scheduler está corriendo
dk logs sky-sriscrapping-worker-1 --tail=20

# Ver si hizo scraping hoy
dk logs sky-sriscrapping-worker-1 | grep "scrape"

# Forzar scraping manual (vía API)
curl -X POST \
  -H "X-API-Key: 5b0hQO6fb--7SaIZGYpO72D_sM8Pq1mn4dINyQOLK44" \
  http://192.168.1.9:8000/admin/tenants/1/trigger

# Ver logs de scrape en BD (desde tu Mac)
PGPASSWORD=TestPass123 psql -h 192.168.1.9 -U sri -d sri_db \
  -c "SELECT id, tenant_id, status, started_at, completed_at, comprobantes_nuevos, LEFT(error_message,100) FROM scrape_logs ORDER BY id DESC LIMIT 10;"

# Ver comprobantes descargados (desde tu Mac)
PGPASSWORD=TestPass123 psql -h 192.168.1.9 -U sri -d sri_db \
  -c "SELECT id, tenant_id, clave_acceso, estado, fecha_autorizacion FROM comprobantes ORDER BY id DESC LIMIT 10;"
```

---

## Actualizar código en el servidor

```bash
ssh b2b@192.168.1.9
cd ~/apps/sky-sriScrapping
git pull

# Reconstruir imágenes con nuevo código
DOCKER_HOST=unix:///var/run/docker.sock /usr/bin/docker build -f api/Dockerfile -t sky-sriscrapping-api:latest .
DOCKER_HOST=unix:///var/run/docker.sock /usr/bin/docker build -f api/Dockerfile.worker -t sky-sriscrapping-worker:latest .

# Reiniciar contenedores con nueva imagen
DOCKER_HOST=unix:///var/run/docker.sock /usr/bin/docker rm -f sky-sriscrapping-api-1 sky-sriscrapping-worker-1

DOCKER_HOST=unix:///var/run/docker.sock /usr/bin/docker run -d \
  --name sky-sriscrapping-api-1 \
  --network sky-sriscrapping_default \
  -p 8000:8000 \
  --env-file ~/apps/sky-sriScrapping/.env \
  -e DATABASE_URL=postgresql+asyncpg://sri:TestPass123@db:5432/sri_db \
  --restart unless-stopped \
  sky-sriscrapping-api:latest

DOCKER_HOST=unix:///var/run/docker.sock /usr/bin/docker run -d \
  --name sky-sriscrapping-worker-1 \
  --network sky-sriscrapping_default \
  --env-file ~/apps/sky-sriScrapping/.env \
  -e DATABASE_URL=postgresql+asyncpg://sri:TestPass123@db:5432/sri_db \
  --restart unless-stopped \
  sky-sriscrapping-worker:latest
```

---

## Host nativo (staging paralelo en `192.168.1.12`)

La preparación nativa deja:

- PostgreSQL nativo en `127.0.0.1:15432`
- API nativa en `http://127.0.0.1:18000`
- Worker nativo instalado vía `systemd`, pero deshabilitado hasta validar manualmente
- Chrome real instalado en el host para Patchright (`BROWSER_CHANNEL=chrome`)

### 1. Bootstrap del host

```bash
ssh b2b@192.168.1.12
cd ~/apps/sky-sriScrapping
bash deploy/native/bootstrap_native_host.sh
```

Esto instala Chrome, Xvfb, PostgreSQL, crea `.venv-native/` y registra:

- `sky-sri-api.service`
- `sky-sri-worker.service`

### 2. Levantar PostgreSQL nativo + restaurar datos + arrancar API nativa

```bash
ssh b2b@192.168.1.12
cd ~/apps/sky-sriScrapping
bash deploy/native/setup_native_postgres.sh
```

El script:

- crea un cluster PostgreSQL nativo en `15432`
- restaura un dump lógico desde `sky-sriscrapping-db-1`
- actualiza `.env` con `DATABASE_URL`, `INTERNAL_API_URL`, `BROWSER_CHANNEL` y `WORKER_RUNTIME_ROOT`
- ejecuta `alembic upgrade head`
- arranca la API nativa en `18000`

### 3. Parar solo el worker Docker mientras validas

```bash
ssh b2b@192.168.1.12
DOCKER_HOST=unix:///var/run/docker.sock /usr/bin/docker stop sky-sriscrapping-worker-1
```

Para reactivarlo:

```bash
DOCKER_HOST=unix:///var/run/docker.sock /usr/bin/docker start sky-sriscrapping-worker-1
```

### 4. Prueba manual A/B del scraper nativo

Con solver:

```bash
ssh b2b@192.168.1.12
cd ~/apps/sky-sriScrapping
source .venv-native/bin/activate
REPORT_DATE=2026-03-22 python -m api.app.worker --tenant-id 1 --once
```

Sin solver:

```bash
ssh b2b@192.168.1.12
cd ~/apps/sky-sriScrapping
source .venv-native/bin/activate
TWOCAPTCHA_API_KEY= REPORT_DATE=2026-03-22 python -m api.app.worker --tenant-id 1 --once
```

Si todavía no tienes una `KNOWN_GOOD_DATE`, usa una fecha reciente solo para validar navegación, `Consultar` y artefactos.

### 5. Dónde quedan los artefactos

```bash
~/apps/sky-sriScrapping/runtime/tenant_1/logs/<timestamp>/
~/apps/sky-sriScrapping/runtime/tenant_1/downloads/<timestamp>/
~/apps/sky-sriScrapping/runtime/tenant_1/state/
```

Ahora cada corrida guarda por tipo y etapa:

- screenshot post-consulta
- HTML completo
- `innerText`
- metadata JSON con clasificación (`dom_missing_claves`, `empty_result_detected_*`, `download_all_strategies_failed`, etc.)
- respuestas JSF interceptadas y su resumen JSON

### 6. Logs de systemd

```bash
sudo journalctl -u sky-sri-api.service -f
sudo journalctl -u sky-sri-worker.service -f
```

### 7. Cutover / rollback

Mientras API/DB Docker siguen arriba, la validación nativa corre en paralelo (`18000` / `15432`).

Cutover:

```bash
sudo systemctl stop sky-sri-api.service
DOCKER_HOST=unix:///var/run/docker.sock /usr/bin/docker stop sky-sriscrapping-api-1 sky-sriscrapping-db-1
# luego mover PostgreSQL nativo a 5432 y la API nativa a 8000
```

Rollback rápido:

```bash
sudo systemctl stop sky-sri-api.service sky-sri-worker.service
DOCKER_HOST=unix:///var/run/docker.sock /usr/bin/docker start sky-sriscrapping-db-1 sky-sriscrapping-api-1 sky-sriscrapping-worker-1
```

---

## Seguridad recomendada (pendiente)

```bash
# Deshabilitar login por contraseña SSH (solo llaves)
sudo nano /etc/ssh/sshd_config
# Cambiar: PasswordAuthentication yes → no
sudo systemctl restart sshd

# Habilitar firewall
sudo ufw allow OpenSSH
sudo ufw allow 8000/tcp
sudo ufw allow 5432/tcp   # Solo si necesitas acceso externo a PostgreSQL
sudo ufw enable
```
