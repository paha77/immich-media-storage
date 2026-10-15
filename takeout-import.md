# Google Photos video import

This wrapper imports Google Takeout videos with immich-go's Google Photos importer. It never invokes the generic folder importer. The Takeout remains read-only. Import state is kept under `.import-state/` and is ignored by Git.

## Setup

The current host-specific config is `takeout-import.toml`; keep it local. The API key belongs in `.import-state/credentials.env` with mode `600`:

```text
IMMICH_API_KEY=your-key
```

The key must belong to the account in `target_email` and permit asset read/upload/update and tag create/assign operations. This account is non-admin; the wrapper passes `--pause-immich-jobs=false` and does not grant or use job-management permissions. immich-go v0.32.0 has a known bug that still sends a job-resume request when pausing is disabled; Immich rejects that request with 403 for this non-admin key, so no job setting is changed. The wrapper recognizes only that specific 403 as nonfatal, while retaining all per-asset errors.

The installed binary is `.import-state/bin/immich-go` (v0.32.0). The wrapper validates the authenticated account and expected server version before import. It uses `VIDEO`, includes unmatched, archived, and partner videos, keeps people tags enabled, and leaves trashed videos excluded. It explicitly passes `--sync-albums=false` because this immich-go version defaults album sync to true. The staging tree preserves the relative paths, symlinks video files and all JSON files, and omits every photograph and other media file. Album memberships and album creation are therefore not performed.

## Commands

```bash
python3 takeout-import.py inventory  # deterministic scan + SHA-1 manifest
python3 takeout-import.py stage      # build/verify the video-only symlink tree
python3 takeout-import.py start      # detached persistent runner
python3 takeout-import.py status     # machine-readable current progress
python3 takeout-import.py stop       # graceful checkpoint and stop
python3 takeout-import.py resume     # rerun importer; reconcile checksums first
python3 takeout-import.py retry      # reset failed paths before resume
```

Inspect live output with `tail -f .import-state/runner.log` and structured immich-go events with `tail -f .import-state/immich-go.jsonl`. Durable state includes `manifest.sqlite`, `manifest.jsonl`, `progress.json`, and `summary.json`. SHA-1 duplicate reconciliation is account-scoped through Immich's bulk upload check API. An interrupted import is reconciled before retry; immich-go's duplicate handling is also active. The full video-only stage is generated under `.import-state/video-only-stage/Google Fotos` and contains only video symlinks and JSON sidecar symlinks.

The runner uses one immich-go task. It checks available bytes before starting, every two seconds during the import, and includes the active file size in the stop threshold when its structured event identifies one. It reserves exactly 100,000,000,000 bytes plus the configurable 25,000,000,000-byte safety margin. Immich's background thumbnail/transcode jobs and other host writers continue concurrently: polling and graceful shutdown cannot guarantee the reserve against a sudden external write or an in-flight server job. A stop leaves unfinished files resumable.

The JSON log exposes file lifecycle events, not byte-level upload progress. Status therefore reports completed/verified bytes, transferred bytes accounted for by newly uploaded files, elapsed time, and estimates only when meaningful; it labels current-file progress as file-level when no byte counter is available.

Google Takeout fields are mapped only where Immich/immich-go support them. The importer handles supported creation time, location, description, favorite/archive state, and people tags when present in sidecars. Album membership is deliberately not imported. Google-specific fields without a corresponding Immich feature cannot be transferred. Files without matching JSON are included and flagged; embedded metadata is left to Immich's media parser.

## Removing imported videos from Google Photos

`google-photos-cleanup.py` uses the existing manifest in read-only mode to prepare a per-video report and run a resumable Google Photos web cleanup. It does not open or modify Takeout or Immich media. See [google-photos-cleanup.md](google-photos-cleanup.md) for installation, manual sign-in, report statuses, and recovery instructions.
