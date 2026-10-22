"""Mail tests never connect to a real SMTP server."""
from email import policy
from email.parser import BytesParser
import importlib
import importlib.util
import json
from pathlib import Path
import smtplib
import ssl
import sys
import threading
import unittest
from unittest.mock import patch

import test_monitor as helpers

sys.path.insert(0, str(helpers.SOURCE / 'scripts'))


class SMTPDouble:
    delivered = []
    failure = None
    entered = None
    release = None

    def __init__(self, host, port, *, timeout, context):
        assert host == 'smtp.qq.com' and port == 465 and timeout == 30
        assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED

    def login(self, username, password):
        assert username == 'sender@qq.com' and password == 'private-authorization-code'
        if self.failure == 'auth':
            raise smtplib.SMTPAuthenticationError(535, b'private-authorization-code')

    def mail(self, sender):
        return 250, b'ok'

    def rcpt(self, recipient):
        self.recipient = recipient
        return (550, b'rejected') if recipient.startswith('refused') else (250, b'ok')

    def data(self, content):
        if self.entered:
            self.entered.set()
            self.release.wait(5)
        if self.failure == 'disconnect':
            raise smtplib.SMTPServerDisconnected('private-authorization-code')
        if self.failure == 'interrupt':
            raise KeyboardInterrupt()
        if self.failure == 'malformed':
            return -1, b'unparseable reply after submission'
        if self.failure == 'long_reply':
            raise smtplib.SMTPResponseException(500, b'Line too long.')
        if self.failure == 'data_refused':
            return 550, b'explicitly refused'
        self.delivered.append((self.recipient, BytesParser(policy=policy.default).parsebytes(content)))
        return 250, b'ok'

    def quit(self):
        if self.failure == 'quit':
            raise smtplib.SMTPServerDisconnected('closed after success')

    def close(self):
        pass


class MailTests(helpers.Harness):
    def mailer(self):
        self.assertIsNotNone(importlib.util.find_spec('rank_monitor.mailer'), 'mail module must exist')
        return importlib.import_module('rank_monitor.mailer')

    def configure(self, **changes):
        local = self.root / '.local'
        local.mkdir(exist_ok=True)
        config = {'enabled': True, 'fromAddress': 'sender@qq.com',
                  'recipients': ['receiver@example.com']}
        config.update(changes)
        (local/'mail.json').write_text(json.dumps(config))
        secret = local/'mail-secrets.json'
        secret.write_text(json.dumps({'authCode': 'private-authorization-code'}))
        secret.chmod(0o600)
        return local/'mail.json'

    def closed(self, ids=()):
        first = self.start('2026-10-20T01:00:00Z')
        self.ingest(first, self.snapshot(range(1, 81)))
        self.finish(first)
        if not ids:
            return first
        second = self.start('2026-10-20T04:00:00Z')
        self.ingest(second, self.snapshot([*ids, *range(len(ids)+1, 81)], at='2026-10-20T04:00:00Z'))
        self.finish(second)
        return second

    def store(self, formal=False):
        from rank_monitor.core import Store
        if formal and (self.root/'data/test').exists():
            state = json.loads((self.root/'data/test/state.json').read_text())
            # Move synthetic archived fixtures into a synthetic formal history; never real data.
            import shutil
            for item in state['runs']:
                source = self.root/item['relativePath']
                target = self.root/'data'/source.relative_to(self.root/'data/test')
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(source, target, dirs_exist_ok=True)
                item['relativePath'] = target.relative_to(self.root).as_posix()
                path = target/'manifest.json'
                manifest = json.loads(path.read_text())
                manifest['mode'] = 'formal'
                path.write_text(json.dumps(manifest))
            (self.root/'data/state.json').write_text(json.dumps(state))
        return Store(self.root, 'formal' if formal else 'test')

    def delivery(self, store):
        return self.mailer().MailDelivery(store)

    def test_cli_check_missing_and_disabled_are_offline(self):
        self.assertEqual(self.cli('mail-check')['status'], 'not_configured')
        self.configure(enabled=False)
        self.assertEqual(self.cli('mail-check')['status'], 'disabled')

    def test_configuration_is_masked_and_does_not_change_rank_fingerprint(self):
        store = self.store()
        from rank_monitor.core import config_id
        before = config_id(store.config)
        self.configure()
        with patch('smtplib.SMTP_SSL', side_effect=AssertionError('must remain offline')):
            result = self.mailer().check_mail(self.root)
        self.assertEqual(result['status'], 'ready')
        self.assertNotIn('sender@qq.com', json.dumps(result))
        self.assertNotIn('receiver@example.com', json.dumps(result))
        self.assertNotIn('private-authorization-code', json.dumps(result))
        self.assertEqual(before, config_id(self.store().config))

    def test_bad_addresses_headers_and_transport_are_rejected(self):
        for changes in [{'recipients': []}, {'recipients': ['bad']},
                        {'recipients': ['ok@example.com\r\nBcc: evil@example.com']},
                        {'fromName': 'safe\nBcc: evil@example.com'}, {'enabled': 'true'},
                        {'smtp': {'security': 'none'}}, {'smtp': {'port': True}},
                        {'smtp': {'username': 'other@qq.com'}}, {'smtp': {'host': 'bad/host'}}]:
            with self.subTest(changes=changes):
                self.configure(**changes)
                with self.assertRaises(ValueError):
                    self.mailer().check_mail(self.root)

    def test_secret_permissions_missing_and_invalid_json_are_safe(self):
        self.configure()
        path = self.root/'.local/mail-secrets.json'
        for content, permission in [('{}', 0o600), ('{"authCode":"secret"}', 0o644), ('private-authorization-code', 0o600)]:
            path.write_text(content)
            path.chmod(permission)
            with self.assertRaises(ValueError) as error:
                self.mailer().check_mail(self.root)
            self.assertNotIn('private-authorization-code', str(error.exception))
        path.unlink()
        with self.assertRaises(ValueError):
            self.mailer().check_mail(self.root)

    def test_preview_has_every_entry_links_and_no_active_content(self):
        run = self.closed([81, 82])
        store = self.store()
        result = self.delivery(store).preview(run['runId'])
        text = (self.root/result['html']).read_text()
        plain = (self.root/result['text']).read_text()
        for app in ['81', '82']:
            self.assertIn(f'https://www.qimai.cn/app/rank/appid/{app}/country/us', text)
            self.assertIn(f'Synthetic {app}', plain)
        self.assertIn('完整榜单', text)
        self.assertIn('2026-10-20 12:00:00', text)
        for forbidden in ['<script', '<link', '<img', 'file:', '../../', '<style']:
            self.assertNotIn(forbidden, text)

    def test_template_escapes_and_never_truncates(self):
        run = self.closed([81])
        store = self.store()
        report = store.compare(run['runId'])
        report['entries'] = [{**report['entries'][0], 'name': '<b>Injected & name</b>', 'appId': str(1000+i),
                              'qimaiUrl': f'https://www.qimai.cn/app/rank/appid/{1000+i}/country/us'} for i in range(1200)]
        result = self.mailer().render_mail(report, store.manifest(run['runId'])['config'])
        self.assertEqual(result['html'].count('&lt;b&gt;Injected &amp; name&lt;/b&gt;'), 1200)
        self.assertIn('/appid/2199/country/us', result['html'])
        self.assertNotIn('<b>Injected', result['html'])
        report['entries'][0]['qimaiUrl'] = 'javascript:alert(1)'
        with self.assertRaises(ValueError):
            self.mailer().render_mail(report, store.config)

    def test_baseline_failed_and_unknown_counts_remain_distinct(self):
        run = self.closed()
        store = self.store()
        report = store.compare(run['runId'])
        rendered = self.mailer().render_mail(report, store.config)
        self.assertIn('建立基线', rendered['text'])
        self.assertIn('已确认入选：0', rendered['text'])
        report['run']['releasePolicy'] = {'windowStarts': {'6': '2026-04-20'}, 'referenceDate': '2026-10-20'}
        report['coverage']['scopes'][0]['comparison'] = 'comparable'
        report['counts'].update(rankCandidates=2, pendingRelease=2, excludedOlder=0)
        rendered = self.mailer().render_mail(report, store.config)
        self.assertIn('已确认入选：待核实', rendered['text'])
        self.assertIn('首次发布日期待核实：2', rendered['text'])
        report['run']['status'] = 'failed'
        self.assertIn('失败', self.mailer().render_mail(report, store.config)['subject'])

    def test_send_rejects_test_running_and_imported_runs(self):
        self.configure()
        run = self.closed()
        with patch('smtplib.SMTP_SSL', side_effect=AssertionError('no network')):
            with self.assertRaises(ValueError):
                self.delivery(self.store()).send(run['runId'])
            store = self.store(formal=True)
            p = store.locate(run['runId'])/'manifest.json'
            original = json.loads(p.read_text())
            for key, value in [('origin', 'legacy_import'), ('status', 'running')]:
                p.write_text(json.dumps({**original, key: value}))
                with self.assertRaises(ValueError):
                    self.delivery(store).send(run['runId'])

    def test_missing_disabled_skip_without_touching_collection(self):
        run = self.closed()
        store = self.store(formal=True)
        before = (store.locate(run['runId'])/'manifest.json').read_bytes()
        with patch('smtplib.SMTP_SSL', side_effect=AssertionError('no network')):
            self.assertEqual(self.delivery(store).send(run['runId'])['status'], 'not_configured')
            self.configure(enabled=False)
            self.assertEqual(self.delivery(store).send(run['runId'])['status'], 'disabled')
        self.assertEqual(before, (store.locate(run['runId'])/'manifest.json').read_bytes())

    def test_delivery_is_per_recipient_idempotent_and_plain_plus_html(self):
        self.configure(recipients=['receiver@example.com', 'refused@example.com', 'receiver@example.com'])
        run = self.closed([81])
        store = self.store(formal=True)
        SMTPDouble.delivered = []
        with patch('smtplib.SMTP_SSL', SMTPDouble):
            result = self.delivery(store).send(run['runId'])
            repeated = self.delivery(store).send(run['runId'])
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(len(SMTPDouble.delivered), 1)
        message = SMTPDouble.delivered[0][1]
        self.assertEqual(message['To'], 'receiver@example.com')
        self.assertNotIn('refused@example.com', message.as_string())
        self.assertEqual([p.get_content_type() for p in message.iter_parts()], ['text/plain', 'text/html'])
        self.assertIn('Synthetic 81', message.get_body(preferencelist=('html',)).get_content())
        self.assertTrue(repeated['recipients'][0]['skipped'])
        self.assertNotIn('private-authorization-code', json.dumps(result))

    def test_auth_and_data_disconnect_are_sanitized_and_not_retried(self):
        run = self.closed()
        store = self.store(formal=True)
        for failure, status in [('auth', 'failed'), ('disconnect', 'unknown')]:
            with self.subTest(failure=failure):
                self.configure(recipients=[f'{failure}@example.com'])
                with patch.object(SMTPDouble, 'failure', failure), patch('smtplib.SMTP_SSL', SMTPDouble) as smtp:
                    result = self.delivery(store).send(run['runId'])
                    self.assertEqual(result['status'], status)
                    self.assertNotIn('private-authorization-code', json.dumps(result))
                    if status == 'unknown':
                        with patch('smtplib.SMTP_SSL', side_effect=AssertionError('must not resend')):
                            self.assertEqual(self.delivery(store).send(run['runId'])['status'], 'unknown')

    def test_only_transient_connect_failures_retry_once(self):
        self.configure()
        run = self.closed()
        store = self.store(formal=True)
        with patch('smtplib.SMTP_SSL', side_effect=[TimeoutError(), SMTPDouble('smtp.qq.com',465,timeout=30,context=ssl.create_default_context())]) as smtp:
            self.assertEqual(self.delivery(store).send(run['runId'])['status'], 'submitted')
            self.assertEqual(smtp.call_count, 2)
        self.configure(recipients=['cert@example.com'])
        with patch('smtplib.SMTP_SSL', side_effect=ssl.SSLCertVerificationError('bad cert')) as smtp:
            self.assertEqual(self.delivery(store).send(run['runId'])['status'], 'failed')
            self.assertEqual(smtp.call_count, 1)

    def test_interrupted_send_becomes_unknown_and_does_not_resend(self):
        self.configure()
        run = self.closed()
        store = self.store(formal=True)
        with patch.object(SMTPDouble, 'failure', 'interrupt'), patch('smtplib.SMTP_SSL', SMTPDouble):
            with self.assertRaises(KeyboardInterrupt):
                self.delivery(store).send(run['runId'])
        with patch('smtplib.SMTP_SSL', side_effect=AssertionError('must not resend')):
            self.assertEqual(self.delivery(store).send(run['runId'])['status'], 'unknown')

    def test_quit_disconnect_does_not_undo_acceptance(self):
        self.configure()
        run = self.closed()
        with patch.object(SMTPDouble, 'failure', 'quit'), patch('smtplib.SMTP_SSL', SMTPDouble):
            self.assertEqual(self.delivery(self.store(formal=True)).send(run['runId'])['status'], 'submitted')

    def test_concurrent_send_uses_mail_lock_without_collection_lock(self):
        self.configure()
        run = self.closed()
        store = self.store(formal=True)
        entered, release = threading.Event(), threading.Event()
        results = []
        with patch.object(SMTPDouble, 'entered', entered), patch.object(SMTPDouble, 'release', release), patch('smtplib.SMTP_SSL', SMTPDouble):
            thread = threading.Thread(target=lambda: results.append(self.delivery(store).send(run['runId'])))
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(self.delivery(store).send(run['runId'])['status'], 'busy')
                with store.guard():
                    self.assertEqual(store.manifest(run['runId'])['status'], 'partial')
            finally:
                release.set()
                thread.join(5)
        self.assertEqual(results[0]['status'], 'submitted')

    def test_preview_does_not_overwrite_frozen_sent_content(self):
        self.configure()
        run = self.closed([81])
        store = self.store(formal=True)
        with patch('smtplib.SMTP_SSL', SMTPDouble):
            self.delivery(store).send(run['runId'])
        with patch.object(self.mailer(), 'render_mail', side_effect=AssertionError('reuse frozen body')):
            self.delivery(store).preview(run['runId'])
            self.assertEqual(self.delivery(store).send(run['runId'])['status'], 'submitted')

    def test_unrecognized_delivery_state_stops_instead_of_resending(self):
        self.configure()
        run = self.closed()
        store = self.store(formal=True)
        with patch('smtplib.SMTP_SSL', SMTPDouble):
            result = self.delivery(store).send(run['runId'])
        path = self.root/result['record']
        state = json.loads(path.read_text())
        state['recipients']['receiver@example.com']['status'] = 'unrecognized'
        path.write_text(json.dumps(state))
        with patch('smtplib.SMTP_SSL', side_effect=AssertionError('corrupt state must not send')):
            with self.assertRaises(ValueError):
                self.delivery(store).send(run['runId'])

    def test_acceptance_followed_by_disk_failure_becomes_unknown(self):
        self.configure()
        run = self.closed()
        store = self.store(formal=True)
        from rank_monitor.core import write_json
        def fail_after_acceptance(path, value):
            if str(path).endswith('delivery.json') and any(r['status']=='submitted' for r in value['recipients'].values()):
                raise OSError('simulated disk failure')
            return write_json(path, value)
        SMTPDouble.delivered = []
        with patch('smtplib.SMTP_SSL', SMTPDouble), patch.object(self.mailer(), 'write_json', side_effect=fail_after_acceptance):
            with self.assertRaises(OSError):
                self.delivery(store).send(run['runId'])
        self.assertEqual(len(SMTPDouble.delivered), 1)
        with patch('smtplib.SMTP_SSL', side_effect=AssertionError('must not resend')):
            self.assertEqual(self.delivery(store).send(run['runId'])['status'], 'unknown')

    def test_malformed_data_replies_are_unknown_but_explicit_refusal_is_failed(self):
        run=self.closed()
        store=self.store(formal=True)
        for failure, expected in [('malformed','unknown'),('long_reply','unknown'),('data_refused','failed')]:
            with self.subTest(failure=failure):
                self.configure(recipients=[f'{failure}@example.com'])
                with patch.object(SMTPDouble,'failure',failure),patch('smtplib.SMTP_SSL',SMTPDouble):
                    result=self.delivery(store).send(run['runId'])
                self.assertEqual(result['status'],expected)
                if expected=='unknown':
                    with patch('smtplib.SMTP_SSL',side_effect=AssertionError('ambiguous DATA must not resend')):
                        self.assertEqual(self.delivery(store).send(run['runId'])['status'],'unknown')

    def test_recipient_domain_case_change_cannot_resend(self):
        self.configure()
        run=self.closed()
        store=self.store(formal=True)
        with patch('smtplib.SMTP_SSL',SMTPDouble):
            self.delivery(store).send(run['runId'])
        self.configure(recipients=['receiver@EXAMPLE.COM','receiver@example.com'])
        with patch('smtplib.SMTP_SSL',side_effect=AssertionError('same recipient domain')):
            result=self.delivery(store).send(run['runId'])
        self.assertEqual(result['status'],'submitted')
        self.assertEqual(len(result['recipients']),1)
        self.assertTrue(result['recipients'][0]['skipped'])

    def test_existing_uppercase_domain_ledger_is_reused(self):
        self.configure()
        run=self.closed()
        store=self.store(formal=True)
        with patch('smtplib.SMTP_SSL',SMTPDouble):
            result=self.delivery(store).send(run['runId'])
        path=self.root/result['record']
        value=json.loads(path.read_text())
        value['recipients']['receiver@EXAMPLE.COM']=value['recipients'].pop('receiver@example.com')
        path.write_text(json.dumps(value))
        with patch('smtplib.SMTP_SSL',side_effect=AssertionError('reuse existing domain alias')):
            result=self.delivery(store).send(run['runId'])
        self.assertTrue(result['recipients'][0]['skipped'])

    def test_address_local_part_case_is_preserved(self):
        self.configure(recipients=['Receiver@example.com','receiver@EXAMPLE.COM'])
        result=self.mailer().load_mail(self.root)
        self.assertEqual(result['recipients'],['Receiver@example.com','receiver@example.com'])

    def test_sealed_release_results_deduplicate_dual_charts_and_ignore_later_cache(self):
        from test_releases import POLICY, ReleaseTests
        cfg=self.root/'config/monitor.json'
        value=json.loads(cfg.read_text())
        value['releasePolicy']=POLICY
        cfg.write_text(json.dumps(value))
        first=self.start('2026-10-20T01:00:00Z')
        for chart in ['free','grossing']:
            for country in ['us','de']:
                self.ingest(first,self.snapshot(range(1,81),chart,country=country,
                    sourceUrl=f'https://www.qimai.cn/rank/index/brand/{chart}/device/iphone/country/{country}/genre/6007',
                    rows=[{**r,'qimaiUrl':r['qimaiUrl'].replace('/country/us',f'/country/{country}')} for r in self.snapshot(range(1,81))['rows']]))
        self.finish(first)
        second=self.start('2026-10-20T04:00:00Z')
        for chart in ['free','grossing']:
            for country in ['us','de']:
                rows=self.snapshot([81,82,83,*range(4,81)])['rows']
                self.ingest(second,self.snapshot([],chart,at='2026-10-20T04:00:00Z',country=country,
                    sourceUrl=f'https://www.qimai.cn/rank/index/brand/{chart}/device/iphone/country/{country}/genre/6007',
                    rows=[{**r,'qimaiUrl':r['qimaiUrl'].replace('/country/us',f'/country/{country}')} for r in rows]))
        for country in ['us','de']:
            for app,date in [(81,'2026-10-01'),(82,'2018-01-01')]:
                evidence=ReleaseTests.evidence(self,app,date,country=country,pageCountry={'us':'美国','de':'德国'}[country],
                    sourceUrl=f'https://www.qimai.cn/app/rank/appid/{app}/country/{country}')
                self.cli('ingest-release',second['runId'],'--token',second['token'],payload=evidence)
        self.finish(second)
        store=self.store()
        report=store.compare(second['runId'])
        rendered=self.mailer().render_mail(report,store.config)
        self.assertEqual(len(report['entries']),2)
        self.assertEqual(rendered['html'].count('>Synthetic 81</a>'),2)
        self.assertNotIn('>Synthetic 82</a>',rendered['html'])
        self.assertNotIn('>Synthetic 83</a>',rendered['html'])
        self.assertIn('免费榜、畅销榜',rendered['text'])
        self.assertIn('首次发布日期待核实：2',rendered['text'])
        (self.root/'data/test/release-cache.json').write_text('{}')
        self.assertEqual(rendered,self.mailer().render_mail(store.compare(second['runId']),store.config))
