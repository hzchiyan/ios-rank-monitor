import gzip
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from rank_monitor.core import Store, read_json, write_json
import test_monitor as helpers


class RecoveryTests(helpers.Harness):
    def test_archive_written_before_seal_can_resume_new_scope_without_loss(self):
        r = self.start("2026-10-20T01:00:00Z")
        self.ingest(r, self.snapshot(range(1, 81)))
        run = self.root / r["relativePath"]
        saved = json.loads(next((run / "checkpoints").glob("*.json")).read_text())
        # Simulate interruption after archive replacement but before manifest seal.
        with gzip.open(run / "snapshots.jsonl.gz", "wt") as f:
            f.write(json.dumps(saved) + "\n")
        self.cli("resume", r["runId"], "--token", r["token"])
        self.ingest(r, self.snapshot(range(1, 81), "grossing"))
        self.finish(r)
        with gzip.open(run / "snapshots.jsonl.gz", "rt") as f:
            snapshots = [json.loads(line) for line in f]
        self.assertEqual({s["chart"] for s in snapshots}, {"free", "grossing"})
        self.assertEqual(self.result(r)["coverage"]["completeScopes"], 2)

    def test_new_start_closes_orphaned_registered_run(self):
        a = self.start("2026-10-20T01:00:00Z")
        (self.root / "data/test/active.json").unlink()
        b = self.start("2026-10-20T04:00:00Z")
        old = json.loads((self.root / a["relativePath"] / "manifest.json").read_text())
        self.assertEqual(old["status"], "failed")
        self.ingest(a, self.snapshot(range(1, 81)), ok=False)
        self.assertEqual(json.loads((self.root / "data/test/active.json").read_text())["runId"], b["runId"])

    def test_resume_restores_missing_active_marker_for_latest_run(self):
        r = self.start("2026-10-20T01:00:00Z")
        (self.root / "data/test/active.json").unlink()
        result = self.cli("resume", r["runId"], "--token", r["token"])
        self.assertEqual(result["runId"], r["runId"])
        self.ingest(r, self.snapshot(range(1, 81)))


class DeadlineRecoveryTests(helpers.Harness):
    def test_sealed_run_with_leftover_active_does_not_block_start(self):
        store = Store(self.root, "test")
        now = datetime(2026, 10, 20, 1, tzinfo=timezone.utc)
        with patch("rank_monitor.core.utcnow", return_value=now):
            first = store.start()
            original = (store.base / "active.json").read_bytes()
            store.finalize(first["runId"], first["token"])
            (store.base / "active.json").write_bytes(original)
        with patch("rank_monitor.core.utcnow", return_value=now + timedelta(minutes=1)):
            second = store.start()
        self.assertNotIn("skipped", second)
        self.assertEqual(second["previousRunId"], first["runId"])

    def test_fixed_deadline_is_not_extended_by_ingest_or_resume(self):
        store = Store(self.root, "test")
        now = datetime(2026, 10, 20, 1, tzinfo=timezone.utc)
        with patch("rank_monitor.core.utcnow", return_value=now):
            first = store.start()
        with patch("rank_monitor.core.utcnow", return_value=now + timedelta(minutes=89)):
            store.ingest(first["runId"], first["token"], self.snapshot(range(1, 81), at="2026-10-20T02:29:00Z"))
            store.resume(first["runId"], first["token"])
        with patch("rank_monitor.core.utcnow", return_value=now + timedelta(minutes=91)):
            second = store.start()
        self.assertNotIn("skipped", second)
        self.assertEqual(store.manifest(first["runId"])["status"], "partial")
        self.assertEqual(store.snapshots(first["runId"])[0]["rows"][0]["appId"], "1")

    def test_successful_retry_removes_old_failure_reason(self):
        store = Store(self.root, "test")
        first = store.start("2026-10-20T01:00:00Z")
        snapshot = self.snapshot(range(1, 81))
        store.mark(first["runId"], first["token"], snapshot, "failed", "old failure")
        store.ingest(first["runId"], first["token"], snapshot)
        store.finalize(first["runId"], first["token"])
        status = store.manifest(first["runId"])["scopes"]["us:应用:6007:free"]
        self.assertNotIn("reason", status)
        self.assertNotIn("old failure", (store.locate(first["runId"]) / "report.md").read_text())

    def test_seal_recovered_snapshot_removes_old_failure_reason(self):
        store = Store(self.root, "test")
        first = store.start("2026-10-20T01:00:00Z")
        store.ingest(first["runId"], first["token"], self.snapshot(range(1, 81)))
        path = store.locate(first["runId"]) / "manifest.json"
        manifest = read_json(path)
        manifest["scopes"]["us:应用:6007:free"].update(status="failed", reason="old interrupted failure")
        write_json(path, manifest)
        store.finalize(first["runId"], first["token"])
        self.assertNotIn("reason", store.manifest(first["runId"])["scopes"]["us:应用:6007:free"])
