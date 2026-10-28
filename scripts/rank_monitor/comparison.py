"""Immutable per-scope references to the latest compatible archived observation."""
from .core import (UNFINISHED, completion_driven, digest, normalize_snapshot, parse_time,
                   read_json, scope_key, shared_scope_compatible, write_json)

LATEST_VERSION = "latest_valid_scope_v1"


class FrozenBaselines:
    def __init__(self, store):
        self.store = store

    def path(self, run_id):
        return self.store.locate(run_id)/"baselines.json"

    def _past(self, m):
        runs = self.store.state()["runs"]
        index = next(i for i, r in enumerate(runs) if r["runId"] == m["runId"])
        return runs[:index]

    def _snapshot(self, snap, manifest, key):
        try:
            clean = normalize_snapshot(snap, manifest["scopes"][key], manifest["config"],
                                       manifest["startedAt"], manifest["mode"] == "test",
                                       fixed_source_day=manifest["date"] if completion_driven(manifest) else None)
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ValueError("历史基准快照未通过完整性校验，保留文件并停止") from None
        if clean != snap:
            raise ValueError("历史基准快照格式不一致，不能作为完整数据使用")
        return snap

    def prepare(self, m):
        path = self.path(m["runId"])
        if m.get("comparisonBaselinesHash"):
            return self.read(m)
        if m["status"] not in UNFINISHED:
            raise ValueError("归档运行缺少已冻结基准，不能重新选择")
        if path.exists():
            data = read_json(path)
            self._shape(data, m)
        else:
            past = self._past(m)
            refs = {key: None for key in m["scopes"]}
            unresolved = set(refs)
            for index in range(len(past)-1, -1, -1):
                previous = self.store.manifest(past[index]["runId"])
                if previous["status"] in UNFINISHED:
                    raise ValueError("历史轮次尚未归档，不能作为比较基准")
                wanted = {key for key in unresolved
                          if key in previous["scopes"] and previous["scopes"][key]["status"] == "complete"
                          and shared_scope_compatible(m, previous, key)}
                if not wanted:
                    continue
                if not (self.store.locate(previous["runId"])/"snapshots.jsonl.gz").is_file():
                    raise ValueError("历史完整快照归档缺失，不能静默回溯")
                snaps = {scope_key(s): s for s in self.store.snapshots(previous["runId"])}
                for key in wanted:
                    if key not in snaps:
                        raise ValueError("历史完整状态缺少对应快照，不能静默回溯")
                    snap = self._snapshot(snaps[key], previous, key)
                    refs[key] = {"baselineRunId": previous["runId"], "sourceDate": snap["sourceDate"],
                                 "capturedAt": snap["capturedAt"], "snapshotHash": digest(snap),
                                 "skippedRuns": len(past)-1-index}
                    unresolved.remove(key)
                if not unresolved:
                    break
            data = {"schemaVersion": 1, "comparisonVersion": LATEST_VERSION,
                    "runId": m["runId"], "scopes": refs}
            write_json(path, data)
        # Recover an interruption after the reference file but before its manifest checksum.
        m["comparisonBaselinesHash"] = digest(data)
        write_json(self.store.locate(m["runId"])/"manifest.json", m)
        return data

    def _shape(self, data, m):
        if (not isinstance(data, dict) or data.get("schemaVersion") != 1
                or data.get("comparisonVersion") != LATEST_VERSION or data.get("runId") != m["runId"]
                or not isinstance(data.get("scopes"), dict) or set(data["scopes"]) != set(m["scopes"])):
            raise ValueError("本轮基准记录格式不一致，不能重新选择")

    def read(self, m):
        path = self.path(m["runId"])
        if not path.is_file():
            raise ValueError("本轮已冻结的基准记录缺失，不能重新选择")
        data = read_json(path)
        self._shape(data, m)
        if not m.get("comparisonBaselinesHash") or digest(data) != m["comparisonBaselinesHash"]:
            raise ValueError("本轮基准记录摘要不匹配，保留文件并停止")
        return data

    def snapshots(self, m):
        refs = self.read(m)["scopes"]
        past = {r["runId"] for r in self._past(m)}
        previous, loaded = {}, {}
        for key, ref in refs.items():
            if ref is None:
                continue
            run_id = ref["baselineRunId"]
            if run_id not in past:
                raise ValueError("比较基准不属于本轮之前的历史")
            if run_id not in loaded:
                pm = self.store.manifest(run_id)
                if pm["status"] in UNFINISHED or not (self.store.locate(run_id)/"snapshots.jsonl.gz").is_file():
                    raise ValueError("已选基准未归档或快照归档缺失")
                loaded[run_id] = (pm, {scope_key(s): s for s in self.store.snapshots(run_id)})
            pm, snaps = loaded[run_id]
            snap = snaps.get(key)
            if (snap is None or pm["scopes"].get(key, {}).get("status") != "complete"
                    or not shared_scope_compatible(m, pm, key) or digest(snap) != ref["snapshotHash"]
                    or snap["capturedAt"] != ref["capturedAt"] or snap["sourceDate"] != ref["sourceDate"]):
                raise ValueError("已选基准快照或规则与冻结摘要不一致，不能静默回溯")
            previous[key] = self._snapshot(snap, pm, key)
        return previous, refs


def interval(ref, current):
    elapsed = (parse_time(current["capturedAt"])-parse_time(ref["capturedAt"])).total_seconds()
    if elapsed < 0 or current["sourceDate"] < ref["sourceDate"]:
        raise ValueError("基准观测时间或来源日期晚于本轮，不能比较")
    return {**ref, "currentCapturedAt": current["capturedAt"], "currentSourceDate": current["sourceDate"],
            "elapsedSeconds": elapsed}
