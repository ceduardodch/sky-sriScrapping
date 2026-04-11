# AGENTS

Estas reglas aplican a cualquier asistente de IA o automatización que trabaje en este repositorio.

## Ramas

- Solo existen tres ramas permanentes: `develop`, `release` y `main`.
- `main` es producción.
- `release` es validación y preproducción.
- `develop` es desarrollo.
- La rama de trabajo por defecto es `release`.
- No se deben crear ramas temporales como `codex/*`, `claude/*`, `fix/*`, `feat/*` o similares sin autorización explícita.

## Flujo de entrega

- Flujo preferido: `release -> PR -> main`.
- Si hace falta trabajo exploratorio o integración adicional, debe ir a `develop` y luego avanzar a `release`.
- No hacer `push`, `merge` ni abrir PR sin aprobación explícita.
- No cambiar la política de ramas sin aprobación explícita.

## Despliegue y CI

- Coolify despliega desde `main`.
- GitHub Actions queda solo para CI: `lint`, `test`, `build` y validaciones de PR.
- No agregar jobs de deploy en GitHub Actions.
- No reintroducir scripts o documentación de despliegue heredado salvo autorización explícita.

## Cambios permitidos

- Priorizar cambios de gobernanza, CI, documentación y mantenimiento del repositorio.
- No tocar lógica de negocio salvo que sea estrictamente necesario para que CI o el flujo del repositorio queden consistentes.
