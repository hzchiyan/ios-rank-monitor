import json
import test_monitor as helpers


class ImportTests(helpers.Harness):
    def test_legacy_import_preserves_time_and_does_not_duplicate(self):
        snap = self.snapshot(range(1, 81))
        snap["title"] = "AppStore美国iPhone效率免费榜实时排名|iOS榜单-七麦数据"
        snap.pop("device")
        snap.pop("genreId")
        original = {"run": {"runId": "20261020T010000Z"}, "snapshots": [snap]}
        file = self.root / "legacy.json"
        file.write_text(json.dumps(original))
        first = self.cli("import-legacy", str(file))
        self.assertEqual(first["importedScopes"], 1)
        state = json.loads((self.root / "data/state.json").read_text())
        self.assertEqual(state["runs"][0]["runId"], "20261020T010000Z")
        again = self.cli("import-legacy", str(file))
        self.assertTrue(again["skipped"])
        self.assertEqual(len(json.loads((self.root / "data/state.json").read_text())["runs"]), 1)
