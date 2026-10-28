"""Mode-scoped ownership and cancellation intent that survives interruption."""
from datetime import datetime,timedelta,timezone
from pathlib import Path
import sys
from unittest.mock import patch

import test_monitor as helpers

sys.path.insert(0,str(helpers.SOURCE/'scripts'))
from rank_monitor.core import Store, read_json, write_json

NOW=datetime(2026,10,28,1,tzinfo=timezone.utc)


class ControlRecoveryTests(helpers.Harness):
    def setUp(self):
        super().setUp()
        config=read_json(self.root/'config/monitor.json')
        config.update(executionPolicy='completion_v1',scanFamilies=['应用'])
        write_json(self.root/'config/monitor.json',config)
        self.store=Store(self.root,'test')
        self.clock=patch('rank_monitor.core.utcnow',return_value=NOW);self.clock.start();self.addCleanup(self.clock.stop)

    def begin(self,store=None):
        return (store or self.store).start()

    def old_token(self,store,run):
        scoped=store.local/'tokens'/store.mode/run['runId']
        scoped.unlink(missing_ok=True)
        legacy=store.local/'tokens'/run['runId'];legacy.parent.mkdir(parents=True,exist_ok=True)
        legacy.write_text(run['token']);legacy.chmod(0o600)
        path=store.locate(run['runId'])/'manifest.json';m=read_json(path);m.pop('tokenVersion',None);write_json(path,m)
        return legacy

    def interrupted_abort(self,run):
        with patch.object(self.store,'_seal',side_effect=OSError('synthetic archive interruption')):
            with self.assertRaises(OSError):self.store.abort(run['runId'],run['token'],'用户明确取消')

    def test_same_id_modes_keep_independent_tokens_and_reject_cross_tokens(self):
        formal=Store(self.root);a=self.begin(formal);b=self.begin()
        self.assertEqual(a['runId'],b['runId'])
        for store,run,other in [(formal,a,b),(self.store,b,a)]:
            path=store.local/'tokens'/store.mode/run['runId']
            self.assertTrue(path.exists())
            self.assertEqual(path.stat().st_mode&0o777,0o600)
            self.assertEqual(store.resume(run['runId'],run['token'])['runId'],run['runId'])
            with self.assertRaisesRegex(ValueError,'令牌'):store.resume(run['runId'],other['token'])

    def test_unique_legacy_token_migrates_without_touching_other_mode(self):
        run=self.begin();legacy=self.old_token(self.store,run)
        self.store.resume(run['runId'],run['token'])
        self.assertTrue((self.store.local/'tokens/test'/run['runId']).exists())
        self.assertFalse(legacy.exists())
        self.assertEqual(self.store.manifest(run['runId'])['tokenVersion'],'mode_v1')

    def test_new_scoped_token_does_not_overwrite_unique_old_other_mode(self):
        formal=Store(self.root);a=self.begin(formal);self.old_token(formal,a)
        b=self.begin()
        self.assertEqual(formal.resume(a['runId'],a['token'])['runId'],a['runId'])
        self.assertEqual(self.store.resume(b['runId'],b['token'])['runId'],b['runId'])

    def test_ambiguous_old_tokens_fail_closed_then_mode_claim_recovers(self):
        formal=Store(self.root);a=self.begin(formal);b=self.begin()
        self.old_token(formal,a);self.old_token(self.store,b)
        for store in [formal,self.store]:
            with self.assertRaisesRegex(ValueError,'旧令牌|歧义|判属'):store.resume(a['runId'],b['token'])
        with patch('rank_monitor.core.utcnow',return_value=NOW+timedelta(hours=1)):
            recovered=formal.start()
        self.assertTrue(recovered['resumed'])
        self.assertEqual(formal.resume(a['runId'],recovered['token'])['runId'],a['runId'])
        with self.assertRaises(ValueError):self.store.resume(b['runId'],b['token'])

    def test_missing_new_scoped_token_never_falls_back_to_old_shared_file(self):
        run=self.begin();path=self.store.local/'tokens/test'/run['runId']
        self.assertTrue(path.exists());path.unlink()
        (self.store.local/'tokens'/run['runId']).write_text(run['token'])
        with self.assertRaisesRegex(ValueError,'令牌'):self.store.resume(run['runId'],run['token'])

    def test_cli_same_id_requires_explicit_mode(self):
        formal=Store(self.root);a=self.begin(formal);b=self.begin()
        error=self.cli('pending',a['runId'],ok=False)
        self.assertIn('--mode',error['error'])
        self.assertEqual(len(self.cli('pending',a['runId'],'--mode','formal')['scopes']),144)
        self.assertEqual(len(self.cli('pending',b['runId'],'--mode','test')['scopes']),144)

    def test_cancel_intent_persisted_before_archive_and_blocks_new_collection(self):
        run=self.begin();self.interrupted_abort(run)
        self.assertEqual(self.store.manifest(run['runId'])['status'],'cancelling')
        progress=self.store.progress(run['runId'],run['token'])
        self.assertEqual(progress['phase'],'cancelling');self.assertEqual(progress['pending'],[])
        self.assertFalse(progress['canFinalize']);self.assertEqual(self.store.pending(run['runId']),[])
        with self.assertRaisesRegex(ValueError,'取消'):self.store.ingest(run['runId'],run['token'],self.snapshot(range(1,81),at=NOW.isoformat(),sourceDate='2026-10-28'))
        with self.assertRaisesRegex(ValueError,'取消'):self.store.suspend(run['runId'],run['token'],'cannot undo cancel')

    def test_next_start_finishes_cancel_instead_of_restarting_or_opening_run(self):
        run=self.begin();self.interrupted_abort(run)
        with patch('rank_monitor.core.utcnow',return_value=NOW+timedelta(hours=1)):
            result=self.store.start()
        self.assertEqual(result['runId'],run['runId']);self.assertTrue(result['cancelled'])
        self.assertNotIn('token',result);self.assertEqual(len(self.store.state()['runs']),1)
        self.assertEqual(self.store.manifest(run['runId'])['status'],'failed')

    def test_resume_and_finalize_finish_only_cancellation_even_after_config_change(self):
        run=self.begin();self.interrupted_abort(run)
        path=self.root/'config/monitor.json';config=read_json(path);config['scanFamilies']=['应用','游戏'];write_json(path,config)
        self.store=Store(self.root,'test')
        result=self.store.resume(run['runId'],run['token'])
        self.assertTrue(result['cancelled']);self.assertEqual(len(self.store.state()['runs']),1)

    def test_legacy_running_abort_reason_is_treated_as_cancel_intent(self):
        run=self.begin();path=self.store.locate(run['runId'])/'manifest.json'
        m=read_json(path);m['abortReason']='旧版已请求取消';write_json(path,m)
        result=self.store.resume(run['runId'],run['token'])
        self.assertTrue(result['cancelled']);self.assertEqual(result['abortReason'],'旧版已请求取消')

    def test_repeated_abort_after_render_failure_is_idempotent(self):
        run=self.begin()
        with patch.object(self.store,'_render',side_effect=OSError('synthetic report failure')):
            with self.assertRaises(OSError):self.store.abort(run['runId'],run['token'],'用户明确取消')
        before=self.store.manifest(run['runId'])
        result=self.store.abort(run['runId'],run['token'],'retry must preserve original reason')
        self.assertTrue(result['cancelled']);self.assertEqual(result['abortReason'],'用户明确取消')
        self.assertEqual(self.store.manifest(run['runId']),before)
        self.assertTrue((self.store.locate(run['runId'])/'report.html').exists())

    def test_cancelling_run_is_not_rendered_until_sealed(self):
        run=self.begin();self.interrupted_abort(run)
        self.store.render()
        self.assertFalse((self.store.locate(run['runId'])/'report-data.json').exists())
        with self.assertRaisesRegex(ValueError,'封存'):
            self.store.archived_report(run['runId'])

    def test_cancel_blocks_mark_and_date_commands_and_finalize_completes_cancel(self):
        run=self.begin();self.interrupted_abort(run)
        scope=self.store.manifest(run['runId'])['scopes']['us:应用:6007:free']
        for call in [lambda:self.store.mark(run['runId'],run['token'],scope,'failed','no',attempt_id='1'),
                     lambda:self.store.prepare_release(run['runId'],run['token']),
                     lambda:self.store.ingest_release(run['runId'],run['token'],{})]:
            with self.assertRaisesRegex(ValueError,'取消'):call()
        self.assertTrue(self.store.finalize(run['runId'],run['token'])['cancelled'])

    def test_cancelled_report_and_mail_explicitly_show_reason_and_escape_it(self):
        from rank_monitor.mailer import MailDelivery
        run=self.begin();reason='用户取消 <script>alert(1)</script>'
        self.store.abort(run['runId'],run['token'],reason)
        report=(self.store.locate(run['runId'])/'report.md').read_text()
        self.assertIn('- 人工取消：',report);self.assertIn('&lt;script&gt;',report)
        preview=MailDelivery(self.store).preview(run['runId'])
        html=(self.root/preview['html']).read_text();plain=(self.root/preview['text']).read_text()
        self.assertIn('人工取消',plain.splitlines()[0]);self.assertIn(reason,plain)
        self.assertIn('&lt;script&gt;',html);self.assertNotIn('<script>',html)

    def test_migration_interruption_keeps_scoped_token_recoverable(self):
        run=self.begin();self.old_token(self.store,run)
        with patch('rank_monitor.core.write_json',side_effect=OSError('metadata save interrupted')):
            with self.assertRaises(OSError):self.store.resume(run['runId'],run['token'])
        self.assertEqual(self.store.resume(run['runId'],run['token'])['runId'],run['runId'])

    def test_render_and_resume_of_terminal_cancel_never_reopens_run(self):
        run=self.begin();self.store.abort(run['runId'],run['token'],'用户取消')
        with self.assertRaisesRegex(ValueError,'结束'):self.store.resume(run['runId'],run['token'])
        self.assertEqual(len(self.store.state()['runs']),1)

    def test_start_recovers_terminal_cancel_after_render_failure_without_new_run(self):
        run=self.begin()
        with patch.object(self.store,'_render',side_effect=OSError('synthetic report failure')):
            with self.assertRaises(OSError):self.store.abort(run['runId'],run['token'],'用户取消')
        before=self.store.manifest(run['runId'])
        with patch('rank_monitor.core.utcnow',return_value=NOW+timedelta(hours=1)):
            result=self.store.start()
        self.assertTrue(result.get('cancelled'));self.assertNotIn('token',result)
        self.assertEqual(result['runId'],run['runId']);self.assertEqual(len(self.store.state()['runs']),1)
        self.assertEqual(self.store.manifest(run['runId']),before)
        self.assertTrue((self.store.locate(run['runId'])/'report.html').exists())
        self.assertFalse((self.store.base/'active.json').exists())

    def test_suspended_cancel_retains_recovery_until_report_succeeds(self):
        run=self.begin();self.store.suspend(run['runId'],run['token'],'synthetic global block')
        self.assertFalse((self.store.base/'active.json').exists())
        with patch.object(self.store,'_render',side_effect=OSError('synthetic report failure')):
            with self.assertRaises(OSError):self.store.abort(run['runId'],run['token'],'用户取消')
        with patch('rank_monitor.core.utcnow',return_value=NOW+timedelta(hours=1)):
            result=self.store.start()
        self.assertTrue(result.get('cancelled'));self.assertEqual(len(self.store.state()['runs']),1)

    def test_legacy_cancel_without_active_keeps_terminal_report_recoverable(self):
        run=self.begin();path=self.store.locate(run['runId'])/'manifest.json'
        m=read_json(path);m['abortReason']='旧版取消';write_json(path,m)
        (self.store.base/'active.json').unlink()
        with patch.object(self.store,'_render',side_effect=OSError('synthetic report failure')):
            with self.assertRaises(OSError):self.store.resume(run['runId'],run['token'])
        with patch('rank_monitor.core.utcnow',return_value=NOW+timedelta(hours=1)):
            result=self.store.start()
        self.assertTrue(result.get('cancelled'));self.assertEqual(result['abortReason'],'旧版取消')
        self.assertEqual(len(self.store.state()['runs']),1)

    def test_cancel_checkpoint_cleanup_interruption_recovers_without_new_run(self):
        run=self.begin()
        self.store.ingest(run['runId'],run['token'],self.snapshot(range(1,81),at=NOW.isoformat(),sourceDate='2026-10-28'))
        with patch('rank_monitor.core.shutil.rmtree',side_effect=OSError('synthetic cleanup failure')):
            with self.assertRaises(OSError):self.store.abort(run['runId'],run['token'],'用户取消')
        before=self.store.manifest(run['runId'])
        with patch('rank_monitor.core.utcnow',return_value=NOW+timedelta(hours=1)):
            result=self.store.start()
        self.assertTrue(result.get('cancelled'));self.assertEqual(len(self.store.state()['runs']),1)
        self.assertEqual(self.store.manifest(run['runId']),before)
        self.assertEqual(len(self.store.snapshots(run['runId'])),1)

    def test_cancel_active_cleanup_failure_after_report_is_safe_to_retry(self):
        run=self.begin();active=self.store.base/'active.json';original=Path.unlink
        def fail_active(path,*args,**kwargs):
            if path==active:raise OSError('synthetic active cleanup failure')
            return original(path,*args,**kwargs)
        with patch.object(Path,'unlink',fail_active):
            with self.assertRaises(OSError):self.store.abort(run['runId'],run['token'],'用户取消')
        self.assertTrue((self.store.locate(run['runId'])/'report.html').exists())
        result=self.store.start()
        self.assertTrue(result.get('cancelled'));self.assertEqual(len(self.store.state()['runs']),1)
        self.assertFalse(active.exists())

    def test_repeated_old_abort_does_not_remove_later_run_active_marker(self):
        old=self.begin();self.store.abort(old['runId'],old['token'],'用户取消')
        with patch('rank_monitor.core.utcnow',return_value=NOW+timedelta(hours=1)):
            new=self.begin()
        before=self.store.manifest(old['runId'])
        result=self.store.abort(old['runId'],old['token'],'repeat original cancellation')
        self.assertTrue(result['cancelled'])
        self.assertEqual(read_json(self.store.base/'active.json')['runId'],new['runId'])
        self.assertEqual(self.store.manifest(old['runId']),before)
