---
name: immich-upgrade
description: Upgrade this Docker Compose Immich installation to a requested release, including release checks, backup verification, deployment, and health checks. Use for Immich version upgrades in this repository.
---

# Upgrade Immich in this repository

1. Inspect `git status`, `.env.example`, the version line in `.env`, `docker-compose.yml`, any active override, and `docker compose ps`. Preserve uncommitted changes and never print secrets from `.env`.
2. Confirm the requested stable tag exists. Read Immich's release notes and current [upgrade guide](https://docs.immich.app/install/upgrading/) for changes between the running and target versions. Check this Compose stack against any new image, database, environment, or migration requirements. Do not assume the previous upgrade's compatibility findings still apply.
3. Before changing a running installation, verify its data and backup mounts are available, there is enough space, and recent database and library backups completed. Use the configured paths and backup service logs; do not infer a usable backup from a healthy container alone. Resolve missing or failed backups before applying a migration.
4. Update `IMMICH_VERSION` in the local `.env` and the tracked `.env.example`, plus the version stated in `README.md`. Keep server and machine learning images on the same tag. Make only other configuration changes required by that release.
5. Validate the Compose configuration, pull the target Immich images, and recreate the Immich services. Prefer `docker compose pull immich-server immich-machine-learning` and `docker compose up -d --no-deps immich-server immich-machine-learning` when dependencies do not need changes. Account for any active override files.
6. Check both Immich containers become healthy, `GET http://127.0.0.1:2283/api/server/ping` returns HTTP 200 with `pong`, and startup logs show migrations finished without errors. Confirm the other stack services remain healthy. If startup fails, investigate the logs and release requirements; Immich does not support downgrades after a database migration.
7. Review the final diff and report the running version, checks, and any limitation. Commit or push only when the user requests it. The remote for this repository must remain SSH.

The October 2026 v3.2.2 to v3.3.0 upgrade used the existing `ghcr.io/immich-app/postgres:14-vectorchord0.4.3-pgvectors0.2.0` database image and changed only the version pin and README. Treat that as history, not a template for future release requirements.
