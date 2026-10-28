"""Per-scope latest compatible observations, frozen references and historic reports."""
from copy import deepcopy
import gzip
import json
import shutil
import subprocess
import sys
from unittest.mock import patch

import test_monitor as helpers
import test_releases as release_helpers

sys.path.insert(0, str(helpers.SOURCE/'scripts'))
from rank_monitor.core import Store, load_config, read_json, write_json

VERSION = 'latest_valid_scope_v1'
KEY = 'us:应用:6007:free'


class LatestValidTests(helpers.Harness):
    def setUp(self):
        super().setUp()
        self.configure()
        self.store = Store(self.root, 'test')

    def configure(self, version=VERSION):
        path = self.root/'config/monitor.json'
        config = read_json(path)
        config.update(comparisonVersion=version, scanFamilies=['应用'])
        write_json(path, config)

    def begin(self, at='2026-10-20T01:00:00Z'):
        return self.store.start(at)

    def save(self, run, ids=range(1, 81), chart='free', **changes):
        at = changes.pop('at', self.store.manifest(run['runId'])['startedAt'])
        snap = self.snapshot(ids, chart, at, sourceDate=at[:10], **changes)
        self.store.ingest(run['runId'], run['token'], snap)

    def close(self, run):
        return self.store.finalize(run['runId'], run['token'])

    def report(self, run):
        return self.store.compare(run['runId'])

    def baseline(self, run, key=KEY):
        return next(s for s in self.report(run)['coverage']['scopes'] if ':'.join(
            [s['country'], s['categoryFamily'], s['genreId'], s['chart']]) == key)['baseline']

    def pair_with_gap(self):
        a = self.begin(); self.save(a); self.close(a)
        b = self.begin('2026-10-20T04:00:00Z'); self.close(b)
        c = self.begin('2026-10-20T10:00:00Z'); self.save(c, range(2, 82))
        return a, b, c

    def test_repository_enables_new_comparison_version(self):
        self.assertEqual(load_config(helpers.SOURCE).get('comparisonVersion'), VERSION)

    def test_failed_run_does_not_break_next_scope_comparison(self):
        a, b, c = self.pair_with_gap()
        r = self.report(c)
        self.assertEqual(r['run']['previousRunId'], b['runId'])
        self.assertEqual([e['appId'] for e in r['entries']], ['81'])
        base = self.baseline(c)
        self.assertEqual(base['baselineRunId'], a['runId'])
        self.assertEqual(base['skippedRuns'], 1)
        self.assertEqual(base['elapsedSeconds'], 9*3600)
        self.assertEqual(r['coverage']['fallbackScopes'], 1)

    def test_each_chart_and_country_selects_its_own_latest_snapshot(self):
        a = self.begin(); self.save(a); self.save(a, chart='grossing'); self.close(a)
        b = self.begin('2026-10-20T04:00:00Z'); self.save(b, chart='grossing'); self.close(b)
        c = self.begin('2026-10-20T10:00:00Z')
        self.save(c, range(2, 82)); self.save(c, range(2, 82), chart='grossing')
        self.assertEqual(self.baseline(c)['baselineRunId'], a['runId'])
        self.assertEqual(self.baseline(c, 'us:应用:6007:grossing')['baselineRunId'], b['runId'])
        self.assertIsNone(self.baseline(c, 'de:应用:6007:free'))
        entry = self.report(c)['entries'][0]
        self.assertEqual(entry['triggers'], ['free', 'grossing'])
        self.assertEqual(entry['comparisonBaselines']['free']['elapsedSeconds'], 32400)
        self.assertEqual(entry['comparisonBaselines']['grossing']['elapsedSeconds'], 21600)

    def test_no_lookback_age_limit_and_new_complete_becomes_next_baseline(self):
        a = self.begin(); self.save(a); self.close(a)
        b = self.begin('2027-04-21T01:00:00Z'); self.save(b, range(2, 82)); self.close(b)
        self.assertEqual(self.baseline(b)['baselineRunId'], a['runId'])
        self.assertGreater(self.baseline(b)['elapsedSeconds'], 180*86400)
        c = self.begin('2027-04-21T04:00:00Z'); self.save(c, range(2, 82))
        self.assertEqual(self.baseline(c)['baselineRunId'], b['runId'])
        self.assertEqual(self.report(c)['entries'], [])

    def test_missing_current_and_missing_history_remain_unknown(self):
        a = self.begin(); self.save(a); self.close(a)
        b = self.begin('2026-10-20T04:00:00Z')
        self.save(b, chart='grossing')
        r = self.report(b)
        self.assertEqual(r['entries'], [])
        self.assertIsNone(r['counts']['confirmedNew'])
        self.assertEqual(r['coverage']['noBaselineScopes'], 1)
        self.assertEqual(r['coverage']['currentMissingScopes'], 143)

    def test_skips_recent_incompatible_scope_for_older_compatible_one(self):
        a = self.begin(); self.save(a); self.close(a)
        b = self.begin('2026-10-20T04:00:00Z'); self.save(b); self.close(b)
        path = self.store.locate(b['runId'])/'manifest.json'
        m = read_json(path); m['config']['categories'][0]['sourceCategory'] = 'other category'; write_json(path, m)
        c = self.begin('2026-10-20T10:00:00Z'); self.save(c, range(2, 82))
        self.assertEqual(self.baseline(c)['baselineRunId'], a['runId'])

    def test_legacy_version_is_preserved_but_its_snapshot_can_be_used(self):
        self.configure('shared_scopes_v1'); self.store = Store(self.root, 'test')
        a = self.begin(); self.save(a); self.close(a)
        b = self.begin('2026-10-20T04:00:00Z'); self.close(b)
        self.configure(); self.store = Store(self.root, 'test')
        c = self.begin('2026-10-20T10:00:00Z'); self.save(c, range(2, 82))
        self.assertEqual(self.baseline(c)['baselineRunId'], a['runId'])
        self.assertEqual(self.store.manifest(a['runId'])['comparisonVersion'], 'shared_scopes_v1')

    def test_recovery_keeps_frozen_refs_and_rejects_missing_or_changed_file(self):
        a, _, c = self.pair_with_gap()
        path = self.store.locate(c['runId'])/'baselines.json'
        data = path.read_bytes()
        self.store.resume(c['runId'], c['token'])
        self.assertEqual(path.read_bytes(), data)
        path.unlink()
        with self.assertRaisesRegex(ValueError, '基准'):
            self.store.resume(c['runId'], c['token'])
        path.write_bytes(data)
        x = read_json(path); x['scopes'][KEY]['baselineRunId'] = c['runId']; write_json(path, x)
        with self.assertRaisesRegex(ValueError, '基准'):
            self.report(c)

    def test_changed_or_missing_selected_snapshot_cannot_silently_fallback(self):
        a, _, c = self.pair_with_gap()
        path = self.store.locate(a['runId'])/'snapshots.jsonl.gz'
        rows = self.store.snapshots(a['runId']); rows[0]['rows'][0]['name'] = 'changed'
        with gzip.open(path, 'wt', encoding='utf-8') as f:
            f.write(json.dumps(rows[0])+'\n')
        with self.assertRaisesRegex(ValueError, '基准|摘要'):
            self.report(c)

    def test_broken_latest_complete_archive_is_not_treated_as_normal_missing(self):
        a = self.begin(); self.save(a); self.close(a)
        (self.store.locate(a['runId'])/'snapshots.jsonl.gz').unlink()
        with self.assertRaisesRegex(ValueError, '归档|快照'):
            self.begin('2026-10-20T04:00:00Z')

    def test_formal_history_cannot_use_test_snapshots(self):
        a = self.begin(); self.save(a); self.close(a)
        formal = Store(self.root)
        b = formal.start()
        refs = read_json(formal.locate(b['runId'])/'baselines.json')
        self.assertTrue(all(v is None for v in refs['scopes'].values()))

    def test_release_candidates_after_fallback_use_same_fixed_reference(self):
        a = self.begin(); self.save(a); self.close(a)
        b = self.begin('2026-10-20T04:00:00Z'); self.close(b)
        path = self.root/'config/monitor.json'; config = read_json(path)
        config['releasePolicy'] = release_helpers.POLICY; write_json(path, config)
        self.store = Store(self.root, 'test')
        c = self.begin('2026-10-20T10:00:00Z'); self.save(c, range(2, 82))
        pending = self.store.prepare_release(c['runId'], c['token'])['pending']
        self.assertEqual([e['appId'] for e in pending], ['81'])
        evidence = release_helpers.ReleaseTests.evidence(self, 81, observedAt='2026-10-20T10:10:00Z')
        self.store.ingest_release(c['runId'], c['token'], evidence)
        self.close(c)
        self.assertEqual(self.report(c)['counts']['confirmedNew'], 1)

    def test_archived_reports_are_reused_without_recomparing_old_runs(self):
        a = self.begin(); self.save(a); self.close(a)
        folder = self.store.locate(a['runId'])
        before = {p.name:p.read_bytes() for p in folder.glob('report*')}
        original = self.store.compare
        def only_new(run_id):
            if run_id == a['runId']:
                raise AssertionError('Historical report must not be recomputed')
            return original(run_id)
        b = self.begin('2026-10-20T04:00:00Z'); self.save(b, range(2, 82))
        with patch.object(self.store, 'compare', side_effect=only_new):
            self.close(b)
            self.store.render()
        self.assertEqual({p.name:p.read_bytes() for p in folder.glob('report*')}, before)

    def test_render_does_not_rewrite_existing_historical_daily_pages(self):
        a = self.begin(); self.save(a); self.close(a)
        path = self.store.locate(a['runId']).parents[1]/'index.html'
        before = path.read_bytes()
        (self.root/'assets/report.html').write_text('changed template')
        self.store.render()
        self.assertEqual(path.read_bytes(), before)

    def test_invalid_comparison_version_rejected(self):
        for version in ['invalid', None, [], {}, 10, True]:
            with self.subTest(version=version):
                self.configure(version)
                with self.assertRaisesRegex(ValueError, '比较版本'):
                    Store(self.root)

    def test_consecutive_failed_runs_preserve_latest_complete_baseline(self):
        a = self.begin(); self.save(a); self.close(a)
        for at in ['2026-10-20T04:00:00Z', '2026-10-20T10:00:00Z']:
            b = self.begin(at); self.close(b)
        c = self.begin('2026-10-21T01:00:00Z'); self.save(c, range(2, 82))
        self.assertEqual(self.baseline(c)['baselineRunId'], a['runId'])
        self.assertEqual(self.baseline(c)['skippedRuns'], 2)
        self.assertEqual(self.report(c)['counts']['confirmedNew'], 1)

    def test_reference_file_written_before_manifest_checksum_can_resume(self):
        a = self.begin(); self.save(a); self.close(a)
        config_path = self.root/'config/monitor.json'
        config = read_json(config_path); config['executionPolicy'] = 'completion_v1'; write_json(config_path, config)
        self.store = Store(self.root, 'test')
        original = write_json
        def interrupted(path, data):
            if str(path).endswith('manifest.json') and data.get('comparisonBaselinesHash'):
                raise OSError('synthetic interruption after baselines file')
            return original(path, data)
        with patch('rank_monitor.comparison.write_json', side_effect=interrupted):
            with self.assertRaises(OSError):
                self.begin('2026-10-20T04:00:00Z')
        run_id = self.store.state()['latestRunId']
        before = (self.store.locate(run_id)/'baselines.json').read_bytes()
        result = self.store.start('2026-10-20T04:01:00Z')
        self.assertEqual(result['runId'], run_id)
        self.assertEqual((self.store.locate(run_id)/'baselines.json').read_bytes(), before)

    def test_markdown_mail_and_html_show_both_chart_intervals(self):
        from rank_monitor.mailer import MailDelivery
        a = self.begin(); self.save(a); self.save(a, chart='grossing'); self.close(a)
        b = self.begin('2026-10-20T04:00:00Z'); self.save(b, chart='grossing'); self.close(b)
        c = self.begin('2026-10-20T10:00:00Z')
        self.save(c, range(2, 82)); self.save(c, range(2, 82), chart='grossing')
        self.close(c)
        folder = self.store.locate(c['runId'])
        text = (folder/'report.md').read_text()
        self.assertIn('实际跨度', text)
        self.assertIn(a['runId'], text)
        self.assertIn(b['runId'], text)
        self.assertIn('9小时', text)
        self.assertIn('6小时', text)
        with patch.object(self.store, 'compare', side_effect=AssertionError('mail cannot recompute archive')):
            preview = MailDelivery(self.store).preview(c['runId'])
        mail = (self.root/preview['text']).read_text()
        self.assertIn('实际跨度', mail)
        self.assertIn('9小时', mail)
        self.assertIn('6小时', mail)
        html = (folder.parents[1]/'index.html').read_text()
        self.assertIn('比较基准 / 实际跨度', html)
        self.assertIn('回溯比较', html)
        self.assertIn('comparisonBaselines', html)

    def test_new_baseline_strings_are_escaped_in_markdown_and_mail(self):
        from rank_monitor.reports import markdown_report
        from rank_monitor.mailer import render_mail
        a, _, c = self.pair_with_gap()
        self.close(c)
        report = deepcopy(self.report(c))
        report['entries'][0]['comparisonBaselines']['free']['baselineRunId'] = '<img src=x onerror=alert(1)>'
        text = markdown_report(report, self.store.config)
        mail = render_mail(report, self.store.config)
        self.assertNotIn('<img src=x', text)
        self.assertNotIn('<img src=x', mail['html'])
        self.assertIn('&lt;img', mail['html'])

    def test_missing_historical_json_is_reported_instead_of_recomputed(self):
        a = self.begin(); self.save(a); self.close(a)
        (self.store.locate(a['runId'])/'report-data.json').unlink()
        with self.assertRaisesRegex(ValueError, '封存报告数据缺失'):
            self.store.render()

    def test_actual_html_script_filters_paginates_and_keeps_chart_intervals(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node unavailable for optional offline JavaScript runtime verification')
        a = self.begin(); self.save(a); self.save(a, chart='grossing'); self.close(a)
        b = self.begin('2026-10-20T04:00:00Z'); self.save(b, chart='grossing'); self.close(b)
        c = self.begin('2026-10-20T10:00:00Z')
        self.save(c, range(81, 161)); self.save(c, range(161, 241), chart='grossing'); self.close(c)
        path = self.store.locate(c['runId']).parents[1]/'index.html'
        result = subprocess.run([node, str(helpers.SOURCE/'tests/report_runtime.js'), str(path)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('filters, intervals and full view passed',result.stdout)

    def _interrupted_daily_page(self, filename):
        import rank_monitor.reports as reports
        a, _, c = self.pair_with_gap()
        day = self.store.locate(c['runId']).parents[1]
        original = reports.write_text
        def fail(path, value):
            if path == day/filename:
                raise OSError('synthetic interrupted daily page write')
            return original(path, value)
        with patch('rank_monitor.reports.write_text', side_effect=fail):
            with self.assertRaises(OSError):
                self.close(c)
        self.assertTrue((self.store.locate(c['runId'])/'report.html').exists())
        self.store.render()
        html = (day/'index.html').read_text()
        payload = json.loads(html.split('<script id="payload" type="application/json">')[1].split('</script>')[0])
        self.assertIn(c['runId'], {r['run']['runId'] for r in payload['runs']})
        self.assertIn('runs/'+c['runId']+'/report.md', (day/'index.md').read_text())

    def test_retry_render_repairs_interruption_before_daily_html(self):
        self._interrupted_daily_page('index.html')

    def test_retry_render_repairs_interruption_between_daily_html_and_markdown(self):
        self._interrupted_daily_page('index.md')

    def _legacy_lifecycle_freeze_failure(self, filename):
        a = self.begin(); self.save(a); self.close(a)
        original = write_json
        def interrupted(path, data):
            if path.name == filename and (filename != 'manifest.json' or data.get('comparisonBaselinesHash')):
                raise OSError('synthetic incomplete baseline freeze')
            return original(path, data)
        with patch('rank_monitor.comparison.write_json', side_effect=interrupted):
            with self.assertRaises(OSError):
                self.begin('2026-10-20T04:00:00Z')
        run_id = self.store.state()['latestRunId']
        self.begin('2026-10-20T04:01:00Z')
        self.assertTrue(self.store.manifest(run_id).get('comparisonBaselinesHash'))
        self.assertNotEqual(self.store.manifest(run_id)['status'], 'running')
        self.store.render()
        self.assertTrue((self.store.locate(run_id)/'report-data.json').exists())

    def test_legacy_lifecycle_recovers_interruption_before_baseline_file(self):
        self._legacy_lifecycle_freeze_failure('baselines.json')

    def test_legacy_lifecycle_recovers_interruption_before_baseline_checksum(self):
        self._legacy_lifecycle_freeze_failure('manifest.json')
