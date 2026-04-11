# Deployment

Producción se despliega desde Coolify usando la rama `main`.

- GitHub Actions se usa solo para CI y validaciones.
- Este repositorio no mantiene deploys por GitHub Actions.
- Este repositorio no mantiene guías ni artefactos de despliegue manual heredados.
- Los `Dockerfile` siguen versionados porque forman parte del build del proyecto y de la validación en CI.
- `release` es la rama de validación.
- `develop` es la rama de desarrollo.
- El flujo preferido de entrega es `release -> PR -> main`.
