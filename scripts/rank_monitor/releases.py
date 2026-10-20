"""Qimai earliest-release evidence and reproducible calendar-month admission."""
from calendar import monthrange
from datetime import date, timedelta
import re
from urllib.parse import urlparse

from .core import parse_time, read_json, stamp, utcnow, write_json

POLICY_VERSION = "qimai_first_release_6_months_v1"
LABEL = "最早发布"
EVIDENCE_VERSION = 1


def month_cutoff(reference, months):
    if type(months) is not int or not 1 <= months <= 6:
        raise ValueError("上线筛选月份必须为1至6")
    year, month = divmod(reference.year * 12 + reference.month - 1 - months, 12)
    month += 1
    return date(year, month, min(reference.day, monthrange(year, month)[1]))


def release_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("最早发布日期必须为YYYY-MM-DD")
    return date.fromisoformat(value)


def policy_for(config, reference_day):
    setting = config.get("releasePolicy")
    if setting is None:
        return None
    if setting != {"version": POLICY_VERSION, "months": 6, "sourceLabel": LABEL}:
        raise ValueError("上线准入策略不受支持")
    reference = release_date(reference_day)
    return {**setting, "referenceDate": reference.isoformat(),
            "windowStarts": {str(n): month_cutoff(reference, n).isoformat() for n in range(1, 7)}}


def release_key(entry):
    return entry["country"] + ":" + str(entry["appId"])


def read_records(path):
    if not path.exists():
        return {}
    data = read_json(path)
    if data.get("schemaVersion") != EVIDENCE_VERSION or not isinstance(data.get("records"), dict):
        raise ValueError("上线日期记录格式不兼容，保留文件并停止")
    return data["records"]


def save_records(path, records):
    write_json(path, {"schemaVersion": EVIDENCE_VERSION, "records": records})


def normalize_evidence(raw, expected, manifest, from_cache=False):
    if raw.get("country") != expected["country"] or str(raw.get("appId")) != expected["appId"]:
        raise ValueError("上线日期的国家或App ID不匹配")
    url = urlparse(raw.get("sourceUrl", ""))
    path = rf"/app/(?:rank|baseinfo)/appid/{expected['appId']}/country/{expected['country']}/?"
    if url.scheme != "https" or url.hostname != "www.qimai.cn" or url.username or not re.fullmatch(path, url.path):
        raise ValueError("上线日期来源URL与App范围不匹配")
    observed = parse_time(raw["observedAt"])
    started = parse_time(manifest["startedAt"])
    deadline = started + timedelta(minutes=manifest["config"]["maxRunMinutes"])
    if not from_cache and not started <= observed <= deadline:
        raise ValueError("发布日期核实时间不在本轮90分钟窗口内")
    if manifest["mode"] != "test" and observed > utcnow():
        raise ValueError("发布日期核实时间不能晚于当前时间")
    status = raw.get("status")
    if status not in {"verified", "unavailable", "failed"}:
        raise ValueError("上线日期状态不受支持")
    label, text = raw.get("sourceLabel", ""), raw.get("rawText", "")
    if not isinstance(label, str) or not isinstance(text, str):
        raise ValueError("来源标签和原始文本必须为文字")
    record = {"country": expected["country"], "appId": expected["appId"], "status": status,
              "releaseDate": None, "sourceUrl": raw["sourceUrl"], "sourceLabel": label,
              "rawText": text, "observedAt": stamp(observed), "origin": "cache" if from_cache else "browser"}
    if status == "verified":
        if str(raw.get("pageAppId")) != expected["appId"] or raw.get("pageCountry") != manifest["config"]["countries"][expected["country"]]:
            raise ValueError("详情页App ID或国家证据不一致")
        if label != LABEL:
            raise ValueError("必须读取明确标注的最早发布，不能使用版本更新日期")
        published = release_date(raw.get("releaseDate"))
        if LABEL not in text or re.findall(r"\d{4}-\d{2}-\d{2}", text) != [published.isoformat()]:
            raise ValueError("最早发布日期与原始页面文字不一致")
        record.update(pageAppId=expected["appId"], pageCountry=raw["pageCountry"])
        if published > release_date(manifest["releasePolicy"]["referenceDate"]):
            record.update(status="unavailable", reason="来源最早发布日期晚于本轮参考日")
        else:
            record["releaseDate"] = published.isoformat()
    else:
        reason = raw.get("reason", "")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("未知或失败的上线日期必须提供原因")
        record["reason"] = reason[:500]
    return record


def apply_admission(report, manifest, records):
    """Filter only rank candidates; rank coverage and archived top80 stay intact."""
    policy = manifest["releasePolicy"]
    cutoff, reference = policy["windowStarts"]["6"], policy["referenceDate"]
    candidates, qualified, pending, older = report["entries"], [], [], []
    for entry in candidates:
        record = records.get(release_key(entry), {})
        known = record.get("status") == "verified" and bool(record.get("releaseDate"))
        enriched = {**entry, "releaseDate": record.get("releaseDate"),
                    "releaseStatus": record.get("status", "unavailable"),
                    "releaseSourceUrl": record.get("sourceUrl", entry["qimaiUrl"]),
                    "releaseObservedAt": record.get("observedAt"), "releaseOrigin": record.get("origin"),
                    "releaseReason": record.get("reason", "" if known else "本轮未取得可核实的最早发布日期")}
        if not known or record["releaseDate"] > reference:
            pending.append(enriched)
        elif record["releaseDate"] < cutoff:
            older.append(enriched)
        else:
            qualified.append(enriched)
    report["entries"], report["releasePending"] = qualified, pending
    report["releaseRecords"] = records
    comparable = report["coverage"]["comparableScopes"] > 0
    known_count = len(qualified) + len(older)
    report["counts"].update(rankCandidates=len(candidates) if comparable else None,
                            confirmedNew=len(qualified) if comparable and (not candidates or known_count) else None,
                            excludedOlder=len(older) if comparable else None,
                            pendingRelease=len(pending) if comparable else None)
    for chart in manifest["config"]["charts"]:
        chart_comparable = any(s["chart"] == chart and s["comparison"] == "comparable" for s in report["coverage"]["scopes"])
        has_candidates = any(chart in e["triggers"] for e in candidates)
        has_known = any(chart in e["triggers"] for e in [*qualified, *older])
        report["counts"][chart + "New"] = sum(chart in e["triggers"] for e in qualified) if chart_comparable and (not has_candidates or has_known) else None
    return report


class ReleaseRecords:
    def __init__(self, store):
        self.store = store
        self.cache_path = store.base / "release-cache.json"

    def path(self, run_id):
        return self.store.locate(run_id) / "releases.json"

    def candidates(self, run_id):
        return self.store._compare_rank(run_id)["entries"]

    def prepare(self, manifest):
        candidates = {release_key(e): e for e in self.candidates(manifest["runId"])}
        records = read_records(self.path(manifest["runId"]))
        cache = read_records(self.cache_path)
        for key, entry in candidates.items():
            if records.get(key, {}).get("status") == "verified" or key not in cache:
                continue
            try:
                record = normalize_evidence(cache[key], entry, manifest, from_cache=True)
            except (ValueError, KeyError, TypeError):
                continue
            if record["status"] == "verified":
                record["attempts"] = 0
                records[key] = record
        save_records(self.path(manifest["runId"]), records)
        limit = manifest["config"]["maxScopeRetries"] + 1
        targets = [{"country": e["country"], "appId": e["appId"], "name": e["name"], "sourceUrl": e["qimaiUrl"],
                    "attempts": records.get(key, {}).get("attempts", 0),
                    "attemptsRemaining": max(0, limit - records.get(key, {}).get("attempts", 0))}
                   for key, e in candidates.items() if records.get(key, {}).get("status") != "verified"]
        pending = [e for e in targets if e["attemptsRemaining"]]
        return {"runId": manifest["runId"], "pending": pending,
                "candidateApps": len(candidates), "verifiedApps": len(candidates) - len(targets),
                "exhaustedApps": len(targets) - len(pending)}

    def ingest(self, manifest, raw):
        candidates = {release_key(e): e for e in self.candidates(manifest["runId"])}
        key = release_key(raw)
        if key not in candidates:
            raise ValueError("该App不是本轮排名新进候选")
        record = normalize_evidence(raw, candidates[key], manifest)
        path = self.path(manifest["runId"])
        records = read_records(path)
        existing = records.get(key)
        existing_evidence = {k: v for k, v in (existing or {}).items() if k != "attempts"}
        if existing_evidence == record:
            return {"accepted": True, "app": key, "status": record["status"]}
        if existing and existing["status"] == "verified":
            raise ValueError("该App已有已核实的发布日期，不能覆盖")
        attempts = (existing or {}).get("attempts", 0)
        if attempts >= manifest["config"]["maxScopeRetries"] + 1:
            raise ValueError("该App的发布日期补查已用完本轮重试次数")
        record["attempts"] = attempts + 1
        records[key] = record
        save_records(path, records)
        if record["status"] == "verified":
            cache = read_records(self.cache_path)
            cache[key] = record
            save_records(self.cache_path, cache)
        return {"accepted": True, "app": key, "status": record["status"]}

    def freeze(self, manifest):
        records = read_records(self.path(manifest["runId"]))
        for entry in self.candidates(manifest["runId"]):
            key = release_key(entry)
            if key not in records:
                records[key] = {"country": entry["country"], "appId": entry["appId"], "status": "unavailable",
                                "releaseDate": None, "sourceUrl": entry["qimaiUrl"], "sourceLabel": LABEL,
                                "rawText": "", "observedAt": None, "origin": "unobserved",
                                "attempts": 0,
                                "reason": "本轮结束前未取得可核实的最早发布日期"}
        save_records(self.path(manifest["runId"]), records)
        report = apply_admission(self.store._compare_rank(manifest["runId"]), manifest, records)
        return report
