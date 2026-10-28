"""Import saved snapshots chronologically, without modifying the source."""
from .core import UNFINISHED, normalize_snapshot, parse_time, read_json, scope_key, scopes


def import_legacy(store, source):
    document = read_json(source)
    if not isinstance(document, dict) or not document.get("snapshots"):
        raise ValueError("输入应包含非空snapshots数组以及run元数据")
    run_id = document.get("run", {}).get("runId")
    if not run_id:
        raise ValueError("历史数据缺少run.runId")
    if any(r["runId"] == run_id for r in store.state()["runs"]):
        if store.manifest(run_id)["status"] in UNFINISHED:
            raise ValueError("该导入尚未结束；请使用原令牌恢复")
        return {"skipped": True, "runId": run_id, "reason": "该运行编号已导入"}
    started = min((s["capturedAt"] for s in document["snapshots"]), key=parse_time)
    planned = {scope_key(s): s for s in scopes(store.config, include_inactive=True)}
    normalized, seen = [], set()
    for old in document["snapshots"]:
        genre = old["sourceUrl"].split("/genre/")[-1].split("/")[0]
        raw = {**old, "genreId": genre, "device": "iphone"}
        key = scope_key(raw)
        if key not in planned or key in seen:
            raise ValueError("历史快照有未知或重复范围")
        seen.add(key)
        normalized.append(normalize_snapshot(raw, planned[key], store.config, started, store.mode == "test"))
    run = store.start(started, run_id=run_id, origin="legacy_import")
    if run.get("skipped"):
        raise ValueError("已有采集正在进行，不能插入历史")
    for snap in normalized:
        store.ingest(run_id, run["token"], snap)
    for missing in document.get("unavailable", []):
        for scope in planned.values():
            if all(scope[k] == missing.get(k) for k in ["country", "category", "chart"]):
                store.mark(run_id, run["token"], scope, "source_no_data", missing.get("reason", "历史来源无数据"))
    store.finalize(run_id, run["token"])
    return {"runId": run_id, "importedScopes": len(normalized), "relativePath": run["relativePath"]}
