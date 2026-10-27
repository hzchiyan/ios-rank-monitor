"""Completion-driven runs: persistence, fencing, readiness and fixed source day."""
from datetime import datetime, timedelta, timezone
import json
import subprocess
import sys
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

import test_monitor as helpers
import test_releases as release_helpers

sys.path.insert(0, str(helpers.SOURCE / 'scripts'))
from rank_monitor.core import Store, read_json, write_json, load_config

NOW = datetime(2026, 10, 20, 1, tzinfo=timezone.utc)
POLICY = release_helpers.POLICY


class CompletionTests(helpers.Harness):
    def setUp(self):
        super().setUp()
        self.configure()
        self.store = Store(self.root, 'test')
        self.clock = patch('rank_monitor.core.utcnow', return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def configure(self):
        p = self.root / 'config/monitor.json'
        config = read_json(p)
        config.update(executionPolicy='completion_v1', scanFamilies=['应用'], releasePolicy=POLICY)
        write_json(p, config)

    def begin(self, when=NOW):
        with patch('rank_monitor.core.utcnow', return_value=when):
            return self.store.start()

    def mark_absent(self, run, exclude=()):
        for s in self.store.pending(run['runId']):
            if s['genreId'] in exclude and s['country'] == 'us' and s['chart'] == 'free':
                continue
            self.store.mark(run['runId'], run['token'], s, 'source_no_data',
                            '公开页面明确所请求日期无榜单', source_date='2026-10-20')

    def candidate_run(self):
        a = self.begin()
        self.store.ingest(a['runId'], a['token'], self.snapshot(range(1, 81), title='美国iPhone效率免费榜'))
        self.mark_absent(a)
        self.store.finalize(a['runId'], a['token'])
        b = self.begin(NOW + timedelta(hours=3))
        with patch('rank_monitor.core.utcnow', return_value=NOW + timedelta(hours=3)):
            self.store.ingest(b['runId'], b['token'], self.snapshot(range(2, 82), at='2026-10-20T04:00:00Z', title='美国iPhone效率免费榜'))
            self.mark_absent(b)
        return b

    def test_default_enables_completion_policy(self):
        self.assertEqual(load_config(helpers.SOURCE).get('executionPolicy'), 'completion_v1')

    def test_late_start_recovers_same_run_and_fences_old_writer(self):
        a = self.begin()
        self.store.ingest(a['runId'], a['token'], self.snapshot(range(1, 81)))
        b = self.begin(NOW + timedelta(hours=4))
        self.assertEqual(b['runId'], a['runId'])
        self.assertTrue(b['resumed'])
        self.assertNotEqual(b['token'], a['token'])
        self.assertEqual(len(self.store.state()['runs']), 1)
        self.assertEqual(self.store.manifest(a['runId'])['status'], 'running')
        with self.assertRaisesRegex(ValueError, '令牌'):
            self.store.resume(a['runId'], a['token'])
        self.assertEqual(self.store.resume(b['runId'], b['token'])['completedScopes'], 1)
        self.assertNotIn('deadlineAt', b)

    def test_live_worker_skip_does_not_mutate_run_or_token(self):
        a = self.begin()
        before = self.store.manifest(a['runId'])
        b = self.begin(NOW + timedelta(minutes=1))
        self.assertTrue(b['skipped'])
        self.assertNotIn('token', b)
        self.assertEqual(self.store.manifest(a['runId']), before)

    def test_missing_active_marker_recovers_without_sealing(self):
        a = self.begin()
        (self.store.base / 'active.json').unlink()
        b = self.begin(NOW + timedelta(days=2))
        self.assertEqual(b['runId'], a['runId'])
        self.assertEqual(self.store.manifest(a['runId'])['date'], '2026-10-20')

    def test_heartbeat_keeps_worker_ownership_without_deadline(self):
        a = self.begin()
        with patch('rank_monitor.core.utcnow', return_value=NOW + timedelta(minutes=14)):
            result = self.store.resume(a['runId'], a['token'])
        self.assertNotIn('windowExpired', result)
        self.assertTrue(self.begin(NOW + timedelta(minutes=20))['skipped'])

    def test_cross_day_snapshot_keeps_run_source_date(self):
        a = self.begin()
        snap = self.snapshot(range(1, 81), at='2026-10-21T04:00:00Z')
        self.store.ingest(a['runId'], a['token'], snap)
        with self.assertRaisesRegex(ValueError, '来源日期'):
            self.store.ingest(a['runId'], a['token'], self.snapshot(range(1, 81), 'grossing',
                              '2026-10-21T04:00:00Z', sourceDate='2026-10-21'))
        self.assertEqual(self.store.snapshots(a['runId'])[0]['sourceDate'], '2026-10-20')

    def test_formal_future_capture_rejected(self):
        store = Store(self.root)
        with patch('rank_monitor.core.utcnow', return_value=NOW):
            a = store.start()
            with self.assertRaisesRegex(ValueError, '当前时间'):
                store.ingest(a['runId'], a['token'], self.snapshot(range(1, 81),
                             at='2026-10-20T02:00:00Z', title='美国iPhone效率免费榜'))

    def test_pending_rankings_prevent_finalize_and_release_stage(self):
        a = self.begin()
        with self.assertRaisesRegex(ValueError, '可执行|未完成'):
            self.store.finalize(a['runId'], a['token'])
        with self.assertRaisesRegex(ValueError, '榜单'):
            self.store.prepare_release(a['runId'], a['token'])
        self.assertEqual(self.store.manifest(a['runId'])['status'], 'running')
        self.assertFalse((self.store.locate(a['runId']) / 'snapshots.jsonl.gz').exists())

    def test_rank_retry_budget_persists_and_submission_is_idempotent(self):
        a = self.begin()
        s = self.store.pending(a['runId'])[0]
        for attempt in range(3):
            for _ in range(2):
                self.store.mark(a['runId'], a['token'], s, 'failed', '页面解析失败', attempt_id=str(attempt))
            self.store = Store(self.root, 'test')
            self.assertEqual(self.store.manifest(a['runId'])['scopes']['us:应用:6007:free']['attempts'], attempt + 1)
        self.assertNotIn('us:应用:6007:free', [':'.join([s['country'], s['categoryFamily'], s['genreId'], s['chart']])
                                             for s in self.store.pending(a['runId'])])
        with self.assertRaisesRegex(ValueError, '重试'):
            self.store.mark(a['runId'], a['token'], s, 'failed', 'again', attempt_id='4')

    def test_no_data_requires_fixed_source_day(self):
        a = self.begin()
        s = self.store.pending(a['runId'])[0]
        with self.assertRaisesRegex(ValueError, '来源日期'):
            self.store.mark(a['runId'], a['token'], s, 'source_no_data', '无数据', source_date='2026-10-21')

    def test_global_block_suspends_without_burning_retries_or_archiving(self):
        a = self.begin()
        result = self.store.suspend(a['runId'], a['token'], '需要人工验证码')
        self.assertEqual(result['status'], 'running')
        self.assertEqual(len(self.store.pending(a['runId'])), 144)
        with self.assertRaises(ValueError):
            self.store.finalize(a['runId'], a['token'])
        b = self.begin(NOW + timedelta(minutes=1))
        self.assertEqual(b['runId'], a['runId'])
        self.assertNotIn('blocked', self.store.manifest(a['runId']))
        self.assertEqual(self.store.manifest(a['runId'])['lastBlock']['reason'], '需要人工验证码')

    def test_recover_checkpoint_before_deciding_pending(self):
        a = self.begin()
        self.store.ingest(a['runId'], a['token'], self.snapshot(range(1, 81)))
        p = self.store.locate(a['runId']) / 'manifest.json'
        m = read_json(p)
        m['scopes']['us:应用:6007:free'].update(status='failed', reason='crash before manifest saved')
        write_json(p, m)
        self.store.resume(a['runId'], a['token'])
        s = self.store.manifest(a['runId'])['scopes']['us:应用:6007:free']
        self.assertEqual(s['status'], 'complete')
        self.assertNotIn('reason', s)

    def test_baseline_and_terminal_missing_can_archive(self):
        a = self.begin()
        self.mark_absent(a)
        progress = self.store.progress(a['runId'], a['token'])
        self.assertTrue(progress['canFinalize'])
        self.assertEqual(progress['release']['candidateApps'], 0)
        result = self.store.finalize(a['runId'], a['token'])
        self.assertEqual(result['status'], 'failed')

    def test_release_checks_continue_across_days_and_block_early_archive(self):
        b = self.candidate_run()
        with self.assertRaisesRegex(ValueError, '可执行|发布日期'):
            self.store.finalize(b['runId'], b['token'])
        self.assertEqual(len(self.store.prepare_release(b['runId'], b['token'])['pending']), 1)
        evidence = release_helpers.ReleaseTests.evidence(self, 81, observedAt='2026-10-22T04:01:00Z')
        self.store.ingest_release(b['runId'], b['token'], evidence)
        self.assertTrue(self.store.progress(b['runId'], b['token'])['canFinalize'])
        self.store.finalize(b['runId'], b['token'])
        report = self.store.compare(b['runId'])
        self.assertEqual(report['counts']['confirmedNew'], 1)
        self.assertEqual(report['run']['releasePolicy']['referenceDate'], '2026-10-20')
        with self.assertRaises(ValueError):
            self.store.ingest_release(b['runId'], b['token'], evidence)

    def test_exhausted_release_remains_unknown_and_allows_partial_archive(self):
        b = self.candidate_run()
        for minute in range(3):
            self.store.ingest_release(b['runId'], b['token'], release_helpers.ReleaseTests.evidence(
                self, 81, status='failed', reason='DOM changed', observedAt=f'2026-10-22T04:0{minute}:00Z'))
        progress = self.store.progress(b['runId'], b['token'])
        self.assertTrue(progress['canFinalize'])
        self.assertEqual(progress['release']['exhaustedApps'], 1)
        self.store.finalize(b['runId'], b['token'])
        self.assertIsNone(self.store.compare(b['runId'])['counts']['confirmedNew'])
        self.assertEqual(self.store.compare(b['runId'])['counts']['pendingRelease'], 1)

    def test_manual_abort_preserves_explicit_reason(self):
        a = self.begin()
        self.store.abort(a['runId'], a['token'], '用户明确取消本轮')
        m = self.store.manifest(a['runId'])
        self.assertEqual(m['abortReason'], '用户明确取消本轮')
        self.assertEqual(m['status'], 'failed')

    def test_invalid_execution_policy_rejected(self):
        p = self.root / 'config/monitor.json'
        config = read_json(p)
        config['executionPolicy'] = 'unknown'
        write_json(p, config)
        with self.assertRaisesRegex(ValueError, '执行策略'):
            Store(self.root)

    def test_cli_suspend_progress_and_no_automatic_email(self):
        a = self.start('2026-10-20T01:00:00Z')
        progress = self.cli('progress', a['runId'], '--token', a['token'])
        self.assertEqual(progress['phase'], 'rankings')
        result = self.cli('suspend', a['runId'], '--token', a['token'], '--reason', '工具暂不可用')
        self.assertEqual(result['status'], 'running')
        self.assertFalse((self.root/a['relativePath']/'mail').exists())

    def test_concurrent_recovery_has_exactly_one_claimant(self):
        a = self.begin()
        with patch('rank_monitor.core.utcnow', return_value=NOW + timedelta(hours=4)):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: Store(self.root, 'test').start(), range(2)))
        self.assertEqual(sum(r.get('resumed', False) for r in results), 1)
        self.assertEqual(sum(r.get('skipped', False) for r in results), 1)
        self.assertEqual({r['runId'] for r in results}, {a['runId']})

    def test_import_cannot_claim_or_disturb_unfinished_browser_run(self):
        a = self.begin()
        with patch('rank_monitor.core.utcnow', return_value=NOW + timedelta(days=1)):
            with self.assertRaisesRegex(ValueError, '导入|进行'):
                self.store.start('2026-10-19T01:00:00Z', run_id='20261019T010000Z', origin='legacy_import')
        self.store.resume(a['runId'], a['token'])
        self.assertEqual(len(self.store.state()['runs']), 1)

    def test_abort_is_available_after_configuration_change(self):
        a = self.begin()
        p = self.root / 'config/monitor.json'
        config = read_json(p)
        config['scanFamilies'] = ['应用', '游戏']
        write_json(p, config)
        self.store = Store(self.root, 'test')
        self.store.abort(a['runId'], a['token'], '用户取消以切换配置')
        self.assertEqual(self.store.manifest(a['runId'])['status'], 'failed')

    def test_archive_interruption_can_resume_without_recapturing(self):
        a = self.begin()
        self.store.ingest(a['runId'], a['token'], self.snapshot(range(1, 81)))
        self.mark_absent(a)
        def fail_seal(path, data):
            if str(path).endswith('manifest.json') and data['status'] != 'running':
                raise OSError('synthetic disk interruption')
            return write_json(path, data)
        with patch('rank_monitor.core.write_json', side_effect=fail_seal):
            with self.assertRaises(OSError):
                self.store.finalize(a['runId'], a['token'])
        self.assertEqual(self.store.manifest(a['runId'])['status'], 'running')
        result = self.store.resume(a['runId'], a['token'])
        self.assertTrue(result['canFinalize'])
        self.assertEqual(result['completedScopes'], 1)
        self.store.finalize(a['runId'], a['token'])
        self.assertEqual(len(self.store.snapshots(a['runId'])), 1)

    def test_complete_run_has_all_144_snapshots_and_no_deadline(self):
        a = self.begin()
        for scope in list(self.store.pending(a['runId'])):
            snap = self.snapshot(range(1, 81), at='2026-10-21T04:00:00Z')
            snap.update({k: v for k, v in scope.items() if k not in {'status', 'attemptsRemaining'}})
            for row in snap['rows']:
                row['qimaiUrl'] = row['qimaiUrl'].replace('/country/us', '/country/' + scope['country'])
            self.store.ingest(a['runId'], a['token'], snap)
        self.assertEqual(self.store.finalize(a['runId'], a['token'])['status'], 'complete')
        self.assertEqual(len(self.store.snapshots(a['runId'])), 144)
        b = self.begin(NOW + timedelta(days=2))
        self.assertNotEqual(a['runId'], b['runId'])
        self.assertEqual(b['previousRunId'], a['runId'])

    def test_legacy_import_ignores_new_execution_policy(self):
        from rank_monitor.legacy import import_legacy
        p = self.root / 'legacy.json'
        write_json(p, {'run': {'runId': '20261020T010000Z'}, 'snapshots': [self.snapshot(range(1, 81))]})
        result = import_legacy(self.store, p)
        m = self.store.manifest(result['runId'])
        self.assertNotIn('executionPolicy', m)
        self.assertEqual(m['status'], 'partial')
        self.assertEqual(len(m['scopes']), 240)

    def test_formal_release_checks_after_90_minutes_do_not_expire(self):
        self.store = Store(self.root, 'formal')
        b = self.candidate_run()
        with patch('rank_monitor.core.utcnow', return_value=NOW + timedelta(days=2)), \
             patch('rank_monitor.releases.utcnow', return_value=NOW + timedelta(days=2)):
            self.assertEqual(len(self.store.prepare_release(b['runId'], b['token'])['pending']), 1)
            self.store.ingest_release(b['runId'], b['token'], release_helpers.ReleaseTests.evidence(
                self, 81, observedAt='2026-10-22T01:00:00Z'))
            self.store.finalize(b['runId'], b['token'])
        self.assertEqual(self.store.compare(b['runId'])['counts']['confirmedNew'], 1)

    def test_successful_rank_retry_clears_reason_and_does_not_reappear(self):
        a = self.begin()
        snap = self.snapshot(range(1, 81))
        self.store.mark(a['runId'], a['token'], snap, 'failed', 'temporary', attempt_id='1')
        self.store.ingest(a['runId'], a['token'], snap)
        scope = self.store.manifest(a['runId'])['scopes']['us:应用:6007:free']
        self.assertEqual(scope['attempts'], 1)
        self.assertNotIn('reason', scope)
        self.assertNotIn('6007', [s['genreId'] for s in self.store.pending(a['runId']) if s['country']=='us' and s['chart']=='free'])

    def test_archived_residual_active_marker_does_not_block_next_run(self):
        a = self.begin()
        self.mark_absent(a)
        self.store.finalize(a['runId'], a['token'])
        write_json(self.store.base/'active.json', {'runId': a['runId'], 'leaseUpdatedAt': '2026-10-20T01:00:00Z'})
        b = self.begin(NOW + timedelta(minutes=1))
        self.assertNotEqual(b['runId'], a['runId'])

    def test_old_release_failure_replay_cannot_exhaust_real_attempts(self):
        b = self.candidate_run()
        evidence = [release_helpers.ReleaseTests.evidence(
            self, 81, status='failed', reason='single page failed',
            observedAt=f'2026-10-20T04:0{minute}:00Z') for minute in range(3)]
        for raw in evidence[:2]:
            self.store.ingest_release(b['runId'], b['token'], raw)
        self.store = Store(self.root, 'test')
        self.store.resume(b['runId'], b['token'])
        self.store.ingest_release(b['runId'], b['token'], evidence[0])
        progress = self.store.progress(b['runId'], b['token'])
        self.assertFalse(progress['canFinalize'])
        target = progress['release']['pending'][0]
        self.assertEqual((target['attempts'], target['attemptsRemaining']), (2, 1))
        with self.assertRaises(ValueError):
            self.store.finalize(b['runId'], b['token'])
        self.store.ingest_release(b['runId'], b['token'], evidence[2])
        # An older replay also remains harmless after the budget is exhausted.
        self.store.ingest_release(b['runId'], b['token'], evidence[0])
        self.assertTrue(self.store.progress(b['runId'], b['token'])['canFinalize'])
        record = read_json(self.store.locate(b['runId'])/'releases.json')['records']['us:81']
        self.assertEqual(record['attempts'], 3)
        self.assertEqual(record['observedAt'], evidence[2]['observedAt'])

    def test_two_processes_cannot_both_claim_one_interrupted_run(self):
        a = self.begin()
        code = '''
import json, sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(sys.argv[1])/'scripts'))
from rank_monitor.core import Store, parse_time
with patch('rank_monitor.core.utcnow', return_value=parse_time('2026-10-20T05:00:00Z')):
    result = Store(sys.argv[1], 'test').start()
result.pop('token', None)
print(json.dumps(result))
'''
        def worker(_):
            result = subprocess.run([sys.executable, '-c', code, str(self.root)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(worker, range(2)))
        self.assertEqual(sum(r.get('resumed', False) for r in results), 1)
        self.assertEqual(sum(r.get('skipped', False) for r in results), 1)
        self.assertEqual({r['runId'] for r in results}, {a['runId']})
