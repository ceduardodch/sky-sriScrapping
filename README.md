# sky-sriScrapping

Scraper y API para descargar comprobantes recibidos desde el portal del SRI de Ecuador, resolver sus claves de acceso, consultar el XML autorizado por SOAP y almacenarlo en una API multi-tenant con PostgreSQL.

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
- `deploy/native/`: scripts y unidades `systemd` para levantar el stack nativo en Linux.
- `docker-compose.yml`: stack Docker con `db`, `api` y `worker`.
- `SERVIDOR.md`: apuntes operativos históricos del servidor.

## Estructura rápida

```text
.
├── api/
│   ├── app/
│   ├── alembic/
│   └── requirements.txt
├── deploy/native/
├── sri_scraper/
├── tests/
├── docker-compose.yml
├── run.py
└── .env.example
```

## Requisitos

- Python 3.11 o superior
- Google Chrome instalado si se usa modo nativo
- PostgreSQL 16 si se usa modo nativo
- Docker y Docker Compose si se usa el stack contenedorizado

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

## Arranque rápido con Docker

1. Completar `.env`.
2. Levantar servicios:

```bash
docker compose up --build
```

3. Verificar:

```bash
curl http://127.0.0.1:8000/health
```

Servicios esperados:

- API: `http://127.0.0.1:8000`
- Swagger: `http://127.0.0.1:8000/docs`
- PostgreSQL: `127.0.0.1:5432`

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

En el host nativo `192.168.1.12`, la corrida validada del scraper directo fue:

```bash
HEADLESS=false xvfb-run -a python run.py scrape --date 2026-03-23
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

## Despliegue nativo en Linux

Los scripts de `deploy/native/` preparan un host Ubuntu con Chrome, Xvfb, PostgreSQL, virtualenv y servicios `systemd`.

Bootstrap inicial:

```bash
bash deploy/native/bootstrap_native_host.sh
```

Preparar PostgreSQL nativo y levantar API:

```bash
bash deploy/native/setup_native_postgres.sh
```

Unidades instaladas:

- `sky-sri-api.service`
- `sky-sri-worker.service`

Comandos útiles:

```bash
systemctl status sky-sri-api.service
systemctl status sky-sri-worker.service
curl http://127.0.0.1:18000/health
```

Importante:

- Si cambias `.env`, reinicia la API nativa para que tome la nueva configuración.
- El worker nativo se deja deshabilitado a propósito mientras se valida manualmente.

Ejemplo:

```bash
sudo systemctl restart sky-sri-api.service
```

## Estado validado en el host `192.168.1.12` el 2026-03-25

Se revisó el servidor y esto es lo confirmado:

- El host responde por red y SSH.
- El repo activo está en `/home/b2b/apps/sky-sriScrapping`.
- Los contenedores Docker activos incluyen `sky-sriscrapping-api-1` y `sky-sriscrapping-db-1`.
- La API nativa en `127.0.0.1:18000` estaba corriendo con configuración vieja de `.env`; tras reiniciarla volvió a reportar `{"status":"ok","db":"ok"}`.
- El `worker` nativo está deshabilitado, que coincide con la estrategia de validar manualmente.
- En `scrape_logs` existen ejecuciones recientes exitosas para el tenant `1`.
- La descarga del TXT sí quedó validada: existe el archivo `runtime/tenant_1/downloads/20260325T132948Z/sri_recibidos_20260323.txt`.
- Ese TXT contiene 12 claves de acceso y el parser las reconoce como válidas.
- Una clave de muestra del TXT respondió `AUTORIZADO` en el WS SOAP del SRI y devolvió XML.

Conclusión operativa:

- La parte “login + navegación + descarga TXT + parse + consulta SOAP” quedó comprobada.
- La parte más sensible hoy es la estabilidad del captcha del portal SRI durante reintentos manuales.

## Observación importante del 2026-03-25

En una corrida manual nueva:

```bash
HEADLESS=true REPORT_DATE=2026-03-23 python -m api.app.worker --tenant-id 1 --once
```

el portal empezó a responder `Captcha incorrecta` al reconsultar, aunque ya existía una descarga correcta previa del mismo día en ese mismo host.

Eso sugiere que el problema actual no es “no descarga”, sino la intermitencia del captcha/WAF del SRI.

En cambio, la corrida directa del scraper con:

```bash
HEADLESS=false xvfb-run -a python run.py scrape --date 2026-03-23
```

sí descargó `downloads/sri_recibidos_20260323.txt` y generó `downloads/claves_20260323.json`.

## Troubleshooting

### La API nativa devuelve `connection refused` en `/health`

Probable causa:
la API quedó levantada con un `.env` anterior.

Acción:

```bash
sudo systemctl restart sky-sri-api.service
curl http://127.0.0.1:18000/health
```

### El worker devuelve `Captcha incorrecta`

Probar en este orden:

1. Reintentar con sesión caliente existente en `runtime/.../state/chrome_profile`.
2. Ejecutar con `HEADLESS=false` solo para depuración visual.
3. Configurar `TWOCAPTCHA_API_KEY` si se va a automatizar de forma estable.

### Hay descargas pero no se insertan comprobantes

Revisar:

- `INTERNAL_API_URL`
- salud de `127.0.0.1:18000/health`
- `ADMIN_API_KEY`
- tabla `comprobantes`
- logs del worker

### El servidor se está quedando sin espacio

Durante la validación del 2026-03-25, el host reportó el disco raíz cerca del 95% de uso. Conviene limpiar artefactos viejos en `runtime/` y revisar logs antes de seguir acumulando pruebas.

## Siguientes pasos recomendados

1. Decidir si el staging nativo va a trabajar contra PostgreSQL Docker `:5432` o contra PostgreSQL nativo `:15432`, y dejar `.env` + servicios consistentes con una sola opción.
2. Agregar manejo operativo del captcha para corridas repetidas.
3. Limpiar artefactos viejos de `runtime/` para bajar consumo de disco.
4. Si el objetivo inmediato es validar extremo a extremo, repetir la corrida cuando el captcha esté estable y confirmar inserción en `comprobantes`.
