from html.parser import HTMLParser
import json
from pathlib import Path
import re
import unittest

import test_monitor as helpers


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        for k, v in attrs:
            if k in ["href", "src"]:
                self.links.append(v)


class ReportTests(helpers.Harness):
    def test_baseline_displays_zero_but_keeps_unknown_comparison_data(self):
        r = self.start("2026-10-20T01:00:00Z")
        self.ingest(r, self.snapshot(range(1, 81)))
        self.finish(r)
        result = self.result(r)
        self.assertIsNone(result["counts"]["confirmedNew"])
        self.assertEqual(result["coverage"]["comparableScopes"], 0)
        run = self.root / r["relativePath"]
        self.assertIn("已确认新进：0；免费榜：0；畅销榜：0", (run / "report.md").read_text())
        self.assertIn("无排名比较依据时数量显示0", (run / "report.md").read_text())
        self.assertIn("| 1/240 | 0 | 0 |", (run.parents[1] / "index.md").read_text())
        self.assertIn("最近新进 0", (self.root / "data/test/index.html").read_text())

    def test_offline_payload_and_markdown_back_links(self):
        r = self.start("2026-10-20T01:00:00Z")
        s = self.snapshot(range(1, 81))
        s["rows"][0]["name"] = '</script><img src=x onerror="alert(1)">'
        self.ingest(r, s)
        self.finish(r)
        run = self.root / r["relativePath"]
        date = run.parents[1]
        text = (date / "index.html").read_text()
        payload = json.loads(re.search(r'<script id="payload" type="application/json">(.*?)</script>', text, re.S)[1])
        self.assertEqual(len(payload["runs"][0]["snapshots"][0]["rows"]), 80)
        self.assertEqual(payload["runs"][0]["snapshots"][0]["rows"][0]["name"], s["rows"][0]["name"])
        self.assertNotIn('<img src=x', text)
        self.assertNotIn('fetch(', text)
        self.assertTrue((self.root / "data/test/index.md").exists())
        for p in (self.root / "data/test").rglob("*.md"):
            for link in re.findall(r'\]\(([^)]+)\)', p.read_text()):
                if not link.startswith("https://"):
                    self.assertTrue((p.parent / link.split("#")[0]).exists(), (p, link))

    def test_complete_snapshot_is_immutable_and_idempotent(self):
        r = self.start("2026-10-20T01:00:00Z")
        s = self.snapshot(range(1, 81))
        self.ingest(r, s)
        self.ingest(r, s)
        self.ingest(r, self.snapshot(range(2, 82)), ok=False)


if __name__ == "__main__":
    unittest.main()
