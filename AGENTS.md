# AGENTS.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Docker Compose project that runs a self-hosted [Immich](https://immich.app) instance for family photo/video backups. There is no custom application code — only Docker Compose configuration and environment variables.

## Stack

| Service | Image | Purpose |
|---|---|---|
| `immich-server` | `ghcr.io/immich-app/immich-server` | Unified API server, web UI, and background workers |
| `immich-machine-learning` | `ghcr.io/immich-app/immich-machine-learning` | Face recognition, CLIP search |
| `redis` | `valkey/valkey:9` | Job queue (Valkey, Redis-compatible) |
| `database` | `ghcr.io/immich-app/postgres` | PostgreSQL 14 with vector extensions |

## Key files

- `docker-compose.yml` — main stack definition (do not add host-specific paths here)
- `.env` — environment variables (version, DB credentials, default storage paths)
- `docker-compose.local.yml` — **not committed**; host-specific overrides (media path, ports, hardware acceleration). See `docker-compose.local.yml.example`.
- `.gitignore` — excludes `docker-compose.local.yml`, `.env.local`, and runtime data dirs

## Common commands

```bash
# Start the stack
docker compose up -d

# Start with local overrides (e.g. custom media path)
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d

# View logs
docker compose logs -f              # all services
docker compose logs -f immich-server # single service

# Stop
docker compose down

# Update to latest Immich release (IMMICH_VERSION is set in .env)
docker compose pull && docker compose up -d

# Check service health
docker compose ps
```

## Configuration approach

- **Defaults** go in `.env` and `docker-compose.yml` (committed, portable).
- **Host-specific overrides** (media storage path, port changes, hardware acceleration) go in `docker-compose.local.yml` (not committed).
- The default media path is `./library` relative to the project directory. Override via `UPLOAD_LOCATION` in `.env` or by bind-mounting a different path in `docker-compose.local.yml`.
- `DB_PASSWORD` in `.env` ships as `postgres` — should be changed on the server.

## Immich web UI

Accessible at `http://<server-ip>:2283` after starting the stack. First-time setup creates the admin user through the browser.
