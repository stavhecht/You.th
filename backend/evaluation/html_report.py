# ruff: noqa: E501 — most of this file is CSS and HTML markup, which reads worse wrapped.
"""``results.json`` as a self-contained, shareable HTML page.

Written beside ``report.md`` on every run. The page carries the same numbers as
the Markdown report and no new ones; it is rendered from ``results.json`` so a
re-score regenerates it and the two can never disagree.

Optional ``findings.json`` in the run directory adds a "What we found" section:
a person's reading of the run (severity, title, detail, case ids, fix). The
framework cannot write that part; it only renders it when someone has.

The markup follows the artifact page contract — no ``<html>``/``<head>`` of its
own, every colour a token with a dark theme, nothing loaded but two Google
fonts — so the file can be published as-is. Palette and type are You.th's
own (``frontend/src/styles/youth.css``); the status colours are a fixed
good/warning/critical set, always paired with a glyph and a word.
"""

from __future__ import annotations

import json
from collections import Counter
from html import escape
from pathlib import Path
from typing import Any

from evaluation.config import Threshold
from evaluation.report import GROUP_ORDER, GROUP_TITLES, fmt, status, threshold_text

_PLAIN_SCALE = (
    "tool_call_count",
    "unnecessary_tool_calls_per_case",
    "irrelevant_in_top_k_per_case",
)

_STATUS = {
    "PASS": ("pass", "✓", "Pass"),
    "WARN": ("warn", "!", "Warn"),
    "FAIL": ("fail", "✕", "Fail"),
    "N/A": ("na", "\u2013", "n/a"),
    "INFO": ("info", "·", "Info"),
}

_SEVERITY = {"critical": "fail", "serious": "warn", "warning": "warn", "info": "info"}

_CSS = """
/* Layout: one reading column; wide tables scroll inside their own frame. */
:root {
  --bg: #f4f1ec; --surface: #fffefb; --tile: #e7e3dc; --ink: #201e1d; --ink-2: #57524c;
  --muted: #7a736b; --rule: #cfc8bd; --rule-soft: #e0dbd2; --accent: #ec3013; --accent-ink: #ae1800;
  --good: #0ca30c; --warn: #fab219; --bad: #d03b3b; --good-wash: #e8f4e6; --warn-wash: #fbf1d9;
  --bad-wash: #f9e6e3; --na-wash: #ece8e2;
  --font-ui: "Archivo", system-ui, -apple-system, "Segoe UI", sans-serif;
  --font-display: "Instrument Serif", Georgia, "Times New Roman", serif;
  --font-mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg: #191716; --surface: #221f1e; --tile: #302c2a; --ink: #f2efea; --ink-2: #c9c3bb;
  --muted: #9a938a; --rule: #443f3b; --rule-soft: #34302e; --accent: #ff5c3d; --accent-ink: #ff8368;
  --good-wash: #1d2a1b; --warn-wash: #2e2614; --bad-wash: #321b19; --na-wash: #2a2624;
  color-scheme: dark; } }
:root[data-theme="dark"] {
  --bg: #191716; --surface: #221f1e; --tile: #302c2a; --ink: #f2efea; --ink-2: #c9c3bb;
  --muted: #9a938a; --rule: #443f3b; --rule-soft: #34302e; --accent: #ff5c3d; --accent-ink: #ff8368;
  --good-wash: #1d2a1b; --warn-wash: #2e2614; --bad-wash: #321b19; --na-wash: #2a2624;
  color-scheme: dark; }
* { box-sizing: border-box; }
body { background: var(--bg); color: var(--ink); font: 15px/1.55 var(--font-ui); }
.page { max-width: 1120px; margin: 0 auto; padding-inline: 20px; padding-block: 28px 64px;
  display: grid; gap: 40px; }
a { color: var(--accent-ink); }
a:focus-visible, summary:focus-visible, button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
h1, h2, h3 { text-wrap: balance; margin: 0; }
h1 { font: 400 clamp(34px, 6vw, 52px)/1.05 var(--font-display); letter-spacing: -0.01em; }
h2 { font: 400 30px/1.15 var(--font-display); }
h3 { font-size: 15px; font-weight: 700; }
p { margin: 0; max-width: 72ch; }
.eyebrow { font-size: 12px; font-weight: 700; letter-spacing: 0.09em; text-transform: uppercase;
  color: var(--accent-ink); }
.masthead { display: grid; gap: 14px; border-top: 4px solid var(--accent); padding-top: 18px; }
.meta { display: flex; flex-wrap: wrap; gap: 6px 18px; color: var(--ink-2); font-size: 13.5px; }
.meta b { color: var(--ink); font-weight: 600; }
.verdict { display: grid; gap: 6px; padding: 16px 18px; border-radius: 6px; border: 1px solid var(--rule); }
.verdict.fail { background: var(--bad-wash); border-color: color-mix(in srgb, var(--bad) 45%, transparent); }
.verdict.pass { background: var(--good-wash); border-color: color-mix(in srgb, var(--good) 45%, transparent); }
.verdict strong { font-size: 17px; }
.toc { display: flex; flex-wrap: wrap; gap: 6px 16px; font-size: 13.5px; padding-block: 2px; }
section { display: grid; gap: 16px; min-width: 0; }
.section-head { display: grid; gap: 6px; border-bottom: 1px solid var(--rule); padding-bottom: 10px; }
.lede { color: var(--ink-2); }
.strip { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 1px;
  background: var(--rule); border: 1px solid var(--rule); border-radius: 6px; overflow: hidden; }
.strip > div { background: var(--surface); padding: 12px 14px; display: grid; gap: 2px; }
.strip .num { font: 400 30px/1.1 var(--font-display); font-variant-numeric: tabular-nums; }
.strip .lbl { font-size: 12.5px; color: var(--ink-2); }
.frame { overflow-x: auto; border: 1px solid var(--rule); border-radius: 6px; background: var(--surface); }
table { border-collapse: collapse; width: 100%; font-size: 13.5px; }
th, td { text-align: left; padding: 8px 12px; border-bottom: 1px solid var(--rule-soft); vertical-align: top; }
th { font-size: 11.5px; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase;
  color: var(--muted); background: var(--surface); position: sticky; top: 0; }
tr:last-child td { border-bottom: 0; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.mname { font-weight: 600; }
.mdesc { color: var(--muted); font-size: 12.5px; }
code, .mono { font-family: var(--font-mono); font-size: 12.5px; }
.pill { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; font-weight: 600;
  padding: 2px 9px 2px 7px; border-radius: 999px; white-space: nowrap; background: var(--na-wash); color: var(--ink); }
.pill i { font-style: normal; display: inline-grid; place-items: center; width: 15px; height: 15px;
  border-radius: 50%; font-size: 10px; font-weight: 800; color: #fff; background: var(--muted); }
.pill.pass { background: var(--good-wash); } .pill.pass i { background: var(--good); }
.pill.warn { background: var(--warn-wash); } .pill.warn i { background: var(--warn); color: #201e1d; }
.pill.fail { background: var(--bad-wash); } .pill.fail i { background: var(--bad); }
.meter { position: relative; width: 140px; height: 8px; border-radius: 4px; background: var(--tile); }
.meter .fill { position: absolute; inset: 0 auto 0 0; border-radius: 4px; background: var(--muted); }
.meter.pass .fill { background: var(--good); } .meter.warn .fill { background: var(--warn); }
.meter.fail .fill { background: var(--bad); }
.meter .tick { position: absolute; top: -4px; bottom: -4px; width: 2px; margin-left: -1px;
  background: var(--ink); border-radius: 1px; }
.findings { display: grid; gap: 12px; }
.finding { display: grid; grid-template-columns: 6px 1fr; gap: 0 14px; background: var(--surface);
  border: 1px solid var(--rule-soft); border-radius: 6px; padding: 14px 16px 14px 0; overflow: hidden; }
.finding .bar { background: var(--muted); border-radius: 0 3px 3px 0; }
.finding.fail .bar { background: var(--bad); } .finding.warn .bar { background: var(--warn); }
.finding .body { display: grid; gap: 6px; min-width: 0; }
.finding .fix { color: var(--ink-2); font-size: 13.5px; }
.finding .cases { display: flex; flex-wrap: wrap; gap: 6px; }
.chip { font-family: var(--font-mono); font-size: 11.5px; padding: 1px 7px; border-radius: 999px;
  border: 1px solid var(--rule); color: var(--ink-2); }
.cols { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 16px; }
.cols > * { min-width: 0; }
.bars { display: grid; gap: 8px; }
.barrow { display: grid; grid-template-columns: minmax(120px, 190px) 1fr 52px; gap: 10px; align-items: center;
  font-size: 13.5px; }
.barrow .track { height: 10px; background: var(--tile); border-radius: 0 4px 4px 0; }
.barrow .track span { display: block; height: 100%; background: var(--ink-2); border-radius: 0 4px 4px 0; }
.barrow .val { text-align: right; font-variant-numeric: tabular-nums; }
.filters { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.filters button { font: inherit; font-size: 13px; padding: 4px 12px; border-radius: 999px;
  border: 1px solid var(--rule); background: var(--surface); color: var(--ink); cursor: pointer; }
.filters button[aria-pressed="true"] { background: var(--ink); color: var(--bg); border-color: var(--ink); }
.filters .count { color: var(--muted); font-size: 13px; margin-left: auto; }
details > summary { cursor: pointer; }
.case-detail { display: grid; gap: 6px; padding-top: 8px; color: var(--ink-2); font-size: 13px; }
.case-detail li { margin-bottom: 4px; }
.case-detail ul { margin: 0; padding-left: 18px; }
.q { max-width: 46ch; }
.note { font-size: 13px; color: var(--muted); }
.callout { background: var(--surface); border-left: 3px solid var(--rule); padding: 10px 14px; font-size: 13.5px;
  color: var(--ink-2); }
.delta-bad { color: var(--bad); font-weight: 600; } .delta-good { color: var(--good); font-weight: 600; }
:root[data-theme="dark"] .delta-good, :root[data-theme="dark"] .delta-bad { filter: brightness(1.15); }
@media (max-width: 640px) { .meter { width: 90px; } .barrow { grid-template-columns: 1fr 1fr 44px; } }
@media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
"""

_SCRIPT = """
(() => {
  const rows = [...document.querySelectorAll('#cases tbody tr[data-cat]')];
  const count = document.getElementById('case-count');
  let cat = 'all', state = 'all';
  function apply() {
    let shown = 0;
    for (const row of rows) {
      const ok = (cat === 'all' || row.dataset.cat === cat) && (state === 'all' || row.dataset.state === state);
      row.hidden = !ok;
      const detail = row.nextElementSibling;
      if (detail && detail.classList.contains('detail-row')) detail.hidden = !ok;
      if (ok) shown++;
    }
    count.textContent = shown + ' of ' + rows.length + ' cases';
  }
  function wire(group, set) {
    for (const button of document.querySelectorAll('[data-' + group + ']')) {
      button.addEventListener('click', () => {
        for (const other of document.querySelectorAll('[data-' + group + ']')) other.setAttribute('aria-pressed', 'false');
        button.setAttribute('aria-pressed', 'true');
        set(button.dataset[group]);
        apply();
      });
    }
  }
  wire('cat', v => { cat = v; });
  wire('state', v => { state = v; });
  apply();
})();
"""


def _e(value: Any) -> str:
    return escape(str(value), quote=True)


def _label(name: str) -> str:
    """``rank_precision_at_5`` -> "Rank precision @5"; ``latency_p95_s`` -> "Latency p95 (s)"."""
    text = name.replace("_at_", " @")
    if text.endswith("_s"):
        text = text[:-2] + " (s)"
    text = text.replace("_", " ")
    return text[:1].upper() + text[1:]


def _pill(state: str) -> str:
    css, glyph, word = _STATUS.get(state, ("na", "\u2013", state))
    return f'<span class="pill {css}"><i aria-hidden="true">{glyph}</i>{word}</span>'


def _meter(name: str, metric: dict[str, Any], threshold: Threshold | None, state: str) -> str:
    value = metric.get("value")
    if value is None:
        return ""
    if name.startswith("latency") or name in _PLAIN_SCALE:
        top = max(value, threshold.value if threshold else 0) * 1.15 or 1.0
    else:
        top = 1.0
    width = max(0.0, min(1.0, value / top)) * 100
    tick = ""
    if threshold is not None:
        position = max(0.0, min(1.0, threshold.value / top)) * 100
        tick = f'<span class="tick" style="left:{position:.1f}%"></span>'
    css = _STATUS.get(state, ("na",))[0]
    tip = f"{fmt(name, value)}"
    if threshold is not None:
        tip += f" · threshold {threshold_text(name, {name: threshold})}"
    tip += f" · n={metric['n']}"
    return (
        f'<div class="meter {css}" role="img" aria-label="{_e(tip)}" title="{_e(tip)}">'
        f'<span class="fill" style="width:{width:.1f}%"></span>{tick}</div>'
    )


def _metrics_section(results: dict[str, Any], thresholds: dict[str, Threshold]) -> str:
    parts = []
    for group in GROUP_ORDER:
        names = [n for n, m in results["metrics"].items() if m["group"] == group]
        if not names:
            continue
        rows = []
        for name in names:
            metric = results["metrics"][name]
            state = status(name, metric, thresholds)
            threshold = thresholds.get(name)
            rows.append(
                "<tr>"
                f'<td><div class="mname">{_e(_label(name))}</div>'
                f'<div class="mdesc">{_e(metric.get("description", ""))}</div></td>'
                f'<td class="num">{_e(fmt(name, metric["value"]))}</td>'
                f"<td>{_meter(name, metric, threshold, state)}</td>"
                f"<td>{_pill(state)}</td>"
                f'<td class="num">{_e(threshold_text(name, thresholds))}</td>'
                f'<td class="num">{metric["n"]}</td>'
                "</tr>"
            )
        distribution = metric_distribution(results, group)
        parts.append(
            f'<h3 id="m-{group}">{_e(GROUP_TITLES[group])}</h3>'
            '<div class="frame"><table><thead><tr><th>Metric</th><th class="num">Value</th>'
            '<th>Against threshold</th><th>Status</th><th class="num">Threshold</th>'
            '<th class="num">n</th></tr></thead><tbody>'
            + "".join(rows)
            + "</tbody></table></div>"
            + distribution
        )
    return "".join(parts)


def metric_distribution(results: dict[str, Any], group: str) -> str:
    if group != "citations":
        return ""
    distribution = (results["metrics"].get("citation_correctness") or {}).get("distribution")
    if not distribution:
        return ""
    total = sum(distribution.values())
    order = ["supported", "partially_supported", "not_supported", "contradicted"]
    text = ", ".join(
        f"{label.replace('_', ' ')} {distribution.get(label, 0)}" for label in order
    )
    return f'<p class="note">Judge labels behind citation correctness ({total} statements): {_e(text)}.</p>'


def _bars(title: str, counts: list[tuple[str, int]], total_label: str) -> str:
    top = max((count for _, count in counts), default=0) or 1
    rows = "".join(
        f'<div class="barrow"><span>{_e(label)}</span>'
        f'<div class="track" title="{_e(label)}: {count}"><span style="width:{count / top * 100:.1f}%"></span></div>'
        f'<span class="val">{count}</span></div>'
        for label, count in counts
    )
    return f'<div class="bars"><h3>{_e(title)}</h3>{rows}<p class="note">{_e(total_label)}</p></div>'


def _breakdowns(results: dict[str, Any]) -> str:
    quality = [
        r
        for r in results["cases"]
        if r["status"] == "executed" and not r["is_failure_case"] and r.get("answer")
    ]
    signals: Counter[str] = Counter()
    for case in quality:
        kinds = set()
        for reason in case["answer"]["hallucination_reasons"]:
            if "(misattributed)" in reason:
                kinds.add("Number cited to the wrong source")
            elif "(invented)" in reason:
                kinds.add("Number in no source")
            elif reason.startswith("numbers"):
                kinds.add("Number not in cited source")
            elif reason.startswith("cites handles"):
                kinds.add("Invented citation handle")
            elif "contradicts" in reason:
                kinds.add("Source contradicts the sentence")
            elif reason.startswith("asserts forbidden"):
                kinds.add("Forbidden claim asserted")
            elif reason.startswith("contains forbidden"):
                kinds.add("Forbidden phrase")
            elif reason.startswith("cites sources no"):
                kinds.add("Source no registry knows")
            else:
                kinds.add("Unsupported, low word overlap")
        signals.update(kinds)
    distribution = (results["metrics"].get("citation_correctness") or {}).get(
        "distribution"
    ) or {}
    labels = [
        ("Supported", distribution.get("supported", 0)),
        ("Partially supported", distribution.get("partially_supported", 0)),
        ("Not supported", distribution.get("not_supported", 0)),
        ("Contradicted", distribution.get("contradicted", 0)),
    ]
    outcomes = Counter(r["outcome"] for r in results["cases"])
    left = _bars(
        "Hallucination signals",
        signals.most_common(),
        f"Articles with at least one signal, out of {len(quality)} quality-case articles. "
        "One article can show several.",
    )
    middle = _bars(
        "Judge verdicts on cited statements",
        labels,
        "Each cited statement checked against the source text it cites.",
    )
    right = _bars("Run outcomes", outcomes.most_common(), f"All {len(results['cases'])} cases.")
    return f'<div class="cols">{left}{middle}{right}</div>'


def _findings(findings: list[dict[str, Any]]) -> str:
    items = []
    for item in findings:
        css = _SEVERITY.get(item.get("severity", "info"), "info")
        cases = "".join(f'<span class="chip">{_e(c)}</span>' for c in item.get("cases", []))
        fix = f'<p class="fix"><b>Fix:</b> {_e(item["fix"])}</p>' if item.get("fix") else ""
        status_line = (
            f'<p class="fix"><b>Status:</b> {_e(item["status"])}</p>'
            if item.get("status")
            else ""
        )
        items.append(
            f'<article class="finding {css}"><div class="bar"></div><div class="body">'
            f"<h3>{_e(item['title'])}</h3><p>{_e(item['detail'])}</p>{fix}{status_line}"
            f"{f'<div class=cases>{cases}</div>' if cases else ''}</div></article>"
        )
    return f'<div class="findings">{"".join(items)}</div>'


def _comparison(comparison: dict[str, Any]) -> str:
    """Before and after on matched cases: ``comparison.json`` beside the run.

    Written by a person or a script when two runs are compared; the framework
    renders it and computes nothing new for it.
    """
    rows = []
    for row in comparison.get("rows", []):
        name = row["metric"]
        before, after = row.get("before"), row.get("after")
        better = row.get("better", "higher")
        delta = "" if before is None or after is None else after - before
        css = ""
        if delta != "":
            improved = delta > 0 if better == "higher" else delta < 0
            css = "delta-good" if improved and abs(delta) > 1e-9 else ("delta-bad" if abs(delta) > 1e-9 else "")
            delta = (
                f"{delta:+.1f}s" if name.startswith("latency")
                else f"{delta:+.2f}" if name in _PLAIN_SCALE else f"{delta * 100:+.1f} pts"
            )
        rows.append(
            f"<tr><td>{_e(row.get('label') or _label(name))}</td>"
            f'<td class="num">{_e(fmt(name, before))}</td><td class="num">{_e(fmt(name, after))}</td>'
            f'<td class="num {css}">{_e(delta)}</td><td class="num">{_e(row.get("n", ""))}</td></tr>'
        )
    notes = "".join(f"<p>{_e(note)}</p>" for note in comparison.get("notes", []))
    return (
        f'<section id="comparison"><div class="section-head"><p class="eyebrow">Before and after the fixes</p>'
        f'<h2>{_e(comparison.get("title", "Before and after"))}</h2></div>{notes}'
        '<div class="frame"><table><thead><tr><th>Metric</th><th class="num">Before</th>'
        '<th class="num">After</th><th class="num">Change</th><th class="num">n</th></tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table></div></section>"
    )


def _failure_table(results: dict[str, Any]) -> str:
    rows = []
    for case in results["cases"]:
        if not case["is_failure_case"]:
            continue
        failure = case.get("failure") or {}
        research = case.get("research") or {}
        scenario = research.get("scenario") or case.get("subcategory") or ""

        def flag(value: bool | None) -> str:
            return "—" if value is None else ("yes" if value else "no")

        rows.append(
            "<tr>"
            f'<td class="mono">{_e(case["id"])}</td><td>{_e(scenario)}</td>'
            f'<td class="mono">{_e(case["outcome"])}</td>'
            f"<td>{flag(failure.get('recovered'))}</td><td>{flag(failure.get('fallback_ok'))}</td>"
            f"<td>{flag(failure.get('retry_classification_ok'))}</td>"
            f"<td>{_pill('PASS' if case['passed'] else 'FAIL')}</td>"
            "</tr>"
        )
    return (
        '<div class="frame"><table><thead><tr><th>Case</th><th>Scenario</th><th>Outcome</th>'
        "<th>Recovered</th><th>Fallback answered</th><th>Retry classified</th><th>Result</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
    )


def _outages(results: dict[str, Any]) -> str:
    outages = results.get("provider_outages")
    if not outages:
        return ""
    rows = "".join(
        f'<tr><td>{_e(_label(name))}</td><td class="num">{_e(fmt(name, row["all_providers"]))}</td>'
        f'<td class="num">{row["all_providers_n"]}</td>'
        f'<td class="num">{_e(fmt(name, row["provider_down"]))}</td>'
        f'<td class="num">{row["provider_down_n"]}</td></tr>'
        for name, row in outages["comparison"].items()
    )
    providers = ", ".join(f"{p} ({n} cases)" for p, n in outages["providers"].items())
    return (
        '<section id="outages"><div class="section-head"><p class="eyebrow">Run conditions</p>'
        "<h2>Provider outage during the run</h2></div>"
        f"<p>{len(outages['cases'])} cases ran while a search provider failed every call, with no "
        f"fault injected: {_e(providers)}. The pipeline carries on with the providers it has left. "
        "The two groups below also differ in which cases they hold, so read this as a description, "
        "not a controlled comparison.</p>"
        '<div class="frame"><table><thead><tr><th>Metric</th><th class="num">All providers up</th>'
        '<th class="num">n</th><th class="num">Provider down</th><th class="num">n</th></tr></thead>'
        f"<tbody>{rows}</tbody></table></div></section>"
    )


def _performance(results: dict[str, Any]) -> str:
    perf = results.get("performance") or {}
    if not perf:
        return ""

    def cells(stats: dict[str, Any], scale: float = 1.0) -> str:
        out = [f'<td class="num">{stats.get("n", 0)}</td>']
        for key in ("mean", "median", "p95", "p99"):
            value = stats.get(key)
            out.append(
                f'<td class="num">{"—" if value is None else f"{value / scale:.1f}s"}</td>'
            )
        return "".join(out)

    rows = [
        ("Whole case (wall clock)", perf["wall_clock_s"], 1.0),
        ("Search calls (live, per case)", perf["search_live_s"], 1.0),
        ("Model calls (live, per case)", perf["llm_live_s"], 1.0),
        ("Retrieve + rank stages", perf["retrieval_stage_s"], 1.0),
    ]
    rows += [
        (f"One {tool} call", stats, 1000.0)
        for tool, stats in perf["live_latency_ms_by_tool"].items()
    ]
    body = "".join(
        f"<tr><td>{_e(label)}</td>{cells(stats, scale)}</tr>" for label, stats, scale in rows
    )
    per_case = perf.get("per_case") or {}
    cost = (
        f"${perf['cost_usd']:.4f}"
        if perf.get("cost_usd") is not None
        else "unpriced ("
        + ", ".join(perf.get("unpriced_models") or [])
        + " are local models missing "
        "from llm/pricing.py; reported as unpriced, never as $0)"
    )
    return (
        '<div class="frame"><table><thead><tr><th>Measure</th><th class="num">n</th>'
        '<th class="num">Mean</th><th class="num">Median</th><th class="num">p95</th>'
        f'<th class="num">p99</th></tr></thead><tbody>{body}</tbody></table></div>'
        f'<p class="note">Per case: {per_case.get("calls") or 0:.1f} tool calls, '
        f"{per_case.get('tokens_in') or 0:,.0f} input and {per_case.get('tokens_out') or 0:,.0f} "
        f"output tokens. Cost: {_e(cost)}. Live latency is what each call took when it was made; "
        f"{perf['totals'].get('replayed_calls', 0)} of {perf['totals'].get('calls', 0)} calls in "
        "this run were replayed from the recording.</p>"
    )


def _regression(results: dict[str, Any]) -> str:
    regression = results.get("regression") or {}
    if not regression.get("rows"):
        note = regression.get("note") or "No baseline to compare with."
        return f'<p class="note">{_e(note)}</p>'
    better = {"higher": 1, "lower": -1, "neutral": 0}
    rows = []
    for row in sorted(regression["rows"], key=lambda r: (not r["regression"], r["metric"])):
        delta = row["delta"]
        if abs(delta) < 1e-9:
            continue
        name = row["metric"]
        direction = better[row["better"]]
        css = (
            "delta-bad"
            if row["regression"]
            else ("delta-good" if delta * direction > 0 else "")
        )
        shown = (
            f"{delta:+.1f}s"
            if name.startswith("latency")
            else f"{delta:+.2f}"
            if name in _PLAIN_SCALE
            else f"{delta * 100:+.1f} pts"
        )
        rows.append(
            f'<tr><td>{_e(_label(name))}</td><td class="num">{_e(fmt(name, row["baseline"]))}</td>'
            f'<td class="num">{_e(fmt(name, row["current"]))}</td>'
            f'<td class="num {css}">{_e(shown)}</td>'
            f"<td>{'Regression' if row['regression'] else ''}</td></tr>"
        )
    changes = regression.get("cases") or {}
    env = regression.get("environment_changed")
    env_note = (
        f'<p class="callout">The environment differs from the baseline ({_e(env)}), so these '
        "deltas reflect that change as well as the code.</p>"
        if env
        else ""
    )
    return (
        f"<p>Against the baseline saved {_e(regression['baseline_created_at'])} "
        f"(git <code>{_e(regression.get('baseline_git'))}</code>). Only metrics that moved are listed.</p>"
        + env_note
        + '<div class="frame"><table><thead><tr><th>Metric</th><th class="num">Before</th>'
        '<th class="num">After</th><th class="num">Change</th><th></th></tr></thead><tbody>'
        + ("".join(rows) or '<tr><td colspan="5">No metric moved.</td></tr>')
        + "</tbody></table></div>"
        f'<p class="note">Newly failing cases: {_e(", ".join(changes.get("newly_failing") or []) or "none")}. '
        f"Newly passing: {_e(', '.join(changes.get('newly_passing') or []) or 'none')}.</p>"
    )


def _cases(results: dict[str, Any]) -> str:
    categories = sorted({r["category"] for r in results["cases"]})
    cat_buttons = (
        '<button type="button" data-cat="all" aria-pressed="true">All</button>'
        + "".join(
            f'<button type="button" data-cat="{_e(c)}" aria-pressed="false">{_e(c.replace("_", " "))}</button>'
            for c in categories
        )
    )
    state_buttons = "".join(
        f'<button type="button" data-state="{key}" aria-pressed="{"true" if key == "all" else "false"}">{label}</button>'
        for key, label in (
            ("all", "Any result"),
            ("fail", "Failed a check"),
            ("pass", "Passed every check"),
        )
    )
    rows = []
    for case in results["cases"]:
        checks = [c for c in case.get("checks") or [] if c["passed"] is not None]
        ok = sum(1 for c in checks if c["passed"])
        state = (
            "skip" if case["status"] == "skipped" else ("pass" if case["passed"] else "fail")
        )
        verdict = (case.get("answer") or {}).get("verdict") or ""
        failed = [c for c in checks if not c["passed"]]
        detail = ""
        if failed or case.get("error"):
            items = "".join(
                f"<li><b>{_e(c['name'].replace('_', ' '))}</b>: {_e(c['detail'][:420])}</li>"
                for c in failed[:8]
            )
            error = case.get("error") or {}
            err = (
                f"<p><b>Run error:</b> {_e(error.get('type'))}: {_e(str(error.get('message'))[:300])}</p>"
                if error and case["outcome"] not in ("answered", "no_evidence")
                else ""
            )
            detail = (
                f'<tr class="detail-row" data-for="{_e(case["id"])}"><td colspan="6">'
                f'<details><summary>Why it failed</summary><div class="case-detail">{err}<ul>{items}</ul>'
                "</div></details></td></tr>"
            )
        rows.append(
            f'<tr data-cat="{_e(case["category"])}" data-state="{state}">'
            f'<td class="mono">{_e(case["id"])}</td>'
            f'<td class="q">{_e(case["query"][:220])}</td>'
            f'<td class="mono">{_e(case["outcome"])}</td>'
            f"<td>{_e(verdict)}</td>"
            f'<td class="num">{ok}/{len(checks)}</td>'
            f"<td>{_pill('PASS' if state == 'pass' else 'FAIL' if state == 'fail' else 'N/A')}</td>"
            "</tr>" + detail
        )
    return (
        f'<div class="filters" role="group" aria-label="Category">{cat_buttons}</div>'
        f'<div class="filters" role="group" aria-label="Result">{state_buttons}'
        '<span class="count" id="case-count" aria-live="polite"></span></div>'
        '<div class="frame"><table id="cases"><thead><tr><th>Case</th><th>Query</th><th>Outcome</th>'
        '<th>Verdict</th><th class="num">Checks</th><th>Result</th></tr></thead><tbody>'
        + "".join(rows)
        + "</tbody></table></div>"
    )


def render(
    results: dict[str, Any],
    thresholds: dict[str, Threshold],
    findings: list[dict[str, Any]] | None = None,
    comparison: dict[str, Any] | None = None,
) -> str:
    counts = results["counts"]
    env = results["environment"]
    states = Counter(status(n, m, thresholds) for n, m in results["metrics"].items())
    critical = results.get("critical_failures") or []
    failed_run = bool(critical)
    judge = results.get("judge") or {}
    gaps = results.get("capability_gaps") or []
    toc_items = [
        ("summary", "Summary"),
        *([("comparison", "Before and after")] if comparison else []),
        *([("findings", "What we found")] if findings else []),
        ("metrics", "Metrics"),
        ("breakdown", "Breakdowns"),
        ("failures", "Failure handling"),
        *([("outages", "Provider outage")] if results.get("provider_outages") else []),
        ("performance", "Performance"),
        ("regression", "Regression"),
        ("gaps", "Capability gaps"),
        ("cases", "All cases"),
        ("method", "How this was measured"),
    ]
    toc = "".join(f'<a href="#{key}">{_e(label)}</a>' for key, label in toc_items)
    verdict_text = (
        f"{len(critical)} critical metric{'s' if len(critical) != 1 else ''} below threshold"
        if failed_run
        else "Every critical metric met its threshold"
    )
    critical_list = f"<p>{_e(', '.join(_label(n) for n in critical))}.</p>" if critical else ""
    gaps_html = (
        "<ul>"
        + "".join(f"<li><b>{_e(g['gap'])}</b>: {_e(g['detail'])}</li>" for g in gaps)
        + "</ul>"
        if gaps
        else '<p class="note">None recorded.</p>'
    )
    skipped = results.get("skipped") or []
    skipped_html = (
        "<p>Skipped: " + _e("; ".join(f"{s['id']} ({s['reason']})" for s in skipped)) + "</p>"
        if skipped
        else ""
    )
    return f"""<title>You.th Evaluation Report</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@400;500;600;700&family=Instrument+Serif&display=swap">
<style>{_CSS}</style>
<main class="page">
  <header class="masthead" id="summary">
    <p class="eyebrow">You.th · article pipeline evaluation</p>
    <h1>Evaluation report</h1>
    <div class="meta">
      <span>Run <b>{_e(results["started_at"][:16].replace("T", " "))} UTC</b></span>
      <span>Suite <b>{_e(results["suite"])}</b></span>
      <span>Mode <b>{_e(results["mode"])}</b></span>
      <span>Commit <b class="mono">{_e(results["git"])}</b></span>
      <span>Writer <b>{_e(env.get("synthesis_model"))}</b></span>
      <span>Judge <b>{_e(judge.get("model") or "off")}</b></span>
      <span>Providers <b>{_e(", ".join(env.get("enabled_providers") or []))}</b></span>
    </div>
    <div class="verdict {"fail" if failed_run else "pass"}">
      <strong>{_pill("FAIL" if failed_run else "PASS")} {_e(verdict_text)}</strong>
      {critical_list}
    </div>
    <div class="strip">
      <div><span class="num">{counts["executed"]}</span><span class="lbl">cases executed of {counts["selected"]}</span></div>
      <div><span class="num">{counts["passed"]}</span><span class="lbl">passed every check</span></div>
      <div><span class="num">{counts["skipped"]}</span><span class="lbl">skipped</span></div>
      <div><span class="num">{states.get("PASS", 0)}</span><span class="lbl">metrics pass</span></div>
      <div><span class="num">{states.get("WARN", 0)}</span><span class="lbl">metrics warn</span></div>
      <div><span class="num">{states.get("FAIL", 0)}</span><span class="lbl">critical metrics fail</span></div>
    </div>
    {skipped_html}
    <nav class="toc" aria-label="Sections">{toc}</nav>
  </header>
  {_comparison(comparison) if comparison else ""}
  {f'<section id="findings"><div class="section-head"><p class="eyebrow">Analysis</p><h2>What we found</h2><p class="lede">The defects in the pipeline that the failing checks trace back to, each confirmed in the run traces.</p></div>{_findings(findings)}</section>' if findings else ""}
  <section id="metrics">
    <div class="section-head"><p class="eyebrow">Results</p><h2>Metrics</h2>
      <p class="lede">Each metric is computed only over the cases it applies to; n says how many. A critical metric below its threshold fails the run. The bar shows the value, the dark tick the threshold.</p></div>
    {_metrics_section(results, thresholds)}
  </section>
  <section id="breakdown">
    <div class="section-head"><p class="eyebrow">Detail</p><h2>Breakdowns</h2></div>
    {_breakdowns(results)}
  </section>
  <section id="failures">
    <div class="section-head"><p class="eyebrow">Robustness</p><h2>Failure handling</h2>
      <p class="lede">Faults injected at the network layer and into the models, embeddings and database, plus the research agent's tool outages. Recovered means a controlled outcome, bounded retries and no fabricated output.</p></div>
    {_failure_table(results)}
  </section>
  {_outages(results)}
  <section id="performance">
    <div class="section-head"><p class="eyebrow">Cost of a case</p><h2>Performance</h2></div>
    {_performance(results)}
  </section>
  <section id="regression">
    <div class="section-head"><p class="eyebrow">Change over time</p><h2>Regression</h2></div>
    {_regression(results)}
  </section>
  <section id="gaps">
    <div class="section-head"><p class="eyebrow">Out of scope today</p><h2>Capability gaps</h2>
      <p class="lede">What cases asked for that the system has no mechanism to do. Reported here, never counted against tool selection.</p></div>
    {gaps_html}
  </section>
  <section id="cases">
    <div class="section-head"><p class="eyebrow">Every case</p><h2>All cases</h2></div>
    {_cases(results)}
  </section>
  <section id="method">
    <div class="section-head"><p class="eyebrow">Method</p><h2>How this was measured</h2></div>
    <p>The cases run through the production pipeline stages and its LangGraph graph: extract, retrieve, rank, full text, synthesize, validate. Search providers, models and embeddings are recorded so a rerun replays the same answers. Storage is replaced by an in-memory stand-in, so the evaluation never writes an article or touches the database. Faults are injected below the production retry and throttle code.</p>
    <p>Answers are checked two ways. Deterministic checks parse every cited sentence, confirm each number appears in the source it cites, confirm cited PMIDs and DOIs exist in PubMed, Crossref and doi.org with matching titles and no retraction, and compare outcomes and verdicts with each case's expected range. An LLM judge ({_e(judge.get("model") or "off")}), a different model from the writer, rates whether each cited source supports its sentence and how relevant, complete and well hedged the article is. A statement counts as hallucinated only when the judge's view is backed by a deterministic signal, except for forbidden claims, which only meaning can catch.</p>
    <p class="note">Generated from <code>results.json</code> by <code>evaluation/html_report.py</code>. Full traces, every check and the Markdown report sit in the run directory.</p>
  </section>
</main>
<script>{_SCRIPT}</script>
"""


def write(run_dir: Path, results: dict[str, Any], thresholds: dict[str, Threshold]) -> Path:
    findings_path = run_dir / "findings.json"
    findings = json.loads(findings_path.read_text()) if findings_path.exists() else None
    comparison_path = run_dir / "comparison.json"
    comparison = json.loads(comparison_path.read_text()) if comparison_path.exists() else None
    path = run_dir / "report.html"
    path.write_text(render(results, thresholds, findings, comparison))
    return path
