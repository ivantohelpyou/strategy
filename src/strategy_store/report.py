"""`strategy report` — render a framework cascade FROM the store (sea-mii7.1).

The document is output, not source. Sections: header (with the run's own
declared scope), one section per framework element, alignment tests, forcing
gates, integrity findings. Semantic contradictions between elements are not
automatable — the report puts rows side by side and a human decides.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from pathlib import Path

from jinja2 import Environment
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from . import db, env, queries
from .frameworks import FRAMEWORKS
from .models import SCOPE_RANK, Assessment, Evidence, Forcing, Position

MARK = {"holding": "○", "contested": "◐", "resolved": "✓", "retired": "❄"}
# Stored values stay terse; the report speaks plainly.
STATUS_LABEL = {
    "holding": "open",
    "contested": "disputed",
    "resolved": "decided",
    "retired": "dropped",
}
SCOPE_LABEL = {
    "web-chat": "decided in a chat, without seeing the code or projects",
    "single-rig": "decided inside one project",
    "fleet": "decided with the whole portfolio in view",
    "fleet+external": "decided with the portfolio plus outside facts",
}

TEMPLATE = """\
# {{ fw.name }} — strategy memo

_{{ now }}. Generated from the strategy store; a printout, not the source. Full
evidence and every flagged item: `{{ detail_name }}`. Reference numbers at the end._

## Bottom line — what needs your attention

{% if prose.bluf %}{{ prose.bluf }}

{% endif %}
{% for q in bluf %}
{{ loop.index }}. **{{ q.head }}** {{ q.body }}{% if q.ref %} _({{ q.ref }})_{% endif %}

{% endfor %}
{% for el in elements %}

## {{ el.ordinal }}. {{ el.name }}

{% if prose.get(el.key) %}{{ prose.get(el.key) }}

{% endif %}
{% if not el.rows %}
Nothing is decided here yet{% if el.required %}, and this box has to be filled before the rest can hold{% endif %}.
{% else %}
_On the record:_
{% set decided = el.rows|selectattr("status","equalto","resolved")|list %}
{% set open_ = el.rows|selectattr("status","equalto","holding")|list %}
{% set disputed = el.rows|selectattr("status","equalto","contested")|list %}
{% if decided %}
**Decided.** {% for p in decided %}{{ p.plain }}{% if p.decided_on %} ({{ p.decided_on|string|truncate(10, true, "") }}){% endif %}. {% endfor %}


{% endif %}
{% if open_ %}
**Still open.** {% for p in open_ %}{{ p.plain }}. {% endfor %}


{% endif %}
{% if disputed %}
**In dispute.** {% for p in disputed %}{{ p.plain }}. {% endfor %}

{% endif %}
{% endif %}
{% endfor %}

## Deadlines and triggers

{% for f in forcing.dated %}
- **{{ f.due_on|string|truncate(10, true, "") }}** ({{ f.days }} days) — {{ f.title }}{% if f.venture %} · {{ f.venture }}{% endif %}

{% endfor %}
{% if forcing.missed %}

Missed and still unresolved: {% for f in forcing.missed %}{{ f.title }}{% if not loop.last %}; {% endif %}{% endfor %}.
{% endif %}
{% if forcing.undated %}

Waiting on a date ({{ forcing.undated|length }}): {% for f in forcing.undated %}{{ f.title }}{% if not loop.last %}; {% endif %}{% endfor %}.
{% endif %}

## Housekeeping the store can see

{% for f in findings if f.rows %}
- {{ f.title }}: {{ f.rows|length }}
{% endfor %}

---

### Reference

{% for el in elements %}{% for p in el.rows %}
- #{{ p.id }} {{ p.plain|truncate(60) }} — {{ p.status_label }}; {{ p.scope_label }}; {{ p.decided_on or "no date" }}; `{{ p.ref }}`

{% endfor %}{% endfor %}

_Based on {{ scope.positions }} decisions ({{ scope.current }} current), {{ scope.evidence }} pieces of evidence, {{ scope.forcing }} deadlines; sources last pulled {{ scope.synced_line }}.{% if scope.stale %} ⚠ {{ scope.stale }}{% endif %}_
"""

DETAIL_TEMPLATE = """\
# {{ fw.name }} — detail

_Companion to the summary. Generated {{ now }}. Everything the summary counts is
listed here in full._

## What this is based on

- {{ scope.positions }} decisions on record ({{ scope.current }} current, {{ scope.superseded }} replaced by later ones),
  {{ scope.evidence }} pieces of evidence, {{ scope.forcing }} deadlines/triggers
- Last pulled from the source systems: {{ scope.synced_line }}
- How much was in view when decided: {{ scope.by_scope_line }}
{% for el in elements %}

## Box {{ el.ordinal }} — {{ el.name }}

_{{ el.prompt }}_

{% for p in el.rows %}
- {{ p.mark }} **#{{ p.id }}** {{ p.title }}
  {{ p.status_label }} · {{ p.scope_label }} · {{ p.decided_on or "no date" }} · from `{{ p.ref }}`{% if p.also_refs %}
  also answered in `{{ p.also_refs }}` — one question, {{ p.sources_n }} sources{% endif %}

{% endfor %}
{% for p in el.rows if p.status == "contested" or p.evidence %}
### #{{ p.id }} {{ "— in dispute" if p.status == "contested" else "— evidence" }}: {{ p.title[:80] }}

{{ p.statement_excerpt }}
{% for e in p.evidence %}

- [{{ e.scope }}] {{ e.observed_on or "" }} {{ e.source }}: {{ e.summary }}
{% endfor %}

{% endfor %}
{% endfor %}

## Questions to ask of any project

_{{ matrix.answered }} of {{ matrix.total }} answered. A blank cell is unanswered —
it is not a pass. Answer one with `strategy assess <venture> <test> --verdict pass|fail|irrelevant`._

{% for t in fw.tests %}{{ loop.index }}. **{{ t[0] }}** — {{ t[1] }}
{% endfor %}
{% if matrix.table %}

{{ matrix.table }}

_✓ passes · ✗ fails · – not applicable · blank not answered_
{% endif %}
{% if matrix.notes %}

{% for n in matrix.notes %}
- **{{ n.venture }} / {{ n.test }}** ({{ n.verdict }}): {{ n.note }}
{% endfor %}
{% endif %}

## Deadlines and triggers — with conditions

{% for f in forcing.dated %}
- **{{ f.due_on }}** (+{{ f.days }}d) {{ f.title }} — _{{ f.venture or "portfolio" }}_{% if f.pos %} · decision {{ f.pos }}{% endif %}
  gates: {{ f.gates }}

{% endfor %}
{% for f in forcing.undated %}
- **no date** {{ f.title }} — _{{ f.venture or "portfolio" }}_
  gates: {{ f.gates }}

{% endfor %}

## Problems the store can see — every item

{% for f in findings %}

### {{ f.title }} ({{ f.rows|length }})

{{ f.why }}
{% for r in f.rows %}
- {{ r }}
{% endfor %}
{% endfor %}

_What no report can catch: two decisions that contradict each other in meaning
(a deadline that another decision makes impossible, say). A person has to read
them side by side and choose._
"""


def _ref(p: Position) -> str:
    return f"{p.source_system}:{p.source_ref}"


_PREFIX = re.compile(
    r"^\s*(DECIDED|DONE|CONFLICT|WATCH|FIX|UNRESOLVED)\s*:\s*|^\s*[\U0001F300-\U0001FAFF\u2600-\u27BF]\s*",
    re.IGNORECASE,
)


def _plain(title: str) -> str:
    t = _PREFIX.sub("", title).strip()
    t = re.sub(r"\s*\((read-back, confirm|proposed)\)\s*", " ", t)
    t = t.rstrip(".")
    return t[:1].upper() + t[1:] if t else t


def claim_groups(rows: list[Position]) -> list[list[Position]]:
    """Positions that answer the same question, gathered.

    A shared claim_key means one question; everything else is its own group.
    Order is stable — first appearance wins — so the report does not reshuffle
    between runs."""
    groups: list[list[Position]] = []
    index: dict[str, int] = {}
    for p in rows:
        if p.claim_key and p.claim_key in index:
            groups[index[p.claim_key]].append(p)
            continue
        if p.claim_key:
            index[p.claim_key] = len(groups)
        groups.append([p])
    return groups


def _lead(group: list[Position]) -> Position:
    """The row that speaks for the claim: widest scope, then latest, then newest."""
    return max(
        group,
        key=lambda p: (SCOPE_RANK[p.scope], p.decided_on or date.min, p.id),
    )


def _claim_status(group: list[Position]) -> str:
    """One question answered two ways IS a dispute, whatever the rows say
    individually — that is the whole reason to gather them (sea-mii7.1)."""
    statuses = {p.status for p in group}
    if "contested" in statuses or len(statuses) > 1:
        return "contested"
    return statuses.pop()


def _row(group: list[Position] | Position, ev_n: dict) -> dict:
    group = [group] if isinstance(group, Position) else group
    p = _lead(group)
    status = _claim_status(group)
    others = [q for q in group if q.id != p.id]
    # Each other source becomes an evidence line under the one claim, rather
    # than a second position that reads like a second argument.
    evidence = [
        {
            "scope": q.scope,
            "observed_on": q.decided_on,
            "source": f"{_ref(q)} (#{q.id}, {q.status})",
            "summary": q.title,
        }
        for q in others
    ]
    for q in group:
        evidence += [
            {
                "scope": e.scope,
                "observed_on": e.observed_on,
                "source": e.source,
                "summary": e.summary,
            }
            for e in q.evidence
        ]
    return {
        "plain": _plain(p.title),
        "id": p.id,
        "mark": MARK[status],
        "status": status,
        "scope": p.scope,
        "status_label": STATUS_LABEL[status],
        "scope_label": SCOPE_LABEL[p.scope],
        "decided_on": p.decided_on,
        "ref": _ref(p),
        "also_refs": " · ".join(_ref(q) for q in others),
        "sources_n": len(group),
        "title": p.title,
        "evidence_n": ev_n.get(p.id, 0) + len(evidence),
        "statement_excerpt": "\n".join(p.statement.splitlines()[:8]),
        "evidence": evidence,
    }


def _elements(fw: dict, current: list[Position], ev_n: dict) -> list[dict]:
    out = []
    for key, ordinal, name, required, prompt in fw["elements"]:
        rows = sorted(
            (p for p in current if p.element_key == key),
            key=lambda p: (p.status != "contested", p.id),
        )
        groups = sorted(
            claim_groups(rows),
            key=lambda g: (_claim_status(g) != "contested", _lead(g).id),
        )
        out.append(
            {
                "key": key,
                "ordinal": ordinal,
                "name": name,
                "required": required,
                "prompt": prompt,
                "rows": [_row(g, ev_n) for g in groups],
            }
        )
    return out


VERDICT_MARK = {"pass": "✓", "fail": "✗", "irrelevant": "–"}


def _matrix(fw: dict, framework: str, current: list[Position], assessments) -> dict:
    """Tests x ventures. An unanswered cell stays BLANK — never defaulted to a
    pass, which would invent an opinion nobody holds (sea-mii7)."""
    ventures = sorted(
        {p.venture for p in current if p.venture} | {a.venture for a in assessments}
    )
    cells = {(a.test_key, a.venture): a for a in assessments if a.framework == framework}
    rows = []
    for key, text in fw["tests"]:
        rows.append(
            {
                "key": key,
                "text": text,
                "cells": [
                    {
                        "venture": v,
                        "mark": VERDICT_MARK.get(getattr(cells.get((key, v)), "verdict", ""), ""),
                        "verdict": getattr(cells.get((key, v)), "verdict", ""),
                        "note": getattr(cells.get((key, v)), "note", ""),
                    }
                    for v in ventures
                ],
            }
        )
    total = len(fw["tests"]) * len(ventures)
    answered = sum(1 for (k, v) in cells if v in ventures)
    # The table is assembled here rather than in the template: Jinja's
    # trim_blocks eats the newlines between inline loops and folds the whole
    # grid onto one line.
    table = []
    if ventures:
        table.append("| | " + " | ".join(ventures) + " |")
        table.append("|---|" + "---|" * len(ventures))
        for r in rows:
            table.append(f"| **{r['key']}** | " + " | ".join(c["mark"] or " " for c in r["cells"]) + " |")
    return {
        "ventures": ventures,
        "rows": rows,
        "table": "\n".join(table),
        "answered": answered,
        "total": total,
        "notes": [
            {"test": k, "venture": v, "verdict": a.verdict, "note": a.note}
            for (k, v), a in sorted(cells.items())
            if a.note and v in ventures
        ],
    }


def _forcing(forcings: list[Forcing]) -> dict:
    today = date.today()
    buckets = {"dated": [], "missed": [], "undated": []}
    for f in sorted(forcings, key=lambda f: (f.due_on is None, f.due_on or date.max)):
        if f.resolved_on:
            continue
        days = (f.due_on - today).days if f.due_on else None
        d = {
            "due_on": f.due_on,
            "title": f.title,
            "venture": f.venture,
            "gates": f.gates,
            "pos": f"#{f.position_id}" if f.position_id else None,
            "days": days,
        }
        bucket = "undated" if days is None else ("missed" if days < 0 else "dated")
        buckets[bucket].append(d)
    return buckets


def _finding(title: str, why: str, rows: list[str]) -> dict:
    return {"title": title, "why": why, "rows": rows}


def _findings(positions, current, elements, forcings, ev_n) -> list[dict]:
    """Each finding is a query, not an opinion."""
    by_id = {p.id: p for p in positions}
    _claimed: dict[str, int] = {}
    for p in current:
        if p.claim_key:
            _claimed[p.claim_key] = _claimed.get(p.claim_key, 0) + 1
    gated = {f.position_id for f in forcings if f.due_on}
    contested_no_gate = [p for p in current if p.status == "contested" and p.id not in gated]
    bad_super = [
        p
        for p in positions
        if p.superseded_by_id
        and by_id.get(p.superseded_by_id)
        and SCOPE_RANK[by_id[p.superseded_by_id].scope] < SCOPE_RANK[p.scope]
        and not ev_n.get(p.id)
    ]
    by_el: dict[str, list[Position]] = {}
    for p in current:
        # A position already gathered into a multi-source claim is no longer
        # "the same question answered in two places" — it is one claim with the
        # other sources attached as evidence, which is the fix this finding asks
        # for. Reporting it again would be asking for work already done.
        if p.element_key and not (p.claim_key and _claimed.get(p.claim_key, 0) > 1):
            by_el.setdefault(p.element_key, []).append(p)
    cross = [
        f"{key}: " + " · ".join(f"#{p.id} {_ref(p)} [{p.status}/{p.scope}]" for p in ps)
        for key, ps in by_el.items()
        if len({p.source_system for p in ps}) > 1 and any(p.status == "contested" for p in ps)
    ]
    return [
        _finding(
            "Decisions not filed under any box",
            "A decision that sits outside the boxes can't be checked against the aspiration "
            "(this is how decisions get orphaned). File it: `strategy set <id> --element`.",
            [f"#{p.id} `{_ref(p)}` {p.title}" for p in current if p.element_key is None],
        ),
        _finding(
            "Boxes with nothing decided",
            'The strategy can\'t hang together with an empty box.',
            [
                f"Box {e['ordinal']} — {e['name']}"
                for e in elements
                if e["required"] and not e["rows"]
            ],
        ),
        _finding(
            "Disputes with no date to settle them by",
            "An open argument with no deadline drifts. Give it a date, or drop it.",
            [f"#{p.id} `{_ref(p)}` {p.title}" for p in contested_no_gate],
        ),
        _finding(
            "Later decisions that overrode earlier ones while seeing less",
            "A decision made in a chat may not quietly override one made with the whole portfolio in view — unless it names new evidence.",
            [
                f"#{p.superseded_by_id} ({by_id[p.superseded_by_id].scope}) "
                f"supersedes #{p.id} ({p.scope})"
                for p in bad_super
            ],
        ),
        _finding(
            "Same question answered differently in different places",
            "The same question decided in more than one system, at least one still in dispute. "
            "Read them together; merge into one decision with the evidence attached, or "
            "mark one as replacing the other.",
            cross,
        ),
        _finding(
            "Deadlines with no date",
            "If it can't be true on a specific Tuesday, it isn't a deadline.",
            [
                f"{f.title} ({f.venture or 'portfolio'})"
                for f in forcings
                if f.due_on is None and not f.resolved_on
            ],
        ),
    ]


def _scope(positions, current, superseded_ids, forcings, ev_n) -> dict:
    now = datetime.now(timezone.utc)
    synced: dict[str, datetime] = {}
    for p in positions:
        if p.synced_at:
            synced[p.source_system] = max(synced.get(p.source_system, p.synced_at), p.synced_at)
    by_scope: dict[str, int] = {}
    for p in current:
        by_scope[p.scope] = by_scope.get(p.scope, 0) + 1
    stale = [
        f"{k} last synced {(now - t.replace(tzinfo=timezone.utc)).days} days ago"
        for k, t in synced.items()
        if (now - t.replace(tzinfo=timezone.utc)).days > 7
    ]
    return {
        "db": env.mask(db.db_url()),
        "positions": len(positions),
        "current": len(current),
        "superseded": len(superseded_ids),
        "evidence": sum(ev_n.values()),
        "forcing": len(forcings),
        "synced_line": " · ".join(f"{k} {v:%Y-%m-%d %H:%M UTC}" for k, v in sorted(synced.items())),
        "by_scope_line": ", ".join(
            f"{v} {SCOPE_LABEL[k]}"
            for k, v in sorted(by_scope.items(), key=lambda kv: SCOPE_RANK[kv[0]])
        ),
        "stale": "; ".join(stale),
        "now": now.strftime("%Y-%m-%d %H:%M UTC"),
    }


def _bluf(current, elements, forcings) -> list[dict]:
    """The questions that need the owner: disputes first, then near deadlines,
    then empty required boxes, then undated triggers. Capped at six."""
    today = date.today()
    out = []
    for f in sorted(
        (f for f in forcings if f.due_on and not f.resolved_on), key=lambda f: f.due_on
    ):
        days = (f.due_on - today).days
        if days <= 21:
            out.append(
                {
                    "head": f"{f.title} — due {f.due_on}, {days} days.",
                    "body": f.condition,
                    "ref": f"gate #{f.id}",
                }
            )
    for p in sorted((p for p in current if p.status == "contested"), key=lambda p: p.id):
        out.append(
            {"head": _plain(p.title) + ".", "body": "Decide which way it goes.", "ref": f"#{p.id}"}
        )
    for e in elements:
        if e["required"] and not e["rows"]:
            out.append(
                {
                    "head": f"Box {e['ordinal']} ({e['name']}) is empty.",
                    "body": "Nothing can be checked against it until it is filled.",
                    "ref": "",
                }
            )
    undated = [f for f in forcings if not f.due_on and not f.resolved_on]
    if undated:
        out.append(
            {
                "head": f"{len(undated)} triggers have no date.",
                "body": "Give each a date it can be true on, or drop it: "
                + "; ".join(f.title for f in undated[:4])
                + ("…" if len(undated) > 4 else "."),
                "ref": "",
            }
        )
    return out[:6]


def build_context(framework: str) -> dict:
    fw = FRAMEWORKS[framework]
    with db.session() as s:
        # Eager-load evidence: `_row` touches p.evidence for every position, and
        # lazily that is one round trip each — 2.4s of this call's 2.5s against a
        # hosted store, slow enough that a one-second readiness probe never saw
        # the page come up.
        positions = list(s.scalars(select(Position).options(selectinload(Position.evidence))))
        # Candidate cascades are hypotheses, not the record (solutions-tx3.1).
        speculative = queries.speculative_ids(s)
        positions = [p for p in positions if p.id not in speculative]
        forcings = list(s.scalars(select(Forcing)))
        ev_n = dict(
            s.execute(
                select(Evidence.position_id, func.count()).group_by(Evidence.position_id)
            ).all()
        )
        superseded_ids = {p.id for p in positions if p.superseded_by_id}
        # One reader for "what is current", so the memo, the admin cascade and
        # the CLI cannot disagree (position #67). This used to be a third
        # hand-written filter here, and it was the one that missed `retired` —
        # a box cut to five still rendered seven.
        current = queries.current_positions(s)
        assessments = list(s.scalars(select(Assessment)))
        elements = _elements(fw, current, ev_n)
        matrix = _matrix(fw, framework, current, assessments)
        scope = _scope(positions, current, superseded_ids, forcings, ev_n)
        bluf = _bluf(current, elements, forcings)
        return {
            "fw": fw,
            "bluf": bluf,
            "elements": elements,
            "matrix": matrix,
            "findings": _findings(positions, current, elements, forcings, ev_n),
            "forcing": _forcing(forcings),
            "scope": scope,
            "now": scope["now"],
        }


PROSE_PATH = (
    # The TRACKED home. This used to point at ~/gt/solutions/crew/ivan/now/strategy,
    # which is gitignored there (`**/crew/`) — so the hand-written prose that heads
    # the memo had no history, and nobody noticed it had been stale since Aug 17
    # while the generated numbers beneath it kept updating. 2026-08-23.
    Path.home() / "projects" / "spawn" / "spawn-solutions" / "now" / "strategy" / "memo_prose.yaml"
)


def load_prose(path: Path | None = None) -> dict:
    """Authored full-sentence paragraphs per box (and a BLUF); the generator
    prints them first and the store's record beneath. Missing file → empty."""
    path = path or PROSE_PATH
    if not path.exists():
        return {}
    import yaml

    data = yaml.safe_load(path.read_text()) or {}
    return {k: str(v).strip() for k, v in data.items() if not str(k).startswith("_")}


def render(framework: str = "ptw", detail: bool = False, detail_name: str = "") -> str:
    env = Environment(autoescape=False, trim_blocks=True, lstrip_blocks=True)
    ctx = build_context(framework)
    ctx["prose"] = load_prose()
    ctx["detail_name"] = detail_name or f"{ctx['fw']['name'].replace(' ', '_')}_Detail.md"
    return env.from_string(DETAIL_TEMPLATE if detail else TEMPLATE).render(**ctx)
