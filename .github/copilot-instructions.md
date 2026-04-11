# GitHub Copilot Instructions

## Branch policy

- Work by default on `release`.
- Use only `develop`, `release` and `main`.
- Do not create new branches without explicit authorization.
- Do not use branch names like `codex/*`, `claude/*`, `fix/*`, `feat/*` or similar.

## Delivery policy

- Preferred flow: `release -> PR -> main`.
- `main` is production.
- `release` is validation/preproduction.
- `develop` is development.
- Do not push, merge or open pull requests without explicit approval.

## Deployment and CI

- Coolify deploys from `main`.
- GitHub Actions is CI-only.
- Do not add deploy jobs, SSH steps, VPS scripts, self-hosted runner deployment, backups or temporary infra automation to workflows.

## Scope

- Prefer repository governance, CI, documentation and maintenance work.
- Avoid business-logic changes unless they are strictly required for CI or repository consistency.
