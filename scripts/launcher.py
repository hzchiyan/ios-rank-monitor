#!/usr/bin/env python3
"""Install a stable local launcher; lock before loading mutable repository code."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys

LOCK_ENV = 'IOS_RANK_REPOSITORY_LOCK_FD'


def inherited_fd(root):
    try:
        fd = int(os.environ[LOCK_ENV])
        opened = os.fstat(fd)
        expected = (root/'.local/repository.guard').stat()
        return fd if (opened.st_dev, opened.st_ino) == (expected.st_dev, expected.st_ino) else None
    except (KeyError, ValueError, OSError):
        return None


def install(root, enable=False):
    local = root/'.local'
    local.mkdir(mode=0o700, exist_ok=True)
    with (local/'repository.guard').open('a') as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        target = local/'monitor-launcher.py'
        temp = target.with_suffix('.tmp')
        with temp.open('wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(Path(__file__).read_bytes())
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temp, target)
        policy = local/'update.json'
        if not policy.exists():
            config = json.loads((root/'config/update.example.json').read_text())
            config['enabled'] = enable
            temp = policy.with_suffix('.tmp')
            with temp.open('w') as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(config, stream, ensure_ascii=False)
                stream.flush(); os.fsync(stream.fileno())
            os.replace(temp, policy)
    return {'installed': True, 'launcher': '.local/monitor-launcher.py', 'policy': '.local/update.json',
            'note': '已有更新策略和其他本机配置保留；未启动采集或发信'}


def main():
    root = Path(__file__).resolve().parents[1]
    args = sys.argv[1:]
    if args and args[0] == 'install' and all(x == '--enable-update' for x in args[1:]):
        print(json.dumps(install(root, '--enable-update' in args), ensure_ascii=False))
        return 0
    if not args or args[0] not in {'startup', 'command'}:
        raise ValueError('使用install [--enable-update]、startup --profile或command 子命令')
    local = root/'.local'; local.mkdir(mode=0o700, exist_ok=True)
    with (local/'repository.guard').open('a') as guard:
        fcntl.flock(guard, fcntl.LOCK_EX if args[0] == 'startup' else fcntl.LOCK_SH)
        env = dict(os.environ); env[LOCK_ENV] = str(guard.fileno())
        script = 'update_start.py' if args[0] == 'startup' else 'monitor.py'
        result = subprocess.run([sys.executable, str(root/'scripts'/script), *args[1:]], cwd=root,
                                env=env, pass_fds=(guard.fileno(),))
        return result.returncode


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError):
        print(json.dumps({'error': '稳定启动器无法安装或执行，请检查本地路径、权限及调用格式'}, ensure_ascii=False))
        sys.exit(1)
