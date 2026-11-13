"""Power policy checks use synthetic macOS replies; never change host settings."""
import importlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SOURCE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SOURCE/'scripts'))
POLICY={'schemaVersion':1,'requireACPower':True,'acSleepMinutes':0,'displaySleepMinutes':10}


class PowerFixture:
    def __init__(self):
        self.ac={'sleep':1,'displaysleep':0,'womp':1}
        self.battery={'sleep':3,'displaysleep':2,'womp':0}
        self.source='AC Power';self.ignore_write=False;self.fail_write=False;self.deny=False

    def run(self,args,**kwargs):
        if args==['pmset','-g','custom']:
            value='Battery Power:\n'+''.join(f' {k} {v}\n' for k,v in self.battery.items())+'AC Power:\n'+''.join(f' {k} {v}\n' for k,v in self.ac.items())
            return subprocess.CompletedProcess(args,0,value,'')
        if args==['pmset','-g','batt']:
            return subprocess.CompletedProcess(args,0,f"Now drawing from '{self.source}'\n",'')
        expected=['/usr/bin/pmset','-c','sleep','0','displaysleep','10']
        if args==expected or args==['osascript','-e','do shell script "/usr/bin/pmset -c sleep 0 displaysleep 10" with administrator privileges']:
            if self.fail_write or self.deny:return subprocess.CompletedProcess(args,1,'','synthetic private diagnostic')
            if not self.ignore_write:self.ac.update(sleep=0,displaysleep=10)
            return subprocess.CompletedProcess(args,0,'','')
        raise AssertionError('Unexpected external command')


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((SOURCE/'scripts/rank_monitor/runtime.py').exists(),'缺少系统环境策略模块')
        self.runtime=importlib.import_module('rank_monitor.runtime')
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        (self.root/'config').mkdir();self.config= self.root/'config/runtime.json';self.config.write_text(json.dumps(POLICY))
        self.power=PowerFixture()
        for target,value in [('sys.platform','darwin'),('subprocess.run',self.power.run),('os.geteuid',lambda:0)]:
            p=patch('rank_monitor.runtime.'+target,value);p.start();self.addCleanup(p.stop)

    def test_bad_power_settings_are_reported_without_changing_system(self):
        r=self.runtime.check_environment(self.root)
        self.assertFalse(r['ready']);self.assertEqual(r['status'],'blocked')
        self.assertFalse(r['checks']['acSleepDisabled']);self.assertFalse(r['checks']['displaySleepConfigured'])
        self.assertEqual(self.power.ac,{'sleep':1,'displaysleep':0,'womp':1});self.assertFalse((self.root/'data').exists())

    def test_ac_policy_ready_does_not_assert_locked_browser_verified(self):
        self.power.ac.update(sleep=0,displaysleep=10)
        r=self.runtime.check_environment(self.root)
        self.assertTrue(r['ready']);self.assertEqual(r['powerSource'],'AC Power')
        self.assertFalse(r['browserWhileLockedVerified'])

    def test_battery_and_unknown_power_source_never_count_as_ready(self):
        self.power.ac.update(sleep=0,displaysleep=10)
        for value in ['Battery Power','UPS Power']:
            with self.subTest(source=value):
                self.power.source=value;r=self.runtime.check_environment(self.root);self.assertFalse(r['ready'])
                self.assertFalse(r['checks']['acPower'])

    def test_missing_ac_keys_are_not_treated_as_disabled_sleep(self):
        self.power.ac.pop('sleep')
        r=self.runtime.check_environment(self.root);self.assertFalse(r['ready']);self.assertFalse(r['checks']['acSleepDisabled'])

    def test_invalid_policy_fails_without_echoing_untrusted_values(self):
        for change in [{'schemaVersion':True},{'requireACPower':False},{'acSleepMinutes':1},{'displaySleepMinutes':True},{'displaySleepMinutes':0},{'displaySleepMinutes':181},{'private':'synthetic-private-value'}]:
            with self.subTest(change=change):
                self.config.write_text(json.dumps({**POLICY,**change}));r=self.runtime.check_environment(self.root)
                self.assertFalse(r['ready']);self.assertEqual(r['reason'],'invalid_runtime_config');self.assertNotIn('synthetic-private-value',json.dumps(r))

    def test_missing_policy_preserves_legacy_start_compatibility_but_cannot_apply(self):
        self.config.unlink();r=self.runtime.check_environment(self.root)
        self.assertTrue(r['ready']);self.assertEqual(r['status'],'not_configured')
        self.assertFalse(self.runtime.apply_environment(self.root)['ready'])

    def test_runtime_policy_does_not_change_ranking_fingerprint(self):
        from rank_monitor.core import load_config,config_id
        (self.root/'config/monitor.json').write_bytes((SOURCE/'config/monitor.json').read_bytes())
        before=config_id(load_config(self.root))
        self.config.write_text(json.dumps({**POLICY,'displaySleepMinutes':20}))
        self.assertEqual(config_id(load_config(self.root)),before)

    def test_unsupported_platform_is_explicit(self):
        with patch('rank_monitor.runtime.sys.platform','linux'):r=self.runtime.check_environment(self.root)
        self.assertFalse(r['ready']);self.assertEqual(r['reason'],'unsupported_platform')

    def test_system_query_timeout_is_redacted_and_blocks(self):
        with patch('rank_monitor.runtime.subprocess.run',side_effect=subprocess.TimeoutExpired('synthetic private diagnostic',5)):
            r=self.runtime.check_environment(self.root)
        self.assertFalse(r['ready']);self.assertEqual(r['reason'],'power_query_failed');self.assertNotIn('synthetic private',json.dumps(r))

    def test_apply_persists_original_ac_and_only_changes_ac_sleep_display(self):
        before=dict(self.power.battery);r=self.runtime.apply_environment(self.root)
        self.assertTrue(r['ready']);self.assertTrue(r['systemApplied']);self.assertEqual(r['status'],'applied')
        self.assertEqual(self.power.battery,before);self.assertEqual(self.power.ac,{'sleep':0,'displaysleep':10,'womp':1})
        record=json.loads((self.root/r['record']).read_text());self.assertEqual(record['before']['ac']['sleep'],1)
        self.assertEqual(record['before']['ac']['displaysleep'],0);self.assertEqual(record['status'],'applied')

    def test_apply_requires_explicit_administrator_authorization(self):
        with patch('rank_monitor.runtime.os.geteuid',lambda:501):r=self.runtime.apply_environment(self.root)
        self.assertFalse(r['ready']);self.assertEqual(r['reason'],'administrator_required');self.assertEqual(self.power.ac['sleep'],1)

    def test_native_admin_authorization_uses_fixed_ac_only_command(self):
        with patch('rank_monitor.runtime.os.geteuid',lambda:501):r=self.runtime.apply_environment(self.root,authorize=True)
        self.assertTrue(r['ready']);self.assertEqual(self.power.battery['sleep'],3)

    def test_native_admin_denial_does_not_claim_applied_or_leak_diagnostics(self):
        self.power.deny=True
        with patch('rank_monitor.runtime.os.geteuid',lambda:501):r=self.runtime.apply_environment(self.root,authorize=True)
        self.assertFalse(r['ready']);self.assertEqual(r['reason'],'apply_failed');self.assertEqual(self.power.ac['sleep'],1)
        self.assertNotIn('synthetic private diagnostic',json.dumps(r))

    def test_apply_write_failure_is_not_success(self):
        self.power.fail_write=True;r=self.runtime.apply_environment(self.root)
        self.assertFalse(r['ready']);self.assertEqual(r['reason'],'apply_failed');self.assertFalse(r['systemApplied'])

    def test_apply_readback_failure_keeps_backup_and_reports_uncertainty(self):
        self.power.ignore_write=True;r=self.runtime.apply_environment(self.root)
        self.assertFalse(r['ready']);self.assertEqual(r['reason'],'verification_failed')
        self.assertEqual(json.loads((self.root/r['record']).read_text())['before']['ac']['sleep'],1)

    def test_backup_failure_prevents_system_changes(self):
        with patch('rank_monitor.runtime.write_json',side_effect=OSError('synthetic private diagnostic')):r=self.runtime.apply_environment(self.root)
        self.assertFalse(r['ready']);self.assertEqual(r['reason'],'record_failed');self.assertEqual(self.power.ac['sleep'],1)

    def test_battery_apply_does_not_change_any_power_settings(self):
        self.power.source='Battery Power';r=self.runtime.apply_environment(self.root)
        self.assertFalse(r['ready']);self.assertEqual(r['reason'],'ac_power_required');self.assertEqual(self.power.ac['sleep'],1)

    def test_policy_links_cannot_downgrade_to_legacy_compatibility(self):
        for dangling in [True,False]:
            with self.subTest(dangling=dangling):
                self.config.unlink()
                target=self.root/'linked.json'
                if not dangling:target.write_text(json.dumps(POLICY))
                self.config.symlink_to(target)
                r=self.runtime.check_environment(self.root)
                self.assertFalse(r['ready']);self.assertEqual(r['reason'],'invalid_runtime_config')

    def test_unsearchable_policy_is_blocked_instead_of_legacy(self):
        with patch('rank_monitor.runtime.Path.lstat',side_effect=PermissionError('synthetic private diagnostic')):
            r=self.runtime.check_environment(self.root)
        self.assertFalse(r['ready']);self.assertEqual(r['reason'],'invalid_runtime_config')

    def test_admin_timeout_preserves_unknown_outcome_and_original_backup(self):
        def run(args,**kwargs):
            if args[0]=='osascript':raise subprocess.TimeoutExpired(args,60)
            return self.power.run(args,**kwargs)
        with patch('rank_monitor.runtime.os.geteuid',lambda:501),patch('rank_monitor.runtime.subprocess.run',run):
            r=self.runtime.apply_environment(self.root,authorize=True)
        self.assertFalse(r['ready']);self.assertEqual(r['status'],'unknown');self.assertEqual(r['reason'],'apply_timeout')
        self.assertIsNone(r['systemApplied']);self.assertTrue(r['retryRequiresReview'])
        record=json.loads((self.root/r['record']).read_text());self.assertEqual(record['status'],'unknown')
        self.assertEqual(record['before']['ac']['sleep'],1)

    def test_timeout_after_settings_changed_still_requires_auth_request_review(self):
        def run(args,**kwargs):
            if args[0]=='osascript':
                self.power.ac.update(sleep=0,displaysleep=10);raise subprocess.TimeoutExpired(args,60)
            return self.power.run(args,**kwargs)
        with patch('rank_monitor.runtime.os.geteuid',lambda:501),patch('rank_monitor.runtime.subprocess.run',run):
            r=self.runtime.apply_environment(self.root,authorize=True)
        self.assertEqual(r['status'],'unknown');self.assertTrue(r['lastReadbackReady']);self.assertIsNone(r['systemApplied'])

    def test_partial_apply_exposes_actual_readback_and_keeps_original_values(self):
        def run(args,**kwargs):
            if args[0]=='/usr/bin/pmset':
                self.power.ac['sleep']=0;return subprocess.CompletedProcess(args,0,'','')
            return self.power.run(args,**kwargs)
        with patch('rank_monitor.runtime.subprocess.run',run):r=self.runtime.apply_environment(self.root)
        self.assertFalse(r['ready']);self.assertEqual(r['reason'],'verification_failed')
        record=json.loads((self.root/r['record']).read_text());self.assertEqual(record['after']['observed']['ac']['sleep'],0)
        self.assertEqual(record['after']['observed']['ac']['displaysleep'],0);self.assertEqual(record['before']['ac']['sleep'],1)

    def test_unreadable_after_apply_does_not_reuse_old_observation(self):
        written=False
        def run(args,**kwargs):
            nonlocal written
            if args[0]=='/usr/bin/pmset':
                value=self.power.run(args,**kwargs);written=True;return value
            if written:raise OSError('synthetic private diagnostic')
            return self.power.run(args,**kwargs)
        with patch('rank_monitor.runtime.subprocess.run',run):r=self.runtime.apply_environment(self.root)
        self.assertEqual(r['status'],'unknown');self.assertEqual(r['reason'],'verification_unavailable')
        self.assertIsNone(r['systemApplied']);self.assertNotIn('observed',r)

    def test_successful_apply_clears_prior_failure_reason(self):
        r=self.runtime.apply_environment(self.root)
        self.assertTrue(r['ready']);self.assertNotIn('reason',r)

    def test_post_apply_record_failure_retains_original_backup_and_actual_state(self):
        original=self.runtime.write_json;writes=0
        def write(path,value):
            nonlocal writes
            writes+=1
            if writes==2:raise OSError('synthetic private diagnostic')
            return original(path,value)
        with patch('rank_monitor.runtime.write_json',write):r=self.runtime.apply_environment(self.root)
        self.assertFalse(r['ready']);self.assertEqual(r['reason'],'record_failed');self.assertTrue(r['systemApplied'])
        self.assertEqual(json.loads((self.root/r['record']).read_text())['before']['ac']['sleep'],1)

    def test_already_compliant_apply_needs_no_administrator_or_new_backup(self):
        self.power.ac.update(sleep=0,displaysleep=10)
        with patch('rank_monitor.runtime.os.geteuid',lambda:501):r=self.runtime.apply_environment(self.root)
        self.assertTrue(r['ready']);self.assertEqual(r['status'],'unchanged');self.assertFalse(r['systemApplied']);self.assertFalse((self.root/'data').exists())
