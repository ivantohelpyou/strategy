"""HTML for the admin surface: one shell, a few table helpers, no engine.

Inline HTML rather than a template package: the pages are tables, the data is
already shaped by `queries` and `report.build_context`, and a template
directory would put the markup one indirection away from the query that fills
it.

The house style is a terminal that happens to have links. Every page states the
CLI command it mirrors, because surfacing the CLI's capabilities is the job —
not replacing them.
"""

from __future__ import annotations

from datetime import date
from html import escape
from typing import Any, Iterable

STATUS_MARK = {"holding": "○", "contested": "◐", "resolved": "✓", "retired": "❄"}
STATUS_CLASS = {
    "holding": "st-holding",
    "contested": "st-contested",
    "resolved": "st-resolved",
    "retired": "st-retired",
}

NAV = [
    ("/", "cascade"),
    ("/cascades", "cascades"),
    ("/positions", "positions"),
    ("/triage", "triage"),
    ("/pending", "pending"),
    ("/questions", "questions"),
    ("/gates", "gates"),
    ("/findings", "findings"),
    ("/matrix", "matrix"),
    ("/drift", "drift"),
    ("/metrics", "metrics"),
    ("/drops", "drops"),
    ("/funnel", "funnel"),
    ("/profile", "profile"),
    ("/report", "report"),
    ("/store", "store"),
    ("/commands", "commands"),
]

CSS = """
:root{--bg:#fbfbf9;--fg:#1c1c1a;--dim:#6b6b64;--line:#dedcd4;--card:#fff;
--accent:#8a5a00;--warn:#a3320f;--ok:#1f6b3a;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
@media (prefers-color-scheme:dark){:root{--bg:#15161a;--fg:#e6e4de;--dim:#8f8d85;
--line:#2e3038;--card:#1c1e23;--accent:#d9a441;--warn:#e8785a;--ok:#6cc48c}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
a{color:inherit}
header{border-bottom:1px solid var(--line);padding:.6rem 1rem;display:flex;
gap:1rem;align-items:baseline;flex-wrap:wrap;position:sticky;top:0;background:var(--bg);z-index:5}
header .brand{font-weight:700;letter-spacing:-.01em}
header .brand span{font-weight:400;color:var(--dim);font-size:.82rem;margin-left:.4rem}
nav{display:flex;gap:.1rem;flex-wrap:wrap}
nav a{font:12px/1 var(--mono);padding:.35rem .5rem;border-radius:4px;
text-decoration:none;color:var(--dim)}
nav a:hover{background:var(--line);color:var(--fg)}
nav a.on{background:var(--fg);color:var(--bg)}
main{padding:1rem;max-width:1200px}
h1{font-size:1.05rem;margin:0 0 .2rem}
h2{font-size:.9rem;margin:1.6rem 0 .4rem;text-transform:uppercase;
letter-spacing:.08em;color:var(--dim)}
.cmd{font:12px/1.4 var(--mono);color:var(--dim);margin:0 0 .9rem}
.cmd b{color:var(--accent);font-weight:600}
.note{color:var(--dim);font-size:.85rem;margin:.2rem 0 1rem;max-width:70ch}
.wrap{overflow-x:auto;border:1px solid var(--line);border-radius:6px;background:var(--card)}
table{border-collapse:collapse;width:100%;font-size:13px}
th{text-align:left;font:11px/1 var(--mono);text-transform:uppercase;
letter-spacing:.06em;color:var(--dim);padding:.5rem .6rem;border-bottom:1px solid var(--line);
white-space:nowrap}
td{padding:.42rem .6rem;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:0}
tr:hover td{background:color-mix(in srgb,var(--line) 35%,transparent)}
td.n,th.n{text-align:right;font-family:var(--mono);white-space:nowrap}
td.m{font-family:var(--mono);font-size:12px;white-space:nowrap;color:var(--dim)}
.st-holding{color:var(--dim)}.st-contested{color:var(--warn);font-weight:600}
.st-resolved{color:var(--ok)}.st-retired{color:var(--dim);opacity:.6}
.strip{display:flex;gap:.5rem;flex-wrap:wrap;margin:0 0 1.2rem}
.tile{border:1px solid var(--line);border-radius:6px;background:var(--card);
padding:.5rem .7rem;min-width:8.5rem;text-decoration:none;display:block}
.tile b{display:block;font:600 1.35rem/1.1 var(--mono)}
.tile s{display:block;font-size:11px;text-transform:uppercase;letter-spacing:.06em;
color:var(--dim);text-decoration:none;margin-top:.15rem}
.tile.hot b{color:var(--warn)}.tile.good b{color:var(--ok)}
.box{border:1px solid var(--line);border-radius:6px;background:var(--card);
padding:.7rem .9rem;margin:0 0 .7rem}
.box>h3{margin:0 0 .1rem;font-size:.92rem}
.box>p.prompt{margin:0 0 .6rem;color:var(--dim);font-size:.82rem}
ul.rows{list-style:none;margin:0;padding:0}
ul.rows li{padding:.3rem 0;border-top:1px solid var(--line)}
li.cascade-row{padding:.55rem 0}
li.cascade-row b{font-weight:600;line-height:1.35}
/* The statement sits under its headline so the box reads as a list of claims
   rather than a table of metadata. Indented to the headline, muted, and given
   room to breathe — this page is for reading, not for scanning fields. */
.claim{margin:.25rem 0 0 0;color:var(--dim);line-height:1.5;max-width:78ch}
ul.rows li:first-child{border-top:0}
.pill{font:11px/1 var(--mono);border:1px solid var(--line);border-radius:99px;
padding:.18rem .45rem;color:var(--dim);white-space:nowrap}
pre{font:12px/1.55 var(--mono);white-space:pre-wrap;word-wrap:break-word;margin:0}
pre.doc{border:1px solid var(--line);border-radius:6px;background:var(--card);padding:1rem}
.filters{display:flex;gap:.4rem;flex-wrap:wrap;margin:0 0 .8rem;align-items:center}
.filters a{font:11px/1 var(--mono);padding:.28rem .45rem;border:1px solid var(--line);
border-radius:4px;text-decoration:none;color:var(--dim)}
.filters a.on{background:var(--fg);color:var(--bg);border-color:var(--fg)}
.filters span{font:11px/1 var(--mono);color:var(--dim);text-transform:uppercase;
letter-spacing:.06em;margin-right:.1rem}
footer{color:var(--dim);font:11px/1.6 var(--mono);padding:2rem 1rem 3rem;max-width:1200px}
.warnbar{border:1px solid var(--warn);border-left-width:3px;border-radius:6px;
padding:.6rem .8rem;margin:0 0 1rem;background:var(--card)}
.warnbar b{color:var(--warn)}
.case{display:grid;gap:.7rem;margin:.9rem 0;
grid-template-columns:repeat(auto-fit,minmax(19rem,1fr))}
.case section{border:1px solid var(--line);border-radius:6px;background:var(--card);
padding:.6rem .8rem}
.case h4{margin:0 0 .4rem;font:11px/1 var(--mono);text-transform:uppercase;
letter-spacing:.07em;color:var(--dim)}
.case section.for h4{color:var(--ok)}.case section.against h4{color:var(--warn)}
.case ul{margin:0;padding-left:1.1rem}.case li{margin:.3rem 0}
.case li small{display:block;color:var(--dim);font-family:var(--mono);font-size:11px}
.acts{display:grid;gap:.6rem;grid-template-columns:repeat(auto-fit,minmax(15rem,1fr));
margin:1rem 0 0}
.acts form{border:1px solid var(--line);border-radius:6px;background:var(--card);
padding:.6rem .8rem}
.acts h4{margin:0 0 .4rem;font:11px/1 var(--mono);text-transform:uppercase;
letter-spacing:.07em;color:var(--dim)}
.acts input[type=text],.acts textarea,.acts select{width:100%;font:13px/1.4 inherit;
background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:4px;
padding:.35rem .45rem;margin-bottom:.4rem}
.acts textarea{min-height:3.4rem;resize:vertical}
.acts button{font:12px/1 var(--mono);padding:.45rem .8rem;border-radius:4px;cursor:pointer;
border:1px solid var(--fg);background:var(--fg);color:var(--bg)}
.acts form.ghost button{background:transparent;color:var(--fg)}
.acts button.warn{border-color:var(--warn);background:var(--warn);color:#fff}
.q{border-left:3px solid var(--accent);padding-left:.6rem;margin:.4rem 0}
.flash{border:1px solid var(--ok);border-left-width:3px;border-radius:6px;padding:.5rem .8rem;
margin:0 0 1rem;background:var(--card)}
.flash.err{border-color:var(--warn)}
.queue{display:flex;gap:.3rem;flex-wrap:wrap;margin:0 0 .8rem}
.queue a{font:11px/1 var(--mono);padding:.3rem .45rem;border:1px solid var(--line);
border-radius:4px;text-decoration:none;color:var(--dim)}
.queue a.on{background:var(--fg);color:var(--bg);border-color:var(--fg)}
.queue a.bare{opacity:.5}
"""


def esc(v: Any) -> str:
    return escape("" if v is None else str(v), quote=True)


def shell(title: str, active: str, body: str, *, store_line: str = "") -> str:
    nav = "".join(
        f'<a href="{esc(href)}" class="{"on" if href == active else ""}">{esc(label)}</a>'
        for href, label in NAV
    )
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>{esc(title)} · strategy admin</title><style>{CSS}</style></head><body>"
        f'<header><div class="brand">strategy<span>admin surface · read-only</span></div>'
        f"<nav>{nav}</nav></header><main>{body}</main>"
        f'<footer>{esc(store_line)}<br>Read-only by design — every write stays on the CLI, '
        "where the supersession scope rule, store-owned columns and claim contest live "
        "(position #67).</footer></body></html>"
    )


def cmd(text: str, note: str = "") -> str:
    out = f'<p class="cmd">$ <b>{esc(text)}</b></p>'
    if note:
        out += f'<p class="note">{esc(note)}</p>'
    return out


def tile(label: str, value: Any, href: str = "", kind: str = "") -> str:
    inner = f"<b>{esc(value)}</b><s>{esc(label)}</s>"
    cls = f"tile {kind}".strip()
    if href:
        return f'<a class="{cls}" href="{esc(href)}">{inner}</a>'
    return f'<div class="{cls}">{inner}</div>'


def table(
    headers: Iterable[str],
    rows: Iterable[Iterable[str]],
    numeric: set[int] = frozenset(),
) -> str:
    headers = list(headers)
    head = "".join(
        f'<th class="{"n" if i in numeric else ""}">{esc(h)}</th>' for i, h in enumerate(headers)
    )
    body = ""
    for r in rows:
        cells = "".join(
            f'<td class="{"n" if i in numeric else ""}">{c}</td>' for i, c in enumerate(r)
        )
        body += f"<tr>{cells}</tr>"
    if not body:
        body = f'<tr><td colspan="{len(headers)}" class="m">nothing here</td></tr>'
    return (
        f'<div class="wrap"><table><thead><tr>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table></div>"
    )


def status_cell(status: str) -> str:
    return (
        f'<span class="{STATUS_CLASS.get(status, "")}">'
        f"{STATUS_MARK.get(status, '?')} {esc(status)}</span>"
    )


def pos_link(pid: int, title: str, limit: int = 90) -> str:
    t = title if len(title) <= limit else title[: limit - 1] + "…"
    return f'<a href="/positions/{int(pid)}">{esc(t)}</a>'


def days_cell(days: int | None, missed: bool) -> str:
    if days is None:
        return '<span class="st-contested">NO DATE</span>'
    if missed:
        return f'<span class="st-contested">MISSED {abs(days)}d</span>'
    cls = "st-contested" if days <= 7 else ""
    return f'<span class="{cls}">{days:+d}d</span>'


def fmt_date(d: date | None) -> str:
    return d.isoformat() if d else "—"


def case(d: dict) -> str:
    """The dossier as HTML — the same case the CLI card shows, in the same order.

    Both read `queries.dossier`, so the two surfaces cannot argue differently
    about the same position; only the rendering differs."""
    cols = []
    for kind, label, cls in (
        ("for", "For", "for"),
        ("against", "Against", "against"),
        ("test", "Would settle it", ""),
    ):
        rows = d["factors"][kind]
        if rows:
            items = "".join(f"<li>{esc(f.text)}</li>" for f in rows)
            cols.append(f'<section class="{cls}"><h4>{esc(label)}</h4><ul>{items}</ul></section>')
    for stance, label, cls in (
        ("supports", "Evidence for", "for"),
        ("opposes", "Evidence against", "against"),
        ("complicates", "Evidence — complicates", ""),
    ):
        rows = d["evidence"][stance]
        if rows:
            items = "".join(
                f"<li>{esc(e.summary)}<small>{fmt_date(e.observed_on)} · "
                f"{esc(e.source)}</small></li>"
                for e in rows
            )
            cols.append(f'<section class="{cls}"><h4>{esc(label)}</h4><ul>{items}</ul></section>')
    out = f'<div class="case">{"".join(cols)}</div>' if cols else ""
    if d["bare"]:
        out += (
            '<div class="warnbar"><b>No case recorded either way</b> — this is why it has '
            "not been decided. Deciding it needs research, not judgement: add a factor, or "
            "ask the question that would settle it.</div>"
        )
    if d["evidence"]["unstated"]:
        n = len(d["evidence"]["unstated"])
        items = "".join(
            f"<li>{esc(e.summary)}<small>{fmt_date(e.observed_on)} · "
            f"{esc(e.source)}</small></li>"
            for e in d["evidence"]["unstated"]
        )
        out += (
            f'<details><summary class="m">{n} further observation(s), stance not '
            f"recorded</summary><ul>{items}</ul></details>"
        )
    if d["open_questions"]:
        qs = "".join(
            f'<div class="q"><b>q{q.id}</b> ({fmt_date(q.asked_on)}) {esc(q.text)}</div>'
            for q in d["open_questions"]
        )
        out += f"<h2>Open questions</h2>{qs}"
    return out


def _sel(name: str, options, blank: str | None = None) -> str:
    opts = f'<option value="">{esc(blank)}</option>' if blank else ""
    opts += "".join(f'<option value="{esc(o)}">{esc(o)}</option>' for o in options)
    return f'<select name="{esc(name)}">{opts}</select>'


def actions(pid: int, stances, factor_kinds) -> str:
    """The four decisions and the two ways to build a case, as plain forms.

    No JavaScript: every action is a POST that redirects, so the browser's own
    back button and reload behave, and a failed write shows the reason rather
    than vanishing into a fetch nobody watched."""
    a = f"/triage/{int(pid)}"
    return f'''<div class="acts">
<form method="post" action="{a}/adopt"><h4>Adopt — I hold this</h4>
<input type="text" name="note" placeholder="why you hold it (optional)">
<button type="submit">Adopt</button></form>
<form method="post" action="{a}/reject"><h4>Reject — I do not hold this</h4>
<input type="text" name="note" placeholder="why not (required)" required>
<button type="submit" class="warn">Reject</button></form>
<form method="post" action="{a}/ask"><h4>Question — I would decide if I knew</h4>
<input type="text" name="text" placeholder="what would you need to know" required>
<button type="submit">Flag &amp; keep pending</button></form>
<form method="post" action="{a}/factor" class="ghost"><h4>Add a factor</h4>
{_sel("kind", factor_kinds)}
<textarea name="text" placeholder="a reason for, against, or the test that settles it"
 required></textarea>
<button type="submit">Add factor</button></form>
<form method="post" action="{a}/note" class="ghost"><h4>Add an observation</h4>
{_sel("stance", stances, blank="stance not recorded")}
<textarea name="text" placeholder="what was observed" required></textarea>
<button type="submit">Add evidence</button></form>
<form method="post" action="{a}/skip" class="ghost"><h4>Skip</h4>
<p class="note">No decision, no record. It comes back next time.</p>
<button type="submit">Next &rarr;</button></form>
</div>'''
