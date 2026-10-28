"""Markdown navigation and self-contained daily HTML; all output stays in data/."""
from collections import defaultdict
from datetime import datetime
import html
import json
import os
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from .core import UNFINISHED, write_json


def write_text(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(value, encoding="utf-8")
    os.replace(temp, path)


def clock(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")


def cell(value):
    text = html.escape(str(value if value not in [None, ""] else "—"))
    for char in "\\|[]":
        text = text.replace(char, "\\" + char)
    return text.replace("\n", " ").replace("\r", " ")


def count(value):
    return "0" if value is None else str(value)


def report_count(report, key="confirmedNew"):
    value = report["counts"][key]
    if value is not None or not report["run"].get("releasePolicy"):
        return count(value)
    chart = key.removesuffix("New") if key in ["freeNew", "grossingNew"] else None
    comparable = any(s["comparison"] == "comparable" and (chart is None or s["chart"] == chart)
                     for s in report["coverage"]["scopes"])
    return "待核实" if comparable else "0"


def duration(seconds):
    seconds = round(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return "".join(f"{n}{unit}" for n, unit in [(days, "天"), (hours, "小时"), (minutes, "分"), (seconds, "秒")] if n) or "0秒"


def baseline_text(baseline):
    text = (f"基准 {baseline['baselineRunId']}；来源日期 {baseline['sourceDate']}；"
            f"基准采集 {clock(baseline['capturedAt'])}（北京时间）")
    if "elapsedSeconds" in baseline:
        text += f"；本轮采集 {clock(baseline['currentCapturedAt'])}；实际跨度 {duration(baseline['elapsedSeconds'])}"
    return text + f"；跨过{baseline['skippedRuns']}轮"


def entry_baselines(entry, config):
    return "；".join(config["charts"][chart]+"："+baseline_text(baseline)
                    for chart, baseline in sorted(entry.get("comparisonBaselines", {}).items()))


def release_period(policy):
    return "一年" if policy["months"] == 12 else "六个月"


def markdown_report(report, config):
    r, coverage = report["run"], report["coverage"]
    policy = r.get("releasePolicy")
    latest = r.get("comparisonVersion") == "latest_valid_scope_v1"
    lines = [f"# iOS 榜单监测 · {clock(r['startedAt'])}", "",
             "[返回日期页](../../index.md) · [返回总览](../../../index.md) · [筛选查看](report.html)", "",
             f"- 运行编号：{r['runId']}；状态：{r['status']}；来源：{r['origin']}",
             f"- 上一任务：{r['previousRunId'] or '无（建立基线）'}；规则版本：{r['ruleVersion']}",
             f"- 完整榜单：{coverage['completeScopes']}/{coverage['plannedScopes']}；可比较：{coverage['comparableScopes']}；待比较：{coverage['unknownScopes']}",
             f"- 已确认新进：{report_count(report, 'confirmedNew')}；免费榜：{report_count(report, 'freeNew')}；畅销榜：{report_count(report, 'grossingNew')}", "",
             "无排名比较依据时数量显示0，具体原因见覆盖说明；已确认数量仅覆盖可比较范围。前次未在前80内，不代表已知前次具体名次。", ""]
    if r.get("cancelled"):
        lines += ["- 人工取消：" + cell(r.get("abortReason")), ""]
    if latest:
        lines += [f"- 比较口径：每榜单最近有效完整快照，无回溯期限；回溯比较 {coverage['fallbackScopes']}；无历史基准 {coverage['noBaselineScopes']}；本轮缺数 {coverage['currentMissingScopes']}",
                  "上一任务仅表示执行顺序，各榜单实际基准见下表。仅比较两个观测端点，不能推断中间变化或首次进榜时间。", ""]
    if policy:
        lines += [f"- 上线准入：七麦最早发布 {policy['windowStarts'][str(policy['months'])]} 至 {policy['referenceDate']}（包含边界日）；策略：{policy['version']}",
                  f"- 排名候选：{count(report['counts']['rankCandidates'])}；超过{release_period(policy)}排除：{count(report['counts']['excludedOlder'])}；首次发布日期待核实：{count(report['counts']['pendingRelease'])}", ""]
    grouped = defaultdict(list)
    for e in report["entries"]:
        grouped[(e["categoryFamily"], e["category"], e["country"])].append(e)
    for (family, category, country), entries in sorted(grouped.items()):
        release_header = " 最早发布 |" if policy else ""
        lines += [f"## {cell(family)} / {cell(category)} · {config['countries'][country]}", "",
                  f"| App | 开发者 | 免费榜 | 畅销榜 | 触发 | 评分 / 数量 |{release_header} 版本日期 |" + (" 比较基准 / 实际跨度 |" if latest else ""),
                  "|---|---|---:|---:|---|---|" + ("---|" if policy else "") + "---|" + ("---|" if latest else "")]
        for e in entries:
            rank = lambda chart: e["ranks"][chart] or ("前80以外/未上榜" if e["rankStates"][chart] == "outside_top80" else "缺数")
            release_value = f" {cell(e.get('releaseDate'))} |" if policy else ""
            lines.append(f"| [{cell(e['name'])}]({e['qimaiUrl']}) · {e['appId']} | {cell(e['developer'])} | {rank('free')} | {rank('grossing')} | {'、'.join(config['charts'][x] for x in e['triggers'])} | {cell(e['rating'])} / {cell(e['ratingCount'])} |{release_value} {cell(e['latestVersionDate'])} |" + (f" {cell(entry_baselines(e, config))} |" if latest else ""))
        lines.append("")
    if not grouped:
        lines += ["本轮没有已确认的新进记录。请结合上方可比较范围判断是否为基线或缺数。", ""]
    if policy and report["releasePending"]:
        lines += ["## 首次发布日期待核实", "", "| App | 国家 / 分类 | 原因 |", "|---|---|---|"]
        for e in report["releasePending"]:
            lines.append(f"| [{cell(e['name'])}]({e['qimaiUrl']}) · {e['appId']} | {cell(config['countries'][e['country']])} / {cell(e['categoryFamily'])} / {cell(e['category'])} | {cell(e['releaseReason'])} |")
        lines.append("")
    lines += ["## 数据覆盖与异常", "", "| 国家 | 大类 / 分类 | 榜单 | 当前状态 | 比较 | 原因 |", "|---|---|---|---|---|---|"]
    for s in coverage["scopes"]:
        if s["status"] != "complete" or s["comparison"] == "unknown":
            reason = s.get("reason") or s.get("comparisonReason") or "上次缺少对应完整数据或规则不兼容"
            lines.append(f"| {config['countries'][s['country']]} | {s['categoryFamily']} / {s['category']} | {config['charts'][s['chart']]} | {cell(s['status'])} | {s['comparison']} | {cell(reason)} |")
    if latest:
        lines += ["", "## 各榜单比较基准与实际跨度", "", "| 国家 / 分类 | 榜单 | 基准与跨度 |", "|---|---|---|"]
        for s in coverage["scopes"]:
            if s["comparison"] == "comparable":
                lines.append(f"| {cell(config['countries'][s['country']])} / {cell(s['categoryFamily'])} / {cell(s['category'])} | {cell(config['charts'][s['chart']])} | {cell(baseline_text(s['baseline']))} |")
    return "\n".join(lines) + "\n"


def daily_contains(path, expected):
    """Both daily outputs must contain every archived run, including after a failed write."""
    try:
        text = (path/"index.html").read_text(encoding="utf-8")
        payload = json.loads(text.split('<script id="payload" type="application/json">', 1)[1].split('</script>', 1)[0])
        html_runs = {r["run"]["runId"] for r in payload["runs"]}
        markdown = (path/"index.md").read_text(encoding="utf-8")
        markdown_runs = set(re.findall(r'\]\(runs/([^/)]+)/report\.md\)', markdown))
        return payload["date"] == path.name and html_runs == markdown_runs == expected
    except (OSError, ValueError, IndexError, KeyError, TypeError):
        return False


def render_all(store):
    state = store.state()
    days = defaultdict(list)
    for entry in state["runs"]:
        m = store.manifest(entry["runId"])
        if m["status"] in UNFINISHED:
            continue
        path = store.locate(m["runId"])
        report = store.archived_report(m["runId"])
        if not (path/"report.md").exists():
            write_text(path / "report.md", markdown_report(report, m["config"]))
        target = "../../index.html#run=" + m["runId"]
        if not (path/"report.html").exists():
            write_text(path / "report.html", '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>打开单次报告</title>'
                       + f'<meta http-equiv="refresh" content="0;url={target}"><a href="{target}">打开此时点报告</a></html>')
        days[m["date"]].append({**report, "snapshots": store.snapshots(m["runId"])})
    dates = sorted(days, reverse=True)
    template = (store.root / "assets/report.html").read_text(encoding="utf-8")
    overview = ["# iOS 榜单监测总览", "", "[打开本地浏览入口](index.html)", "", "数据仅保存本机；时间均为北京时间。", "", "| 日期 | 最近采集 | 次数 | 最近已确认新进 |", "|---|---|---:|---:|"]
    archive_links = []
    for day in dates:
        runs = days[day]
        latest = runs[-1]
        date_path = store.base / day
        payload = {"date": day, "dates": dates, "config": store.config, "runs": runs}
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
        update_day = not daily_contains(date_path, {r["run"]["runId"] for r in runs})
        if update_day:
            write_text(date_path / "index.html", template.replace("__PAYLOAD__", encoded).replace("__DATE__", day))
        lines = [f"# {day} · iOS 榜单监测", "", "[返回总览](../index.md) · [筛选查看](index.html)", "", "| 北京时间 | 状态 | 完整榜单 | 可比较 | 已确认新进 | 报告 |", "|---|---|---:|---:|---:|---|"]
        for r in reversed(runs):
            meta, cov = r["run"], r["coverage"]
            status = "人工取消" if meta.get("cancelled") else meta["status"]
            lines.append(f"| {clock(meta['startedAt'])} | {status} | {cov['completeScopes']}/{cov['plannedScopes']} | {cov['comparableScopes']} | {report_count(r)} | [文字](runs/{meta['runId']}/report.md) · [筛选](runs/{meta['runId']}/report.html) |")
        if update_day:
            write_text(date_path / "index.md", "\n".join(lines) + "\n")
        overview.append(f"| [{day}]({day}/index.md) | {clock(latest['run']['startedAt'])} | {len(runs)} | {report_count(latest)} |")
        archive_links.append(f'<li><a href="{day}/index.html"><strong>{day}</strong><span>{len(runs)} 次采集 · 最近新进 {report_count(latest)}</span></a></li>')
    write_text(store.base / "index.md", "\n".join(overview) + "\n")
    write_text(store.base / "index.html", '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
               '<title>iOS 榜单监测 · 日期总览</title><style>body{background:#f6f5f0;color:#172f3b;font:16px "PingFang SC",sans-serif;max-width:850px;margin:70px auto;padding:24px}h1{font:40px "Songti SC",serif}small{letter-spacing:3px;color:#426e76}ul{padding:0;list-style:none}li a{color:inherit;text-decoration:none;display:flex;justify-content:space-between;border-top:1px solid #cad4d2;padding:24px 0}span,p{color:#53666c}a:focus{outline:2px solid #167684}</style>'
               '<small>APP SIGNALS / LOCAL ARCHIVE</small><h1>iOS 榜单监测</h1><p>选择日期，再按时点、国家和分类筛选。全部数据仅保存在本机。</p><ul>'
               + "".join(archive_links) + '</ul><p><a href="index.md">Markdown 总览</a></p></html>')
    return {"renderedRuns": sum(len(x) for x in days.values()), "dates": dates, "overview": (store.base / "index.md").relative_to(store.root).as_posix()}
