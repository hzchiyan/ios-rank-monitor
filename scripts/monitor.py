#!/usr/bin/env python3
"""Repository-local command line interface. Browser interaction stays in Codex."""
import argparse
import json
from pathlib import Path
import sys

if sys.version_info < (3, 11):
    raise SystemExit("需要 Python 3.11 或更新版本")

from rank_monitor.core import Store, config_id, scopes


def main():
    parser = argparse.ArgumentParser(description="七麦 iOS 榜单本地监测工具")
    sub = parser.add_subparsers(dest="command", required=True)
    importer = sub.add_parser("import-legacy", help="导入本地旧版快照，不访问网络")
    importer.add_argument("path")
    importer.add_argument("--mode", choices=["formal", "test"], default="formal")
    for name in ["doctor", "start", "pending", "resume", "ingest", "mark", "finalize", "render"]:
        p = sub.add_parser(name)
        p.add_argument("--mode", choices=["formal", "test"], default=None)
        if name in ["pending", "resume", "ingest", "mark", "finalize"]:
            p.add_argument("run_id")
        if name in ["resume", "ingest", "mark", "finalize"]:
            p.add_argument("--token", required=True)
        if name == "start":
            p.add_argument("--at")
        if name in ["ingest", "mark"]:
            p.add_argument("--input", help="JSON文件；省略时读取标准输入")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    mode = args.mode
    if mode is None and getattr(args, "run_id", None):
        test = Store(root, "test")
        mode = "test" if any(r["runId"] == args.run_id for r in test.state()["runs"]) else "formal"
    store = Store(root, mode or "formal")
    if args.command == "import-legacy":
        from rank_monitor.legacy import import_legacy
        return import_legacy(store, Path(args.path))
    if args.command == "doctor":
        return {"ready": True, "repositoryRoot": str(root), "plannedScopes": len(scopes(store.config)),
                "configFingerprint": config_id(store.config), "python": sys.version.split()[0],
                "browserVerified": False, "note": "本命令仅检查本地程序；浏览器、账号和定时权限必须由Codex在目标机实测"}
    if args.command == "start":
        if args.at and store.mode != "test":
            raise ValueError("正式采集使用实际时间；--at只允许用于独立测试")
        return store.start(args.at)
    if args.command == "pending":
        return {"scopes": store.pending(args.run_id)}
    if args.command == "resume":
        return store.resume(args.run_id, args.token)
    if args.command in ["ingest", "mark"]:
        raw = json.loads(Path(args.input).read_text() if args.input else sys.stdin.read())
        if args.command == "ingest":
            return store.ingest(args.run_id, args.token, raw)
        return store.mark(args.run_id, args.token, raw["scope"], raw["status"], raw["reason"])
    if args.command == "finalize":
        return store.finalize(args.run_id, args.token)
    return store.render()


if __name__ == "__main__":
    try:
        print(json.dumps(main(), ensure_ascii=False))
    except (ValueError, KeyError, OSError, json.JSONDecodeError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        sys.exit(1)
