# Google Photos video cleanup

This runner uses the existing Takeout-to-Immich manifest to find video candidates and operates the Google Photos website in a visible Firefox window inside Docker. Google Photos' official API does not provide library-item deletion, so this UI automation depends on Google's changeable web interface. Nothing needs to be installed on the host beyond Docker and Docker Compose.

## Safety boundaries

- Reads `.import-state/manifest.sqlite` using SQLite read-only and immutable mode. It does not rescan media or calculate hashes.
- Processes unique checksum groups whose catalog status is `uploaded` or `already_present`.
- Never opens Takeout media or Immich library files. It does not call Immich or write to either archive.
- Only proceeds when there is one exact filename in the catalog and one exact filename text match in Google Photos.
- Moves matched items to Google Photos Trash. It never empties Trash.
- Opens Firefox visibly through a local noVNC page. You sign in yourself; the script never enters credentials. If the saved session is valid, cleanup starts automatically. Otherwise it waits for manual sign-in and proceeds as soon as Google Photos is ready; no terminal confirmation is needed.
- The cleanup container uses only two local bind mounts: the catalog read-only and this cleanup state directory read/write. It declares no Docker-managed volumes. Its standalone Compose file does not load the Immich stack, and its Docker build context excludes `.env`, `.import-state`, Takeout, and Immich library files.
- Does not save browser screenshots, page contents, passwords, or cookies to the diagnostic event log. Firefox's local profile does retain the normal signed-in session.

Because the Photos website is not a supported automation interface, any unrecognized page/control is recorded as an error and left for a later run. Review `report.md` and `events.jsonl` after each run. The report lists each unique video, its source catalog paths, Immich status, cleanup result, and details.

## Run

Prepare a report and candidate state without opening a browser or changing Google Photos:

```bash
PUID="$(id -u)" PGID="$(id -g)" docker compose \
  -f docker-compose.google-photos-cleanup.yml \
  run --build --no-deps --rm google-photos-cleanup prepare
```

Start or resume the cleanup:

```bash
PUID="$(id -u)" PGID="$(id -g)" docker compose \
  -f docker-compose.google-photos-cleanup.yml \
  run --build --no-deps --rm --service-ports google-photos-cleanup run
```

Open `http://127.0.0.1:6080/vnc.html?autoconnect=true` in a host browser. If sign-in is needed, complete it manually in the headed Firefox window. The runner detects when Google Photos is ready and starts without terminal confirmation. It processes all unfinished catalog candidates. `--limit N` can cap a run for a smaller initial batch. The VNC service is bound to host loopback only.

The persistent runner data is on the host in `.import-state/google-photos-cleanup/`:

- `report.md` — regenerated Markdown report
- `events.jsonl` — timestamped diagnostic events
- `state.json` — per-video checkpoints used to resume
- `firefox-profile/` — container Firefox profile retaining the browser session

Run the same `run` command to resume after an interruption. Completed (`deleted`, `kept_not_found`, and `kept_ambiguous`) records are not retried. Errors and interrupted `in_progress` records are retried; the runner searches Google Photos again before attempting another action. Never delete `state.json` or the Firefox profile if you want to retain resume progress or sign-in.

Regenerate the report from saved state:

```bash
PUID="$(id -u)" PGID="$(id -g)" docker compose \
  -f docker-compose.google-photos-cleanup.yml \
  run --build --no-deps --rm google-photos-cleanup report
```

## Report statuses

- `deleted`: the Photos UI indicated that the item was moved to Trash.
- `kept_not_found`: no exact filename was present in the current Photos search results.
- `kept_ambiguous`: the catalog or search results did not identify exactly one item.
- `error`: the UI was not recognized or the result of an attempted action could not be confirmed.
- `pending` / `in_progress`: not yet finished or interrupted.

When diagnosing a problem, share `report.md` and `events.jsonl`. They include catalog-relative filenames and paths; review them for personal information before sharing. Do not share the Firefox profile.
