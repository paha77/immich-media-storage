#!/usr/bin/env python3
"""Guarded, resumable Google Photos Takeout video import for this Immich host."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
import signal
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / ".import-state"
CONFIG = ROOT / "takeout-import.toml"
CONFIG_EXAMPLE = ROOT / "takeout-import.example.toml"
VIDEO_EXTENSIONS = {".3gp", ".avi", ".flv", ".insv", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4", ".mpg", ".mts", ".webm", ".wmv"}
STOP = threading.Event()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_config():
    import tomllib
    with CONFIG.open("rb") as f:
        cfg = tomllib.load(f)
    for key in ("source", "import_source"):
        path = Path(cfg[key]).expanduser()
        if not path.is_absolute(): cfg[key] = str((ROOT / path).resolve())
    return cfg


def load_key():
    p = STATE / "credentials.env"
    if not p.is_file():
        raise RuntimeError("Missing .import-state/credentials.env")
    if p.stat().st_mode & 0o077:
        raise RuntimeError("credentials.env must have mode 600 (chmod 600 .import-state/credentials.env)")
    for line in p.read_text().splitlines():
        if line.strip().startswith("IMMICH_API_KEY="):
            key = line.split("=", 1)[1].strip().strip("\"'")
            if key:
                return key
    raise RuntimeError("credentials.env must define IMMICH_API_KEY")


def db_connect():
    STATE.mkdir(mode=0o700, exist_ok=True)
    db = sqlite3.connect(STATE / "manifest.sqlite", timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.execute("""CREATE TABLE IF NOT EXISTS videos (
        path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
        sha1 TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
        error TEXT, warning TEXT, asset_id TEXT, metadata_status TEXT NOT NULL DEFAULT 'pending',
        album_status TEXT NOT NULL DEFAULT 'pending', updated_at TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT,
        status TEXT NOT NULL, exit_code INTEGER, message TEXT
    )""")
    return db


def atomic_json(path: Path, obj):
    path.parent.mkdir(mode=0o700, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, path)
        dfd = os.open(path.parent, os.O_DIRECTORY)
        try: os.fsync(dfd)
        finally: os.close(dfd)
    finally:
        with contextlib.suppress(FileNotFoundError): os.unlink(temp)


def sidecar_candidates(path: Path):
    return (Path(str(path) + ".json"), Path(str(path) + ".supplemental-metadata.json"),
            Path(str(path) + ".supplemental-metada.json"), path.with_suffix(".json"))


def sha1_file(path: Path):
    h = hashlib.sha1()
    with path.open("rb", buffering=0) as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def inventory(db, cfg):
    source = Path(cfg["source"]).expanduser()
    if not source.is_dir(): raise RuntimeError(f"Takeout source directory missing: {source}")
    found = []
    for base, dirs, files in os.walk(source):
        dirs.sort()
        for name in sorted(files):
            p = Path(base) / name
            if p.suffix.lower() in VIDEO_EXTENSIONS and p.is_file():
                found.append(p)
    seen = {r["path"]: r for r in db.execute("SELECT * FROM videos")}
    try:
        for index, p in enumerate(found, 1):
            if STOP.is_set(): break
            rel = p.relative_to(source).as_posix()
            st = p.stat()
            old = seen.get(rel)
            if old and old["size"] == st.st_size and old["mtime_ns"] == st.st_mtime_ns:
                continue
            sha1 = sha1_file(p)
            matches = next((q for q in sidecar_candidates(p) if q.is_file()), None)
            warning = None if matches else "No adjacent Google JSON sidecar; embedded metadata will be used where available"
            db.execute("BEGIN IMMEDIATE")
            db.execute("""INSERT INTO videos(path,size,mtime_ns,sha1,status,attempts,error,warning,asset_id,metadata_status,album_status,updated_at)
                VALUES(?,?,?,?,'pending',0,NULL,?,NULL,'pending','pending',?)
                ON CONFLICT(path) DO UPDATE SET size=excluded.size,mtime_ns=excluded.mtime_ns,sha1=excluded.sha1,
                status='pending',error=NULL,warning=excluded.warning,asset_id=NULL,metadata_status='pending',album_status='pending',updated_at=excluded.updated_at""",
                (rel, st.st_size, st.st_mtime_ns, sha1, warning, now()))
            db.commit()
            if index % 25 == 0: print(f"inventory {index}/{len(found)} videos hashed", flush=True)
        paths = {p.relative_to(source).as_posix() for p in found}
        if not STOP.is_set(): db.execute("BEGIN IMMEDIATE")
        for rel in seen:
            if not STOP.is_set() and rel not in paths: db.execute("DELETE FROM videos WHERE path=?", (rel,))
        if not STOP.is_set(): db.commit()
        if not STOP.is_set():
            db.execute("UPDATE videos SET metadata_status='embedded_only' WHERE warning IS NOT NULL AND metadata_status='pending'")
            db.commit()
    except BaseException:
        db.rollback(); raise
    db.execute("UPDATE videos SET metadata_status='embedded_only' WHERE warning IS NOT NULL AND metadata_status='pending'")
    if not cfg.get("sync_albums", False): db.execute("UPDATE videos SET album_status='not_requested'")
    db.commit()
    # Emit a portable manifest snapshot without truncating or mutating source media.
    temp = STATE / "manifest.jsonl.tmp"
    with temp.open("w", encoding="utf-8") as f:
        for row in db.execute("SELECT * FROM videos ORDER BY path COLLATE BINARY"):
            f.write(json.dumps(dict(row), ensure_ascii=False) + "\n")
        f.flush(); os.fsync(f.fileno())
    os.chmod(temp, 0o600); os.replace(temp, STATE / "manifest.jsonl")


def prepare_video_stage(cfg, selected_paths=None, stage_path=None):
    source = Path(cfg["source"]).expanduser().resolve()
    stage = Path(stage_path or cfg["import_source"]).expanduser()
    stage.mkdir(mode=0o700, parents=True, exist_ok=True)
    videos = sidecars = 0
    if selected_paths is None:
        selected = []
        for base, dirs, files in os.walk(source):
            dirs.sort()
            selected.extend((Path(base) / name) for name in sorted(files)
                            if Path(name).suffix.lower() in VIDEO_EXTENSIONS)
    else:
        selected = [source / rel for rel in sorted(selected_paths)]
    selected_dirs = {p.parent for p in selected}
    stage_items = set()
    for original in selected:
        if not original.is_file() or original.suffix.lower() not in VIDEO_EXTENSIONS:
            raise RuntimeError(f"Selected source is not a supported video: {original}")
        stage_items.add(original)
    # Keep every JSON file alongside selected videos so the Google Takeout
    # importer retains supplemental metadata and folder context. Other media
    # is omitted; videos elsewhere in the source cannot suppress same-name files.
    for folder in selected_dirs:
        stage_items.update(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".json")
    for original in sorted(stage_items, key=lambda p: p.relative_to(source).as_posix()):
        rel = original.relative_to(source)
        staged = stage / rel
        staged.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        resolved = original.resolve(strict=True)
        if staged.is_symlink():
            if staged.resolve(strict=True) != resolved:
                raise RuntimeError(f"Staging link conflicts with source: {staged}")
        elif staged.exists():
            raise RuntimeError(f"Unexpected non-link in video-only staging tree: {staged}")
        else:
            staged.symlink_to(resolved)
        if original.suffix.lower() in VIDEO_EXTENSIONS: videos += 1
        else: sidecars += 1
    for base, dirs, files in os.walk(stage):
        dirs.sort()
        for name in files:
            staged = Path(base) / name
            suffix = staged.suffix.lower()
            if suffix not in VIDEO_EXTENSIONS and suffix != ".json":
                raise RuntimeError(f"Non-video media found in video-only staging tree: {staged}")
            if not staged.is_symlink() or not staged.exists():
                raise RuntimeError(f"Staged item is not a valid source symlink: {staged}")
    return {"video_paths": videos, "json_sidecars": sidecars, "other_media_staged": 0,
            "source": str(source), "import_source": str(stage)}


def restore_metadata_state(db, cfg):
    db.execute("UPDATE videos SET metadata_status=CASE WHEN warning IS NOT NULL THEN 'embedded_only' ELSE 'pending' END")
    db.execute("UPDATE videos SET album_status=?", ("pending" if cfg.get("sync_albums", False) else "not_requested",))
    log = STATE / "immich-go.jsonl"
    if log.is_file():
        with log.open(encoding="utf-8") as f:
            for line in f:
                try: event=json.loads(line)
                except json.JSONDecodeError: continue
                file=event.get("file", "")
                if not isinstance(file, str) or not file.startswith("Google Fotos:"): continue
                rel=file[len("Google Fotos:"):]
                msg=event.get("msg")
                if msg == "associated metadata":
                    db.execute("UPDATE videos SET metadata_status='complete',warning=NULL WHERE path=?", (rel,))
                elif msg == "metadata updated":
                    db.execute("UPDATE videos SET metadata_status='complete' WHERE path=?", (rel,))
                elif msg == "missing metadata":
                    db.execute("UPDATE videos SET metadata_status='embedded_only',warning=COALESCE(warning,'No matching JSON metadata reported by immich-go; embedded metadata will be used where available') WHERE path=?", (rel,))
                elif msg == "added to album" and cfg.get("sync_albums", False):
                    db.execute("UPDATE videos SET album_status='complete' WHERE path=?", (rel,))
    db.commit()


def restore_upload_history(db, log_path):
    """Keep successful uploads cumulative when later resumes see duplicates."""
    if not log_path.is_file():
        return
    with log_path.open("r", encoding="utf-8") as f:
        for line in f:
            try: row=json.loads(line)
            except json.JSONDecodeError: continue
            if row.get("msg") not in ("uploaded successfully", "server asset upgraded"): continue
            name=row.get("file")
            if isinstance(name,str) and name.startswith("Google Fotos:"):
                rel=name[len("Google Fotos:"):]
                db.execute("UPDATE videos SET status='uploaded',error=NULL WHERE path=?", (rel,))
    db.commit()


def api(cfg, key, endpoint, payload=None):
    url = cfg["server"].rstrip("/") + "/api/" + endpoint.lstrip("/")
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"x-api-key": key, "Accept": "application/json"}
    if data is not None: headers["Content-Type"] = "application/json"
    for attempt in range(4):
        req = urllib.request.Request(url, data=data, headers=headers, method="GET" if data is None else "POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as r: return json.loads(r.read())
        except (urllib.error.URLError, TimeoutError):
            if attempt == 3: raise
            time.sleep(2 ** attempt)


def batches(items, n):
    for i in range(0, len(items), n): yield items[i:i+n]


def reconcile(db, cfg, key):
    hashes = [r[0] for r in db.execute("SELECT DISTINCT sha1 FROM videos ORDER BY sha1")]
    response_map = {}
    for part in batches(hashes, 400):
        payload = {"assets": [{"id": h, "checksum": h} for h in part]}
        result = api(cfg, key, "assets/bulk-upload-check", payload)
        for item in result.get("results", []): response_map[item.get("id")] = item
    db.execute("BEGIN IMMEDIATE")
    try:
        for h in hashes:
            result = response_map.get(h)
            if not result: continue
            if result.get("action") == "reject" and result.get("reason") == "duplicate" and result.get("assetId"):
                db.execute("UPDATE videos SET asset_id=?,updated_at=? WHERE sha1=?", (result["assetId"], now(), h))
                db.execute("UPDATE videos SET status=CASE WHEN status IN ('pending','in_flight','failed','deferred_for_space') THEN 'already_present' ELSE status END WHERE sha1=?", (h,))
        db.commit()
    except BaseException:
        db.rollback(); raise
    return response_map


def destination_free(cfg):
    sentinel = cfg.get("mount_sentinel")
    if sentinel and not Path(sentinel).is_file():
        raise RuntimeError(f"Storage mount sentinel missing or stale: {sentinel}")
    values = {}
    for path in cfg["storage_paths"]:
        p = Path(path)
        if not p.is_dir(): raise RuntimeError(f"Storage check path unavailable or stale: {path}")
        values[path] = shutil.disk_usage(p).free
    return min(values.values())


def projected_space_need(source_bytes, cfg):
    return cfg["reserve_bytes"] + cfg["safety_margin_bytes"] + math.ceil(source_bytes * cfg["write_multiplier"])


def space_allows(free_bytes, source_bytes, cfg, active_buffer_bytes=0):
    return free_bytes >= projected_space_need(source_bytes, cfg) + active_buffer_bytes


def space_guard_triggered(free_bytes, active_video_bytes, cfg, remaining_source_bytes=0):
    required = projected_space_need(remaining_source_bytes, cfg) + 2 * active_video_bytes
    return free_bytes <= required


def remaining_source_bytes(db):
    return sum(row[0] for row in db.execute("""SELECT MAX(size) FROM videos
        WHERE status IN ('pending','in_flight','failed','deferred_for_space') GROUP BY sha1"""))


def graceful_stop(child, grace_seconds=30):
    if child.poll() is None:
        with contextlib.suppress(ProcessLookupError): os.killpg(child.pid, signal.SIGINT)
        try: child.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError): os.killpg(child.pid, signal.SIGTERM)
            try: child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError): os.killpg(child.pid, signal.SIGKILL)
                child.wait()
    return child.poll()


def byte_fmt(n):
    units = ("B", "KB", "MB", "GB", "TB")
    x = float(n)
    for u in units:
        if x < 1000 or u == "TB": return f"{x:.1f} {u}"
        x /= 1000


def counts(db):
    rows = list(db.execute("SELECT sha1,MAX(size) size,MIN(status) status,MAX(warning IS NOT NULL) warn FROM videos GROUP BY sha1"))
    statuses = {s: 0 for s in ("pending", "in_flight", "uploaded", "already_present", "failed", "deferred_for_space")}
    by_hash = {}
    for r in db.execute("SELECT sha1,status FROM videos"):
        by_hash.setdefault(r["sha1"], set()).add(r["status"])
    priority = ("uploaded", "already_present", "in_flight", "failed", "deferred_for_space", "pending")
    for found in by_hash.values():
        status = next((s for s in priority if s in found), "pending")
        statuses[status] += 1
    statuses["unique_total"] = len(rows)
    statuses["source_files"] = db.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
    statuses["metadata_warning_count"] = sum(r["warn"] for r in rows)
    for field, column in (("metadata_complete", "metadata_status"), ("album_complete", "album_status")):
        statuses[field] = db.execute(f"SELECT COUNT(*) FROM videos WHERE {column}='complete'").fetchone()[0]
        statuses[field.replace("complete", "pending")] = db.execute(f"SELECT COUNT(*) FROM videos WHERE {column}='pending'").fetchone()[0]
    statuses["album_not_requested"] = db.execute("SELECT COUNT(*) FROM videos WHERE album_status='not_requested'").fetchone()[0]
    statuses["metadata_embedded_only"] = db.execute("SELECT COUNT(*) FROM videos WHERE metadata_status='embedded_only'").fetchone()[0]
    statuses["remaining"] = statuses["pending"] + statuses["in_flight"] + statuses["failed"] + statuses["deferred_for_space"]
    statuses["accounted_bytes"] = sum(r["size"] for r in rows if any(s in by_hash[r["sha1"]] for s in ("uploaded", "already_present")))
    statuses["uploaded_bytes"] = sum(r["size"] for r in rows if "uploaded" in by_hash[r["sha1"]])
    return statuses


def write_progress(db, cfg):
    c = counts(db)
    unique = list(db.execute("SELECT sha1,MAX(size) size FROM videos GROUP BY sha1"))
    c["source_bytes_unique"] = sum(r["size"] for r in unique)
    c["remaining_bytes"] = max(0, c["source_bytes_unique"] - c["accounted_bytes"])
    seconds = 0
    for r in db.execute("SELECT started_at,ended_at FROM runs"):
        try:
            a=datetime.fromisoformat(r["started_at"]); b=datetime.fromisoformat(r["ended_at"]) if r["ended_at"] else datetime.now(timezone.utc)
            seconds += max(0, int((b-a).total_seconds()))
        except (TypeError,ValueError): pass
    c["elapsed_seconds_cumulative"] = seconds
    c["average_upload_bytes_per_second"] = int(c["uploaded_bytes"] / seconds) if seconds else None
    c["eta_seconds"] = int(c["remaining_bytes"] / c["average_upload_bytes_per_second"]) if c["average_upload_bytes_per_second"] else None
    c.update({"updated_at": now(), "server": cfg["server"], "target_email": cfg["target_email"],
              "destination_free_bytes": destination_free(cfg), "reserve_bytes": cfg["reserve_bytes"],
              "safety_margin_bytes": cfg["safety_margin_bytes"], "current_video": None,
              "upload_progress": "File-level status only; immich-go 0.32.0 JSON log has no per-file byte-progress field"})
    incident = STATE / "photo-import-incident.json"
    if incident.is_file():
        with contextlib.suppress(Exception): c["prior_photo_import_incident"] = json.loads(incident.read_text(encoding="utf-8"))
    running = STATE / "current.json"
    if running.exists():
        with contextlib.suppress(Exception): c["current_video"] = json.loads(running.read_text()).get("current_video")
    atomic_json(STATE / "progress.json", c)
    return c


def log_event(row, db):
    msg = row.get("msg", "")
    f = row.get("file")
    if not isinstance(f, str): return
    prefix = "Google Fotos:"
    if not f.startswith(prefix): return
    rel = f[len(prefix):]
    video = db.execute("SELECT size FROM videos WHERE path=?", (rel,)).fetchone()
    if video is None: return
    if row.get("msg") in ("uploading", "upload started", "processing video", "associated metadata", "missing metadata"):
        try:
            cp=STATE/"current.json"
            current=json.loads(cp.read_text()) if cp.exists() else {}
            current["current_video"]={"path":rel,"size_bytes":video[0],"event":row.get("msg"),"file_level_only":True}
            atomic_json(cp,current)
        except Exception: pass
    db.execute("UPDATE videos SET updated_at=? WHERE path=?", (now(), rel))
    if msg in ("uploaded successfully", "server asset upgraded"):
        db.execute("UPDATE videos SET status='uploaded',error=NULL,updated_at=? WHERE path=?", (now(), rel))
    elif msg == "server has duplicate":
        # Preserve durable provenance when a later resume sees an asset that
        # this importer already confirmed uploading in an earlier run.
        db.execute("UPDATE videos SET status=CASE WHEN status='uploaded' THEN status ELSE 'already_present' END,updated_at=? WHERE path=?", (now(), rel))
    elif msg in ("server error", "upload error", "processing error"):
        error = str(row.get("error", row.get("reason", msg)))[:2000]
        db.execute("UPDATE videos SET status='failed',error=?,attempts=attempts+1,updated_at=? WHERE path=?", (error, now(), rel))
    elif msg == "missing metadata":
        db.execute("UPDATE videos SET metadata_status='embedded_only',warning=COALESCE(warning,'No matching JSON metadata reported by immich-go; embedded metadata will be used where available'),updated_at=? WHERE path=?", (now(), rel))
    elif msg == "associated metadata":
        db.execute("UPDATE videos SET metadata_status='complete',warning=NULL,updated_at=? WHERE path=?", (now(), rel))
    elif msg == "metadata updated":
        db.execute("UPDATE videos SET metadata_status='complete',updated_at=? WHERE path=?", (now(), rel))
    elif msg == "added to album":
        db.execute("UPDATE videos SET album_status='complete',updated_at=? WHERE path=?", (now(), rel))


def is_nonfatal_job_resume_403(row):
    return (row.get("msg") == "Immich Job command sent" and bool(row.get("resume"))
            and "403" in str(row.get("err", "")))


def publish_manifest(db):
    temp = STATE / "manifest.jsonl.tmp"
    with temp.open("w", encoding="utf-8") as f:
        for row in db.execute("SELECT * FROM videos ORDER BY path COLLATE BINARY"):
            f.write(json.dumps(dict(row), ensure_ascii=False) + "\n")
        f.flush(); os.fsync(f.fileno())
    os.chmod(temp, 0o600); os.replace(temp, STATE / "manifest.jsonl")


def write_human_summary(data):
    lines = [
        "# Google Photos video import status", "",
        f"Updated: {data.get('updated_at')}", f"Run status: {data.get('run_status','not started')}",
        f"Target: {data.get('target_email')}", f"Unique videos: {data.get('unique_total',0)}",
        f"Source video paths: {data.get('source_files',0)}", f"Completed: {data.get('uploaded',0)+data.get('already_present',0)}",
        f"Newly uploaded: {data.get('uploaded',0)} ({byte_fmt(data.get('uploaded_bytes',0))} transferred)",
        f"Already present: {data.get('already_present',0)}", f"Failed: {data.get('failed',0)}",
        f"Deferred for space: {data.get('deferred_for_space',0)}", f"Remaining: {data.get('remaining',0)}",
        f"Metadata warnings: {data.get('metadata_warning_count',0)}", f"Source bytes (unique): {byte_fmt(data.get('source_bytes_unique',0))}",
        f"Metadata complete paths: {data.get('metadata_complete',0)}; embedded-only: {data.get('metadata_embedded_only',0)}; pending: {data.get('metadata_pending',0)}",
        f"Album complete paths: {data.get('album_complete',0)}; pending/unconfirmed: {data.get('album_pending',0)}; not requested: {data.get('album_not_requested',0)}",
        f"Bytes accounted for: {byte_fmt(data.get('accounted_bytes',0))}",
        f"Destination free: {data.get('destination_free_bytes',0)} bytes",
        f"Reserve: {data.get('reserve_bytes',0)} bytes; safety margin: {data.get('safety_margin_bytes',0)} bytes",
        f"Elapsed seconds: {data.get('elapsed_seconds_cumulative',data.get('elapsed_seconds',0))}",
        f"Average upload bytes/sec: {data.get('average_upload_bytes_per_second')}",
        f"ETA seconds: {data.get('eta_seconds')}", "",
        "Current per-file byte progress is not exposed by this immich-go build; live progress is file-level.",
        "Google-specific fields without an Immich/immich-go mapping cannot be transferred.", ""
    ]
    incident=data.get("prior_photo_import_incident")
    if incident:
        lines[0:0]=[f"Prior interrupted run uploaded {incident.get('photo_uploads',0)} photographs and created {len(incident.get('created_albums',[]))} album(s) before it was stopped. No cleanup was performed.", ""]
    p=STATE/"summary.md"; temp=p.with_suffix(".md.tmp")
    temp.write_text("\n".join(lines),encoding="utf-8"); os.chmod(temp,0o600); os.replace(temp,p)


def run_import(resume=False):
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    cfg = load_config(); key = load_key(); db = db_connect()
    lock = (STATE / "import.lock").open("a+")
    try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError: raise RuntimeError("Another import process holds .import-state/import.lock")
    inventory(db, cfg)
    if STOP.is_set():
        publish_manifest(db)
        raise RuntimeError("Stopped during inventory; no upload was started")
    restore_metadata_state(db, cfg)
    restore_upload_history(db, STATE/"immich-go.jsonl")
    me = api(cfg, key, "users/me")
    if me.get("email", "").lower() != cfg["target_email"].lower(): raise RuntimeError("API key account does not match configured target_email")
    # Verify server version and exact intended owner before any upload.
    about = api(cfg, key, "server/about")
    if str(about.get("version", "")).lstrip("v") != cfg["immich_version"].lstrip("v"):
        raise RuntimeError(f"Immich version changed: expected {cfg['immich_version']}, got {about.get('version')}")
    existing = reconcile(db, cfg, key)
    # Work left in-flight by a stopped prior process is no longer active. Duplicate
    # reconciliation above gets first chance to confirm it before it becomes retryable.
    db.execute("UPDATE videos SET status='pending',updated_at=? WHERE status='in_flight'", (now(),))
    # Space is needed only for unique content not yet confirmed in the target
    # account. Requiring room for already-present assets makes late resumes
    # fail even when the remaining batch easily fits.
    projected = remaining_source_bytes(db)
    # Conservatively allow up to 15% for near-term generated files; safety margin is additional.
    largest_video = db.execute("SELECT COALESCE(MAX(size),0) FROM videos WHERE status IN ('pending','in_flight','failed','deferred_for_space')").fetchone()[0]
    need = projected_space_need(projected, cfg) + 2 * largest_video
    quota_size = me.get("quotaSizeInBytes")
    quota_used = me.get("quotaUsageInBytes") or 0
    if quota_size:
        quota_free = max(0, quota_size - quota_used)
        if quota_free < projected:
            raise RuntimeError(f"Account quota has {quota_free} bytes free for {projected} bytes of unique source videos")
    free = destination_free(cfg)
    if not space_allows(free, projected, cfg, 2 * largest_video):
        db.execute("UPDATE videos SET status='deferred_for_space' WHERE status='pending'"); db.commit(); publish_manifest(db)
        raise RuntimeError(f"Space guard: {free} bytes free; require {need} bytes before starting")
    db.execute("UPDATE videos SET status='pending',updated_at=? WHERE status='deferred_for_space'", (now(),))
    db.commit()
    selected_paths = [r[0] for r in db.execute("SELECT path FROM videos WHERE status='pending' ORDER BY path COLLATE BINARY")]
    run_stage = STATE / "pending-video-stages" / f"run-{os.getpid()}" / "Google Fotos"
    stage_info = prepare_video_stage(cfg, selected_paths=selected_paths, stage_path=run_stage)
    print(f"video-only stage ready: {stage_info['video_paths']} video paths, {stage_info['json_sidecars']} JSON files, {stage_info['other_media_staged']} other media", flush=True)
    run_id = db.execute("INSERT INTO runs(started_at,status,message) VALUES(?, 'running',?)", (now(), "resume" if resume else "initial")).lastrowid
    db.commit()
    log_path = STATE / "immich-go.jsonl"
    log_path.touch(mode=0o600, exist_ok=True)
    os.chmod(log_path, 0o600)
    # Logs are append-only and resume-safe; annotate each invocation with byte offset.
    log_start = log_path.stat().st_size
    current = {"run_id": run_id, "started_at": now(), "current_video": None}
    atomic_json(STATE / "current.json", current)
    db.execute("UPDATE videos SET status='in_flight',attempts=attempts+1,updated_at=? WHERE status='pending'", (now(),)); db.commit()
    config_fd, config_path = tempfile.mkstemp(prefix="immich-go-", suffix=".json", dir="/dev/shm")
    os.fchmod(config_fd, 0o600)
    with os.fdopen(config_fd, "w", encoding="utf-8") as f:
        json.dump({"upload": {"server": cfg["server"], "api-key": key}}, f)
    cmd = [str(STATE / "bin/immich-go"), "upload", "from-google-photos",
           "--config=" + config_path,
           "--include-type=VIDEO", "--include-unmatched", "--include-archived", "--include-partner",
           "--sync-albums=false", "--people-tag", "--takeout-tag",
           "--concurrent-tasks=1", "--on-errors=continue", "--pause-immich-jobs=false",
           "--no-ui", "--log-type=JSON", "--log-file", str(log_path), str(stage_info["import_source"])]
    child = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    pos = log_start
    last_check = 0.0
    started = time.monotonic()
    last_bytes = 0
    try:
        while child.poll() is None:
            time.sleep(cfg["poll_seconds"])
            try:
                with log_path.open("r", encoding="utf-8") as f:
                    f.seek(pos)
                    # Do not iterate over the text stream: tell() is disabled
                    # by Python's buffered iterator, which left the durable
                    # byte checkpoint at the start of the log on long runs.
                    while True:
                        line = f.readline()
                        if not line:
                            break
                        pos = f.tell()
                        try: row = json.loads(line)
                        except json.JSONDecodeError: continue
                        if row.get("msg") in ("uploading", "upload started", "processing video"):
                            current["current_video"] = row.get("file")
                            atomic_json(STATE / "current.json", current)
                        log_event(row, db)
                    db.commit()
            except (OSError, ValueError):
                pass
            if time.monotonic() - last_check >= cfg["poll_seconds"]:
                check_time = time.monotonic()
                sample_seconds = max(0.1, check_time - last_check) if last_check else cfg["poll_seconds"]
                free = destination_free(cfg)
                active = current.get("current_video")
                active_size = 0
                if isinstance(active, dict):
                    rel = active.get("path")
                    row = db.execute("SELECT size FROM videos WHERE path=?", (rel,)).fetchone()
                    if row: active_size = row[0]
                if space_guard_triggered(free, active_size, cfg, remaining_source_bytes(db)):
                    current["stop_reason"] = "space threshold reached"
                    atomic_json(STATE / "current.json", current)
                    graceful_stop(child)
                    break
                last_check = check_time
                progress = write_progress(db, cfg)
                transferred = progress["uploaded_bytes"] - last_bytes
                last_bytes = progress["uploaded_bytes"]
                elapsed = max(1, int(time.monotonic() - started))
                print(f"{now()} unique={progress['unique_total']} done={progress['uploaded']+progress['already_present']} uploaded={progress['uploaded']} existing={progress['already_present']} failed={progress['failed']} remaining={progress['remaining']} dest_free={free} reserve={cfg['reserve_bytes']} margin={cfg['safety_margin_bytes']} recent_transferred={transferred}B rate={int(transferred/sample_seconds)}B/s elapsed={elapsed}s eta={progress['eta_seconds']}s", flush=True)
            if STOP.is_set():
                graceful_stop(child)
                break
        # Drain final structured records before reconciling.
        with log_path.open("r", encoding="utf-8") as f:
            f.seek(pos)
            while True:
                line = f.readline()
                if not line:
                    break
                pos = f.tell()
                try: log_event(json.loads(line), db)
                except (json.JSONDecodeError, sqlite3.Error): pass
        db.commit()
        code = child.poll()
        if code is None: code = 128 + (signal.SIGINT if STOP.is_set() else signal.SIGTERM)
        expected_resume_error = False
        unrelated_cli_error = False
        with log_path.open("r", encoding="utf-8") as f:
            f.seek(log_start)
            for line in f:
                try: row=json.loads(line)
                except json.JSONDecodeError: continue
                if is_nonfatal_job_resume_403(row):
                    expected_resume_error=True
                elif row.get("level")=="ERROR" and row.get("msg")!="Some errors have occurred. Look at the log file for details\n":
                    unrelated_cli_error=True
        try: reconcile(db, cfg, key)
        except Exception as e:
            db.execute("UPDATE videos SET status='pending',error=? WHERE status='in_flight'", ("Reconciliation unavailable: " + str(e)[:1000],)); db.commit()
        if current.get("stop_reason"):
            db.execute("UPDATE videos SET status='deferred_for_space' WHERE status='in_flight'"); run_status="deferred_for_space"
        elif STOP.is_set():
            db.execute("UPDATE videos SET status='pending' WHERE status='in_flight'"); run_status="interrupted"
        else:
            db.execute("UPDATE videos SET status='pending' WHERE status='in_flight'")
            current_counts=counts(db)
            fully_reconciled=(current_counts["failed"]==0 and current_counts["pending"]==0 and current_counts["in_flight"]==0 and current_counts["deferred_for_space"]==0)
            if code == 0 or (expected_resume_error and not unrelated_cli_error and fully_reconciled):
                run_status="complete_with_job_resume_warning" if expected_resume_error and code != 0 else "complete"
            else: run_status="failed"
        db.execute("UPDATE runs SET ended_at=?,status=?,exit_code=?,message=? WHERE id=?", (now(),run_status,code,current.get("stop_reason"),run_id)); db.commit()
        publish_manifest(db)
        final=write_progress(db,cfg); final.update({"run_status":run_status,"run_id":run_id,"elapsed_seconds":int(time.monotonic()-started)})
        final["immich_go_nonfatal_job_resume_403"]=expected_resume_error and code != 0 and run_status=="complete_with_job_resume_warning"
        atomic_json(STATE/"summary.json",final)
        write_human_summary(final)
        print(json.dumps(final,ensure_ascii=False,indent=2))
        pidfile=STATE/"runner.pid"
        if pidfile.exists():
            with contextlib.suppress(Exception):
                if json.loads(pidfile.read_text()).get("pid")==os.getpid(): pidfile.unlink()
        return 0 if run_status in ("complete","complete_with_job_resume_warning","deferred_for_space","interrupted") else 1
    finally:
        if child.poll() is None:
            with contextlib.suppress(Exception): graceful_stop(child)
        with contextlib.suppress(FileNotFoundError): (STATE / "current.json").unlink()
        with contextlib.suppress(FileNotFoundError): os.unlink(config_path)
        lock.close(); db.close()


def status():
    cfg=load_config(); db=db_connect(); print(json.dumps(write_progress(db,cfg),ensure_ascii=False,indent=2)); db.close()


def start(resume=False):
    pidfile=STATE/"runner.pid"
    if pidfile.exists():
        try:
            pid=json.loads(pidfile.read_text())["pid"]
            cmdline=Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0",b" ").decode(errors="replace")
            if str(Path(__file__).resolve()) in cmdline and " run" in cmdline:
                raise RuntimeError(f"Importer already running (pid {pid})")
        except (ProcessLookupError, FileNotFoundError, ValueError, KeyError, json.JSONDecodeError):
            pass
        pidfile.unlink(missing_ok=True)
    out=(STATE/"runner.log").open("a",buffering=1)
    p=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),"run"]+( ["--resume"] if resume else []),
                       stdin=subprocess.DEVNULL,stdout=out,stderr=subprocess.STDOUT,start_new_session=True,close_fds=True)
    atomic_json(pidfile,{"pid":p.pid,"started_at":now()})
    print(f"Started importer pid {p.pid}; logs: {STATE/'runner.log'}")


def stop():
    p=STATE/"runner.pid"
    if not p.exists(): print("No runner pid file"); return
    data=json.loads(p.read_text()); pid=data["pid"]
    try:
        os.kill(pid, 0)
        os.kill(pid,signal.SIGTERM); print(f"Sent SIGTERM to importer runner {pid}; it will checkpoint and stop the immich-go child")
    except (ProcessLookupError, FileNotFoundError):
        p.unlink(missing_ok=True); print("Importer is not running")


def repair_state():
    cfg=load_config(); db=db_connect(); inventory(db,cfg); restore_metadata_state(db,cfg); publish_manifest(db)
    # Rebuild the cumulative successful-upload state from append-only structured
    # logs. This also repairs checkpoints made by earlier wrapper versions.
    restore_upload_history(db, STATE/"immich-go.jsonl")
    publish_manifest(db)
    prior={}
    with contextlib.suppress(Exception): prior=json.loads((STATE/"summary.json").read_text(encoding="utf-8"))
    result=write_progress(db,cfg)
    for key in ("run_status","run_id","elapsed_seconds","immich_go_nonfatal_job_resume_403"):
        if key in prior: result[key]=prior[key]
    atomic_json(STATE/"summary.json",result); write_human_summary(result); print(json.dumps(result,ensure_ascii=False,indent=2)); db.close()


def main():
    parser=argparse.ArgumentParser(description="Guarded Google Takeout video importer")
    parser.add_argument("command",choices=("inventory","stage","start","run","resume","status","stop","retry","repair-state"))
    parser.add_argument("--resume",action="store_true")
    args=parser.parse_args()
    if args.command=="inventory":
        cfg=load_config(); db=db_connect(); inventory(db,cfg); c=counts(db); print(json.dumps(c,indent=2)); db.close()
    elif args.command=="stage":
        print(json.dumps(prepare_video_stage(load_config()),ensure_ascii=False,indent=2))
    elif args.command=="repair-state": repair_state()
    elif args.command=="status": status()
    elif args.command=="stop": stop()
    elif args.command=="start": start(False)
    elif args.command=="resume": start(True)
    elif args.command=="run": raise SystemExit(run_import(args.resume))
    elif args.command=="retry":
        db=db_connect(); db.execute("UPDATE videos SET status='pending',error=NULL,updated_at=? WHERE status='failed'",(now(),)); n=db.execute("SELECT changes()").fetchone()[0]; db.commit(); publish_manifest(db); print(f"Reset {n} failed paths"); db.close()


if __name__=="__main__":
    try: main()
    except Exception as e:
        print(f"ERROR: {e}",file=sys.stderr); raise SystemExit(1)
