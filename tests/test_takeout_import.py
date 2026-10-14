import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("takeout_import", Path(__file__).parents[1] / "takeout-import.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ImportStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_state = module.STATE
        module.STATE = Path(self.tmp.name) / ".import-state"
        module.STATE.mkdir(mode=0o700)
        self.db = module.db_connect()

    def tearDown(self):
        self.db.close()
        module.STATE = self.old_state
        self.tmp.cleanup()

    def test_inventory_filters_photos_and_associates_google_supplemental_json(self):
        source = Path(self.tmp.name) / "Google Fotos"
        album = source / "Album with spaces"
        album.mkdir(parents=True)
        (album / "video.mp4").write_bytes(b"video bytes")
        (album / "video.mp4.supplemental-metada.json").write_text('{"title":"video.mp4"}')
        (album / "photo.jpg").write_bytes(b"photo bytes")
        module.inventory(self.db, {"source": str(source)})
        rows = list(self.db.execute("SELECT path,warning FROM videos"))
        self.assertEqual([r["path"] for r in rows], ["Album with spaces/video.mp4"])
        self.assertIsNone(rows[0]["warning"])

    def test_missing_sidecar_is_warning_not_exclusion(self):
        source = Path(self.tmp.name) / "Takeout"
        source.mkdir()
        (source / "unmatched.mov").write_bytes(b"embedded metadata possible")
        module.inventory(self.db, {"source": str(source)})
        row = self.db.execute("SELECT status,warning FROM videos").fetchone()
        self.assertEqual(row["status"], "pending")
        self.assertIn("No adjacent Google JSON", row["warning"])

    def test_video_only_stage_symlinks_videos_and_json_but_excludes_photos(self):
        source = Path(self.tmp.name) / "Takeout" / "Google Fotos"
        album = source / "Album"
        album.mkdir(parents=True)
        video = album / "v.mp4"
        sidecar = album / "v.mp4.supplemental-metadata.json"
        photo = album / "p.jpg"
        video.write_bytes(b"video")
        sidecar.write_text('{"title":"v.mp4"}')
        photo.write_bytes(b"photo")
        stage = Path(self.tmp.name) / "state" / "Google Fotos"
        result = module.prepare_video_stage({"source":str(source),"import_source":str(stage)})
        self.assertEqual((result["video_paths"],result["json_sidecars"],result["other_media_staged"]),(1,1,0))
        self.assertTrue((stage / "Album" / "v.mp4").is_symlink())
        self.assertTrue((stage / "Album" / sidecar.name).is_symlink())
        self.assertFalse((stage / "Album" / "p.jpg").exists())
        self.assertEqual(photo.read_bytes(),b"photo")

    def test_pending_stage_preserves_original_tree_and_isolates_duplicate_basenames(self):
        source = Path(self.tmp.name) / "Takeout" / "Google Fotos"
        first, second = source / "Old", source / "Pending"
        first.mkdir(parents=True); second.mkdir(parents=True)
        (first / "same.mp4").write_bytes(b"old, different video")
        selected = second / "same.mp4"
        selected.write_bytes(b"pending video")
        sidecar = second / "same.mp4.supplemental-metadata.json"
        sidecar.write_text('{"title":"same.mp4"}')
        (second / "photo.jpg").write_bytes(b"photo")
        stage = Path(self.tmp.name) / "state" / "Google Fotos"
        result = module.prepare_video_stage(
            {"source":str(source),"import_source":str(stage)},
            selected_paths=["Pending/same.mp4"],
        )
        self.assertEqual((result["video_paths"],result["json_sidecars"]),(1,1))
        self.assertTrue((stage / "Pending" / "same.mp4").is_symlink())
        self.assertTrue((stage / "Pending" / sidecar.name).is_symlink())
        self.assertFalse((stage / "Old" / "same.mp4").exists())
        self.assertFalse((stage / "Pending" / "photo.jpg").exists())

    def test_duplicate_reconciliation_recovers_inflight_checkpoint_and_asset_id(self):
        source = Path(self.tmp.name) / "Takeout"
        source.mkdir()
        video = source / "folder" / "a.mp4"
        video.parent.mkdir(); video.write_bytes(b"known video")
        module.inventory(self.db, {"source": str(source)})
        sha = self.db.execute("SELECT sha1 FROM videos").fetchone()[0]
        self.db.execute("UPDATE videos SET status='in_flight'"); self.db.commit()
        cfg = {"server": "http://immich"}
        fake = {"results": [{"id": sha,"action":"reject","reason":"duplicate","assetId":"asset-123"}]}
        with patch.object(module, "api", return_value=fake):
            module.reconcile(self.db, cfg, "not-logged")
        row = self.db.execute("SELECT status,asset_id FROM videos").fetchone()
        self.assertEqual((row["status"],row["asset_id"]),("already_present","asset-123"))

    def test_duplicate_log_does_not_erase_prior_successful_upload(self):
        source = Path(self.tmp.name) / "Takeout"
        source.mkdir()
        video = source / "album" / "a.mp4"
        video.parent.mkdir(); video.write_bytes(b"video")
        module.inventory(self.db, {"source": str(source)})
        self.db.execute("UPDATE videos SET status='uploaded'"); self.db.commit()
        module.log_event({"msg":"server has duplicate","file":"Google Fotos:album/a.mp4"}, self.db)
        self.assertEqual(self.db.execute("SELECT status FROM videos").fetchone()[0], "uploaded")

    def test_structured_metadata_association_updates_video_record(self):
        source = Path(self.tmp.name) / "Takeout"; source.mkdir()
        p = source / "album" / "v.mp4"; p.parent.mkdir(); p.write_bytes(b"x")
        module.inventory(self.db, {"source": str(source)})
        module.log_event({"msg":"metadata updated","file":"Google Fotos:album/v.mp4"},self.db)
        module.log_event({"msg":"added to album","file":"Google Fotos:album/v.mp4"},self.db)
        self.assertEqual(tuple(self.db.execute("SELECT metadata_status,album_status FROM videos").fetchone()),("complete","complete"))

    def test_metadata_recovery_does_not_claim_album_work_when_not_requested(self):
        source = Path(self.tmp.name) / "Takeout"
        source.mkdir()
        (source / "v.mp4").write_bytes(b"video")
        module.inventory(self.db, {"source":str(source)})
        (module.STATE / "immich-go.jsonl").write_text(
            '{"msg":"associated metadata","file":"Google Fotos:v.mp4","json":"v.mp4.json"}\n'
            '{"msg":"added to album","file":"Google Fotos:v.mp4","album":"album"}\n')
        self.db.execute("UPDATE videos SET warning='No adjacent JSON'"); self.db.commit()
        module.restore_metadata_state(self.db, {"sync_albums":False})
        self.assertEqual(tuple(self.db.execute("SELECT metadata_status,album_status,warning FROM videos").fetchone()),("complete","not_requested",None))

    def test_low_space_guard_includes_reserve_margin_and_generated_write_factor(self):
        cfg={"reserve_bytes":100,"safety_margin_bytes":25,"write_multiplier":1.15}
        self.assertEqual(module.projected_space_need(100, cfg),240)
        self.assertFalse(module.space_allows(239,100,cfg))
        self.assertTrue(module.space_allows(240,100,cfg))
        self.assertTrue(module.space_guard_triggered(324,100,cfg))
        self.assertFalse(module.space_guard_triggered(326,100,cfg))
        self.assertTrue(module.space_guard_triggered(438,100,cfg,100))
        self.assertFalse(module.space_guard_triggered(441,100,cfg,100))

    def test_resume_space_projection_counts_only_unaccounted_unique_videos(self):
        source = Path(self.tmp.name) / "Takeout"
        source.mkdir()
        (source / "present.mp4").write_bytes(b"already in Immich")
        (source / "pending.mp4").write_bytes(b"still to upload")
        module.inventory(self.db, {"source": str(source)})
        self.db.execute("UPDATE videos SET status='already_present' WHERE path='present.mp4'")
        self.db.commit()
        expected = self.db.execute("SELECT size FROM videos WHERE path='pending.mp4'").fetchone()[0]
        self.assertEqual(module.remaining_source_bytes(self.db), expected)

    def test_low_space_shutdown_sends_interrupt_to_child_process_group(self):
        class Child:
            pid=43210
            def __init__(self): self.code=None
            def poll(self): return self.code
            def wait(self,timeout=None): self.code=130; return self.code
        child=Child()
        with patch.object(module.os,"killpg") as killpg:
            self.assertEqual(module.graceful_stop(child),130)
        killpg.assert_called_once_with(child.pid,module.signal.SIGINT)

    def test_missing_mount_sentinel_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError,"mount sentinel"):
            module.destination_free({"mount_sentinel":str(Path(self.tmp.name)/"missing"),"storage_paths":[]})

    def test_atomic_checkpoint_is_machine_readable(self):
        path=module.STATE/"progress.json"
        module.atomic_json(path,{"checkpoint":"ok"})
        self.assertEqual(json.loads(path.read_text()),{"checkpoint":"ok"})
        self.assertFalse((module.STATE/"progress.json.tmp").exists())

    def test_only_expected_immich_go_job_resume_403_is_nonfatal(self):
        self.assertTrue(module.is_nonfatal_job_resume_403({"msg":"Immich Job command sent","resume":"thumbnailGeneration","err":"403 Forbidden"}))
        self.assertFalse(module.is_nonfatal_job_resume_403({"msg":"server error","file":"v.mp4","err":"403 Forbidden"}))


if __name__ == "__main__":
    unittest.main()
