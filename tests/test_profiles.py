"""Independent app/game pipelines; all fixtures and SMTP are synthetic."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import json
import sys
from unittest.mock import patch

import test_monitor as helpers
import test_mail as mail_helpers
import test_releases as release_helpers

sys.path.insert(0, str(helpers.SOURCE/'scripts'))
from rank_monitor.core import Store, config_id, load_config, read_json, write_json

PROFILES={'apps': {'scanFamilies': ['应用']}, 'games': {'scanFamilies': ['游戏']}}
GAME_KEY='us:游戏:7016:free'


class ProfileTests(helpers.Harness):
    def setUp(self):
        super().setUp()
        self.configure()

    def configure(self, **changes):
        p=self.root/'config/monitor.json';c=read_json(p)
        c.update(profiles=deepcopy(PROFILES),scanFamilies=['应用'],comparisonVersion='latest_valid_scope_v1')
        c.update(changes);write_json(p,c)

    def store(self, profile='apps', mode='test'):
        return Store(self.root,mode,profile=profile)

    def game(self, ids=range(1,81), at='2026-10-20T01:00:00Z'):
        return self.snapshot(ids,at=at,sourceDate=at[:10],title='美国iPhone体育免费榜',categoryFamily='游戏',category='体育',genreId='7016',
                             sourceUrl='https://www.qimai.cn/rank/index/brand/free/device/iphone/country/us/genre/7016')

    def close(self, store, run):
        return store.finalize(run['runId'],run['token'])

    def test_cli_default_apps_and_explicit_games_plan_and_pending(self):
        self.assertEqual(self.cli('doctor')['plannedScopes'],144)
        self.assertEqual(self.cli('doctor','--profile','games')['plannedScopes'],96)
        a=self.start('2026-10-20T01:00:00Z')
        g=self.cli('start','--profile','games','--mode','test','--at','2026-10-20T01:00:00Z')
        self.assertEqual(a['runId'],g['runId'])
        self.assertEqual(g['relativePath'],'data/games/test/2026-10-20/runs/'+g['runId'])
        self.assertEqual({s['categoryFamily'] for s in self.cli('pending',g['runId'],'--profile','games')['scopes']},{'游戏'})
        self.assertEqual({s['categoryFamily'] for s in self.cli('pending',a['runId'])['scopes']},{'应用'})

    def test_profile_configs_preserve_default_fingerprint_and_legacy_behavior(self):
        p=self.root/'config/monitor.json';c=read_json(p);effective=load_config(self.root)
        c.pop('profiles');write_json(p,c)
        self.assertEqual(config_id(load_config(self.root)),config_id(effective))
        c.pop('scanFamilies');write_json(p,c)
        Store(self.root).start()
        self.assertEqual(len(Store(self.root).pending(Store(self.root).state()['latestRunId'])),240)
        with self.assertRaisesRegex(ValueError,'游戏|分组'):self.store('games')

    def test_invalid_profiles_are_rejected_without_cross_family_plan(self):
        for definitions in [None, {}, {'apps':PROFILES['apps']}, {**PROFILES,'other':{}},
                            {**PROFILES,'games':{'scanFamilies':['应用','游戏']}},
                            {**PROFILES,'apps':{'scanFamilies':['游戏']}}]:
            self.configure(profiles=definitions)
            with self.assertRaises(ValueError):load_config(self.root)

    def test_tokens_locks_and_histories_are_independent_for_same_ids(self):
        stores=[self.store(p,m) for p in ['apps','games'] for m in ['formal','test']]
        when=datetime(2026,10,20,1,tzinfo=timezone.utc)
        with patch('rank_monitor.core.utcnow',return_value=when):
            runs=[s.start() for s in stores]
            self.assertEqual(len({r['runId'] for r in runs}),1)
            for i,(s,r) in enumerate(zip(stores,runs)):
                self.assertEqual(len(s.state()['runs']),1)
                self.assertEqual(s.resume(r['runId'],r['token'])['runId'],r['runId'])
                for j,foreign in enumerate(runs):
                    if i!=j:
                        with self.assertRaises(ValueError):s.resume(r['runId'],foreign['token'])
        self.assertEqual(len(list((self.root/'.local').rglob('*.guard'))),4)

    def test_game_profile_does_not_use_app_legacy_shared_token(self):
        s=self.store('games');r=s.start('2026-10-20T01:00:00Z')
        (self.root/'.local/tokens/games/test'/r['runId']).unlink()
        (self.root/'.local/tokens'/r['runId']).write_text(r['token'])
        m=s.manifest(r['runId']);m.pop('tokenVersion');write_json(s.locate(r['runId'])/'manifest.json',m)
        with self.assertRaisesRegex(ValueError,'令牌'):s.resume(r['runId'],r['token'])

    def test_game_run_cannot_be_inserted_into_app_history(self):
        games=self.store('games');r=games.start('2026-10-20T01:00:00Z')
        state=games.state();write_json(self.root/'data/test/state.json',state)
        with self.assertRaisesRegex(ValueError,'分组|profile|目录外'):self.store().manifest(r['runId'])

    def test_cancelling_game_does_not_cancel_or_unlock_app(self):
        a=self.store();g=self.store('games');ar=a.start('2026-10-20T01:00:00Z');gr=g.start('2026-10-20T01:00:00Z')
        with patch.object(g,'_render',side_effect=OSError('synthetic render failure')):
            with self.assertRaises(OSError):g.abort(gr['runId'],gr['token'],'synthetic user cancellation')
        self.assertTrue(g.start()['cancelled'])
        self.assertEqual(a.manifest(ar['runId'])['status'],'running')
        self.assertTrue((a.base/'active.json').exists())
        self.assertEqual(len(a.pending(ar['runId'])),144)

    def legacy_game(self, at='2026-10-20T01:00:00Z'):
        p=self.root/'config/monitor.json';c=read_json(p);c.pop('profiles');c['scanFamilies']=['应用','游戏'];write_json(p,c)
        s=self.store();r=s.start(at);s.ingest(r['runId'],r['token'],self.game(at=at));self.close(s,r)
        self.configure()
        return s,r

    def save_game(self,s,r,ids=range(2,82)):
        s.ingest(r['runId'],r['token'],self.game(ids,s.manifest(r['runId'])['startedAt']))

    def test_game_inherits_compatible_legacy_snapshot_without_rewriting_history(self):
        old,r=self.legacy_game();folder=old.locate(r['runId'])
        before={str(p):p.read_bytes() for p in folder.rglob('*') if p.is_file()}
        g=self.store('games');new=g.start('2026-10-20T04:00:00Z');self.save_game(g,new);self.close(g,new)
        report=g.archived_report(new['runId']);baseline=next(s['baseline'] for s in report['coverage']['scopes'] if s['comparison']=='comparable')
        self.assertEqual(report['counts']['confirmedNew'],1)
        self.assertEqual(baseline['baselineRunId'],r['runId']);self.assertEqual(baseline['baselineProfile'],'apps')
        self.assertEqual(baseline['elapsedSeconds'],10800)
        self.assertEqual(before,{str(p):p.read_bytes() for p in folder.rglob('*') if p.is_file()})
        self.assertEqual(len(old.state()['runs']),1)

    def test_game_prefers_its_newer_complete_snapshot_and_freezes_source(self):
        self.legacy_game();g=self.store('games');a=g.start('2026-10-20T04:00:00Z');self.save_game(g,a);self.close(g,a)
        b=g.start('2026-10-20T07:00:00Z');self.save_game(g,b,range(3,83));self.close(g,b)
        report=g.archived_report(b['runId']);ref=report['entries'][0]['comparisonBaselines']['free']
        self.assertEqual(report['counts']['confirmedNew'],1);self.assertEqual(report['entries'][0]['appId'],'82')
        self.assertEqual(ref['baselineRunId'],a['runId']);self.assertEqual(ref['baselineProfile'],'games')

    def test_future_or_unfinished_app_history_does_not_block_or_seed_game(self):
        self.legacy_game('2026-10-20T07:00:00Z')
        apps=self.store();apps.start('2026-10-20T10:00:00Z')
        g=self.store('games');r=g.start('2026-10-20T04:00:00Z');self.save_game(g,r);self.close(g,r)
        report=g.archived_report(r['runId'])
        self.assertEqual(report['coverage']['comparableScopes'],0);self.assertIsNone(report['counts']['confirmedNew'])

    def test_missing_legacy_game_archive_is_not_silently_skipped(self):
        old,r=self.legacy_game();(old.locate(r['runId'])/'snapshots.jsonl.gz').unlink()
        with self.assertRaisesRegex(ValueError,'归档|快照'):self.store('games').start('2026-10-20T04:00:00Z')

    def test_game_rejects_unknown_baseline_profile_even_with_matching_checksum(self):
        from rank_monitor.core import digest
        self.legacy_game();g=self.store('games');r=g.start('2026-10-20T04:00:00Z')
        path=g.locate(r['runId']);data=read_json(path/'baselines.json')
        data['scopes'][GAME_KEY]['baselineProfile']='other';write_json(path/'baselines.json',data)
        m=g.manifest(r['runId']);m['comparisonBaselinesHash']=digest(data);write_json(path/'manifest.json',m)
        with self.assertRaisesRegex(ValueError,'分组|来源'):g.compare(r['runId'])

    def test_separate_html_entrypoints_titles_counts_and_app_history(self):
        a=self.store();ar=a.start('2026-10-20T01:00:00Z');a.ingest(ar['runId'],ar['token'],self.snapshot(range(1,81)));self.close(a,ar)
        before=(a.base/'index.html').read_bytes()
        g=self.store('games');gr=g.start('2026-10-20T01:00:00Z');self.save_game(g,gr);self.close(g,gr)
        html=(g.base/'2026-10-20/index.html').read_text()
        payload=json.loads(html.split('<script id="payload" type="application/json">')[1].split('</script>')[0])
        self.assertTrue('游戏榜单监测' in html);self.assertTrue('新进游戏' in html)
        self.assertIn('游戏榜单监测',(g.base/'index.html').read_text())
        self.assertEqual(payload['runs'][0]['coverage']['plannedScopes'],96)
        self.assertEqual({s['categoryFamily'] for s in payload['runs'][0]['coverage']['scopes']},{'游戏'})
        self.assertEqual((a.base/'index.html').read_bytes(),before)
        app=json.loads((a.base/'2026-10-20/index.html').read_text().split('<script id="payload" type="application/json">')[1].split('</script>')[0])
        self.assertEqual({s['categoryFamily'] for s in app['runs'][0]['coverage']['scopes']},{'应用'})

    def test_same_recipient_gets_two_profile_mails_with_independent_dedup(self):
        from rank_monitor.mailer import MailDelivery
        mail_helpers.MailTests.configure(self)
        mail_helpers.SMTPDouble.delivered=[];mail_helpers.SMTPDouble.failure=None
        mail_helpers.SMTPDouble.entered=None;mail_helpers.SMTPDouble.release=None
        pairs=[]
        for profile in ['apps','games']:
            s=self.store(profile,'formal');r=s.start('2026-10-20T01:00:00Z');self.close(s,r);pairs.append((s,r))
        with patch('smtplib.SMTP_SSL',mail_helpers.SMTPDouble):
            for s,r in pairs:
                self.assertEqual(MailDelivery(s).send(r['runId'])['status'],'submitted')
                self.assertEqual(MailDelivery(s).send(r['runId'])['status'],'submitted')
        self.assertEqual(len(mail_helpers.SMTPDouble.delivered),2)
        subjects=[str(m['Subject']) for _,m in mail_helpers.SMTPDouble.delivered]
        self.assertIn('应用榜单',subjects[0]);self.assertIn('游戏榜单',subjects[1])
        self.assertEqual(len(list((self.root/'.local').glob('mail-*.guard'))),2)
        for s,r in pairs:self.assertTrue((s.locate(r['runId'])/'mail/delivery.json').exists())

    def test_game_release_admission_cache_and_inherited_baseline_notice(self):
        from rank_monitor.mailer import MailDelivery
        self.legacy_game()
        self.configure(releasePolicy=read_json(helpers.SOURCE/'config/monitor.json')['releasePolicy'])
        g=self.store('games');r=g.start('2026-10-20T04:00:00Z');self.save_game(g,r)
        self.assertEqual(g.prepare_release(r['runId'],r['token'])['candidateApps'],1)
        g.ingest_release(r['runId'],r['token'],release_helpers.ReleaseTests.evidence(self,81))
        self.close(g,r)
        report=g.archived_report(r['runId'])
        self.assertEqual([e['appId'] for e in report['entries']],['81'])
        self.assertEqual(report['run']['releasePolicy']['months'],12)
        self.assertTrue((g.base/'release-cache.json').exists());self.assertFalse((self.root/'data/test/release-cache.json').exists())
        preview=MailDelivery(g).preview(r['runId']);text=(self.root/preview['text']).read_text()
        self.assertIn('基准来源：应用历史',text);self.assertNotIn('首次轮次建立基线',text)

    def test_completion_driven_game_resume_does_not_claim_app_worker(self):
        self.configure(executionPolicy='completion_v1')
        now=datetime(2026,10,20,1,tzinfo=timezone.utc)
        with patch('rank_monitor.core.utcnow',return_value=now):
            a=self.store();ar=a.start();g=self.store('games');gr=g.start()
            g.suspend(gr['runId'],gr['token'],'synthetic global browser block')
            recovered=g.start()
            self.assertTrue(recovered['resumed']);self.assertEqual(recovered['profile'],'games')
            self.assertEqual(g.progress(gr['runId'],recovered['token'])['profile'],'games')
            self.assertTrue(a.start()['skipped']);self.assertEqual(len(g.pending(gr['runId'])),96)
            with self.assertRaises(ValueError):g.resume(gr['runId'],gr['token'])

    def test_report_cache_rejects_another_profile_with_same_run_id(self):
        a=self.store();ar=a.start('2026-10-20T01:00:00Z');self.close(a,ar)
        g=self.store('games');gr=g.start('2026-10-20T01:00:00Z');self.close(g,gr)
        write_json(g.locate(gr['runId'])/'report-data.json',a.archived_report(ar['runId']))
        with self.assertRaisesRegex(ValueError,'分组|不一致'):g.archived_report(gr['runId'])

    def test_frozen_mail_rejects_another_profile_with_same_run_id(self):
        from rank_monitor.mailer import MailDelivery
        mail_helpers.MailTests.configure(self)
        mail_helpers.SMTPDouble.failure=None;mail_helpers.SMTPDouble.entered=None;mail_helpers.SMTPDouble.release=None
        a=self.store(mode='formal');ar=a.start('2026-10-20T01:00:00Z');self.close(a,ar)
        g=self.store('games','formal');gr=g.start('2026-10-20T01:00:00Z');self.close(g,gr)
        with patch('smtplib.SMTP_SSL',mail_helpers.SMTPDouble):MailDelivery(a).send(ar['runId'])
        write_json(g.locate(gr['runId'])/'mail/content.json',read_json(a.locate(ar['runId'])/'mail/content.json'))
        with self.assertRaisesRegex(ValueError,'分组|不一致'):MailDelivery(g).preview(gr['runId'])

    def test_formal_game_rejects_app_index_pointing_to_test_history(self):
        old,r=self.legacy_game()
        write_json(self.root/'data/state.json',old.state())
        with self.assertRaisesRegex(ValueError,'模式|mode|测试'):
            self.store('games','formal').start('2026-10-20T04:00:00Z')

    def test_frozen_formal_game_rejects_legacy_manifest_mode_change(self):
        self.legacy_game()
        old=mail_helpers.MailTests.store(self,formal=True)
        g=self.store('games','formal');r=g.start('2026-10-20T04:00:00Z')
        source=old.locate(old.state()['latestRunId'])/'manifest.json';m=read_json(source);m['mode']='test';write_json(source,m)
        with self.assertRaisesRegex(ValueError,'模式|mode|测试'):g.compare(r['runId'])

    def test_inherited_baseline_counts_only_intervening_game_rounds(self):
        g=self.store('games');early=g.start('2026-10-20T01:00:00Z');self.save_game(g,early);self.close(g,early)
        self.legacy_game('2026-10-20T04:00:00Z')
        g=self.store('games');r=g.start('2026-10-20T07:00:00Z');self.save_game(g,r);self.close(g,r)
        report=g.archived_report(r['runId']);ref=report['entries'][0]['comparisonBaselines']['free']
        self.assertEqual(ref['baselineProfile'],'apps');self.assertEqual(ref['skippedRuns'],0)
        self.assertEqual(report['coverage']['fallbackScopes'],0)

    def test_inherited_baseline_counts_a_later_missing_game_round(self):
        self.legacy_game('2026-10-20T01:00:00Z')
        g=self.store('games');gap=g.start('2026-10-20T04:00:00Z');self.close(g,gap)
        r=g.start('2026-10-20T07:00:00Z');self.save_game(g,r);self.close(g,r)
        report=g.archived_report(r['runId']);ref=report['entries'][0]['comparisonBaselines']['free']
        self.assertEqual(ref['skippedRuns'],1);self.assertEqual(report['coverage']['fallbackScopes'],1)
