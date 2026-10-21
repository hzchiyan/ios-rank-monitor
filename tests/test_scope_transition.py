"""App-only plans, adjacent compatibility, legacy imports and release enrichment."""
from copy import deepcopy
import json
from pathlib import Path
import sys

import test_monitor as helpers

sys.path.insert(0, str(helpers.SOURCE / 'scripts'))
from rank_monitor.core import Store, config_id, load_config, scopes


class ScopeTransitionTests(helpers.Harness):
    def configure(self, families):
        path = self.root / 'config/monitor.json'
        config = json.loads(path.read_text())
        config['scanFamilies'] = families
        path.write_text(json.dumps(config))

    def game(self, ids=range(1, 81), at='2026-10-20T01:00:00Z'):
        return self.snapshot(ids, at=at, categoryFamily='游戏', category='体育', genreId='7016',
                             sourceUrl='https://www.qimai.cn/rank/index/brand/free/device/iphone/country/us/genre/7016')

    def historical(self):
        a = self.start('2026-10-20T01:00:00Z')
        self.ingest(a, self.snapshot(range(1, 81)))
        self.ingest(a, self.game())
        self.remove_comparison_version(a)
        self.finish(a)
        return a

    def remove_comparison_version(self, run):
        path = self.root / run['relativePath'] / 'manifest.json'
        manifest = json.loads(path.read_text())
        manifest.pop('comparisonVersion', None)
        path.write_text(json.dumps(manifest))

    def test_repository_default_is_144_app_scopes_and_new_times(self):
        config = load_config(helpers.SOURCE)
        planned = scopes(config)
        self.assertEqual(len(planned), 144)
        self.assertEqual({s['categoryFamily'] for s in planned}, {'应用'})
        self.assertEqual(len({s['genreId'] for s in planned}), 24)
        self.assertEqual(config['schedule'], ['02:00', '05:00', '11:00', '17:00'])

    def test_apps_only_doctor_start_and_pending(self):
        self.configure(['应用'])
        self.assertEqual(self.cli('doctor')['plannedScopes'], 144)
        run = self.start('2026-10-20T01:00:00Z')
        self.assertEqual(run['plannedScopes'], 144)
        pending = self.cli('pending', run['runId'])['scopes']
        self.assertEqual(len(pending), 144)
        self.assertEqual({s['categoryFamily'] for s in pending}, {'应用'})
        self.assertEqual(json.loads((self.root/run['relativePath']/'manifest.json').read_text())['comparisonVersion'], 'shared_scopes_v1')
        self.ingest(run, self.game(), ok=False)

    def test_legacy_default_and_fingerprint_remain_unchanged(self):
        config = load_config(self.root)
        self.assertEqual(len(scopes(config)), 240)
        self.assertEqual(config_id(config), '4090c32ce6378223f8a646d92ca0f7468828e6f9cffd5a29fd03b35d62b45484')
        self.configure(['应用'])
        self.assertNotEqual(config_id(load_config(self.root)), config_id(config))

    def test_invalid_scan_families_are_rejected(self):
        for value in [None, [], '', {}, ['其他'], ['应用', '应用'], ['应用', {}]]:
            with self.subTest(value=value):
                self.configure(value)
                result = self.cli('doctor', ok=False)
                self.assertIn('error', result)

    def test_family_change_during_run_is_rejected(self):
        a = self.start('2026-10-20T01:00:00Z')
        self.configure(['应用'])
        self.cli('resume', a['runId'], '--token', a['token'], ok=False)
        self.ingest(a, self.snapshot(range(1, 81)), ok=False)

    def test_scope_shrink_keeps_adjacent_app_comparison_and_old_report(self):
        a = self.historical()
        before = self.result(a)
        self.configure(['应用'])
        b = self.start('2026-10-20T04:00:00Z')
        self.ingest(b, self.snapshot([81, *range(2, 81)], at='2026-10-20T04:00:00Z'))
        self.finish(b)
        result = self.result(b)
        self.assertEqual(result['run']['previousRunId'], a['runId'])
        self.assertEqual(result['coverage']['plannedScopes'], 144)
        self.assertEqual(result['coverage']['comparableScopes'], 1)
        self.assertEqual([e['appId'] for e in result['entries']], ['81'])
        self.assertEqual(self.result(a), before)
        html = (self.root/b['relativePath']).parents[1]/'index.html'
        payload = json.loads(html.read_text().split('<script id="payload" type="application/json">')[1].split('</script>')[0])
        self.assertEqual({s['categoryFamily'] for s in payload['runs'][0]['coverage']['scopes']}, {'应用', '游戏'})
        self.assertEqual({s['categoryFamily'] for s in payload['runs'][1]['coverage']['scopes']}, {'应用'})

    def test_missing_adjacent_scope_does_not_use_older_snapshot(self):
        self.historical()
        b = self.start('2026-10-20T04:00:00Z')
        self.ingest(b, self.game(at='2026-10-20T04:00:00Z'))
        self.finish(b)
        self.configure(['应用'])
        c = self.start('2026-10-20T10:00:00Z')
        self.ingest(c, self.snapshot([81, *range(2, 81)], at='2026-10-20T10:00:00Z'))
        self.finish(c)
        result = self.result(c)
        self.assertEqual(result['run']['previousRunId'], b['runId'])
        self.assertEqual(result['coverage']['comparableScopes'], 0)
        self.assertIsNone(result['counts']['confirmedNew'])

    def test_incompatible_ranking_semantics_do_not_compare(self):
        a = self.historical()
        self.configure(['应用'])
        b = self.start('2026-10-20T04:00:00Z')
        self.ingest(b, self.snapshot([81, *range(2, 81)], at='2026-10-20T04:00:00Z'))
        store = Store(self.root, 'test')
        path = store.locate(a['runId'])/'manifest.json'
        original = json.loads(path.read_text())
        for field, value in [('ruleVersion', 'other_v1'), ('device', 'ipad'), ('cutoff', 50), ('timezone', 'UTC'), ('schemaVersion', 2), ('category', '不同来源分类')]:
            with self.subTest(field=field):
                changed = deepcopy(original)
                if field == 'category':
                    changed['config']['categories'][0]['sourceCategory'] = value
                else:
                    changed['config'][field] = value
                path.write_text(json.dumps(changed))
                self.assertEqual(store.compare(b['runId'])['coverage']['comparableScopes'], 0)
        path.write_text(json.dumps(original))
        self.assertEqual(store.compare(b['runId'])['coverage']['comparableScopes'], 1)

    def test_old_comparison_version_still_requires_whole_fingerprint(self):
        a = self.historical()
        self.configure(['应用'])
        b = self.start('2026-10-20T04:00:00Z')
        self.remove_comparison_version(b)
        self.ingest(b, self.snapshot([81, *range(2, 81)], at='2026-10-20T04:00:00Z'))
        self.finish(b)
        self.assertEqual(self.result(b)['coverage']['comparableScopes'], 0)

    def test_legacy_game_import_ignores_active_scan_family_filter(self):
        self.configure(['应用'])
        old = self.game()
        old['title'] = 'AppStore美国iPhone体育免费榜实时排名|iOS榜单-七麦数据'
        old.pop('genreId');old.pop('device')
        source = self.root/'legacy.json'
        source.write_text(json.dumps({'run': {'runId': '20261020T010000Z'}, 'snapshots': [old]}))
        result = self.cli('import-legacy', str(source), '--mode', 'test')
        self.assertEqual(result['importedScopes'], 1)
        report = json.loads((self.root/result['relativePath']/'report-data.json').read_text())
        self.assertEqual(report['coverage']['plannedScopes'], 240)
        self.assertNotIn('comparisonVersion', report['run'])
        self.assertEqual(self.cli('doctor')['plannedScopes'], 144)

    def test_release_enrichment_after_family_transition(self):
        self.historical()
        self.configure(['应用'])
        path = self.root/'config/monitor.json';config = json.loads(path.read_text())
        config['releasePolicy'] = {'version': 'qimai_first_release_6_months_v1', 'months': 6, 'sourceLabel': '最早发布'}
        path.write_text(json.dumps(config))
        b = self.start('2026-10-20T04:00:00Z')
        self.ingest(b, self.snapshot([81, *range(2, 81)], at='2026-10-20T04:00:00Z'))
        pending = self.cli('prepare-release', b['runId'], '--token', b['token'])['pending']
        self.assertEqual([e['appId'] for e in pending], ['81'])
        self.cli('ingest-release', b['runId'], '--token', b['token'], payload={
            **pending[0], 'pageAppId': '81', 'pageCountry': '美国', 'status': 'verified',
            'releaseDate': '2026-10-01', 'sourceLabel': '最早发布', 'rawText': '最早发布 2026-10-01',
            'observedAt': '2026-10-20T04:01:00Z'})
        self.finish(b)
        self.assertEqual(self.result(b)['counts']['confirmedNew'], 1)
