# Google Photos video cleanup

Use the machine's normal Google Chrome through the connected Chrome browser controls.
The Docker helper only reads the import catalog and maintains checkpoints and reports.
It does not launch or control a browser. No separate profile, browser container,
remote debugging port, or desktop server is needed. Docker alone cannot automate
Chrome: an operator or assistant with connected Chrome controls performs the UI steps.
If Chrome controls are unavailable, connect Chrome before starting an assisted
cleanup, or carry out the same inspection steps manually in Chrome.

## Prepare

Run commands using only the standalone cleanup Compose file. The catalog must
exist at `.import-state/manifest.sqlite`. Create the writable state directory first:

```bash
mkdir -p .import-state/google-photos-cleanup
```

Then prepare the candidates:

```bash
PUID="$(id -u)" PGID="$(id -g)" docker compose \
  -f docker-compose.google-photos-cleanup.yml \
  run --build --no-deps --rm google-photos-cleanup prepare
```

The container mounts only these two paths, with the catalog read-only.
It never opens either media archive, calls Immich, or recalculates hashes.
Its build context excludes credentials, state, and media.

## Chrome cleanup procedure

1. Select normal Chrome using the connected browser controls and open a Google
   Photos tab. Sign-in remains manual. Never enter or store credentials through
   automation. Check that the intended Google account is active.
2. Claim the next candidate:

   ```bash
   PUID="$(id -u)" PGID="$(id -g)" docker compose \
     -f docker-compose.google-photos-cleanup.yml \
     run --no-deps --rm google-photos-cleanup next
   ```

3. Read the returned JSON filename and checksum key. Search Google Photos for
   that filename. Wait for search results to finish loading. Inspect the results
   and open the item's information panel to verify its exact filename and that
   it is a video. Search relevance or a thumbnail alone is insufficient.
   Only proceed when exactly one result matches; inspect all results, including
   scrolling as needed. If uniqueness or loading cannot be established, record
   `error` and stop for review.
4. Move the verified item to Trash through its visible controls, confirming the
   dialog if needed. Never empty Trash. Require an explicit success indication
   before recording `deleted`. An absent button alone does not prove success.
5. Record the observed result with the exact key from step 2:

   ```bash
   PUID="$(id -u)" PGID="$(id -g)" docker compose \
     -f docker-compose.google-photos-cleanup.yml \
     run --no-deps --rm google-photos-cleanup result \
     --key CHECKSUM --status deleted --detail "Chrome confirmed moved to Trash"
   ```

6. Repeat from step 2 until the helper returns `{"complete": true}`.

Other result statuses are `kept_not_found` (fully loaded search has no exact
filename), `kept_ambiguous` (multiple exact matches), and `error` (unrecognized
UI or uncertain outcome). Always supply a concise factual `--detail`.
The helper trusts the operator's observation; it cannot verify Chrome itself.
Do not record success speculatively or perform bulk deletion.

## Resume and reports

State remains in `.import-state/google-photos-cleanup/`:

- `state.json`: existing per-checksum progress is preserved.
- `report.md`: regenerated report with catalog paths, statuses, and observations.
- `events.jsonl`: diagnostic events, without browser data or credentials.

Completed `deleted`, `kept_not_found`, and `kept_ambiguous` records are skipped.
An interrupted `in_progress` candidate is returned again by `next`.
Errors are retried. After an uncertain Trash action, inspect Photos and Trash
to reconcile the prior action before trying again. Never empty Trash.
Run one operator at a time; the file lock protects individual helper commands,
not the entire human/browser interaction.

Regenerate the report without opening Chrome:

```bash
PUID="$(id -u)" PGID="$(id -g)" docker compose \
  -f docker-compose.google-photos-cleanup.yml \
  run --no-deps --rm google-photos-cleanup report
```

Keep `state.json`; no browser session is stored in cleanup state anymore.
Any old browser-profile data is unused and left untouched.
Review reports for personal filenames before sharing. Never share Chrome profile data.
