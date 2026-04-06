# Immich Media Storage

Self-hosted [Immich](https://immich.app) instance for family photo and video backups, running on a Debian home server via Docker Compose.

## Prerequisites

- Debian server with Docker and Docker Compose installed
- Ports: **2283** available on the server
- Storage: enough disk space for your photo/video library

## Quick Start

1. **Clone this repo** onto your server.

2. **Create your `.env` file** from the example and set a database password:

   ```bash
   cp .env.example .env
   ```

   Edit `.env` and change `DB_PASSWORD` to a random alphanumeric string:

   ```
   DB_PASSWORD=YourRandomPasswordHere
   ```

3. **Start the stack:**

   ```bash
   docker compose up -d
   ```

4. **Open the web UI** at `http://<server-ip>:2283`.

5. **Create your admin account** — the first-time setup wizard will prompt you to set an email and password. This is the Immich admin account, not related to the database credentials.

## Custom Media Storage Path

By default, media is stored in `./library` relative to this directory. To use a different path (e.g. an external drive):

1. Copy the example override file:

   ```bash
   cp docker-compose.override.yml.example docker-compose.override.yml
   ```

2. Edit `docker-compose.override.yml` and set your path:

   ```yaml
   volumes:
     - /mnt/external/immich-library:/data
   ```

3. Start with the override:

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.override.yml up -d
   ```

## Mobile App Setup

Immich has apps for both iOS and Android.

1. Install the **Immich** app from the [App Store](https://apps.apple.com/app/immich/id1613945686) or [Google Play](https://play.google.com/store/apps/details?id=app.alextran.immich).

2. On the login screen, enter:
   - **Server URL:** `http://<server-ip>:2283`
   - **Email / Password:** the admin credentials you created in the web UI

3. Grant photo/video access when prompted.

4. Go to **Backup** settings in the app and enable **Auto Backup** — new photos and videos will upload automatically when on Wi-Fi.

## Updating

```bash
docker compose pull
docker compose up -d
```

The `IMMICH_VERSION` in `.env` controls which release to track (default: `v2`, the latest stable).

## Hardware Acceleration (Optional)

See `docker-compose.override.yml.example` for instructions on enabling VA-API (transcoding) and OpenVINO (ML inference) on Intel hardware.
