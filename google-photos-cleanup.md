# Automated Google Photos video cleanup

The macOS runner controls your normal local Google Chrome through Apple Events.
It searches for each imported video, checks the exact filename and video type,
moves a unique match to Trash, and records the result automatically.
Docker retains read-only catalog access, checkpoints, and report generation.
No Playwright, bundled browser, separate profile, browser extension, VNC, or
remote debugging port is used. The host runner uses macOS's built-in
`bash` and `osascript`; Docker is the only additional command-line dependency.

## Setup

1. The runner opens Google Photos in normal Chrome when no Photos tab exists,
   starting Chrome if needed. It reuses a single existing Photos tab and stops
   if multiple Photos tabs are open. Sign in manually if prompted, select the
   intended account, and rerun the command. English and German Photos
   controls are supported; other languages stop the run.
2. Enable Chrome's **View → Developer → Allow JavaScript from Apple Events**.
   This permits local Apple Events scripts to run JavaScript inside Chrome tabs.
   The runner does not change this setting itself.
   See [Chromium's AppleScript documentation](https://www.chromium.org/developers/applescript/).
3. macOS may ask whether the terminal application can control Google Chrome.
   Allow that normal automation permission to use this runner.
4. The import catalog must already exist at `.import-state/manifest.sqlite`.
   Only checksum groups marked `uploaded` or `already_present` are candidates.

Check Chrome and the expected account without changing the catalog or Photos:

```bash
bash google-photos-cleanup-chrome.sh --check --account you@example.com
```

The shell entry point creates the state directory and prevents concurrent
automated runners. If a force-killed process leaves `chrome-runner.lock/`, verify
that no runner is active before removing that empty lock directory.

## Run

First verify one candidate without moving anything to Trash:

```bash
bash google-photos-cleanup-chrome.sh --account you@example.com --dry-run
```

The candidate remains `in_progress` so the next real run checks it again.
To remove one verified video:

```bash
bash google-photos-cleanup-chrome.sh --account you@example.com --limit 1
```

To process all remaining candidates automatically:

```bash
bash google-photos-cleanup-chrome.sh --account you@example.com
```

The runner builds the helper image before a cleanup run. Keep its Photos tab
open and avoid interacting with that tab while it runs. Other Chrome tabs
are not navigated or closed. The normal Chrome profile retains your login.
The runner does not automate sign-in or save credentials.

The runner scrolls search results, deduplicates repeated links by photo ID,
requires a single unique result, opens Info, checks one exact filename and a
visible video element or Google Photos video-player iframe, and rechecks the item
before clicking Move to trash. Multiple unique search hits are skipped even
if only one might be an exact match. Unrecognized controls, empty/unrecognized
search results without an explicit empty-state message, loading timeouts, account
mismatches, and unsupported languages stop the run. A stable explicit “No results”
screen is recorded as `kept_not_found` and skipped. This does not prove the video
was already removed.
Google's website can change; verify the dry run before running a large batch.

## Recovery and reports

The helper writes `.import-state/google-photos-cleanup/`:

- `state.json`: per-checksum progress; keep this to resume.
- `report.md`: filename, catalog paths, Immich status, and cleanup outcome.
- `events.jsonl`: checkpoint events and concise diagnostic details.

Completed `deleted`, `kept_not_found`, and `kept_ambiguous` records are skipped.
Pre-action errors and interrupted `in_progress` records are checked again.
`needs_review` blocks further automated cleanup: a Trash action was about to
start or its outcome could not be confirmed. Intent is saved before the first
Trash click, and only explicit success clears it automatically.
The runner never empties Trash.
It recognizes the observed German notice about the 30-day Trash recovery
period and continues through that notice. Other unexpected dialogs stop the run.

For `needs_review`, inspect the candidate in Photos and Trash manually.
Once reconciled, record the actual outcome using the checksum from the report
or `next` output. For example, after confirming it is in Trash:

```bash
PUID="$(id -u)" PGID="$(id -g)" docker compose \
  -f docker-compose.google-photos-cleanup.yml \
  run --no-deps --rm google-photos-cleanup result \
  --key CHECKSUM --status deleted --detail "Manually verified item in Trash"
```

If you manually confirm it is still in the library and safe to retry, use
the explicit recovery command:

```bash
PUID="$(id -u)" PGID="$(id -g)" docker compose \
  -f docker-compose.google-photos-cleanup.yml \
  run --no-deps --rm google-photos-cleanup retry \
  --key CHECKSUM --detail "Manually verified still in library; safe to retry"
```

An `error` result intentionally cannot clear `needs_review`.
Do not retry to bypass an uncertain outcome.

Regenerate the report without opening Chrome:

```bash
PUID="$(id -u)" PGID="$(id -g)" docker compose \
  -f docker-compose.google-photos-cleanup.yml \
  run --no-deps --rm google-photos-cleanup report
```

The manual helper commands `prepare`, `next`, and `result` remain available.
Use one operator at a time; do not run manual checkpoint commands concurrently
with the automated runner.

## Storage boundaries

Run the helper only with `docker-compose.google-photos-cleanup.yml`, independently
of the Immich stack. It mounts only the manifest read-only and cleanup state
read/write, using local bind mounts and no Docker-managed volumes.
It never opens Takeout or Immich media, calls Immich, or recalculates hashes.
The build context excludes credentials, state, and media. Browser cookies,
page contents, and screenshots are not written to diagnostic events.
Old browser-profile directories are unused and left untouched.
Review reports for personal filenames before sharing; never share Chrome profile data.
