import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1]


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="rank monitor 中文 ")
        self.root = Path(self.tmp.name)
        for name in ["config", "scripts", "assets"]:
            if (SOURCE / name).exists():
                shutil.copytree(SOURCE / name, self.root / name)
        # Ranking v1 fixtures remain independent of the separately tested release policy.
        config_path = self.root / "config/monitor.json"
        config = json.loads(config_path.read_text())
        config.pop("releasePolicy", None)
        config_path.write_text(json.dumps(config))

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args, ok=True, payload=None):
        p = subprocess.run([sys.executable, str(self.root / "scripts/monitor.py"), *args],
                           input=json.dumps(payload, ensure_ascii=False) if payload is not None else None,
                           capture_output=True, text=True, cwd="/")
        if ok:
            self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        else:
            self.assertNotEqual(p.returncode, 0, p.stdout)
        return json.loads(p.stdout) if p.stdout.strip() else {}

    def start(self, at):
        return self.cli("start", "--at", at, "--mode", "test")

    def snapshot(self, ids, chart="free", at="2026-10-20T01:00:00Z", **changes):
        snap = {"country": "us", "categoryFamily": "应用", "category": "效率",
                "genreId": "6007", "chart": chart, "device": "iphone",
                "sourceUrl": f"https://www.qimai.cn/rank/index/brand/{chart}/device/iphone/country/us/genre/6007",
                "sourceDate": "2026-10-20", "capturedAt": at, "completeTop80": True,
                "rows": [{"position": i + 1, "appId": str(n), "name": f"Synthetic {n}",
                          "developer": "Synthetic Studio", "rating": "4.5", "ratingCount": "123",
                          "latestVersionDate": "2026-10-19",
                          "qimaiUrl": f"https://www.qimai.cn/app/rank/appid/{n}/country/us"}
                         for i, n in enumerate(ids)]}
        snap.update(changes)
        return snap

    def ingest(self, run, snap, ok=True):
        return self.cli("ingest", run["runId"], "--token", run["token"], payload=snap, ok=ok)

    def finish(self, run):
        return self.cli("finalize", run["runId"], "--token", run["token"])

    def result(self, run):
        return json.loads((self.root / run["relativePath"] / "report-data.json").read_text())


class MonitorTests(Harness):
    def test_doctor_finds_portable_root(self):
        d = self.cli("doctor")
        self.assertTrue(d["ready"])
        self.assertEqual(d["plannedScopes"], 240)

    def test_doctor_rejects_incompatible_or_nonportable_history(self):
        data = self.root / "data"
        data.mkdir()
        (data / "state.json").write_text(json.dumps({"schemaVersion": 99, "runs": [], "latestRunId": None}))
        self.cli("doctor", ok=False)
        (data / "state.json").write_text(json.dumps({"schemaVersion": 1, "runs": [{"runId": "20261020T010000Z", "relativePath": "/another/computer/data/run"}], "latestRunId": "20261020T010000Z"}))
        self.cli("doctor", ok=False)

    def test_first_run_is_baseline_not_eighty_entrants(self):
        r = self.start("2026-10-20T01:00:00Z")
        self.ingest(r, self.snapshot(range(1, 81)))
        self.finish(r)
        d = self.result(r)
        self.assertEqual(d["entries"], [])
        self.assertIsNone(d["counts"]["confirmedNew"])
        self.assertEqual(d["coverage"]["comparableScopes"], 0)

    def test_or_and_dual_chart_deduplication(self):
        a = self.start("2026-10-20T01:00:00Z")
        for chart in ["free", "grossing"]:
            self.ingest(a, self.snapshot(range(1, 81), chart))
        self.finish(a)
        b = self.start("2026-10-20T04:00:00Z")
        self.ingest(b, self.snapshot([81, 82, *range(3, 81)], at="2026-10-20T04:00:00Z"))
        self.ingest(b, self.snapshot([81, *range(2, 81)], "grossing", "2026-10-20T04:00:00Z"))
        self.finish(b)
        d = self.result(b)
        self.assertEqual({e["appId"] for e in d["entries"]}, {"81", "82"})
        by_id = {e["appId"]: e for e in d["entries"]}
        self.assertEqual(by_id["81"]["triggers"], ["free", "grossing"])
        self.assertEqual(by_id["82"]["triggers"], ["free"])
        self.assertEqual(by_id["82"]["ranks"]["grossing"], None)

    def test_rank_eighty_qualifies_and_unchanged_does_not(self):
        a = self.start("2026-10-20T01:00:00Z")
        self.ingest(a, self.snapshot(range(1, 81)))
        self.finish(a)
        b = self.start("2026-10-20T04:00:00Z")
        self.ingest(b, self.snapshot([*range(1, 80), 999], at="2026-10-20T04:00:00Z"))
        self.finish(b)
        self.assertEqual([(e["appId"], e["ranks"]["free"]) for e in self.result(b)["entries"]], [("999", 80)])

    def test_failed_adjacent_run_blocks_older_fallback(self):
        a = self.start("2026-10-20T01:00:00Z")
        self.ingest(a, self.snapshot(range(1, 81)))
        self.finish(a)
        b = self.start("2026-10-20T04:00:00Z")
        self.finish(b)
        c = self.start("2026-10-20T10:00:00Z")
        self.ingest(c, self.snapshot(range(2, 82), at="2026-10-20T10:00:00Z"))
        self.finish(c)
        d = self.result(c)
        self.assertEqual(d["run"]["previousRunId"], b["runId"])
        self.assertEqual(d["entries"], [])
        self.assertIsNone(d["counts"]["confirmedNew"])

    def test_invalid_rank_scope_url_or_date_is_rejected(self):
        r = self.start("2026-10-20T01:00:00Z")
        for changes in [{"rows": self.snapshot(range(1, 80))["rows"]},
                        {"sourceUrl": "https://www.qimai.cn/rank/index/brand/free/device/iphone/country/jp/genre/6007"},
                        {"sourceDate": "2026-10-19"}, {"completeTop80": False}]:
            self.ingest(r, self.snapshot(range(1, 81), **changes), ok=False)

    def test_same_named_app_and_game_category_cannot_share_page(self):
        r = self.start("2026-10-20T01:00:00Z")
        s = self.snapshot(range(1, 81), categoryFamily="游戏", category="体育", genreId="7016",
                          title="AppStore美国iPhone体育免费榜实时排名|iOS榜单-七麦数据",
                          sourceUrl="https://www.qimai.cn/rank/index/brand/free/device/iphone/country/us/genre/6004")
        self.ingest(r, s, ok=False)

    def test_duplicate_start_and_resume_keep_one_run(self):
        a = self.start("2026-10-20T01:00:00Z")
        duplicate = self.start("2026-10-20T01:01:00Z")
        self.assertTrue(duplicate["skipped"])
        self.ingest(a, self.snapshot(range(1, 81)))
        resumed = self.cli("resume", a["runId"], "--token", a["token"])
        self.assertEqual(resumed["completedScopes"], 1)
        self.assertEqual(resumed["runId"], a["runId"])
        self.finish(a)

    def test_data_can_be_moved_without_absolute_paths(self):
        a = self.start("2026-10-20T18:01:00Z")
        self.finish(a)
        self.assertTrue(a["relativePath"].startswith("data/test/2026-10-21/"))
        other = self.root / "moved 仓库"
        other.mkdir()
        for name in ["config", "scripts", "assets", "data"]:
            shutil.copytree(self.root / name, other / name)
        old = self.root
        self.root = other
        self.cli("render", "--mode", "test")
        state = json.loads((other / "data/test/state.json").read_text())
        self.assertNotIn(str(old), json.dumps(state))


if __name__ == "__main__":
    unittest.main()
