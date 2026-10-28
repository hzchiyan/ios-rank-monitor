"""One-year release admission without changing ranking or archived six-month results."""
from datetime import date
import json
import shutil
import subprocess
import sys
from unittest.mock import patch

import test_monitor as helpers
import test_releases as old

sys.path.insert(0, str(helpers.SOURCE/'scripts'))
from rank_monitor.core import Store, config_id, read_json, write_json
from rank_monitor.releases import month_cutoff, policy_for

YEAR = {'version':'qimai_first_release_12_months_v1','months':12,'sourceLabel':'最早发布'}


class OneYearTests(helpers.Harness):
    def setUp(self):
        super().setUp()
        self.configure(YEAR)

    def configure(self, policy):
        path=self.root/'config/monitor.json'
        config=read_json(path);config['releasePolicy']=policy;write_json(path,config)

    def prepare(self, run):
        return self.cli('prepare-release',run['runId'],'--token',run['token'])

    def evidence(self, app_id, release='2026-10-01', **changes):
        return old.ReleaseTests.evidence(self,app_id,release,**changes)

    def release(self, run, app_id, released, **changes):
        return self.cli('ingest-release',run['runId'],'--token',run['token'],payload=self.evidence(app_id,released,**changes))

    def pair(self, ids=(81,82,83), dual=False):
        return old.ReleaseTests.pair(self,ids=ids,dual=dual)

    def test_repository_defaults_to_year_and_freezes_all_twelve_cutoffs(self):
        config=read_json(helpers.SOURCE/'config/monitor.json')
        self.assertEqual(config['releasePolicy'],YEAR)
        run=self.start('2026-10-20T01:00:00Z')
        policy=read_json(self.root/run['relativePath']/'manifest.json')['releasePolicy']
        self.assertEqual(policy['months'],12)
        self.assertEqual(policy['windowStarts']['12'],'2025-10-20')
        self.assertEqual(set(policy['windowStarts']),{str(n) for n in range(1,13)})

    def test_calendar_year_clamps_leap_day_and_rejects_out_of_range(self):
        self.assertEqual(month_cutoff(date(2024,2,29),12),date(2023,2,28))
        self.assertEqual(month_cutoff(date(2025,3,31),12),date(2024,3,31))
        self.assertEqual(month_cutoff(date(2026,10,28),12),date(2025,10,28))
        for n in [0,13,True,1.5,'12']:
            with self.assertRaises(ValueError):month_cutoff(date(2026,10,28),n)

    def test_year_boundary_old_future_and_unknown_with_dual_chart_merge(self):
        _,b=self.pair(ids=(81,82,83,84,85,86),dual=True)
        for app_id,released in [(81,'2026-01-20'),(82,'2025-10-20'),(83,'2025-10-19'),(84,'2026-10-20'),(85,'2026-10-21')]:
            self.release(b,app_id,released)
        self.release(b,86,None,status='unavailable',rawText='',reason='page has no release date')
        self.finish(b);report=self.result(b)
        self.assertEqual({e['appId'] for e in report['entries']},{'81','82','84'})
        self.assertTrue(all(e['triggers']==['free','grossing'] for e in report['entries']))
        self.assertEqual([e['appId'] for e in report['releaseExcluded']],['83'])
        self.assertEqual({e['appId'] for e in report['releasePending']},{'85','86'})
        self.assertEqual(report['counts']['confirmedNew'],3)

    def test_inconsistent_policy_version_and_months_rejected(self):
        invalid=[{**YEAR,'months':6},{**old.POLICY,'months':12},{**YEAR,'months':True},
                 {**YEAR,'months':13},{**YEAR,'months':12.0},{**YEAR,'version':'other'},[],{'version':[]},None]
        for policy in invalid[:-1]:
            with self.subTest(policy=policy):
                with self.assertRaises(ValueError):policy_for({'releasePolicy':policy},'2026-10-20')
        self.assertIsNone(policy_for({'releasePolicy':None},'2026-10-20'))

    def mixed_policy_runs(self):
        self.configure(old.POLICY)
        _,b=self.pair()
        for app_id,released in [(81,'2026-05-20'),(82,'2026-01-20'),(83,'2025-10-20')]:self.release(b,app_id,released)
        self.finish(b)
        folder=self.root/b['relativePath']
        frozen={p.name:p.read_bytes() for p in folder.glob('report*')}
        self.configure(YEAR)
        c=self.start('2026-10-20T10:00:00Z')
        self.ingest(c,self.snapshot([84,85,86,*range(4,81)],at='2026-10-20T10:00:00Z'))
        for app_id,released in [(84,'2026-05-20'),(85,'2026-01-20'),(86,'2025-10-20')]:
            self.release(c,app_id,released,observedAt='2026-10-20T10:01:00Z')
        self.finish(c)
        self.assertEqual({p.name:p.read_bytes() for p in folder.glob('report*')},frozen)
        self.assertEqual(self.result(b)['counts']['confirmedNew'],1)
        self.assertEqual(self.result(c)['counts']['confirmedNew'],3)
        self.assertEqual(self.result(c)['coverage']['comparableScopes'],1)
        return b,c

    def test_switch_to_year_preserves_six_month_report_and_ranking_comparison(self):
        self.mixed_policy_runs()

    def test_markdown_and_mail_use_archived_policy_boundaries(self):
        from rank_monitor.mailer import MailDelivery
        _,b=self.pair(ids=(81,));self.release(b,81,'2026-01-20');self.finish(b)
        store=Store(self.root,'test');folder=store.locate(b['runId'])
        text=(folder/'report.md').read_text()
        self.assertIn('2025-10-20 至 2026-10-20',text)
        self.assertIn('超过一年排除',text)
        with patch.object(store,'compare',side_effect=AssertionError('Do not recompute archived mail')):
            preview=MailDelivery(store).preview(b['runId'])
        plain=(self.root/preview['text']).read_text()
        self.assertIn('2025-10-20 至 2026-10-20',plain)
        self.assertIn('超过一年排除',plain)

    def test_html_year_default_legacy_switch_mixed_policies_and_month_filters(self):
        node=shutil.which('node')
        if not node:self.skipTest('Node unavailable for offline HTML runtime test')
        _,c=self.mixed_policy_runs()
        html=(self.root/c['relativePath']).parents[1]/'index.html'
        result=subprocess.run([node,str(helpers.SOURCE/'tests/report_runtime.js'),str(html),'release-year'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('one-year release UI passed',result.stdout)

    def test_verified_date_excluded_by_old_policy_can_be_reused_with_year_policy(self):
        self.configure(old.POLICY)
        _,b=self.pair(ids=(81,));self.release(b,81,'2026-01-20');self.finish(b)
        self.assertEqual(self.result(b)['entries'],[])
        c=self.start('2026-10-20T10:00:00Z');self.ingest(c,self.snapshot(range(1,81),at='2026-10-20T10:00:00Z'));self.finish(c)
        self.configure(YEAR)
        d=self.start('2026-10-21T01:00:00Z')
        self.ingest(d,self.snapshot([81,*range(2,81)],at='2026-10-21T01:00:00Z',sourceDate='2026-10-21'))
        self.assertEqual(self.prepare(d)['pending'],[])
        self.finish(d)
        self.assertEqual([e['appId'] for e in self.result(d)['entries']],['81'])
        self.assertEqual(self.result(d)['releaseRecords']['us:81']['origin'],'cache')

    def test_policy_change_does_not_reset_ranking_or_widen_inflight_half_year_run(self):
        self.configure(old.POLICY)
        before=read_json(self.root/'config/monitor.json')
        _,b=self.pair(ids=(81,));self.release(b,81,'2026-01-20')
        self.configure(YEAR)
        self.assertEqual(config_id(before),config_id(read_json(self.root/'config/monitor.json')))
        self.finish(b)
        self.assertEqual(self.result(b)['run']['releasePolicy']['months'],6)
        self.assertEqual(self.result(b)['entries'],[])
