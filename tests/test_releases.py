"""Release evidence, calendar boundaries and immutable per-run admission."""
from datetime import date
import importlib
import json
from pathlib import Path
import sys
import unittest

import test_monitor as helpers

sys.path.insert(0, str(helpers.SOURCE / "scripts"))
VERSION = "qimai_first_release_6_months_v1"
POLICY = {"version": VERSION, "months": 6, "sourceLabel": "最早发布"}


class CalendarTests(unittest.TestCase):
    def test_calendar_months_and_end_of_month_clamping(self):
        self.assertTrue((helpers.SOURCE / "scripts/rank_monitor/releases.py").exists(), "缺少上线日期处理模块")
        releases = importlib.import_module("rank_monitor.releases")
        self.assertEqual([releases.month_cutoff(date(2026, 10, 20), n).isoformat() for n in range(1, 7)],
                         ["2026-09-20", "2026-08-20", "2026-07-20", "2026-06-20", "2026-05-20", "2026-04-20"])
        self.assertEqual(releases.month_cutoff(date(2024, 8, 31), 6), date(2024, 2, 29))
        self.assertEqual(releases.month_cutoff(date(2025, 8, 31), 6), date(2025, 2, 28))
        self.assertEqual(releases.month_cutoff(date(2026, 8, 31), 1), date(2026, 7, 31))


class ReleaseTests(helpers.Harness):
    def setUp(self):
        super().setUp()
        cfg = self.root / "config/monitor.json"
        config = json.loads(cfg.read_text())
        config["releasePolicy"] = POLICY
        cfg.write_text(json.dumps(config))

    def pair(self, ids=(81, 82, 83), dual=False, second_category=False):
        a = self.start("2026-10-20T01:00:00Z")
        bcharts = ["free", "grossing"] if dual else ["free"]
        for chart in bcharts:
            self.ingest(a, self.snapshot(range(1, 81), chart))
            if second_category:
                self.ingest(a, self.finance(range(1, 81), chart, "2026-10-20T01:00:00Z"))
        self.finish(a)
        b = self.start("2026-10-20T04:00:00Z")
        new = [*ids, *range(len(ids) + 1, 81)]
        for chart in bcharts:
            self.ingest(b, self.snapshot(new, chart, "2026-10-20T04:00:00Z"))
            if second_category:
                self.ingest(b, self.finance(new, chart, "2026-10-20T04:00:00Z"))
        return a, b

    def finance(self, ids, chart, at):
        return self.snapshot(ids, chart, at, category="财务", genreId="6015",
                             sourceUrl=f"https://www.qimai.cn/rank/index/brand/{chart}/device/iphone/country/us/genre/6015")

    def prepare(self, run):
        return self.cli("prepare-release", run["runId"], "--token", run["token"])

    def evidence(self, app_id, release="2026-10-01", **changes):
        data = {"country": "us", "appId": str(app_id), "pageAppId": str(app_id), "pageCountry": "美国",
                "status": "verified", "releaseDate": release, "sourceLabel": "最早发布",
                "sourceUrl": f"https://www.qimai.cn/app/rank/appid/{app_id}/country/us",
                "rawText": f"最早发布 {release}", "observedAt": "2026-10-20T04:01:00Z"}
        data.update(changes)
        return data

    def release(self, run, app_id, release="2026-10-01", ok=True, **changes):
        return self.cli("ingest-release", run["runId"], "--token", run["token"],
                        payload=self.evidence(app_id, release, **changes), ok=ok)

    def test_recent_old_and_unknown_are_separate_without_losing_rank_baseline(self):
        _, b = self.pair()
        self.assertEqual({x["appId"] for x in self.prepare(b)["pending"]}, {"81", "82", "83"})
        self.release(b, 81)
        self.release(b, 82, "2018-01-01")
        self.release(b, 83, None, status="unavailable", rawText="", reason="页面未显示最早发布")
        self.finish(b)
        r = self.result(b)
        self.assertEqual([e["appId"] for e in r["entries"]], ["81"])
        self.assertEqual(r["entries"][0]["releaseDate"], "2026-10-01")
        self.assertEqual(r["entries"][0]["releaseReason"], "")
        self.assertEqual([e["appId"] for e in r["releasePending"]], ["83"])
        self.assertEqual({k:r["counts"][k] for k in ["rankCandidates", "confirmedNew", "excludedOlder", "pendingRelease"]},
                         dict(rankCandidates=3, confirmedNew=1, excludedOlder=1, pendingRelease=1))
        self.assertEqual(r["coverage"]["completeScopes"], 1)
        self.assertEqual(r["coverage"]["comparableScopes"], 1)
        self.assertEqual(len(json.loads(__import__('gzip').open(self.root / b["relativePath"] / "snapshots.jsonl.gz", 'rt').readline())["rows"]), 80)

    def test_inclusive_six_month_boundary_and_future_is_unverified(self):
        _, b = self.pair()
        self.prepare(b)
        self.release(b, 81, "2026-04-20")
        self.release(b, 82, "2026-04-19")
        self.release(b, 83, "2026-10-21")
        self.finish(b)
        r = self.result(b)
        self.assertEqual([e["appId"] for e in r["entries"]], ["81"])
        self.assertEqual(r["counts"]["excludedOlder"], 1)
        self.assertEqual(r["counts"]["pendingRelease"], 1)
        self.assertEqual(r["run"]["releasePolicy"]["windowStarts"]["1"], "2026-09-20")

    def test_all_unknown_is_not_confirmed_zero_and_latest_version_is_not_release(self):
        _, b = self.pair()
        self.finish(b)
        r = self.result(b)
        self.assertIsNone(r["counts"]["confirmedNew"])
        self.assertIsNone(r["counts"]["freeNew"])
        self.assertEqual(r["counts"]["pendingRelease"], 3)
        self.assertEqual(r["entries"], [])

    def test_first_baseline_and_unchanged_run_need_no_detail_visits(self):
        a = self.start("2026-10-20T01:00:00Z")
        self.ingest(a, self.snapshot(range(1,81)))
        self.assertEqual(self.prepare(a)["pending"], [])
        self.finish(a)
        self.assertIsNone(self.result(a)["counts"]["confirmedNew"])
        b = self.start("2026-10-20T04:00:00Z")
        self.ingest(b, self.snapshot(range(1,81), at="2026-10-20T04:00:00Z"))
        self.assertEqual(self.prepare(b)["pending"], [])
        self.finish(b)
        self.assertEqual(self.result(b)["counts"]["confirmedNew"], 0)

    def test_dual_chart_and_cross_category_share_one_release_lookup(self):
        _, b = self.pair(ids=(81,), dual=True, second_category=True)
        self.assertEqual(len(self.prepare(b)["pending"]), 1)
        self.release(b, 81)
        self.finish(b)
        r = self.result(b)
        self.assertEqual(len(r["entries"]), 2)
        self.assertTrue(all(e["triggers"] == ["free", "grossing"] for e in r["entries"]))
        self.assertEqual(r["counts"]["confirmedNew"], 2)

    def test_invalid_evidence_cannot_enter_or_contaminate_cache(self):
        _, b = self.pair(ids=(81,))
        for changes in [{"pageAppId":"99"}, {"pageCountry":"德国"},
                        {"sourceUrl":"https://www.qimai.cn/app/rank/appid/81/country/de"},
                        {"sourceLabel":"最近更新"}, {"rawText":"最早发布 2025-01-01"},
                        {"releaseDate":"2026-02-30"}, {"observedAt":"2026-10-20T06:00:00Z"}]:
            data = self.evidence(81);data.update(changes)
            self.cli("ingest-release",b["runId"],"--token",b["token"],payload=data,ok=False)
        self.assertEqual(len(self.prepare(b)["pending"]), 1)
        self.assertFalse((self.root/'data/test/release-cache.json').exists())

    def test_evidence_is_idempotent_immutable_and_resumable(self):
        _, b = self.pair(ids=(81,))
        self.release(b,81)
        self.release(b,81)
        self.release(b,81,"2026-09-01",ok=False)
        self.cli("resume",b["runId"],"--token",b["token"])
        self.assertEqual(self.prepare(b)["pending"], [])
        self.finish(b)
        self.release(b,81,ok=False)

    def test_verified_cache_is_country_scoped_and_history_is_frozen(self):
        _, b = self.pair(ids=(81,))
        self.release(b,81)
        self.finish(b)
        before=self.result(b)
        cache_path=self.root/'data/test/release-cache.json'
        cache=json.loads(cache_path.read_text())
        self.assertIn('us:81',cache['records'])
        self.assertNotIn('de:81',cache['records'])
        cache['records']['us:81']['releaseDate']='2018-01-01'
        cache_path.write_text(json.dumps(cache))
        self.cli('render','--mode','test')
        self.assertEqual(self.result(b),before)
        self.assertFalse((self.root/'data/release-cache.json').exists())

    def test_cache_reuse_preserves_original_observation_time(self):
        _, b = self.pair(ids=(81,));self.release(b,81);self.finish(b)
        c=self.start('2026-10-20T10:00:00Z')
        self.ingest(c,self.snapshot(range(1,81),at='2026-10-20T10:00:00Z'));self.finish(c)
        d=self.start('2026-10-21T01:00:00Z')
        self.ingest(d,self.snapshot([81,*range(2,81)],at='2026-10-21T01:00:00Z',sourceDate='2026-10-21'))
        self.assertEqual(self.prepare(d)['pending'],[])
        self.finish(d)
        e=self.result(d)['entries'][0]
        self.assertEqual(e['releaseObservedAt'],'2026-10-20T04:01:00Z')
        self.assertEqual(e['releaseOrigin'],'cache')

    def test_new_policy_can_compare_to_legacy_run_without_rewriting_it(self):
        cfg=self.root/'config/monitor.json';config=json.loads(cfg.read_text());config.pop('releasePolicy')
        cfg.write_text(json.dumps(config))
        a=self.start('2026-10-20T01:00:00Z');self.ingest(a,self.snapshot(range(1,81)));self.finish(a)
        old=self.result(a)
        config['releasePolicy']=POLICY;cfg.write_text(json.dumps(config))
        b=self.start('2026-10-20T04:00:00Z');self.ingest(b,self.snapshot([81,*range(2,81)],at='2026-10-20T04:00:00Z'))
        self.assertEqual([e['appId'] for e in self.prepare(b)['pending']],['81'])
        self.release(b,81);self.finish(b)
        self.assertEqual(self.result(b)['counts']['confirmedNew'],1)
        self.assertEqual(self.result(a),old)


    def test_reports_explain_release_admission_pending_and_month_filter(self):
        _, b = self.pair()
        self.release(b,81)
        self.release(b,82,'2018-01-01')
        self.release(b,83,None,status='failed',rawText='',reason='需要人工登录')
        self.finish(b)
        run=self.root/b['relativePath']
        markdown=(run/'report.md').read_text()
        self.assertIn('最早发布',markdown)
        self.assertIn('排名候选',markdown)
        self.assertIn('超过六个月',markdown)
        self.assertIn('首次发布日期待核实',markdown)
        self.assertIn('需要人工登录',markdown)
        html=(run.parents[1]/'index.html').read_text()
        self.assertIn('id="release-months"',html)
        for months in range(1,7):
            self.assertIn(f'value="{months}">最近{months}个月',html)
        self.assertIn('历史运行未采集最早发布日期',html)
        self.assertIn('position:sticky',html)

    def test_same_app_in_two_countries_requires_two_separate_evidences(self):
        a=self.start('2026-10-20T01:00:00Z')
        def country_snapshot(ids,at,country):
            x=self.snapshot(ids,at=at,country=country)
            x['sourceUrl']=x['sourceUrl'].replace('/country/us/','/country/'+country+'/')
            for row in x['rows']:row['qimaiUrl']=row['qimaiUrl'].replace('/country/us','/country/'+country)
            return x
        for country in ['us','de']:self.ingest(a,country_snapshot(range(1,81),'2026-10-20T01:00:00Z',country))
        self.finish(a)
        b=self.start('2026-10-20T04:00:00Z')
        for country in ['us','de']:self.ingest(b,country_snapshot([81,*range(2,81)],'2026-10-20T04:00:00Z',country))
        self.assertEqual({e['country'] for e in self.prepare(b)['pending']},{'us','de'})
        self.release(b,81)
        self.assertEqual([e['country'] for e in self.prepare(b)['pending']],['de'])
        self.release(b,81,'2018-01-01',country='de',pageCountry='德国',sourceUrl='https://www.qimai.cn/app/rank/appid/81/country/de')
        self.finish(b)
        self.assertEqual([e['country'] for e in self.result(b)['entries']],['us'])



    def test_unknown_evidence_is_retried_on_later_run(self):
        _, b = self.pair(ids=(81,))
        self.release(b,81,None,status='unavailable',rawText='',reason='暂未显示')
        self.finish(b)
        self.assertFalse((self.root/'data/test/release-cache.json').exists())
        c=self.start('2026-10-20T10:00:00Z');self.ingest(c,self.snapshot(range(1,81),at='2026-10-20T10:00:00Z'));self.finish(c)
        d=self.start('2026-10-21T01:00:00Z')
        self.ingest(d,self.snapshot([81,*range(2,81)],at='2026-10-21T01:00:00Z',sourceDate='2026-10-21'))
        self.assertEqual([e['appId'] for e in self.prepare(d)['pending']],['81'])

    def test_complete_ranks_with_unverified_release_are_partial(self):
        import gzip
        from rank_monitor.core import Store,normalize_snapshot,scopes
        store=Store(self.root,'test')
        def archive(run,at,new):
            with gzip.open(store.locate(run['runId'])/'snapshots.jsonl.gz','wt') as f:
                for i,scope in enumerate(scopes(store.config)):
                    ids=[81,*range(2,81)] if i==0 and new else range(1,81)
                    raw=self.snapshot(ids,scope['chart'],at,**{k:scope[k] for k in ['country','categoryFamily','category','genreId','sourceUrl']})
                    for row in raw['rows']:row['qimaiUrl']=row['qimaiUrl'].replace('/country/us','/country/'+scope['country'])
                    normalized=normalize_snapshot(raw,scope,store.config,at,test=True)
                    f.write(json.dumps(normalized)+'\n')
        a=store.start('2026-10-20T01:00:00Z');archive(a,'2026-10-20T01:00:00Z',False);store.finalize(a['runId'],a['token'])
        b=store.start('2026-10-20T04:00:00Z');archive(b,'2026-10-20T04:00:00Z',True);store.finalize(b['runId'],b['token'])
        r=store.compare(b['runId'])
        self.assertEqual(r['coverage']['completeScopes'],240)
        self.assertEqual(r['counts']['pendingRelease'],1)
        self.assertEqual(r['run']['status'],'partial')



    def test_release_retries_are_bounded_and_resume_does_not_reset_attempts(self):
        _, b=self.pair(ids=(81,))
        for minute in [1,2,3]:
            self.release(b,81,None,status='failed',rawText='',reason='加载失败',observedAt=f'2026-10-20T04:0{minute}:00Z')
        self.cli('resume',b['runId'],'--token',b['token'])
        plan=self.prepare(b)
        self.assertEqual(plan['pending'],[])
        self.assertEqual(plan['exhaustedApps'],1)
        self.assertEqual(plan['verifiedApps'],0)
        self.release(b,81,None,status='failed',rawText='',reason='第四次',observedAt='2026-10-20T04:04:00Z',ok=False)
        self.finish(b)
        self.assertEqual(self.result(b)['counts']['pendingRelease'],1)

    def test_formal_release_work_rejects_expired_window_but_finalize_still_works(self):
        from rank_monitor.core import Store
        from datetime import datetime, timedelta, timezone
        from unittest.mock import patch
        store=Store(self.root,'formal');now=datetime(2026,10,20,1,tzinfo=timezone.utc)
        with patch('rank_monitor.core.utcnow',return_value=now):a=store.start()
        with patch('rank_monitor.core.utcnow',return_value=now+timedelta(minutes=91)):
            with self.assertRaises(ValueError):store.prepare_release(a['runId'],a['token'])
            store.finalize(a['runId'],a['token'])
        self.assertEqual(store.manifest(a['runId'])['status'],'failed')

    def test_pending_reason_is_escaped_and_metadata_finish_time_uses_instants(self):
        _, b=self.pair(ids=(81,82))
        self.release(b,81,observedAt='2026-10-20T04:00:00.500Z')
        self.release(b,82,None,status='unavailable',rawText='',reason='</script><img src=x onerror=alert(1)>',observedAt='2026-10-20T04:00:00.750Z')
        self.finish(b)
        self.assertEqual(self.result(b)['run']['finishedAt'],'2026-10-20T04:00:00.750000Z')
        html=((self.root/b['relativePath']).parents[1]/'index.html').read_text()
        self.assertNotIn('<img src=x',html)


    def test_release_unknown_labels_do_not_hide_missing_rank_comparison(self):
        _, b=self.pair(ids=(81,))
        self.finish(b)
        run=self.root/b['relativePath']
        markdown=(run/'report.md').read_text()
        self.assertIn('已确认新进：待核实；免费榜：待核实；畅销榜：待比较',markdown)
        for path in [run.parents[1]/'index.md',self.root/'data/test/index.md',self.root/'data/test/index.html']:
            self.assertIn('待核实',path.read_text())

    def test_excluded_release_candidates_keep_filter_dimensions_for_known_counts(self):
        _, b=self.pair(ids=(81,82))
        self.release(b,81,'2018-01-01')
        self.finish(b)
        r=self.result(b)
        self.assertEqual([e['appId'] for e in r['releaseExcluded']],['81'])
        self.assertEqual(r['releaseExcluded'][0]['triggers'],['free'])
        self.assertEqual(r['releaseExcluded'][0]['country'],'us')


if __name__ == '__main__':
    unittest.main()
