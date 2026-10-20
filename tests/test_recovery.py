import gzip
import json
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
