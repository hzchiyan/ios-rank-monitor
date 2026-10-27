"""Validated snapshots and an append-only chain of local ranking runs."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import gzip
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

SCHEMA = 1
COMPARISON_VERSION = "shared_scopes_v1"
EXECUTION_POLICY = "completion_v1"
WORKER_LEASE_MINUTES = 15
TERMINAL = {"source_no_data", "failed", "auth_required", "interrupted"}


def completion_driven(manifest):
    return manifest.get("executionPolicy") == EXECUTION_POLICY


def utcnow():
    return datetime.now(timezone.utc)


def parse_time(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("时间必须包含时区")
    return dt.astimezone(timezone.utc)


def stamp(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def load_config(root):
    c = read_json(root / "config/monitor.json")
    if c["schemaVersion"] != SCHEMA or c["cutoff"] != 80 or c["device"] != "iphone":
        raise ValueError("配置格式、前80门槛或设备不符合当前规则")
    if set(c["countries"]) != {"us", "de", "jp"} or set(c["charts"]) != {"free", "grossing"}:
        raise ValueError("国家或榜单配置不符合当前范围")
    if c["timezone"] != "Asia/Shanghai" or c["ruleVersion"] != "either_chart_new_top80_v1":
        raise ValueError("时区或业务规则版本不受支持")
    cats = c["categories"]
    if len(cats) != 40 or len({x["genreId"] for x in cats}) != 40:
        raise ValueError("需要40个互不重复的分类编号")
    if sum(x["family"] == "应用" for x in cats) != 24 or sum(x["family"] == "游戏" for x in cats) != 16:
        raise ValueError("分类应为24个应用分类和16个游戏分类")
    families = c.get("scanFamilies", ["应用", "游戏"])
    if (not isinstance(families, list) or not families
            or any(not isinstance(f, str) or f not in {"应用", "游戏"} for f in families)
            or len(set(families)) != len(families)):
        raise ValueError("scanFamilies必须是非空、无重复的应用或游戏大类列表")
    if "executionPolicy" in c and c["executionPolicy"] != EXECUTION_POLICY:
        raise ValueError("执行策略不受支持")
    if type(c["maxScopeRetries"]) is not int or not 0 <= c["maxScopeRetries"] <= 10:
        raise ValueError("范围重试次数必须为0至10的整数")
    from .releases import policy_for
    policy_for(c, "2026-01-01")
    return c


def config_id(config):
    value = {k: config[k] for k in ["schemaVersion", "ruleVersion", "timezone", "device", "cutoff", "countries", "charts", "categories"]}
    if "scanFamilies" in config:
        value["scanFamilies"] = config["scanFamilies"]
    if "executionPolicy" in config:
        value["executionPolicy"] = config["executionPolicy"]
    return digest(value)


def scope_key(scope):
    return ":".join([scope["country"], scope["categoryFamily"], str(scope["genreId"]), scope["chart"]])


def scopes(config, *, include_inactive=False):
    return [{"country": country, "categoryFamily": cat["family"], "category": cat["name"],
             "sourceCategory": cat["sourceCategory"], "genreId": cat["genreId"], "chart": chart,
             "device": config["device"],
             "sourceUrl": f"https://www.qimai.cn/rank/index/brand/{chart}/device/iphone/country/{country}/genre/{cat['genreId']}"}
            for country in config["countries"] for cat in config["categories"]
            if include_inactive or cat["family"] in config.get("scanFamilies", ["应用", "游戏"])
            for chart in config["charts"]]


def shared_scope_compatible(current, previous, key):
    """Scope selection can change; the meaning of a compared chart cannot."""
    fields = ["schemaVersion", "ruleVersion", "timezone", "device", "cutoff"]
    if current["ruleVersion"] != previous["ruleVersion"] or any(
            current["config"][f] != previous["config"][f] for f in fields):
        return False
    left, right = current["scopes"][key], previous["scopes"].get(key)
    if not right or any(left[f] != right[f] for f in
                        ["country", "categoryFamily", "category", "sourceCategory", "genreId", "chart", "device", "sourceUrl"]):
        return False
    definitions = [next((c for c in m["config"]["categories"] if c["genreId"] == left["genreId"]), None)
                   for m in [current, previous]]
    return definitions[0] is not None and definitions[0] == definitions[1]


def source_day(value):
    m = re.fullmatch(r"(\d{4})(?:年|-)(\d{1,2})(?:月|-)(\d{1,2})日?", value)
    if not m:
        raise ValueError("来源日期格式无法识别")
    return f"{int(m[1]):04d}-{int(m[2]):02d}-{int(m[3]):02d}"


def normalize_snapshot(raw, expected, config, started_at, test=False, *, fixed_source_day=None):
    s = dict(raw)
    for field in ["country", "categoryFamily", "category", "chart", "device"]:
        if s.get(field) != expected[field]:
            raise ValueError(f"快照范围不匹配: {field}")
    if str(s.get("genreId")) != expected["genreId"]:
        raise ValueError("分类编号不匹配")
    u = urlparse(s.get("sourceUrl", ""))
    wanted = urlparse(expected["sourceUrl"])
    if u.scheme != "https" or u.hostname != "www.qimai.cn" or u.path.rstrip("/") != wanted.path:
        raise ValueError("来源URL与请求范围不一致")
    at = parse_time(s["capturedAt"])
    if fixed_source_day is not None:
        if at < parse_time(started_at):
            raise ValueError("采集时间早于本轮开始时间")
        if not test and at > utcnow():
            raise ValueError("采集时间不能晚于当前时间")
    elif not parse_time(started_at) <= at <= parse_time(started_at) + timedelta(minutes=config["maxRunMinutes"]):
        raise ValueError("采集时间不在本轮90分钟窗口内")
    expected_day = fixed_source_day or at.astimezone(ZoneInfo(config["timezone"])).date().isoformat()
    if source_day(s["sourceDate"]) != expected_day:
        if fixed_source_day:
            raise ValueError("来源日期必须等于本轮固定来源日期；跨天不能混入新一天榜单")
        raise ValueError("来源日期与实际采集日期不一致，请检查是否为旧页面")
    title = s.get("title", "")
    if not test and not all(x in title for x in [config["countries"][expected["country"]], "iPhone", expected["sourceCategory"], config["charts"][expected["chart"]]]):
        raise ValueError("缺少与范围一致的网页标题证据")
    if s.get("completeTop80") is not True or len(s.get("rows", [])) != config["cutoff"]:
        raise ValueError("必须确认已采集完整前80")
    rows = []
    for i, row in enumerate(s["rows"], 1):
        if type(row.get("position")) is not int or row["position"] != i:
            raise ValueError("名次必须连续且为1至80")
        link = urlparse(row.get("qimaiUrl", ""))
        m = re.fullmatch(r"/app/rank/appid/(\d+)/country/([a-z]{2})/?", link.path)
        if link.scheme != "https" or link.hostname != "www.qimai.cn" or not m or m[2] != expected["country"]:
            raise ValueError("App链接或国家不正确")
        app_id = str(row.get("appId", m[1]))
        if app_id != m[1] or not row.get("name"):
            raise ValueError("App ID与链接不一致或名称缺失")
        if row.get("ranks"):
            selected = row["ranks"][-1]
            if selected.get("category") != expected["sourceCategory"] or str(selected.get("rank")) != str(i):
                raise ValueError("页面分类名次与请求分类不一致")
        rows.append({**{f: str(row.get(f) or "") for f in ["name", "developer", "rating", "ratingCount", "latestVersionDate"]},
                     "appId": app_id, "position": i,
                     "qimaiUrl": f"https://www.qimai.cn/app/rank/appid/{app_id}/country/{expected['country']}"})
    if len({r["appId"] for r in rows}) != len(rows):
        raise ValueError("同榜单出现重复App ID")
    clean_scope = {k: expected[k] for k in ["country", "categoryFamily", "category", "sourceCategory", "genreId", "chart", "device", "sourceUrl"]}
    return {**clean_scope, "sourceDate": source_day(s["sourceDate"]), "capturedAt": stamp(at),
            "title": title, "completeTop80": True, "rows": rows}


class Store:
    def __init__(self, root, mode="formal"):
        self.root = Path(root).resolve()
        self.config = load_config(self.root)
        self.mode = mode
        self.base = self.root / ("data/test" if mode == "test" else "data")
        self.local = self.root / ".local"

    @contextmanager
    def guard(self):
        self.local.mkdir(mode=0o700, exist_ok=True)
        with (self.local / f"{self.mode}.guard").open("a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def state(self):
        state = read_json(self.base / "state.json") if (self.base / "state.json").exists() else {"schemaVersion": SCHEMA, "runs": [], "latestRunId": None}
        if state.get("schemaVersion") != SCHEMA or not isinstance(state.get("runs"), list):
            raise ValueError("历史索引版本不兼容，保留数据并停止")
        seen = set()
        for entry in state["runs"]:
            relative = Path(entry["relativePath"])
            if relative.is_absolute() or ".." in relative.parts or not (self.root / relative).resolve().is_relative_to(self.base.resolve()):
                raise ValueError("历史索引含绝对路径或数据目录外路径，不能直接迁移")
            if entry["runId"] in seen:
                raise ValueError("历史索引有重复运行编号")
            seen.add(entry["runId"])
        if state.get("latestRunId") != (state["runs"][-1]["runId"] if state["runs"] else None):
            raise ValueError("历史索引的最新任务与顺序不一致")
        return state

    def check_history(self):
        previous = None
        for entry in self.state()["runs"]:
            m = self.manifest(entry["runId"])
            if m.get("schemaVersion") != SCHEMA or m.get("runId") != entry["runId"] or m.get("previousRunId") != previous:
                raise ValueError("历史任务版本或相邻比较链不兼容，保留文件并停止")
            previous = entry["runId"]
        return previous

    def locate(self, run_id):
        entry = next((x for x in self.state()["runs"] if x["runId"] == run_id), None)
        if not entry:
            raise ValueError("未找到运行编号")
        path = (self.root / entry["relativePath"]).resolve()
        if not path.is_relative_to(self.base.resolve()):
            raise ValueError("运行路径超出数据目录")
        return path

    def manifest(self, run_id):
        return read_json(self.locate(run_id) / "manifest.json")

    def _authorize(self, run_id, token, restore_active=False, require_window=False, allow_config_change=False):
        p = self.local / "tokens" / run_id
        if not p.exists() or not hmac.compare_digest(p.read_text(), token or ""):
            raise ValueError("运行令牌不匹配；不能修改其他运行")
        m = self.manifest(run_id)
        if m["status"] != "running":
            raise ValueError("运行已结束，原始快照不可修改")
        if require_window and not completion_driven(m) and self.mode != "test" and utcnow() >= parse_time(m["startedAt"]) + timedelta(minutes=m["config"]["maxRunMinutes"]):
            raise ValueError("本轮90分钟窗口已结束，请finalize归档")
        if not allow_config_change and m["configFingerprint"] != config_id(self.config):
            raise ValueError("采集期间配置发生改变，请结束本轮后重新启动")
        if self.state()["latestRunId"] != run_id:
            raise ValueError("只有最新进行中的任务可以写入")
        active = self.base / "active.json"
        if active.exists():
            if read_json(active)["runId"] != run_id:
                raise ValueError("另一任务持有采集锁")
        elif not restore_active:
            raise ValueError("运行标记缺失，请先resume恢复最新任务")
        return m

    def _touch(self, run_id):
        write_json(self.base / "active.json", {"runId": run_id, "leaseUpdatedAt": stamp(utcnow())})

    def _reconcile(self, m):
        """Recover a checkpoint saved just before the manifest update was interrupted."""
        snaps = {scope_key(s): s for s in self.snapshots(m["runId"])}
        if not snaps.keys() <= m["scopes"].keys():
            raise ValueError("已保存快照超出本轮范围，保留文件并停止")
        for key, scope in m["scopes"].items():
            if key in snaps:
                scope.update(status="complete", capturedAt=snaps[key]["capturedAt"])
                scope.pop("reason", None)
            elif scope["status"] == "complete":
                raise ValueError("完整状态缺少对应快照，需要检查存储，不能归档")
        write_json(self.locate(m["runId"]) / "manifest.json", m)

    def _work(self, m):
        limit = m["config"]["maxScopeRetries"] + 1
        return [{**s, "attemptsRemaining": max(0, limit - s.get("attempts", 0))}
                for s in m["scopes"].values()
                if s["status"] not in {"complete", "source_no_data"} and s.get("attempts", 0) < limit]

    def _progress(self, m):
        from .releases import ReleaseRecords
        pending = self._work(m)
        releases = None
        if not pending and m.get("releasePolicy") and not m.get("blocked"):
            releases = ReleaseRecords(self).prepare(m)
        ready = not pending and not m.get("blocked") and not (releases and releases["pending"])
        return {"runId": m["runId"], "sourceDate": m["date"], "status": m["status"],
                "phase": "blocked" if m.get("blocked") else "rankings" if pending else "ready" if ready else "releases",
                "blocked": m.get("blocked"), "canFinalize": ready, "pending": pending,
                "completedScopes": sum(s["status"] == "complete" for s in m["scopes"].values()),
                "plannedScopes": len(m["scopes"]), "release": releases}

    def _claim(self, m):
        token = secrets.token_hex(24)
        p = self.local / "tokens" / m["runId"]
        p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        p.write_text(token)
        p.chmod(0o600)
        m.pop("blocked", None)
        self._reconcile(m)
        self._touch(m["runId"])
        return {"runId": m["runId"], "token": token, "resumed": True,
                "relativePath": self.locate(m["runId"]).relative_to(self.root).as_posix(),
                "previousRunId": m["previousRunId"], "plannedScopes": len(m["scopes"]),
                "sourceDate": m["date"], "executionPolicy": EXECUTION_POLICY,
                "leaseExpiresAt": stamp(utcnow() + timedelta(minutes=WORKER_LEASE_MINUTES))}

    def start(self, at=None, run_id=None, origin="browser"):
        with self.guard():
            when = parse_time(at) if at else utcnow()
            clock = when if self.mode == "test" and at else utcnow()
            active_path = self.base / "active.json"
            state = self.state()
            latest = self.manifest(state["latestRunId"]) if state["latestRunId"] else None
            if origin == "legacy_import" and latest and latest["status"] == "running":
                raise ValueError("已有采集正在进行，不能导入历史或接管其执行权")
            if latest and latest["status"] == "running" and completion_driven(latest):
                if latest["configFingerprint"] != config_id(self.config):
                    raise ValueError("未结束轮次的配置与当前不同，请先恢复原配置续跑或人工取消")
                if active_path.exists():
                    active = read_json(active_path)
                    if active["runId"] != latest["runId"]:
                        if self.manifest(active["runId"])["status"] == "running":
                            raise ValueError("运行标记冲突，保留现场并停止")
                        active_path.unlink()
                    elif not latest.get("blocked") and clock < parse_time(active["leaseUpdatedAt"]) + timedelta(minutes=WORKER_LEASE_MINUTES):
                        return {"skipped": True, "runId": latest["runId"], "reason": "当前轮次执行者仍持有租约，等待其完成", "resumable": True}
                return self._claim(latest)
            if not active_path.exists() and state["latestRunId"] and self.manifest(state["latestRunId"])["status"] == "running":
                self._seal(state["latestRunId"], "interrupted")
            if active_path.exists():
                active = read_json(active_path)
                active_manifest = self.manifest(active["runId"])
                if active_manifest["status"] != "running":
                    active_path.unlink()
                elif clock < parse_time(active_manifest["startedAt"]) + timedelta(minutes=active_manifest["config"]["maxRunMinutes"]):
                    return {"skipped": True, "runId": active["runId"], "reason": "已有任务进行中"}
                else:
                    self._seal(active["runId"], "interrupted")
            state = self.state()
            if state["runs"] and when <= parse_time(self.manifest(state["latestRunId"])["startedAt"]):
                raise ValueError("运行时间必须晚于上一正式记录；历史导入应从空目录按顺序进行")
            run_id = run_id or when.strftime("%Y%m%dT%H%M%S%fZ")
            if not re.fullmatch(r"\d{8}T\d{6}(?:\d{6})?Z", run_id) or any(x["runId"] == run_id for x in state["runs"]):
                raise ValueError("运行编号无效或已存在")
            day = when.astimezone(ZoneInfo(self.config["timezone"])).date().isoformat()
            path = self.base / day / "runs" / run_id
            relative = path.relative_to(self.root).as_posix()
            token = secrets.token_hex(24)
            token_path = self.local / "tokens" / run_id
            token_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            token_path.write_text(token)
            token_path.chmod(0o600)
            try:
                version = subprocess.check_output(["git", "describe", "--always", "--dirty"], cwd=self.root, stderr=subprocess.DEVNULL, text=True).strip()
            except (OSError, subprocess.CalledProcessError):
                version = "uncommitted"
            m = {"schemaVersion": SCHEMA, "runId": run_id, "previousRunId": state["latestRunId"],
                 "mode": self.mode, "origin": origin, "status": "running", "date": day,
                 "startedAt": stamp(when), "finishedAt": None, "timezone": self.config["timezone"],
                 "codeVersion": version, "ruleVersion": self.config["ruleVersion"],
                 "configFingerprint": config_id(self.config), "config": self.config,
                 "scopes": {scope_key(s): {**s, "status": "pending"}
                            for s in scopes(self.config, include_inactive=origin == "legacy_import")}}
            if origin == "browser":
                m["comparisonVersion"] = COMPARISON_VERSION
                if self.config.get("executionPolicy") == EXECUTION_POLICY:
                    m["executionPolicy"] = EXECUTION_POLICY
            from .releases import policy_for
            policy = policy_for(self.config, day) if origin == "browser" else None
            if policy:
                m["releasePolicy"] = policy
            write_json(path / "manifest.json", m)
            state["runs"].append({"runId": run_id, "relativePath": relative})
            state["latestRunId"] = run_id
            write_json(self.base / "state.json", state)
            write_json(active_path, {"runId": run_id, "leaseUpdatedAt": stamp(utcnow())})
            result = {"runId": run_id, "token": token, "relativePath": relative, "previousRunId": m["previousRunId"], "plannedScopes": len(m["scopes"])}
            if completion_driven(m):
                result.update(executionPolicy=EXECUTION_POLICY, sourceDate=day,
                              leaseExpiresAt=stamp(utcnow() + timedelta(minutes=WORKER_LEASE_MINUTES)))
            else:
                result["deadlineAt"] = stamp(when + timedelta(minutes=self.config["maxRunMinutes"]))
            return result

    def pending(self, run_id):
        m = self.manifest(run_id)
        if completion_driven(m):
            saved = {scope_key(s) for s in self.snapshots(run_id)}
            return [s for s in self._work(m) if scope_key(s) not in saved]
        return [s for s in m["scopes"].values() if s["status"] != "complete"]

    def resume(self, run_id, token):
        with self.guard():
            m = self._authorize(run_id, token, restore_active=True)
            if completion_driven(m):
                m.pop("blocked", None)
                self._reconcile(m)
                self._touch(run_id)
                return self._progress(m)
            write_json(self.base / "active.json", {"runId": run_id, "leaseUpdatedAt": stamp(utcnow())})
            deadline = parse_time(m["startedAt"]) + timedelta(minutes=m["config"]["maxRunMinutes"])
            return {"runId": run_id, "completedScopes": sum(s["status"] == "complete" for s in m["scopes"].values()), "pending": self.pending(run_id),
                    "deadlineAt": stamp(deadline), "windowExpired": utcnow() >= deadline}

    def ingest(self, run_id, token, raw):
        with self.guard():
            m = self._authorize(run_id, token)
            key = scope_key(raw)
            if key not in m["scopes"]:
                raise ValueError("快照不在本轮计划内")
            snap = normalize_snapshot(raw, m["scopes"][key], m["config"], m["startedAt"], self.mode == "test",
                                      fixed_source_day=m["date"] if completion_driven(m) else None)
            file = self.locate(run_id) / "checkpoints" / (hashlib.sha256(key.encode()).hexdigest() + ".json")
            existing = next((s for s in self.snapshots(run_id) if scope_key(s) == key), None)
            if existing is not None and existing != snap:
                raise ValueError("该范围已有完整快照；不能用后采数据覆盖")
            if completion_driven(m) and existing is None and key not in {scope_key(s) for s in self._work(m)}:
                raise ValueError("该范围已结束或已用完重试次数")
            write_json(file, snap)
            m["scopes"][key].update(status="complete", capturedAt=snap["capturedAt"])
            m["scopes"][key].pop("reason", None)
            write_json(self.locate(run_id) / "manifest.json", m)
            write_json(self.base / "active.json", {"runId": run_id, "leaseUpdatedAt": stamp(utcnow())})
            return {"accepted": True, "scope": key, "rows": len(snap["rows"])}

    def mark(self, run_id, token, scope, status, reason, *, attempt_id=None, source_date=None):
        if status not in TERMINAL or not reason.strip():
            raise ValueError("范围失败必须提供明确状态和原因")
        with self.guard():
            m = self._authorize(run_id, token)
            key = scope_key(scope)
            if key not in m["scopes"] or m["scopes"][key]["status"] == "complete":
                raise ValueError("范围无效或已存在完整快照")
            item = m["scopes"][key]
            if completion_driven(m):
                if status in {"auth_required", "interrupted"}:
                    raise ValueError("全局登录或工具受阻请suspend，不能将全部范围标失败归档")
                if status == "source_no_data":
                    if source_date != m["date"]:
                        raise ValueError("缺数证据必须对应本轮固定来源日期")
                    item["sourceDate"] = source_date
                else:
                    if not isinstance(attempt_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", attempt_id):
                        raise ValueError("每次真实失败须提供唯一attemptId；重发使用原ID")
                    ids = item.get("attemptIds", [])
                    if attempt_id in ids:
                        return {"marked": key, "status": item["status"], "attempts": item["attempts"], "replayed": True}
                    if item["status"] == "source_no_data" or len(ids) >= m["config"]["maxScopeRetries"] + 1:
                        raise ValueError("该范围已结束或已用完重试次数")
                    item.update(attemptIds=[*ids, attempt_id], attempts=len(ids) + 1)
            item.update(status=status, reason=reason[:500])
            write_json(self.locate(run_id) / "manifest.json", m)
            self._touch(run_id)
            return {"marked": key, "status": status}

    def progress(self, run_id, token):
        with self.guard():
            m = self._authorize(run_id, token, restore_active=True)
            if not completion_driven(m):
                raise ValueError("旧轮次不支持完成驱动进度，请使用resume")
            self._reconcile(m)
            if not m.get("blocked"):
                self._touch(run_id)
            return self._progress(m)

    def suspend(self, run_id, token, reason):
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("暂停续跑必须提供阻塞原因")
        with self.guard():
            m = self._authorize(run_id, token, restore_active=True)
            if not completion_driven(m):
                raise ValueError("旧轮次不支持suspend")
            m["blocked"] = m["lastBlock"] = {"reason": reason[:500], "at": stamp(utcnow())}
            write_json(self.locate(run_id) / "manifest.json", m)
            active = self.base / "active.json"
            if active.exists():
                active.unlink()
            return {"runId": run_id, "status": "running", "blocked": m["blocked"], "resumable": True}

    def prepare_release(self, run_id, token):
        from .releases import ReleaseRecords
        with self.guard():
            m = self._authorize(run_id, token, require_window=True)
            if not m.get("releasePolicy"):
                raise ValueError("旧规则运行不补查首次发布日期")
            if completion_driven(m):
                self._reconcile(m)
                if self._work(m):
                    raise ValueError("榜单仍有可执行项，请先完成榜单采集")
            result = ReleaseRecords(self).prepare(m)
            self._touch(run_id)
            return result

    def ingest_release(self, run_id, token, raw):
        from .releases import ReleaseRecords
        with self.guard():
            m = self._authorize(run_id, token, require_window=True)
            if not m.get("releasePolicy"):
                raise ValueError("旧规则运行不补查首次发布日期")
            if completion_driven(m) and self._work(m):
                raise ValueError("榜单仍有可执行项，请先完成榜单采集")
            result = ReleaseRecords(self).ingest(m, raw)
            self._touch(run_id)
            return result

    def snapshots(self, run_id):
        path = self.locate(run_id)
        archive = path / "snapshots.jsonl.gz"
        merged = {}
        if archive.exists():
            with gzip.open(archive, "rt", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        snap = json.loads(line)
                        merged[scope_key(snap)] = snap
        for file in sorted((path / "checkpoints").glob("*.json")):
            snap = read_json(file)
            key = scope_key(snap)
            if key in merged and merged[key] != snap:
                raise ValueError("归档和恢复分片冲突，保留文件并停止")
            merged[key] = snap
        return list(merged.values())

    def _seal(self, run_id, remaining="failed"):
        path = self.locate(run_id)
        m = self.manifest(run_id)
        if m["status"] != "running":
            return m
        snaps = self.snapshots(run_id)
        tmp = path / "snapshots.jsonl.gz.tmp"
        with tmp.open("wb") as raw_file:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw_file, mtime=0) as gz:
                for snap in sorted(snaps, key=scope_key):
                    gz.write((json.dumps(snap, ensure_ascii=False, separators=(",", ":")) + "\n").encode())
        os.replace(tmp, path / "snapshots.jsonl.gz")
        for s in m["scopes"].values():
            saved = next((snap for snap in snaps if scope_key(snap) == scope_key(s)), None)
            if saved:
                s.update(status="complete", capturedAt=saved["capturedAt"])
                s.pop("reason", None)
            elif s["status"] == "complete":
                s.update(status="interrupted", reason="完整状态缺少对应快照，需要检查存储")
            if s["status"] == "pending":
                s.update(status=remaining, reason="任务结束前未取得有效快照")
        m["status"] = "complete" if len(snaps) == len(m["scopes"]) else "partial" if snaps else "failed"
        m["finishedAt"] = max([m["startedAt"], *[s["capturedAt"] for s in snaps]], key=parse_time)
        if m.get("releasePolicy"):
            from .releases import ReleaseRecords
            report = ReleaseRecords(self).freeze(m)
            if report["releasePending"] and m["status"] == "complete":
                m["status"] = "partial"
            observations = [r["observedAt"] for r in report["releaseRecords"].values()
                            if r.get("observedAt") and r.get("origin") == "browser"]
            m["finishedAt"] = max([m["finishedAt"], *observations], key=parse_time)
        m["sealedAt"] = stamp(utcnow())
        write_json(path / "manifest.json", m)
        active = self.base / "active.json"
        if active.exists() and read_json(active)["runId"] == run_id:
            active.unlink()
        if (path / "checkpoints").exists():
            shutil.rmtree(path / "checkpoints")
        return m

    def finalize(self, run_id, token):
        with self.guard():
            m = self._authorize(run_id, token)
            if completion_driven(m):
                self._reconcile(m)
                if not self._progress(m)["canFinalize"]:
                    raise ValueError("本轮仍有可执行项或全局阻塞，不能提前归档；请继续采集或suspend等待续跑")
            m = self._seal(run_id)
            # Keep index generation serialized with start/ingest and other renderers.
            self._render()
        return {"runId": run_id, "status": m["status"], "report": (self.locate(run_id) / "report.md").relative_to(self.root).as_posix()}

    def abort(self, run_id, token, reason):
        """Only for an explicit human cancellation, never an automatic recovery path."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("人工取消必须提供原因")
        with self.guard():
            m = self._authorize(run_id, token, restore_active=True, allow_config_change=True)
            m["abortReason"] = reason[:500]
            m.pop("blocked", None)
            write_json(self.locate(run_id) / "manifest.json", m)
            m = self._seal(run_id, "interrupted")
            self._render()
            return {"runId": run_id, "status": m["status"], "abortReason": m["abortReason"]}

    def _compare_rank(self, run_id):
        m = self.manifest(run_id)
        version = m.get("comparisonVersion")
        if version not in [None, COMPARISON_VERSION]:
            raise ValueError("排名比较版本不受支持，保留数据并停止")
        current = {scope_key(s): s for s in self.snapshots(run_id)}
        previous = {}
        if m["previousRunId"]:
            pm = self.manifest(m["previousRunId"])
            if version == COMPARISON_VERSION:
                previous = {scope_key(s): s for s in self.snapshots(pm["runId"])
                            if scope_key(s) in m["scopes"] and shared_scope_compatible(m, pm, scope_key(s))}
            elif pm["configFingerprint"] == m["configFingerprint"]:
                previous = {scope_key(s): s for s in self.snapshots(pm["runId"])}
        entries, comparison = {}, []
        for key, status in m["scopes"].items():
            comparable = key in current and key in previous
            comparison.append({**status, "comparison": "comparable" if comparable else "unknown",
                               "previousComplete": key in previous})
            if not comparable:
                continue
            old = {r["appId"] for r in previous[key]["rows"]}
            for row in current[key]["rows"]:
                if row["appId"] in old:
                    continue
                identity = (status["country"], status["categoryFamily"], status["genreId"], row["appId"])
                e = entries.setdefault(identity, {**row, **{k: status[k] for k in ["country", "categoryFamily", "category", "genreId"]},
                                                  "triggers": [], "ranks": {}, "capturedAt": current[key]["capturedAt"]})
                e["triggers"].append(status["chart"])
        for e in entries.values():
            e["triggers"].sort()
            e["rankStates"] = {}
            for chart in m["config"]["charts"]:
                key = scope_key({**e, "chart": chart})
                row = next((r for r in current.get(key, {}).get("rows", []) if r["appId"] == e["appId"]), None)
                e["ranks"][chart] = row["position"] if row else None
                e["rankStates"][chart] = "ranked" if row else "outside_top80" if key in current else "unavailable"
        comparable = sum(s["comparison"] == "comparable" for s in comparison)
        result = {"schemaVersion": SCHEMA, "run": {k: v for k, v in m.items() if k not in ["scopes", "config"]},
                  "entries": sorted(entries.values(), key=lambda e: (e["country"], e["categoryFamily"], e["genreId"], min(e["ranks"][x] for x in e["triggers"]), e["appId"])),
                  "coverage": {"plannedScopes": len(comparison), "completeScopes": len(current), "comparableScopes": comparable,
                               "unknownScopes": len(comparison) - comparable, "scopes": comparison},
                  "counts": {"confirmedNew": len(entries) if comparable else None}}
        for chart in m["config"]["charts"]:
            any_comparable = any(s["chart"] == chart and s["comparison"] == "comparable" for s in comparison)
            result["counts"][chart + "New"] = sum(chart in e["triggers"] for e in entries.values()) if any_comparable else None
        return result

    def compare(self, run_id):
        report = self._compare_rank(run_id)
        manifest = self.manifest(run_id)
        if manifest.get("releasePolicy"):
            from .releases import ReleaseRecords, apply_admission, read_records
            report = apply_admission(report, manifest, read_records(ReleaseRecords(self).path(run_id)))
        return report

    def render(self):
        with self.guard():
            return self._render()

    def _render(self):
        from .reports import render_all
        return render_all(self)
