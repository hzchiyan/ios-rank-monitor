"""Read-only power preflight and explicitly authorized macOS deployment."""
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

from .core import stamp, utcnow, write_json


def load_policy(root):
    path = root / 'config/runtime.json'
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('invalid_runtime_config')
    value = json.loads(path.read_text())
    if (not isinstance(value, dict) or set(value) != {'schemaVersion','requireACPower','acSleepMinutes','displaySleepMinutes'}
            or type(value['schemaVersion']) is not int or value['schemaVersion'] != 1
            or value['requireACPower'] is not True
            or type(value['acSleepMinutes']) is not int or value['acSleepMinutes'] != 0
            or type(value['displaySleepMinutes']) is not int or not 1 <= value['displaySleepMinutes'] <= 180):
        raise ValueError('invalid_runtime_config')
    return value


def read_power():
    settings = subprocess.run(['pmset','-g','custom'],capture_output=True,text=True,timeout=10)
    source = subprocess.run(['pmset','-g','batt'],capture_output=True,text=True,timeout=10)
    if settings.returncode or source.returncode:
        raise OSError('power_query_failed')
    sections = {}; current = None
    for line in settings.stdout.splitlines():
        if line.strip() in {'AC Power:','Battery Power:','UPS Power:'}:
            current = line.strip()[:-1]; sections[current] = {}
        else:
            item = re.fullmatch(r'\s*(\w+)\s+(\d+)\s*',line)
            if current and item:
                sections[current][item[1]] = int(item[2])
    match = re.search(r"Now drawing from '(AC Power|Battery Power|UPS Power)'",source.stdout)
    return {'powerSource':match[1] if match else 'unknown',
            'ac':sections.get('AC Power',{}), 'battery':sections.get('Battery Power',{})}


def check_environment(root):
    root = Path(root)
    result = {'ready':False,'status':'blocked','browserWhileLockedVerified':False}
    try:
        policy = load_policy(root)
    except (OSError, ValueError, TypeError):
        return {**result,'reason':'invalid_runtime_config'}
    if policy is None:
        return {**result,'ready':True,'status':'not_configured','reason':'legacy_without_runtime_policy'}
    result['policy'] = policy
    if sys.platform != 'darwin':
        return {**result,'reason':'unsupported_platform'}
    try:
        observed = read_power()
    except (OSError, ValueError, subprocess.SubprocessError):
        return {**result,'reason':'power_query_failed'}
    result.update(powerSource=observed['powerSource'],observed=observed)
    checks = {'acPower':observed['powerSource'] == 'AC Power',
              'acSleepDisabled':observed['ac'].get('sleep') == policy['acSleepMinutes'],
              'displaySleepConfigured':observed['ac'].get('displaysleep') == policy['displaySleepMinutes']}
    result['checks'] = checks
    result['ready'] = all(checks.values())
    result['status'] = 'ready' if result['ready'] else 'blocked'
    if not result['ready']:
        result['reason'] = 'runtime_policy_not_met'
    return result


def apply_environment(root, authorize=False):
    """Deployment only. No ranking/token/mail files are read or changed."""
    root = Path(root)
    checked = check_environment(root)
    checked['systemApplied'] = False
    if checked['status'] == 'not_configured':
        return {**checked,'ready':False,'reason':'runtime_policy_missing'}
    if 'observed' not in checked:
        return checked
    if not checked['checks']['acPower']:
        return {**checked,'reason':'ac_power_required'}
    if checked['ready']:
        return {**checked,'status':'unchanged'}
    if not {'sleep','displaysleep'} <= checked['observed']['ac'].keys():
        return {**checked,'reason':'original_ac_settings_missing'}
    if os.geteuid() != 0 and not authorize:
        return {**checked,'reason':'administrator_required'}
    folder = root / 'data/environment'
    try:
        folder.mkdir(parents=True,exist_ok=True)
        with (folder / '.guard').open('a') as guard:
            fcntl.flock(guard,fcntl.LOCK_EX)
            # Re-read after the deployment lock so parallel invocations cannot
            # replace the original settings with another invocation's result.
            checked = check_environment(root); checked['systemApplied'] = False
            if not checked.get('observed') or not checked.get('checks',{}).get('acPower'):
                return checked
            if checked['ready']:
                return {**checked,'status':'unchanged'}
            if not {'sleep','displaysleep'} <= checked['observed']['ac'].keys():
                return {**checked,'reason':'original_ac_settings_missing'}
            policy = checked['policy']
            path = folder / (utcnow().strftime('%Y%m%dT%H%M%S%fZ')+'.json')
            record = {'observedAt':stamp(utcnow()),'before':checked['observed'],'policy':policy,'status':'prepared'}
            write_json(path,record)
            checked['record'] = path.relative_to(root).as_posix()
            command = ['/usr/bin/pmset','-c','sleep','0','displaysleep',str(policy['displaySleepMinutes'])]
            if os.geteuid() != 0:
                command = ['osascript','-e','do shell script "'+' '.join(command)+'" with administrator privileges']
            timed_out = False
            try:
                response = subprocess.run(command,capture_output=True,text=True,timeout=60)
                accepted = response.returncode == 0
            except subprocess.TimeoutExpired:
                accepted = False
                timed_out = True
            except (OSError, subprocess.SubprocessError):
                accepted = False
            after = check_environment(root)
            observed = 'observed' in after
            applied = observed and after['ready']
            unknown = timed_out or not observed
            reason = ('apply_timeout' if timed_out else 'verification_unavailable' if not observed
                      else 'apply_failed' if not accepted else 'verification_failed' if not applied else None)
            status = 'unknown' if unknown else 'applied' if accepted and applied else 'failed'
            record.update(status=status,after=after,reason=reason,
                          commandOutcome='unknown' if timed_out else 'accepted' if accepted else 'failed')
            checked = {**after,'systemApplied':None if unknown else applied,
                       'record':path.relative_to(root).as_posix(),'lastReadbackReady':applied,
                       'ready':not unknown and accepted and applied,'status':status}
            checked.pop('reason',None)
            if reason:
                checked['reason'] = reason
            if unknown:
                checked['retryRequiresReview'] = record['retryRequiresReview'] = True
            write_json(path,record)
            return checked
    except OSError:
        return {**checked,'ready':False,'status':'failed','reason':'record_failed'}
