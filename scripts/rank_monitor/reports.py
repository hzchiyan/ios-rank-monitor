"""Markdown navigation and self-contained daily HTML; all output stays in data/."""
from collections import defaultdict
from datetime import datetime
import html
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from .core import write_json


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
    return "待比较" if value is None else str(value)


def report_count(report, key="confirmedNew"):
    value = report["counts"][key]
    if value is not None or not report["run"].get("releasePolicy"):
        return count(value)
    chart = key.removesuffix("New") if key in ["freeNew", "grossingNew"] else None
    comparable = any(s["comparison"] == "comparable" and (chart is None or s["chart"] == chart)
                     for s in report["coverage"]["scopes"])
    return "待核实" if comparable else "待比较"


def markdown_report(report, config):
    r, coverage = report["run"], report["coverage"]
    policy = r.get("releasePolicy")
    lines = [f"# iOS 榜单监测 · {clock(r['startedAt'])}", "",
             "[返回日期页](../../index.md) · [返回总览](../../../index.md) · [筛选查看](report.html)", "",
             f"- 运行编号：{r['runId']}；状态：{r['status']}；来源：{r['origin']}",
             f"- 上一任务：{r['previousRunId'] or '无（建立基线）'}；规则版本：{r['ruleVersion']}",
             f"- 完整榜单：{coverage['completeScopes']}/{coverage['plannedScopes']}；可比较：{coverage['comparableScopes']}；待比较：{coverage['unknownScopes']}",
             f"- 已确认新进：{report_count(report, 'confirmedNew')}；免费榜：{report_count(report, 'freeNew')}；畅销榜：{report_count(report, 'grossingNew')}", "",
             "缺数与待比较不等于零；已确认数量仅覆盖可比较范围。前次未在前80内，不代表已知前次具体名次。", ""]
    if policy:
        lines += [f"- 上线准入：七麦最早发布 {policy['windowStarts']['6']} 至 {policy['referenceDate']}（包含边界日）；策略：{policy['version']}",
                  f"- 排名候选：{count(report['counts']['rankCandidates'])}；超过六个月排除：{count(report['counts']['excludedOlder'])}；首次发布日期待核实：{count(report['counts']['pendingRelease'])}", ""]
    grouped = defaultdict(list)
    for e in report["entries"]:
        grouped[(e["categoryFamily"], e["category"], e["country"])].append(e)
    for (family, category, country), entries in sorted(grouped.items()):
        release_header = " 最早发布 |" if policy else ""
        lines += [f"## {cell(family)} / {cell(category)} · {config['countries'][country]}", "",
                  f"| App | 开发者 | 免费榜 | 畅销榜 | 触发 | 评分 / 数量 |{release_header} 版本日期 |",
                  "|---|---|---:|---:|---|---|" + ("---|" if policy else "") + "---|"]
        for e in entries:
            rank = lambda chart: e["ranks"][chart] or ("前80以外/未上榜" if e["rankStates"][chart] == "outside_top80" else "缺数")
            release_value = f" {cell(e.get('releaseDate'))} |" if policy else ""
            lines.append(f"| [{cell(e['name'])}]({e['qimaiUrl']}) · {e['appId']} | {cell(e['developer'])} | {rank('free')} | {rank('grossing')} | {'、'.join(config['charts'][x] for x in e['triggers'])} | {cell(e['rating'])} / {cell(e['ratingCount'])} |{release_value} {cell(e['latestVersionDate'])} |")
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
            reason = s.get("reason", "上次缺少对应完整数据或规则不兼容")
            lines.append(f"| {config['countries'][s['country']]} | {s['categoryFamily']} / {s['category']} | {config['charts'][s['chart']]} | {cell(s['status'])} | {s['comparison']} | {cell(reason)} |")
    return "\n".join(lines) + "\n"


def render_all(store):
    state = store.state()
    days = defaultdict(list)
    for entry in state["runs"]:
        m = store.manifest(entry["runId"])
        if m["status"] == "running":
            continue
        report = store.compare(m["runId"])
        path = store.locate(m["runId"])
        write_json(path / "report-data.json", report)
        write_text(path / "report.md", markdown_report(report, m["config"]))
        target = "../../index.html#run=" + m["runId"]
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
        write_text(date_path / "index.html", template.replace("__PAYLOAD__", encoded).replace("__DATE__", day))
        lines = [f"# {day} · iOS 榜单监测", "", "[返回总览](../index.md) · [筛选查看](index.html)", "", "| 北京时间 | 状态 | 完整榜单 | 可比较 | 已确认新进 | 报告 |", "|---|---|---:|---:|---:|---|"]
        for r in reversed(runs):
            meta, cov = r["run"], r["coverage"]
            lines.append(f"| {clock(meta['startedAt'])} | {meta['status']} | {cov['completeScopes']}/{cov['plannedScopes']} | {cov['comparableScopes']} | {report_count(r)} | [文字](runs/{meta['runId']}/report.md) · [筛选](runs/{meta['runId']}/report.html) |")
        write_text(date_path / "index.md", "\n".join(lines) + "\n")
        overview.append(f"| [{day}]({day}/index.md) | {clock(latest['run']['startedAt'])} | {len(runs)} | {report_count(latest)} |")
        archive_links.append(f'<li><a href="{day}/index.html"><strong>{day}</strong><span>{len(runs)} 次采集 · 最近新进 {report_count(latest)}</span></a></li>')
    write_text(store.base / "index.md", "\n".join(overview) + "\n")
    write_text(store.base / "index.html", '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
               '<title>iOS 榜单监测 · 日期总览</title><style>body{background:#f6f5f0;color:#172f3b;font:16px "PingFang SC",sans-serif;max-width:850px;margin:70px auto;padding:24px}h1{font:40px "Songti SC",serif}small{letter-spacing:3px;color:#426e76}ul{padding:0;list-style:none}li a{color:inherit;text-decoration:none;display:flex;justify-content:space-between;border-top:1px solid #cad4d2;padding:24px 0}span,p{color:#53666c}a:focus{outline:2px solid #167684}</style>'
               '<small>APP SIGNALS / LOCAL ARCHIVE</small><h1>iOS 榜单监测</h1><p>选择日期，再按时点、国家和分类筛选。全部数据仅保存在本机。</p><ul>'
               + "".join(archive_links) + '</ul><p><a href="index.md">Markdown 总览</a></p></html>')
    return {"renderedRuns": sum(len(x) for x in days.values()), "dates": dates, "overview": (store.base / "index.md").relative_to(store.root).as_posix()}
