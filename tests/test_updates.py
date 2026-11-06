"""Controlled updater integration uses local bare remotes and synthetic projects."""
from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import test_monitor as helpers


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='monitor sync 中文 ');self.addCleanup(self.tmp.cleanup)
        base=Path(self.tmp.name);self.root=base/'checkout';self.remote=base/'remote.git';self.publisher=base/'publisher'
        self.git(base,'init','--bare','-b','main',str(self.remote))
        self.git(base,'clone',str(self.remote),str(self.root))
        for name in ['scripts','config','assets']:shutil.copytree(helpers.SOURCE/name,self.root/name)
        (self.root/'.gitignore').write_text('/data/\n/.local/\n__pycache__/\n*.pyc\n')
        (self.root/'release-marker.txt').write_text('initial')
        tests=self.root/'tests';tests.mkdir()
        (tests/'test_smoke.py').write_text("import unittest,sys\nfrom pathlib import Path\nroot=Path(__file__).resolve().parents[1]\nsys.path.insert(0,str(root/'scripts'))\nfrom rank_monitor.core import load_config,scopes\nclass Smoke(unittest.TestCase):\n def test_game_plan(self):self.assertEqual(len(scopes(load_config(root,'games'))),96)\n")
        self.commit(self.root);self.git(self.root,'push','origin','main')
        self.git(base,'clone',str(self.remote),str(self.publisher))
        self.assertTrue((self.root/'scripts/launcher.py').exists(),'缺少稳定启动器入口')
        self.call_source('install','--enable-update')

    def git(self, cwd, *args):
        p=subprocess.run(['git','-c','user.name=Monitor Test','-c','user.email=1+monitor-test@users.noreply.github.com',*args],cwd=cwd,capture_output=True,text=True)
        self.assertEqual(p.returncode,0,p.stderr);return p.stdout.strip()

    def commit(self, root):
        self.git(root,'add','--all');self.git(root,'commit','-m','synthetic revision')
        return self.git(root,'rev-parse','HEAD')

    def publish(self, invalid=False):
        (self.publisher/'release-marker.txt').write_text('updated')
        c=json.loads((self.publisher/'config/monitor.json').read_text());c['maxScopeRetries']=1
        (self.publisher/'config/monitor.json').write_text(json.dumps(c))
        if invalid:(self.publisher/'tests/test_invalid.py').write_text('raise RuntimeError("synthetic candidate failure")\n')
        sha=self.commit(self.publisher);self.git(self.publisher,'push','origin','main');return sha

    def call_source(self,*args):
        p=subprocess.run([sys.executable,str(self.root/'scripts/launcher.py'),*args],cwd=self.root,capture_output=True,text=True)
        self.assertEqual(p.returncode,0,p.stderr+p.stdout);return json.loads(p.stdout)

    def call(self,*args,ok=True):
        p=subprocess.run([sys.executable,str(self.root/'.local/monitor-launcher.py'),*args],cwd=self.root,capture_output=True,text=True)
        self.assertEqual(p.returncode==0,ok,p.stderr+p.stdout)
        return json.loads(p.stdout)

    def configure(self,**changes):
        p=self.root/'.local/update.json';c=json.loads(p.read_text());c.update(changes);p.write_text(json.dumps(c))

    def test_install_preserves_existing_policy_and_private_files(self):
        p=self.root/'.local/update.json';self.configure(enabled=False);before=p.read_bytes()
        secret=self.root/'.local/mail-secrets.json';secret.write_text('synthetic private data')
        self.call_source('install','--enable-update')
        self.assertEqual(p.read_bytes(),before);self.assertEqual(secret.read_text(),'synthetic private data')
        self.assertEqual((self.root/'.local/monitor-launcher.py').stat().st_mode&0o777,0o600)

    def test_check_only_is_offline_and_does_not_register_run(self):
        self.git(self.root,'remote','set-url','origin',str(self.root/'missing-remote'))
        result=self.call('startup','--profile','apps','--check-only')
        self.assertEqual(result['update']['status'],'check_only');self.assertFalse((self.root/'data/state.json').exists())

    def test_no_update_registers_current_code_without_leaking_token(self):
        sha=self.git(self.root,'rev-parse','HEAD');r=self.call('startup','--profile','apps')
        self.assertEqual(r['update']['status'],'current');self.assertEqual(r['run']['plannedScopes'],144)
        m=json.loads((self.root/r['run']['relativePath']/'manifest.json').read_text())
        self.assertEqual(m['codeCommit'],sha)
        token=(self.root/r['run']['tokenFile']).read_text()
        self.assertNotIn(token,json.dumps(r));self.assertNotIn(token,(self.root/r['audit']).read_text())

    def test_disabled_update_keeps_local_code(self):
        before=self.git(self.root,'rev-parse','HEAD');self.publish();self.configure(enabled=False)
        result=self.call('startup','--profile','games')
        self.assertEqual(result['update']['status'],'disabled');self.assertEqual(self.git(self.root,'rev-parse','HEAD'),before)
        self.assertEqual(result['run']['plannedScopes'],96)

    def test_candidate_verified_then_new_process_uses_new_config_and_code(self):
        secret=self.root/'.local/mail-secrets.json';secret.write_text('synthetic private data')
        local=self.root/'data/keep.txt';local.parent.mkdir(exist_ok=True);local.write_text('synthetic history')
        self.publish()
        program=self.publisher/'scripts/monitor.py'
        program.write_text(program.read_text().replace('"ready": True,','"ready": True, "syntheticNewCode": True,',1))
        target=self.commit(self.publisher);self.git(self.publisher,'push','origin','main')
        result=self.call('startup','--profile','games')
        self.assertEqual(result['update']['status'],'updated');self.assertEqual(self.git(self.root,'rev-parse','HEAD'),target)
        m=json.loads((self.root/result['run']['relativePath']/'manifest.json').read_text())
        self.assertEqual(m['config']['maxScopeRetries'],1);self.assertEqual(m['codeCommit'],target)
        self.assertTrue(result['doctors']['games']['syntheticNewCode'])
        self.assertEqual(local.read_text(),'synthetic history');self.assertEqual(secret.read_text(),'synthetic private data')

    def test_failed_candidate_stays_on_known_revision_and_starts_locally(self):
        before=self.git(self.root,'rev-parse','HEAD');self.publish(invalid=True)
        result=self.call('startup','--profile','apps')
        self.assertEqual(result['update']['status'],'kept_current');self.assertEqual(result['update']['reason'],'validation_failed')
        self.assertEqual(self.git(self.root,'rev-parse','HEAD'),before);self.assertIn('runId',result['run'])

    def test_fetch_failure_continues_local_version_and_stop_policy_is_available(self):
        self.git(self.root,'remote','set-url','origin',str(self.root/'missing-remote'))
        self.configure(onFailure='stop');r=self.call('startup','--profile','apps',ok=False)
        self.assertEqual(r['update']['reason'],'fetch_failed');self.assertFalse((self.root/'data/state.json').exists())
        self.configure(onFailure='keep_current');r=self.call('startup','--profile','apps')
        self.assertEqual(r['update']['status'],'kept_current');self.assertIn('runId',r['run'])

    def test_other_profile_unfinished_run_defers_update_for_both(self):
        old=self.git(self.root,'rev-parse','HEAD')
        self.call('command','start','--profile','games');self.publish()
        r=self.call('startup','--profile','apps')
        self.assertEqual(r['update']['status'],'deferred');self.assertEqual(self.git(self.root,'rev-parse','HEAD'),old)
        self.assertEqual(r['update']['busyRounds'][0]['profile'],'games')
        self.assertTrue(self.call('startup','--profile','games')['run']['skipped'])

    def test_unfinished_test_run_also_protects_shared_code(self):
        self.call('command','start','--profile','games','--mode','test');self.publish()
        r=self.call('startup','--profile','apps');self.assertEqual(r['update']['status'],'deferred')
        self.assertEqual(r['update']['busyRounds'][0]['mode'],'test')

    def test_dirty_checkout_is_not_overwritten_or_started(self):
        p=self.root/'release-marker.txt';p.write_text('local edits');self.publish()
        r=self.call('startup','--profile','apps',ok=False)
        self.assertEqual(r['update']['reason'],'dirty_checkout');self.assertEqual(p.read_text(),'local edits')
        self.assertFalse((self.root/'data/state.json').exists())

    def test_protected_candidate_files_never_overwrite_local_data(self):
        p=self.publisher/'.local';p.mkdir();(p/'credentials.json').write_text('synthetic forbidden candidate')
        self.git(self.publisher,'add','-f','.local/credentials.json');self.git(self.publisher,'commit','-m','synthetic protected path');self.git(self.publisher,'push','origin','main')
        r=self.call('startup','--profile','apps')
        self.assertEqual(r['update']['reason'],'protected_paths');self.assertFalse((self.root/'.local/credentials.json').exists())

    def test_divergent_history_is_never_reset_or_force_merged(self):
        (self.root/'local-marker.txt').write_text('local commit');local=self.commit(self.root);self.publish()
        r=self.call('startup','--profile','apps')
        self.assertEqual(r['update']['reason'],'not_fast_forward');self.assertEqual(self.git(self.root,'rev-parse','HEAD'),local)

    def test_corrupt_history_and_interrupted_apply_block_new_runs(self):
        state=self.root/'data/games/state.json';state.parent.mkdir(parents=True);state.write_text('{')
        r=self.call('startup','--profile','apps',ok=False);self.assertEqual(r['update']['reason'],'invalid_history')
        state.unlink();(self.root/'.local/update-state.json').write_text(json.dumps({'phase':'applying'}))
        r=self.call('startup','--profile','apps',ok=False);self.assertEqual(r['update']['reason'],'interrupted_apply')
        self.assertFalse((self.root/'data/state.json').exists())

    def test_concurrent_startups_update_once_and_register_under_same_lock(self):
        target=self.publish()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda p:self.call('startup','--profile',p),['apps','games']))
        self.assertEqual(sorted(r['update']['status'] for r in results),['deferred','updated'])
        self.assertTrue(all(r['update']['afterCommit']==target for r in results))
        self.assertTrue(all('runId' in r['run'] for r in results))

    def test_startup_waits_for_shared_command_lock(self):
        with (self.root/'.local/repository.guard').open('a') as guard:
            fcntl.flock(guard,fcntl.LOCK_SH)
            p=subprocess.Popen([sys.executable,str(self.root/'.local/monitor-launcher.py'),'startup','--profile','apps','--check-only'],cwd=self.root,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            try:
                with self.assertRaises(subprocess.TimeoutExpired):p.communicate(timeout=.15)
            finally:fcntl.flock(guard,fcntl.LOCK_UN)
            out,err=p.communicate(timeout=15);self.assertEqual(p.returncode,0,err);self.assertEqual(json.loads(out)['update']['status'],'check_only')

    def test_manual_revision_change_cannot_resume_pinned_round(self):
        r=self.call('startup','--profile','apps')['run'];token=(self.root/r['tokenFile']).read_text()
        (self.root/'local-marker.txt').write_text('manual switch');self.commit(self.root)
        response=self.call('command','resume',r['runId'],'--profile','apps','--token',token,ok=False)
        self.assertIn('代码版本',response['error'])

    def test_invalid_local_policy_does_not_expose_private_fields(self):
        self.configure(enabled='yes',extra='synthetic-private-value')
        r=self.call('startup','--profile','apps',ok=False)
        self.assertEqual(r['update']['reason'],'invalid_config');self.assertNotIn('synthetic-private-value',json.dumps(r))

    def test_wrong_branch_blocks_update_and_new_collection(self):
        self.git(self.root,'switch','-c','development')
        r=self.call('startup','--profile','apps',ok=False)
        self.assertEqual(r['update']['reason'],'wrong_branch');self.assertFalse((self.root/'data/state.json').exists())

    def test_candidate_timeout_does_not_switch_the_checkout(self):
        before=self.git(self.root,'rev-parse','HEAD')
        (self.publisher/'tests/test_slow.py').write_text('import time\ntime.sleep(3)\n')
        self.commit(self.publisher);self.git(self.publisher,'push','origin','main')
        self.configure(validationTimeoutSeconds=1)
        r=self.call('startup','--profile','apps')
        self.assertEqual(r['update']['reason'],'validation_failed');self.assertEqual(self.git(self.root,'rev-parse','HEAD'),before)

    def test_cancel_recovery_marker_defers_update_and_does_not_open_round(self):
        r=self.call('startup','--profile','apps')['run'];token=(self.root/r['tokenFile']).read_text()
        self.call('command','abort',r['runId'],'--profile','apps','--token',token,'--reason','synthetic user cancellation')
        (self.root/'data/active.json').write_text(json.dumps({'runId':r['runId']}))
        self.publish();result=self.call('startup','--profile','apps')
        self.assertEqual(result['update']['status'],'deferred');self.assertTrue(result['run']['cancelled'])
        self.assertEqual(len(json.loads((self.root/'data/state.json').read_text())['runs']),1)

    def test_manual_dirty_edit_cannot_resume_pinned_round(self):
        r=self.call('startup','--profile','apps')['run'];token=(self.root/r['tokenFile']).read_text()
        (self.root/'release-marker.txt').write_text('manual uncommitted edit')
        response=self.call('command','progress',r['runId'],'--profile','apps','--token',token,ok=False)
        self.assertIn('代码版本',response['error'])

    def test_failed_local_doctor_reports_stage_and_reason_without_starting(self):
        path=self.root/'config/monitor.json';c=json.loads(path.read_text());c['cutoff']=79;path.write_text(json.dumps(c))
        self.commit(self.root);self.configure(enabled=False)
        result=self.call('startup','--profile','apps',ok=False)
        self.assertEqual(result['failure']['step'],'doctor');self.assertEqual(result['failure']['profile'],'apps')
        self.assertIn('前80',result['failure']['detail']);self.assertFalse((self.root/'data/state.json').exists())

    def test_malformed_update_state_returns_a_structured_failure(self):
        (self.root/'.local/update-state.json').write_text('[]')
        r=self.call('startup','--profile','apps',ok=False)
        self.assertEqual(r['update']['reason'],'invalid_update_state')
        self.assertFalse((self.root/'data/state.json').exists())

    def test_dirty_manual_start_is_rejected_before_any_round_or_token_is_written(self):
        (self.root/'release-marker.txt').write_text('manual uncommitted edit')
        result=self.call('command','start','--profile','apps',ok=False)
        self.assertIn('代码版本',result['error'])
        p=subprocess.run([sys.executable,str(self.root/'scripts/monitor.py'),'start','--profile','games'],cwd=self.root,capture_output=True,text=True)
        self.assertNotEqual(p.returncode,0);self.assertIn('代码版本',json.loads(p.stdout)['error'])
        self.assertFalse((self.root/'data/state.json').exists());self.assertFalse((self.root/'data/games/state.json').exists())
        self.assertFalse(list((self.root/'.local/tokens').rglob('*')))

    def test_same_cohort_joins_both_profiles_then_drains_before_update(self):
        old=self.git(self.root,'rev-parse','HEAD')
        app=self.call('startup','--profile','apps')['run']
        game=self.call('startup','--profile','games')['run']
        self.assertFalse(game.get('skipped'));self.assertEqual(game['plannedScopes'],96)
        self.publish()
        def finish(profile,run):
            token=(self.root/run['tokenFile']).read_text()
            self.call('command','abort',run['runId'],'--profile',profile,'--token',token,'--reason','synthetic user cancellation')
        finish('apps',app)
        waiting=self.call('startup','--profile','apps')
        self.assertTrue(waiting['run']['skipped']);self.assertTrue(waiting['run']['waitingForDrain'])
        self.assertEqual(self.git(self.root,'rev-parse','HEAD'),old)
        self.assertEqual(len(json.loads((self.root/'data/state.json').read_text())['runs']),1)
        self.assertTrue(self.call('startup','--profile','games')['run']['skipped'])
        finish('games',game)
        next_game=self.call('startup','--profile','games')
        self.assertEqual(next_game['update']['status'],'updated')
        next_app=self.call('startup','--profile','apps')
        self.assertFalse(next_app['run'].get('skipped'));self.assertEqual(next_app['run']['plannedScopes'],144)
        self.assertEqual(next_app['update']['afterCommit'],next_game['update']['afterCommit'])
        self.assertEqual(len(json.loads((self.root/'data/state.json').read_text())['runs']),2)
        self.assertEqual(len(json.loads((self.root/'data/games/state.json').read_text())['runs']),2)

    def test_legacy_other_profile_work_drains_without_creating_a_new_round(self):
        self.call('command','start','--profile','games');self.publish()
        r=self.call('startup','--profile','apps')
        self.assertTrue(r['run']['skipped']);self.assertTrue(r['run']['waitingForDrain'])
        self.assertFalse((self.root/'data/state.json').exists())

    def test_partial_activation_reports_actual_head_and_preserves_apply_block(self):
        target=self.publish();hook=self.root/'.git/hooks/post-merge'
        hook.write_text('#!/bin/sh\n: > hook-untracked.txt\n');hook.chmod(0o700)
        r=self.call('startup','--profile','apps',ok=False)
        self.assertEqual(r['update']['reason'],'interrupted_apply')
        self.assertEqual(self.git(self.root,'rev-parse','HEAD'),target)
        self.assertEqual(r['update']['afterCommit'],target)
        self.assertEqual(json.loads((self.root/r['audit']).read_text().splitlines()[-1])['update']['afterCommit'],target)
        self.assertEqual(json.loads((self.root/'.local/update-state.json').read_text())['phase'],'applying')
        self.assertFalse((self.root/'data/state.json').exists())

    def test_failed_activation_with_unreadable_head_reports_unknown_version(self):
        self.publish();hook=self.root/'.git/hooks/post-merge'
        hook.write_text('#!/bin/sh\nmv .git/HEAD .git/HEAD.hidden\n');hook.chmod(0o700)
        r=self.call('startup','--profile','apps',ok=False)
        self.assertEqual(r['update']['reason'],'interrupted_apply');self.assertIsNone(r['update']['afterCommit'])
        self.assertIsNone(json.loads((self.root/r['audit']).read_text().splitlines()[-1])['update']['afterCommit'])
        self.assertFalse((self.root/'data/state.json').exists())

    def test_malformed_cohort_state_is_rejected_before_any_registration(self):
        sha=self.git(self.root,'rev-parse','HEAD')
        for cohort in [None,[],{'commit':'invalid','runs':{}},{'commit':sha,'runs':{'apps':'invalid'}}]:
            with self.subTest(cohort=cohort):
                (self.root/'.local/update-state.json').write_text(json.dumps({'phase':'ready','cohort':cohort}))
                r=self.call('startup','--profile','apps',ok=False)
                self.assertEqual(r['update']['reason'],'invalid_update_state')
                self.assertFalse((self.root/'data/state.json').exists())

    def test_unicode_protected_paths_are_not_installed(self):
        folder=self.publisher/'data';folder.mkdir();(folder/'生产数据.json').write_text('synthetic forbidden data')
        self.git(self.publisher,'add','-f','data/生产数据.json');self.git(self.publisher,'commit','-m','synthetic protected unicode path');self.git(self.publisher,'push','origin','main')
        r=self.call('startup','--profile','apps')
        self.assertEqual(r['update']['reason'],'protected_paths')
        self.assertFalse((self.root/'data/生产数据.json').exists())
