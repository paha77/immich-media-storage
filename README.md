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

## Importing Videos from Google Takeout

You can use [immich-go](https://github.com/simulot/immich-go) to import Google Photos exports (via Google Takeout) directly into Immich. The example below imports **only videos from the last 2 years**, but you can adjust the flags to suit your needs.

### 1. Get an Immich API key

Open the web UI at `http://<server-ip>:2283` → click your avatar → **API Keys** → create a new key. When creating the key, grant these permissions:

| Scope | Why |
|---|---|
| `asset.read`, `asset.upload`, `asset.update`, `asset.copy`, `asset.replace`, `asset.delete`, `asset.download`, `asset.statistics` | Read, upload, deduplicate, and manage assets |
| `album.create`, `album.read`, `albumAsset.create` | Create albums from Google Photos albums |
| `server.about` | Server compatibility check |
| `stack.create` | Group related files (e.g. burst photos) |
| `tag.create`, `tag.asset` | Apply tags from metadata |
| `user.read` | Identify the target user library |
| `job.create`, `job.read` | Pause background jobs during upload (admin only) |

If your account is an admin and you prefer simplicity, you can select **All** instead.

### 2. Install immich-go on the server

`immich-go` is a standalone binary (no Docker image available). Download the latest release:

```bash
# Linux x86_64
curl -sL https://github.com/simulot/immich-go/releases/latest/download/immich-go_Linux_x86_64.tar.gz | sudo tar xz -C /usr/local/bin immich-go
```

See the [releases page](https://github.com/simulot/immich-go/releases) for other platforms.

### 3. Extract the Takeout archive on the server

```bash
mkdir -p /path/to/takeout
cd /path/to/takeout
unzip ~/Takeout-*.zip
```

If Google split the export into multiple zips, extract them all into the same directory.

### 4. Dry run (preview what will be uploaded)

```bash
immich-go upload from-google-photos --server=http://localhost:2283 --api-key=YOUR_API_KEY --date-range=2024-04-23,2026-04-23 --include-type=VIDEO --dry-run "/path/to/takeout/Takeout/Google Photos"
```

### 5. Run the actual import

Same command without `--dry-run`:

```bash
immich-go upload from-google-photos --server=http://localhost:2283 --api-key=YOUR_API_KEY --date-range=2024-04-23,2026-04-23 --include-type=VIDEO "/path/to/takeout/Takeout/Google Photos"
```

### Notes

- **`--server=http://localhost:2283`** works because port 2283 is exposed by the compose stack. Use `http://<server-ip>:2283` if running from a different machine.
- **`--date-range`** filters by the JSON sidecar metadata date, not file modification time. Adjust the range as needed.
- **`--include-type=VIDEO`** limits the import to videos only. Use `IMAGE` for photos only, or omit for everything.
- **`--pause-immich-jobs=FALSE`** — by default `immich-go` tries to pause Immich background jobs during upload, which requires an admin API key. If your key doesn't have admin permissions, add this flag to skip job pausing. Alternatively, pass a separate admin key via `--admin-api-key=...`.
- `immich-go` deduplicates automatically, so running it again is safe.

## Hardware Acceleration (Optional)

See `docker-compose.override.yml.example` for instructions on enabling VA-API (transcoding) and OpenVINO (ML inference) on Intel hardware.
