"""Optional local SMTP delivery, separate from collection and immutable run evidence."""
from collections import Counter, defaultdict
from email import policy
from email.message import EmailMessage
from email.utils import format_datetime, formataddr
import errno
import fcntl
import html
import json
from pathlib import Path
import re
import smtplib
import socket
import ssl
import stat

from .core import digest, read_json, stamp, utcnow, write_json
from .reports import clock, count, report_count, write_text


class MailError(ValueError):
    """Only constant, credential-free messages may cross the command boundary."""


def _json(path, label):
    try:
        value = read_json(path)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (OSError, ValueError):
        raise MailError(f"{label}缺失、无法读取或不是有效JSON对象") from None


def _address(value):
    if not isinstance(value, str) or not re.fullmatch(
            r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+", value):
        raise MailError("邮件地址必须是完整地址，不能包含显示名称、空白或换行")
    user, domain = value.rsplit('@', 1)
    return user + '@' + domain.lower()


def masked(address):
    user, domain = address.split('@', 1)
    return user[:1] + '***@' + domain


def load_mail(root):
    local = Path(root)/'.local'
    path = local/'mail.json'
    if not path.exists():
        return {'status': 'not_configured', 'reason': '尚未配置本地邮件发送'}
    raw = _json(path, "邮件配置")
    enabled = raw.get('enabled', False)
    if type(enabled) is not bool:
        raise MailError("邮件enabled必须是布尔值")
    if not enabled:
        return {'status': 'disabled', 'reason': '本地邮件发送开关已关闭'}
    sender = _address(raw.get('fromAddress'))
    recipients = raw.get('recipients')
    if not isinstance(recipients, list) or not recipients:
        raise MailError("邮件recipients必须是非空地址列表")
    recipients = list(dict.fromkeys(_address(r) for r in recipients))
    name = raw.get('fromName', 'iOS 榜单监测')
    if not isinstance(name, str) or not name.strip() or any(ord(c) < 32 for c in name):
        raise MailError("邮件显示名称不能为空或包含控制字符")
    smtp = raw.get('smtp', {})
    if not isinstance(smtp, dict):
        raise MailError("smtp必须是配置对象")
    host, port = smtp.get('host', 'smtp.qq.com'), smtp.get('port', 465)
    if not isinstance(host, str) or not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?', host):
        raise MailError("SMTP服务器地址不合法")
    if type(port) is not int or not 1 <= port <= 65535 or smtp.get('security', 'ssl') != 'ssl':
        raise MailError("SMTP端口必须有效，首版仅支持SSL/TLS加密")
    if _address(smtp.get('username', sender)) != sender:
        raise MailError("SMTP登录账号必须与发件地址一致")
    secret = local/'mail-secrets.json'
    try:
        if secret.is_symlink() or not secret.is_file() or stat.S_IMODE(secret.stat().st_mode) != 0o600:
            raise MailError("邮件凭据文件必须是权限600的普通文件")
    except OSError:
        raise MailError("邮件凭据文件无法检查") from None
    auth = _json(secret, "邮件凭据").get('authCode')
    if not isinstance(auth, str) or not auth or any(c.isspace() for c in auth):
        raise MailError("邮件凭据缺少有效authCode授权码")
    return {'status': 'ready', 'fromAddress': sender, 'fromName': name, 'recipients': recipients,
            'host': host, 'port': port, 'authCode': auth}


def check_mail(root):
    config = load_mail(root)
    if config['status'] != 'ready':
        return config
    return {'status': 'ready', 'fromAddress': masked(config['fromAddress']),
            'recipients': [masked(r) for r in config['recipients']], 'security': 'ssl',
            'note': '仅完成离线检查，尚未验证SMTP账号或实际投递'}


def _link(entry):
    expected = f"https://www.qimai.cn/app/rank/appid/{entry['appId']}/country/{entry['country']}"
    if not re.fullmatch(r'\d+', str(entry['appId'])) or entry['country'] not in {'us', 'de', 'jp'} or entry['qimaiUrl'].rstrip('/') != expected:
        raise MailError("报告含不符合App ID或国家的七麦HTTPS链接")
    return expected


def render_mail(report, config):
    esc = lambda v: html.escape(str(v if v not in [None, ''] else '—'), quote=True)
    run, coverage, counts = report['run'], report['coverage'], report['counts']
    status = {'complete': '完整完成', 'partial': '部分完成', 'failed': '失败'}[run['status']]
    started = clock(run['startedAt'])
    confirmed = report_count(report)
    subject = f"iOS 榜单监测｜{started}｜{status}｜入选{confirmed}"
    release = run.get('releasePolicy')
    policy_text = (f"上线准入：七麦最早发布 {release['windowStarts']['6']} 至 {release['referenceDate']}（包含边界日）。"
                   if release else '本轮为历史规则，未采集首次发布日期。')
    summary = [('已确认入选', confirmed), ('免费榜入选', report_count(report, 'freeNew')),
               ('畅销榜入选', report_count(report, 'grossingNew')),
               ('完整榜单', f"{coverage['completeScopes']}/{coverage['plannedScopes']}"),
               ('可比较范围', f"{coverage['comparableScopes']}/{coverage['plannedScopes']}"),
               ('待比较范围', str(coverage['unknownScopes']))]
    if release:
        summary += [('排名候选', count(counts.get('rankCandidates'))),
                    ('超过六个月排除', count(counts.get('excludedOlder'))),
                    ('首次发布日期待核实', count(counts.get('pendingRelease')))]
    notice = ('首次轮次建立基线；无排名比较依据时数量显示0，不表示已确认没有新进。'
              if not run.get('previousRunId') else '已确认数量仅覆盖可比较范围，缺数及待比较不等于确认没有新进。')
    if release and counts.get('pendingRelease'):
        notice += ' 首次发布日期待核实的候选未进入入选列表。'
    metrics = []
    for i in range(0, len(summary), 3):
        cells = ''.join(f'<td width="33%" style="padding:12px 8px;border:1px solid #ccd5d2;vertical-align:top;">'
                        f'<b style="font-size:22px;color:#147482;">{esc(v)}</b><br>'
                        f'<span style="font-size:12px;color:#5d7075;">{esc(k)}</span></td>' for k, v in summary[i:i+3])
        metrics.append('<tr>'+cells+'</tr>')
    summary_html = '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">'+''.join(metrics)+'</table>'
    grouped = defaultdict(list)
    for entry in report['entries']:
        grouped[(entry['country'], entry['categoryFamily'], entry['category'])].append(entry)
    groups, plain_groups = [], []
    for (country, family, category), entries in sorted(grouped.items()):
        title = f"{config['countries'][country]} / {family} / {category} · {len(entries)} 条"
        groups.append(f'<h2 style="font-size:17px;margin:26px 0 12px;">{esc(title)}</h2><table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;table-layout:fixed;">'
                      '<tr style="background:#eaf0eb;font-size:12px;"><th align="left" width="40%" style="padding:10px;">APP / 开发者</th><th align="left" width="27%" style="padding:10px;">排名 / 触发</th><th align="left" style="padding:10px;">发布 / 评价 / 版本</th></tr>')
        plain_groups.append('\n'+title)
        for e in entries:
            url = _link(e)
            ranks = {chart: str(e['ranks'][chart]) if e['ranks'][chart] is not None else
                     '前80以外/未上榜' if e['rankStates'][chart] == 'outside_top80' else '缺数'
                     for chart in ['free', 'grossing']}
            triggers = '、'.join(config['charts'][c] for c in e['triggers'])
            released = e.get('releaseDate') or '未采集'
            rating = f"{e['rating'] or '—'} / {e['ratingCount'] or '—'}"
            style = 'padding:12px 10px;border-bottom:1px solid #e2e6e0;vertical-align:top;font-size:12px;'
            groups.append(f'<tr><td style="{style}"><a href="{esc(url)}" style="color:#147482;font-size:15px;text-decoration:underline;">{esc(e["name"])}</a><br>{esc(e["developer"])}<br><span style="color:#5d7075;">ID {esc(e["appId"])}</span></td>'
                          f'<td style="{style}">免费榜：{esc(ranks["free"])}<br>畅销榜：{esc(ranks["grossing"])}<br>触发：{esc(triggers)}</td>'
                          f'<td style="{style}">最早发布：{esc(released)}<br>评分 / 数量：{esc(rating)}<br>版本日期：{esc(e["latestVersionDate"])}</td></tr>')
            plain_groups.append(f"{e['name']} · {e['developer']} · ID {e['appId']}\n免费榜：{ranks['free']}；畅销榜：{ranks['grossing']}；触发：{triggers}\n最早发布：{released}；评分 / 数量：{rating}；版本日期：{e['latestVersionDate'] or '—'}\n详情：{url}")
        groups.append('</table>')
    if not grouped:
        empty = '本轮暂无已确认入选记录。请结合运行状态、比较覆盖及日期待核实数量判断。'
        groups.append(f'<p style="padding:18px;background:#eaf0eb;">{empty}</p>')
        plain_groups.append(empty)
    reasons = Counter(s.get('reason') or '上一轮缺少对应完整快照或排名规则不兼容'
                      for s in coverage['scopes'] if s['status'] != 'complete' or s['comparison'] == 'unknown')
    exceptions = [f'{reason}（{n} 个范围）' for reason, n in sorted(reasons.items())] or ['本轮榜单完整且所有范围均可比较。']
    coverage_html = ''.join(f'<p style="font-size:13px;margin:6px 0 12px;">{esc(line)}</p>' for line in exceptions)
    values = {'TITLE': esc(subject), 'TIME': esc(started), 'STATUS': esc(status), 'RUN_ID': esc(run['runId']),
              'SUMMARY': summary_html, 'POLICY': esc(policy_text), 'NOTICE': esc(notice),
              'GROUPS': ''.join(groups), 'COVERAGE': coverage_html}
    template = (Path(__file__).resolve().parents[2]/'assets/mail.html').read_text(encoding='utf-8')
    # One pass: source text must never be interpreted as another template placeholder.
    body = re.sub(r'__(TITLE|TIME|STATUS|RUN_ID|SUMMARY|POLICY|NOTICE|GROUPS|COVERAGE)__', lambda m: values[m[1]], template)
    plain = '\n'.join([subject, f"运行编号：{run['runId']}", *[f'{k}：{v}' for k,v in summary],
                       policy_text, notice, *plain_groups, '\n数据覆盖与异常', *exceptions])+'\n'
    return {'subject': subject, 'html': body, 'text': plain}


def _transient_connect(error):
    if isinstance(error, ssl.SSLError):
        return False
    return (isinstance(error, (TimeoutError, ConnectionError))
            or isinstance(error, socket.gaierror) and error.errno == socket.EAI_AGAIN
            or isinstance(error, OSError) and error.errno in {errno.ETIMEDOUT, errno.ECONNREFUSED, errno.ENETUNREACH, errno.EHOSTUNREACH})


class MailDelivery:
    def __init__(self, store):
        self.store = store

    def _closed(self, run_id, sending=False):
        m = self.store.manifest(run_id)
        if m['status'] not in {'complete', 'partial', 'failed'} or not (self.store.locate(run_id)/'snapshots.jsonl.gz').exists():
            raise MailError('只允许为已归档轮次生成或发送邮件')
        if sending and (self.store.mode != 'formal' or m['origin'] != 'browser' or m['mode'] != 'formal'):
            raise MailError('真实邮件只允许正式浏览器轮次；测试与旧版导入禁止发信')
        return m

    def _content(self, run_id):
        path = self.store.locate(run_id)/'mail/content.json'
        if path.exists():
            content = _json(path, '冻结邮件正文')
            if content.get('runId') != run_id or content.get('hash') != digest(content.get('body')):
                raise MailError('冻结邮件正文与运行编号或内容摘要不一致')
            return content['body']
        return render_mail(self.store.compare(run_id), self.store.manifest(run_id)['config'])

    def preview(self, run_id):
        self._closed(run_id)
        content = self._content(run_id)
        folder = self.store.locate(run_id)/'mail'
        write_text(folder/'preview.html', content['html'])
        write_text(folder/'preview.txt', content['text'])
        return {'runId': run_id, 'status': 'previewed',
                'html': (folder/'preview.html').relative_to(self.store.root).as_posix(),
                'text': (folder/'preview.txt').relative_to(self.store.root).as_posix()}

    def send(self, run_id):
        self._closed(run_id, sending=True)
        config = load_mail(self.store.root)
        if config['status'] != 'ready':
            return {'runId': run_id, **config}
        self.store.local.mkdir(mode=0o700, exist_ok=True)
        key = digest({'mode': self.store.mode, 'runId': run_id})[:32]
        with (self.store.local/f'mail-{key}.guard').open('a') as guard:
            try:
                fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {'runId': run_id, 'status': 'busy', 'reason': '本轮邮件正在由另一进程发送'}
            try:
                return self._send_locked(run_id, config)
            finally:
                fcntl.flock(guard, fcntl.LOCK_UN)

    def _send_locked(self, run_id, config):
        folder = self.store.locate(run_id)/'mail'
        body = self._content(run_id)
        content_hash = digest(body)
        if not (folder/'content.json').exists():
            write_json(folder/'content.json', {'runId': run_id, 'hash': content_hash, 'body': body})
        path = folder/'delivery.json'
        state = _json(path, '邮件发送记录') if path.exists() else {'schemaVersion': 1, 'runId': run_id, 'contentHash': content_hash, 'recipients': {}}
        if state.get('schemaVersion') != 1 or state.get('runId') != run_id or state.get('contentHash') != content_hash or not isinstance(state.get('recipients'), dict):
            raise MailError('邮件发送记录不兼容或与冻结正文不一致，停止以避免重复发送')
        normalized = {}
        for address, record in state['recipients'].items():
            address = _address(address)
            if (not isinstance(record, dict) or record.get('status') not in {'sending', 'submitting', 'submitted', 'failed', 'unknown'}
                    or type(record.get('attempts')) is not int or record['attempts'] < 1):
                raise MailError('邮件发送记录状态损坏，停止以避免重复发送')
            if address in normalized:
                raise MailError('邮件发送记录含重复域名别名，需人工核对，停止发送')
            normalized[address] = record
        if normalized != state['recipients']:
            state['recipients'] = normalized
            write_json(path, state)
        results = []
        for recipient in config['recipients']:
            record = state['recipients'].get(recipient, {})
            if record.get('status') in {'sending', 'submitting'}:
                record.update(status='unknown', reason='上次发送进程中断，需人工核对，不能自动重发', updatedAt=stamp(utcnow()))
                state['recipients'][recipient] = record
                write_json(path, state)
            if record.get('status') in {'submitted', 'unknown'}:
                results.append({'recipient': masked(recipient), 'status': record['status'], 'skipped': True,
                                'reason': record.get('reason', '服务器已接受；不代表已进入收件箱')})
                continue
            message = EmailMessage(policy=policy.SMTP)
            message['From'] = formataddr((config['fromName'], config['fromAddress']))
            message['To'] = recipient
            message['Subject'] = body['subject']
            message['Date'] = format_datetime(utcnow())
            identifier = digest({'runId': run_id, 'recipient': recipient, 'contentHash': content_hash})
            message['Message-ID'] = f"<{identifier}@{config['fromAddress'].split('@')[1]}>"
            message.set_content(body['text'])
            message.add_alternative(body['html'], subtype='html')
            raw_message = message.as_bytes()
            record = {'status': 'sending', 'messageId': message['Message-ID'], 'startedAt': stamp(utcnow()),
                      'attempts': record.get('attempts', 0)+1}
            state['recipients'][recipient] = record
            write_json(path, state)
            client, submitting = None, False
            try:
                for attempt in range(2):
                    try:
                        client = smtplib.SMTP_SSL(config['host'], config['port'], timeout=30, context=ssl.create_default_context())
                        break
                    except OSError as error:
                        if attempt or not _transient_connect(error):
                            raise
                client.login(config['fromAddress'], config['authCode'])
                code, _ = client.mail(config['fromAddress'])
                if not 200 <= code < 300:
                    raise smtplib.SMTPResponseException(code, b'sender refused')
                code, _ = client.rcpt(recipient)
                if code not in {250, 251}:
                    raise smtplib.SMTPResponseException(code, b'recipient refused')
                record['status'] = 'submitting'
                write_json(path, state)
                submitting = True
                code, _ = client.data(raw_message)
                if code != 250:
                    raise smtplib.SMTPDataError(code, b'data refused')
                record.update(status='submitted', reason='服务器已接受；不代表已进入收件箱')
            except (OSError, smtplib.SMTPException) as error:
                # getreply can synthesize SMTPResponseException(500) locally, or
                # return -1 for a malformed reply. Neither proves DATA was refused.
                explicit = isinstance(error, smtplib.SMTPResponseException) and (
                    not submitting or isinstance(error, smtplib.SMTPDataError) and 400 <= error.smtp_code < 600)
                status = 'unknown' if submitting and not explicit else 'failed'
                reason = ('提交后连接异常，结果不明；需人工核对，不能自动重发' if status == 'unknown' else
                          'SMTP认证失败，请检查本地授权码' if isinstance(error, smtplib.SMTPAuthenticationError) else
                          'TLS证书或加密连接验证失败' if isinstance(error, ssl.SSLError) else
                          f'SMTP服务器明确拒绝（代码{error.smtp_code}）' if explicit else 'SMTP连接失败或提交前连接中断')
                record.update(status=status, reason=reason)
            finally:
                if client:
                    try:
                        client.quit()
                    except (OSError, smtplib.SMTPException):
                        pass
                    finally:
                        client.close()
            record['updatedAt'] = stamp(utcnow())
            write_json(path, state)
            results.append({'recipient': masked(recipient), 'status': record['status'], 'reason': record['reason'], 'skipped': False})
        statuses = {r['status'] for r in results}
        overall = ('unknown' if 'unknown' in statuses else 'submitted' if statuses == {'submitted'} else
                   'partial' if 'submitted' in statuses else 'failed')
        return {'runId': run_id, 'status': overall, 'recipients': results,
                'record': path.relative_to(self.store.root).as_posix()}
