# AGENTS.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Docker Compose project that runs a self-hosted [Immich](https://immich.app) instance for family photo/video backups, with small local helper scripts for Takeout video import and Google Photos cleanup.

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
- `docker-compose.override.yml` — **not committed**; host-specific overrides (media path, ports, hardware acceleration). See `docker-compose.override.yml.example`.
- `google-photos-cleanup.py` and `docker-compose.google-photos-cleanup.yml` — catalog and checkpoint helper for Google Photos cleanup in normal host Chrome; the standalone Compose file must remain independent of the Immich stack.
- `google-photos-cleanup.md` — cleanup setup, execution, and recovery guide.
- `.gitignore` — excludes `docker-compose.override.yml`, `.env.local`, and runtime data dirs

## Common commands

For version upgrades, follow [skills/immich-upgrade/SKILL.md](skills/immich-upgrade/SKILL.md).

```bash
# Start the stack
docker compose up -d

# Start with local overrides (e.g. custom media path)
docker compose -f docker-compose.yml -f docker-compose.override.yml up -d

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
- **Host-specific overrides** (media storage path, port changes, hardware acceleration) go in `docker-compose.override.yml` (not committed).
- The default media path is `./library` relative to the project directory. Override via `UPLOAD_LOCATION` in `.env` or by bind-mounting a different path in `docker-compose.override.yml`.
- `DB_PASSWORD` in `.env` ships as `postgres` — should be changed on the server.

## Immich web UI

Accessible at `http://<server-ip>:2283` after starting the stack. First-time setup creates the admin user through the browser.

## Google Photos cleanup safeguards

- Run the cleanup only through `docker-compose.google-photos-cleanup.yml`; do not combine it with the main Compose file.
- The cleanup service may use only local bind mounts: `.import-state/manifest.sqlite` as read-only and `.import-state/google-photos-cleanup/` as writable state. Do not declare or attach named or anonymous Docker volumes.
- Do not mount or open the Takeout source or Immich library from the cleanup container. Keep `.env`, `.import-state`, and all media out of the cleanup Docker build context.
- Use normal Google Chrome on the host through connected Chrome controls for browser actions. Do not add Playwright, a bundled browser, a separate browser profile, VNC, or a remote debugging port to the cleanup stack.
- Google Photos login must remain manual in Chrome. Never automate credential entry or save credentials in project files.
- Use `prepare`, `next`, `result`, and `report` for catalog/checkpoint operations. Inspect one candidate at a time in Chrome, verify its exact filename and video type, and require a unique match before moving it to Trash. Record `deleted` only after an explicit UI success indication.
- The cleanup may move matched Google Photos items to Trash only. Never empty Trash. Preserve the resumable state and Markdown report under `.import-state/google-photos-cleanup/`.
- See [google-photos-cleanup.md](google-photos-cleanup.md) before changing the cleanup workflow.
