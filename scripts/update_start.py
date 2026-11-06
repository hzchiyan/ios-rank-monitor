#!/usr/bin/env python3
"""Idle-only repository synchronization and atomic fresh-process run registration."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

from launcher import LOCK_ENV, inherited_fd


class UpdateError(Exception):
    def __init__(self, reason, **context):
        super().__init__(reason)
        self.context = context


def safe_detail(value):
    text = str(value)[:600]
    text = re.sub(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', '[邮箱已隐藏]', text)
    text = re.sub(r'\b(?:[0-9a-f]{32,}|[a-z]{16})\b', '[长字串已隐藏]', text)
    return re.sub(r'(?i)(password|authcode|token|授权码|密码)\s*[=:：]\s*\S+', r'\1=[已隐藏]', text)


def read(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        raise UpdateError('invalid_history') from None


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name+'.tmp')
    with temp.open('w') as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(value, stream, ensure_ascii=False)
        stream.flush(); os.fsync(stream.fileno())
    os.replace(temp, path)


def policy(root):
    try:
        value = json.loads((root/'.local/update.json').read_text())
        if (not isinstance(value, dict) or set(value) != {'schemaVersion','enabled','remote','branch','onFailure','timeoutSeconds','validationTimeoutSeconds'}
                or value['schemaVersion'] != 1 or type(value['enabled']) is not bool
                or not isinstance(value['remote'], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', value['remote'])
                or value['branch'] != 'main' or value['onFailure'] not in {'keep_current','stop'}
                or type(value['timeoutSeconds']) is not int or not 1 <= value['timeoutSeconds'] <= 300
                or type(value['validationTimeoutSeconds']) is not int or not 1 <= value['validationTimeoutSeconds'] <= 1800):
            raise ValueError()
        return value
    except (OSError, ValueError, KeyError, TypeError):
        raise UpdateError('invalid_config') from None


def git(root, args, timeout=60, reason='git_failed'):
    env = dict(os.environ); env['GIT_TERMINAL_PROMPT'] = '0'
    try:
        result = subprocess.run(['git', *args], cwd=root, env=env, capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            raise UpdateError(reason)
        return result.stdout.strip()
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        raise UpdateError(reason) from None


def busy_rounds(root):
    busy = []
    for profile, folder in [('apps', root/'data'), ('games', root/'data/games')]:
        for mode in ['formal','test']:
            base = folder/'test' if mode == 'test' else folder
            state = read(base/'state.json') if (base/'state.json').exists() else {'schemaVersion':1,'runs':[],'latestRunId':None}
            try:
                if state['schemaVersion'] != 1 or not isinstance(state['runs'], list): raise ValueError()
                ids = [r['runId'] for r in state['runs']]
                if len(ids) != len(set(ids)) or state['latestRunId'] != (ids[-1] if ids else None): raise ValueError()
                for r in state['runs']:
                    rel = Path(r['relativePath']); path = (root/rel).resolve()
                    if (rel.is_absolute() or '..' in rel.parts or not path.is_relative_to(base.resolve())
                            or mode == 'formal' and path.is_relative_to((base/'test').resolve())
                            or profile == 'apps' and path.is_relative_to((root/'data/games').resolve())): raise ValueError()
                    m = read(path/'manifest.json')
                    if m['runId'] != r['runId'] or m.get('profile','apps') != profile or m['mode'] != mode: raise ValueError()
                    if m['status'] not in {'running','cancelling','complete','partial','failed'}: raise ValueError()
                    if m['status'] in {'running','cancelling'}:
                        busy.append({'profile':profile,'mode':mode,'runId':r['runId'],'reason':m['status']})
                if (base/'active.json').exists():
                    active = read(base/'active.json')
                    if active['runId'] not in ids: raise ValueError()
                    if not any(b['profile']==profile and b['mode']==mode and b['runId']==active['runId'] for b in busy):
                        busy.append({'profile':profile,'mode':mode,'runId':active['runId'],'reason':'active_marker'})
            except (KeyError, TypeError, ValueError):
                raise UpdateError('invalid_history') from None
    return busy


def monitor(root, args, fd, timeout=60):
    env = dict(os.environ); env[LOCK_ENV] = str(fd)
    try:
        p = subprocess.run([sys.executable, str(root/'scripts/monitor.py'), *args], cwd=root,
                           env=env, pass_fds=(fd,), capture_output=True, text=True, timeout=timeout)
        value = json.loads(p.stdout)
        if p.returncode or not isinstance(value, dict):
            raise UpdateError('monitor_failed', step=args[0], profile=args[-1],
                              detail=safe_detail(value.get('error', '命令未成功') if isinstance(value,dict) else '结果格式无效'))
        return value
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise UpdateError('monitor_failed', step=args[0], profile=args[-1], detail='无法取得有效命令结果') from None


def validate(root, target, config):
    names = [n for n in git(root, ['ls-tree','-r','-z','--name-only',target]).split('\0') if n]
    if any(Path(n).parts[0] in {'data','.local','.git','.env'} for n in names):
        raise UpdateError('protected_paths')
    checks = root/'data/updates/checks'; checks.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='candidate-', dir=checks) as work:
        work = Path(work); archive = work/'code.tar'; candidate = work/'source'; candidate.mkdir()
        git(root, ['archive','--format=tar','--output='+str(archive),target])
        with tarfile.open(archive) as tar:
            for item in tar:
                dest = (candidate/item.name).resolve()
                if (not dest.is_relative_to(candidate.resolve()) or item.issym() or item.islnk()
                        or not (item.isdir() or item.isfile())): raise UpdateError('unsafe_archive')
                if item.isdir(): dest.mkdir(parents=True, exist_ok=True)
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with tar.extractfile(item) as source, dest.open('wb') as output:
                        shutil.copyfileobj(source, output)
        commands = [('compile',[sys.executable,'-m','compileall','-q','scripts']),
                    ('doctor_apps',[sys.executable,'scripts/monitor.py','doctor','--profile','apps']),
                    ('doctor_games',[sys.executable,'scripts/monitor.py','doctor','--profile','games']),
                    ('tests',[sys.executable,'-m','unittest','discover','-s','tests','-v'])]
        env = {k:v for k,v in os.environ.items() if k in {'PATH','HOME','LANG','LC_ALL','TZ','TMPDIR','SYSTEMROOT'}}
        try:
            for step,command in commands:
                p = subprocess.run(command,cwd=candidate,env=env,capture_output=True,text=True,
                                   timeout=config['validationTimeoutSeconds'])
                if p.returncode: raise UpdateError('validation_failed', step=step)
        except (OSError, subprocess.TimeoutExpired):
            raise UpdateError('validation_failed', step=step) from None


def audit(root, result):
    path = root/'data/updates/audit.jsonl'
    value = {'observedAt':datetime.now(timezone.utc).isoformat(), 'profile':result['profile'],
             'update':result['update'], 'runId':result.get('run',{}).get('runId'), 'failure':result.get('failure')}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a') as stream:
            stream.write(json.dumps(value,ensure_ascii=False)+'\n'); stream.flush(); os.fsync(stream.fileno())
        result['audit'] = path.relative_to(root).as_posix(); result['auditSaved'] = True
    except OSError:
        result['auditSaved'] = False


def actual_commit(root, result):
    """Failure can happen after Git switched HEAD; never report the old SHA as current."""
    try:
        result['update']['afterCommit'] = git(root, ['rev-parse','HEAD'])
    except UpdateError:
        result['update']['afterCommit'] = None


def startup(root, profile, check_only=False):
    fd = inherited_fd(root)
    result = {'profile':profile,'update':{'status':'blocked'}}
    if fd is None:
        result['update']['reason']='launcher_required';return result,1
    try:
        config = policy(root)
        state_path = root/'.local/update-state.json'
        try:
            previous = read(state_path) if state_path.exists() else {}
            if not isinstance(previous, dict) or (state_path.exists() and previous.get('phase') not in {'validating','applying','ready'}):
                raise UpdateError('invalid_update_state')
            if 'cohort' in previous:
                cohort = previous['cohort']
                if (not isinstance(cohort, dict) or set(cohort) != {'commit','runs'}
                        or not isinstance(cohort['commit'], str) or not re.fullmatch(r'[0-9a-f]{40,64}', cohort['commit'])
                        or not isinstance(cohort['runs'], dict) or not cohort['runs'].keys() <= {'apps','games'}
                        or any(not isinstance(run, str) or not re.fullmatch(r'\d{8}T\d{6}(?:\d{6})?Z', run)
                               for run in cohort['runs'].values())):
                    raise UpdateError('invalid_update_state')
        except UpdateError:
            raise UpdateError('invalid_update_state') from None
        if previous.get('phase') == 'applying': raise UpdateError('interrupted_apply')
        before = git(root,['rev-parse','HEAD']); branch = git(root,['symbolic-ref','--short','HEAD'])
        dirty = bool(git(root,['status','--porcelain','--untracked-files=normal']))
        busy = busy_rounds(root)
        update = result['update']; update.update(beforeCommit=before,afterCommit=before,busyRounds=busy)
        if check_only:
            update.update(status='check_only',enabled=config['enabled'],onFailure=config['onFailure'],branch=branch,dirty=dirty)
            return result,0
        if dirty: raise UpdateError('dirty_checkout')
        if config['enabled'] and branch != config['branch']: raise UpdateError('wrong_branch')
        if busy:
            update['status']='deferred'
            cohort = previous.get('cohort', {})
            own_work = any(b['profile'] == profile and b['mode'] == 'formal' for b in busy)
            can_join = (isinstance(cohort, dict) and cohort.get('commit') == before
                        and isinstance(cohort.get('runs'), dict) and profile not in cohort['runs']
                        and all(b['mode'] == 'formal' and cohort['runs'].get(b['profile']) == b['runId'] for b in busy))
            if not own_work and not can_join:
                result['run']={'skipped':True, 'waitingForDrain':True,
                               'reason':'先完成已有轮次；本组等待全局空闲更新后再开新轮'}
                audit(root,result)
                return result,0
        elif not config['enabled']: update['status']='disabled'
        else:
            try:
                git(root,['fetch','--no-tags',config['remote'],f"refs/heads/{config['branch']}:refs/remotes/{config['remote']}/{config['branch']}"],config['timeoutSeconds'],'fetch_failed')
                target = git(root,['rev-parse',f"refs/remotes/{config['remote']}/{config['branch']}"])
                update['remoteCommit']=target
                if target == before: update['status']='current'
                else:
                    git(root,['merge-base','--is-ancestor',before,target],reason='not_fast_forward')
                    write(state_path,{'phase':'validating','beforeCommit':before,'targetCommit':target})
                    validate(root,target,config)
                    if git(root,['rev-parse','HEAD']) != before or git(root,['status','--porcelain','--untracked-files=normal']):
                        raise UpdateError('checkout_changed')
                    write(state_path,{'phase':'applying','beforeCommit':before,'targetCommit':target})
                    git(root,['merge','--ff-only',target],reason='apply_failed')
                    if git(root,['rev-parse','HEAD']) != target or git(root,['status','--porcelain','--untracked-files=normal']):
                        raise UpdateError('apply_failed')
                    write(state_path,{'phase':'ready','commit':target})
                    update.update(status='updated',afterCommit=target)
            except UpdateError as error:
                if state_path.exists() and read(state_path).get('phase') == 'applying': raise UpdateError('interrupted_apply') from None
                if (config['onFailure'] == 'stop' or git(root,['rev-parse','HEAD']) != before
                        or git(root,['status','--porcelain','--untracked-files=normal'])): raise
                write(state_path,{'phase':'ready','commit':before})
                update.update(status='kept_current',reason=str(error))
                if error.context:
                    update['validationStep']=error.context.get('step')
        result['doctors']={p:monitor(root,['doctor','--profile',p],fd) for p in ['apps','games']}
        run = monitor(root,['start','--profile',profile],fd)
        if run.pop('token',None) is not None:
            token_dir = '.local/tokens/games/formal' if profile == 'games' else '.local/tokens/formal'
            run['tokenFile']=token_dir+'/'+run['runId']
        result['run']=run
        if not run.get('skipped') and not run.get('cancelled') and not run.get('resumed'):
            # Each profile may join once, then drain this cohort before opening another.
            members = dict(previous.get('cohort', {}).get('runs', {})) if busy else {}
            members[profile] = run['runId']
            write(state_path,{'phase':'ready','commit':update['afterCommit'],
                              'cohort':{'commit':update['afterCommit'],'runs':members}})
        audit(root,result)
        return result,0
    except UpdateError as error:
        result['update'].update(status='blocked',reason=str(error))
        actual_commit(root,result)
        if error.context:
            result['failure']=error.context
            if error.context.get('step') == 'start': result['registrationUncertain']=True
        audit(root,result);return result,1
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError):
        result['update'].update(status='blocked',reason='local_operation_failed')
        actual_commit(root,result)
        audit(root,result);return result,1


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--profile',choices=['apps','games'],required=True)
    parser.add_argument('--check-only',action='store_true')
    args=parser.parse_args()
    result,code=startup(Path(__file__).resolve().parents[1],args.profile,args.check_only)
    print(json.dumps(result,ensure_ascii=False));sys.exit(code)
