# sky-sriScrapping

Scraper y API para descargar comprobantes recibidos desde el portal del SRI de Ecuador, resolver sus claves de acceso, consultar el XML autorizado por SOAP y almacenarlo en una API multi-tenant con PostgreSQL.

## Flujo del repositorio

Este repositorio usa una política simple y estricta de ramas:

- `develop`: desarrollo
- `release`: validación y preproducción
- `main`: producción

Reglas vigentes:

- El trabajo normal debe hacerse en `release`.
- El flujo preferido es `release -> PR -> main`.
- `main` queda reservado para producción.
- Coolify despliega desde `main`.
- GitHub Actions se usa solo para CI: `lint`, `test`, `build` y checks de PR.

## Qué hace el proyecto

El flujo completo es:

1. Inicia sesión en el portal del SRI con Patchright + Chrome.
2. Navega a `Comprobantes electrónicos recibidos`.
3. Descarga el TXT del día consultado.
4. Extrae y valida claves de acceso.
5. Consulta el WS SOAP del SRI para recuperar el XML autorizado.
6. Inserta o actualiza los comprobantes en la API local.

## Componentes principales

- `sri_scraper/`: automatización del navegador, login, navegación, descarga, parseo y SOAP.
- `api/`: FastAPI, modelos SQLAlchemy, endpoints admin/cliente y worker.
- `scripts/`: utilidades operativas como backfills y monitoreo manual.
- `.github/workflows/ci.yml`: CI del repositorio.
- `DEPLOYMENT.md`: criterio de despliegue y responsabilidades entre Coolify y GitHub Actions.
- `AGENTS.md`: reglas para asistentes de IA y automatización sobre este repo.

## Estructura rápida

```text
.
├── api/
│   ├── app/
│   ├── alembic/
│   └── requirements.txt
├── .github/workflows/
├── scripts/
├── sri_scraper/
├── tests/
├── run.py
└── .env.example
```

## Requisitos

- Python 3.11 o superior
- Google Chrome instalado si se usa modo nativo
- PostgreSQL 16 para desarrollo local si se levanta la API fuera de Coolify

## Variables de entorno

Partir de `.env.example`:

```bash
cp .env.example .env
```

Variables importantes:

- `SRI_RUC`
- `SRI_PASSWORD`
- `HEADLESS`
- `REPORT_DATE`
- `DATABASE_URL`
- `DATA_DATABASE_URLS`
- `DB_PASSWORD`
- `ADMIN_API_KEY`
- `FERNET_KEY`
- `INTERNAL_API_URL`
- `WORKER_RUNTIME_ROOT`
- `TWOCAPTCHA_API_KEY` opcional, pero útil cuando el captcha del SRI se pone agresivo

Nota:
No conviene guardar credenciales reales del servidor, del SRI o llaves de API dentro del repo.

## Arranque nativo

Crear entorno e instalar dependencias:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip wheel
pip install -r requirements.txt -r api/requirements.txt
```

Migrar base:

```bash
cd api
alembic upgrade head
cd ..
```

Levantar API:

```bash
uvicorn api.app.main:app --host 0.0.0.0 --port 18000
```

Ejecutar worker manual:

```bash
python -m api.app.worker --tenant-id 1 --once
```

Ejecutar scheduler:

```bash
python -m api.app.worker
```

## Uso del scraper CLI

Descarga directa del TXT:

```bash
python run.py scrape --date 2026-03-23 --headless
```

Si no se pasa `--date`, usa automáticamente ayer en zona `America/Guayaquil`.

## API disponible

Hay un solo Swagger en `/docs`. Los endpoints se agrupan en tres tags: `admin`, `comprobantes` y `health`.

Admin:

- `GET /admin/tenants`
- `GET /admin/tenants/{id}`
- `POST /admin/tenants`
- `PATCH /admin/tenants/{id}`
- `POST /admin/tenants/{id}/trigger`
- `GET /admin/tenants/{id}/scrape-logs`
- `POST /admin/tenants/{id}/rotate-api-key`

Cliente:

- `GET /api/v1/comprobantes`
- `GET /api/v1/comprobantes/{clave_acceso}`

Interno worker:

- `POST /api/v1/comprobantes`

Autenticación:

- Admin: `X-API-Key: <ADMIN_API_KEY>`
- Cliente: `X-API-Key: <api_key_del_tenant>`

## Modelo multi-cliente

- `tenant = cliente`
- `tenants`, API keys y `scrape_logs` viven en la BD de control.
- `comprobantes` ya se resuelve por `storage_key`.
- En v1, `storage_key=default` apunta a la misma `DATABASE_URL`, pero el router ya acepta varios DSN en `DATA_DATABASE_URLS`.

Ejemplo:

```env
DATABASE_URL=postgresql+asyncpg://sri:secret@127.0.0.1:5432/sri_db
DATA_DATABASE_URLS={"default":"postgresql+asyncpg://sri:secret@127.0.0.1:5432/sri_db","secundaria":"postgresql+asyncpg://sri:secret@127.0.0.1:5432/sri_secundaria"}
```

Cada tenant puede apuntar a uno de esos nombres mediante `storage_key`.

## Despliegue y CI

- Producción: Coolify despliega desde `main`.
- GitHub Actions: solo ejecuta CI y validaciones.
- Este repo no mantiene despliegues por GitHub Actions, SSH, VPS, scripts manuales versionados ni workflows heredados de infraestructura anterior.

Consulta [DEPLOYMENT.md](DEPLOYMENT.md) para la regla operativa vigente.
