#!/usr/bin/env python3
"""Resumable Google Photos cleanup driven by the read-only Takeout import catalog."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent
CATALOG = Path(os.environ.get("GOOGLE_PHOTOS_CATALOG", ROOT / ".import-state" / "manifest.sqlite"))
STATE_DIR = Path(os.environ.get(
    "GOOGLE_PHOTOS_STATE_DIR", ROOT / ".import-state" / "google-photos-cleanup"
))
STATE_FILE = STATE_DIR / "state.json"
EVENT_LOG = STATE_DIR / "events.jsonl"
REPORT = STATE_DIR / "report.md"
PROFILE = STATE_DIR / "firefox-profile"
PHOTOS_URL = "https://photos.google.com/"
DONE = {"deleted", "kept_not_found", "kept_ambiguous"}


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log_event(event, **data):
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    record = {"time": timestamp(), "event": event, **data}
    fd = os.open(EVENT_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def atomic_json(path, payload):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_name)


def load_catalog():
    if not CATALOG.is_file():
        raise RuntimeError(f"Catalog not found: {CATALOG}; run takeout-import.py inventory first")
    uri = f"file:{quote(str(CATALOG))}?mode=ro&immutable=1"
    db = sqlite3.connect(uri, uri=True)
    db.row_factory = sqlite3.Row
    try:
        records = list(db.execute(
            "SELECT path,size,sha1,status FROM videos "
            "WHERE status IN ('uploaded','already_present') ORDER BY sha1,path COLLATE BINARY"
        ))
    finally:
        db.close()

    groups = {}
    for row in records:
        group = groups.setdefault(row["sha1"], {
            "key": row["sha1"], "size": row["size"], "paths": [], "statuses": set()
        })
        group["paths"].append(row["path"])
        group["statuses"].add(row["status"])
        group["size"] = max(group["size"], row["size"])
    candidates = []
    for group in groups.values():
        names = sorted({Path(path).name for path in group["paths"]})
        candidates.append({
            "key": group["key"], "size": group["size"], "paths": group["paths"],
            "filename": names[0] if len(names) == 1 else None,
            "filenames": names, "immich_status": sorted(group["statuses"]),
        })
    name_owners = {}
    for item in candidates:
        if item["filename"]:
            name_owners.setdefault(item["filename"], set()).add(item["key"])
    for item in candidates:
        if item["filename"] and len(name_owners[item["filename"]]) > 1:
            item["filename"] = None
            item["filenames"] = sorted(set(item["filenames"]))
    return sorted(candidates, key=lambda item: item["key"])


def load_state(candidates):
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        old = {item["key"]: item for item in state.get("items", [])}
    else:
        old = {}
        state = {"created_at": timestamp(), "items": []}
    items = []
    for candidate in candidates:
        prior = old.get(candidate["key"], {})
        items.append({**candidate, "status": prior.get("status", "pending"),
                      "updated_at": prior.get("updated_at"),
                      "detail": prior.get("detail"), "attempts": prior.get("attempts", 0)})
    state["updated_at"] = timestamp()
    state["items"] = items
    atomic_json(STATE_FILE, state)
    return state


def save_state(state):
    state["updated_at"] = timestamp()
    atomic_json(STATE_FILE, state)
    write_report(state)


def write_report(state):
    counts = {}
    for item in state["items"]:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    lines = [
        "# Google Photos video cleanup report", "",
        f"Updated: {state['updated_at']}",
        f"Catalog: `{CATALOG.name}` (opened read-only)",
        "Deletion target: Google Photos Trash only; Trash is never emptied.",
        "Takeout and Immich archives are not opened by this program.", "",
        "## Summary", "",
        f"- Unique catalog videos: {len(state['items'])}",
        f"- Deleted (moved to Trash): {counts.get('deleted', 0)}",
        f"- Kept, not found: {counts.get('kept_not_found', 0)}",
        f"- Kept, ambiguous: {counts.get('kept_ambiguous', 0)}",
        f"- Errors: {counts.get('error', 0)}",
        f"- Pending: {counts.get('pending', 0)}", "",
        "## Video list", "",
        "| Status | Filename(s) | Takeout catalog path(s) | Size | Immich catalog status | Detail |",
        "|---|---|---|---:|---|---|",
    ]
    for item in state["items"]:
        def cell(value):
            return str(value).replace("|", "\\|").replace("\n", " ")
        filename = ", ".join(item.get("filenames", []))
        paths = "<br>".join(item.get("paths", []))
        size = int(item.get("size", 0))
        detail = item.get("detail") or ""
        lines.append(f"| {cell(item['status'])} | {cell(filename)} | {cell(paths)} | {size} | "
                     f"{cell(', '.join(item.get('immich_status', [])))} | {cell(detail)} |")
    lines.extend(["", "## Diagnostics", "",
                  f"Structured event log: `{EVENT_LOG.name}`", "",
                  "Share `report.md` and `events.jsonl` when diagnosing a failure. "
                  "The event log does not include browser cookies, passwords, or page screenshots.", ""])
    REPORT.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = REPORT.with_suffix(".md.tmp")
    temp.write_text("\n".join(lines), encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, REPORT)


def set_status(state, item, status, detail):
    item["status"] = status
    item["detail"] = detail
    item["updated_at"] = timestamp()
    save_state(state)
    log_event("item_result", key=item["key"], filename=item.get("filename"),
              status=status, detail=detail, attempts=item["attempts"])


def photos_domain(page):
    return page.url.startswith("https://photos.google.com/")


def search_box(page):
    # UI labels can change; never fall back to coordinate-based clicking.
    for name in ("Search your photos", "Search"):
        locator = page.get_by_role("combobox", name=name, exact=False)
        if locator.count():
            return locator.first
        locator = page.get_by_role("searchbox", name=name, exact=False)
        if locator.count():
            return locator.first
    return None


def cleanup_one(page, item):
    if not item.get("filename"):
        return "kept_ambiguous", "Multiple different filenames share this catalog checksum group"
    if not photos_domain(page):
        return "error", f"Unexpected browser origin: {page.url}"
    box = search_box(page)
    if box is None:
        return "error", "Google Photos search control was not recognized; no action taken"
    filename = item["filename"]
    log_event("search_start", key=item["key"], filename=filename, origin=page.url)
    box.fill('"' + filename + '"')
    box.press("Enter")
    page.wait_for_timeout(1800)

    # Require a single exact, visible filename result. Semantic/partial search hits are ignored.
    exact = page.get_by_text(filename, exact=True)
    count = exact.count()
    log_event("search_result", key=item["key"], filename=filename, exact_text_matches=count)
    if count == 0:
        return "kept_not_found", "No exact filename was visible in Google Photos search results"
    if count != 1:
        return "kept_ambiguous", f"Found {count} exact filename text matches; no action taken"
    exact.first.click()
    page.wait_for_timeout(900)
    if not photos_domain(page):
        return "error", "Navigation left photos.google.com; no delete action taken"
    # Re-check the exact name on the opened item's detail view before using the UI action.
    detail_count = page.get_by_text(filename, exact=True).count()
    log_event("detail_check", key=item["key"], filename=filename,
              exact_text_matches=detail_count, url=page.url)
    if detail_count != 1:
        page.keyboard.press("Escape")
        return "kept_ambiguous", f"Opened item did not show one exact filename (count={detail_count})"
    trash = page.get_by_role("button", name="Move to trash", exact=True)
    if trash.count() != 1:
        page.keyboard.press("Escape")
        return "error", "Move to trash control was absent or ambiguous; no action taken"
    trash.click()
    log_event("trash_action_clicked", key=item["key"], filename=filename)
    page.wait_for_timeout(500)
    confirm = page.get_by_role("button", name="Move to trash", exact=True)
    if confirm.count() > 1:
        confirm = confirm.last
    if confirm.count() == 1:
        confirm.click()
    page.wait_for_timeout(900)
    # Only report success when the item view closes or a confirmation is exposed.
    text = page.locator("body").inner_text(timeout=5000)
    if "Moved to trash" in text or "Move to trash" not in text:
        log_event("trash_action_confirmed", key=item["key"], filename=filename,
                  confirmation_text="Moved to trash" in text)
        return "deleted", "Google Photos UI reported the item moved to Trash"
    log_event("trash_action_uncertain", key=item["key"], filename=filename)
    return "error", "Delete was attempted but success could not be confirmed; reconcile before retry"


def run_browser(state, limit=None):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Playwright is missing from the cleanup container image") from exc

    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    PROFILE.mkdir(mode=0o700, parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        context = playwright.firefox.launch_persistent_context(
            str(PROFILE), headless=False, accept_downloads=False,
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(PHOTOS_URL, wait_until="domcontentloaded")
        print("Sign in manually in the headed Firefox window. No login fields will be automated.")
        input("After signing in manually in the headed browser, press Enter here to start cleanup: ")
        if not photos_domain(page):
            raise RuntimeError(f"Browser is not on photos.google.com: {page.url}")
        if "accounts.google.com" in page.url:
            raise RuntimeError("Manual sign-in is not complete; no cleanup was started")
        log_event("session_ready", origin="photos.google.com", manual_login=True)
        todo = [item for item in state["items"] if item["status"] not in DONE]
        if limit is not None:
            todo = todo[:limit]
        for index, item in enumerate(todo, 1):
            item["attempts"] += 1
            item["status"] = "in_progress"
            item["updated_at"] = timestamp()
            save_state(state)
            log_event("item_start", index=index, total=len(todo), key=item["key"],
                      filename=item.get("filename"), catalog_status=item["immich_status"])
            try:
                status, detail = cleanup_one(page, item)
            except Exception as exc:
                status, detail = "error", f"{type(exc).__name__}: {exc}"
                with contextlib.suppress(Exception):
                    page.goto(PHOTOS_URL, wait_until="domcontentloaded", timeout=15000)
            set_status(state, item, status, detail)
            print(f"[{index}/{len(todo)}] {status}: {item.get('filename') or item['key']}", flush=True)
        context.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run", "report"))
    parser.add_argument("--limit", type=int, help="process at most this many unfinished records")
    args = parser.parse_args()
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = STATE_DIR / "runner.lock"
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another Google Photos cleanup run appears active")
        state = load_state(load_catalog())
        if args.command == "prepare":
            write_report(state)
            print(f"Prepared {len(state['items'])} unique candidates; no Google Photos changes made.")
            print(f"Report: {REPORT}")
        elif args.command == "report":
            write_report(state)
            print(REPORT)
        else:
            write_report(state)
            run_browser(state, args.limit)
            write_report(state)
            remaining = sum(item["status"] not in DONE for item in state["items"])
            print(f"Run finished. Unfinished/error records: {remaining}; report: {REPORT}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrupted. Completed item states are checkpointed; run the same command to resume.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
