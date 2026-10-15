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
        f"- Needs review (uncertain Trash action): {counts.get('needs_review', 0)}",
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


def next_item(state):
    review = next((item for item in state["items"] if item["status"] == "needs_review"), None)
    if review:
        print(json.dumps(review, ensure_ascii=False))
        return
    for item in state["items"]:
        if item["status"] in DONE:
            continue
        if not item.get("filename"):
            set_status(state, item, "kept_ambiguous", "Catalog filename is ambiguous")
            continue
        if item["status"] != "in_progress":
            item["attempts"] += 1
            set_status(state, item, "in_progress", "Awaiting inspection in host Chrome")
        print(json.dumps(item, ensure_ascii=False))
        return
    print(json.dumps({"complete": True}))


def record_result(state, args):
    item = next((item for item in state["items"] if item["key"] == args.key), None)
    if item is None or item["status"] not in {"in_progress", "needs_review"}:
        raise RuntimeError("Result requires a known candidate claimed with next")
    if args.command == "retry" and item["status"] != "needs_review":
        raise RuntimeError("retry requires a candidate awaiting manual review")
    status = {"intent": "needs_review", "retry": "pending"}.get(args.command, args.status)
    if item["status"] == "needs_review" and status == "error":
        status = "needs_review"
    set_status(state, item, status, args.detail)
    print(json.dumps({"key": args.key, "status": status}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "next", "intent", "retry", "result", "report"))
    parser.add_argument("--key")
    parser.add_argument("--status", choices=sorted(DONE | {"error", "needs_review"}))
    parser.add_argument("--detail")
    args = parser.parse_args()
    if args.command == "result" and not all((args.key, args.status, args.detail)):
        parser.error("result requires --key, --status and --detail")
    if args.command in {"intent", "retry"} and not all((args.key, args.detail)):
        parser.error(f"{args.command} requires --key and --detail")
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
        elif args.command == "next":
            next_item(state)
        else:
            record_result(state, args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrupted. Completed item states are checkpointed; run the same command to resume.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
