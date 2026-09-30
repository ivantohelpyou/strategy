"""`strategy prime` — the Direction block for session start.

Push, not pull: an agent that does not know the store exists never queries it,
so gt prime's wrapper appends this. Read-only, one SQLite read, never blocks a
session (any failure prints what is cached, flags it, exits 0).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from . import db, env, queries
from .models import Forcing, Position

MARK = {"holding": "○", "contested": "◐", "resolved": "✓", "retired": "❄"}
GT_ROOT = env.path_value("GT_ROOT", Path.home() / "gt")


def detect_rig(cwd: Path | None = None) -> str | None:
    """~/gt/<rig>/... → <rig>; the mayor dir and anything outside ~/gt → None (portfolio)."""
    cwd = (cwd or Path.cwd()).resolve()
    try:
        rel = cwd.relative_to(GT_ROOT.resolve())
    except ValueError:
        return None
    if not rel.parts or rel.parts[0] in {"mayor", "deacon", "daemon", "logs", "scripts"}:
        return None
    return rel.parts[0]


def _hours(synced: datetime) -> float:
    return (datetime.now(timezone.utc) - synced.replace(tzinfo=timezone.utc)).total_seconds() / 3600


def _span(hours: float) -> str:
    if hours < 1:
        return "<1h"
    if hours < 48:
        return f"{int(hours)}h"
    return f"{int(hours // 24)}d"


def _age(synced: datetime | None) -> tuple[str, bool]:
    if not synced:
        return "never synced", True
    hours = _hours(synced)
    return f"synced {_span(hours)} ago", hours > 24 * 7


def sync_ages(positions) -> dict[str, float]:
    """Hours since the last sync, per source system — synthetic sources
    (`manual`, `doc`) have no upstream and are left out."""
    ages: dict[str, float] = {}
    for p in positions:
        if not p.synced_at:
            continue
        h = _hours(p.synced_at)
        ages[p.source_system] = min(ages.get(p.source_system, h), h)
    return ages


def freshness(positions) -> tuple[str, bool]:
    """Age of the STALEST source, not the freshest.

    Reporting max(synced_at) is how the store told every session "synced <1h
    ago" for three days while its 26 Vikunja rows sat at 85h: the daily hook
    reseed was refreshing beads only (sea-mii7.3). A summary that cannot go
    stale cannot warn."""
    ages = sync_ages(positions)
    if not ages:
        return "never synced", True
    worst_src, worst = max(ages.items(), key=lambda kv: kv[1])
    text, stale = _age_from_hours(worst)
    if len(ages) > 1 and worst - min(ages.values()) > 12:
        text += f" ({worst_src})"
    return text, stale


def _age_from_hours(hours: float) -> tuple[str, bool]:
    return f"synced {_span(hours)} ago", hours > 24 * 7


def maybe_reseed(older_than_hours: float | None) -> str:
    """Reseed if the newest sync is older than the threshold. Never raises."""
    if older_than_hours is None:
        return ""
    try:
        from . import seed as seed_mod

        with db.session() as s:
            ages = sync_ages(list(s.scalars(select(Position))))
        # The STALEST source decides, not the freshest: beads refreshing on its
        # own kept this under the threshold while Vikunja aged out (sea-mii7.3).
        if ages and max(ages.values()) < older_than_hours:
            return ""
        projects = seed_mod._vikunja_projects()
        rows = (seed_mod.fetch_vikunja() if projects else []) + seed_mod.fetch_beads()
        with db.session() as s:
            c = seed_mod.upsert(s, rows)
        note = f"(reseeded: +{c['inserted']} new, {c['updated']} refreshed"
        if not projects:
            note += "; VIKUNJA SKIPPED — STRATEGY_VIKUNJA_PROJECTS unset"
        return note + ")"
    except Exception as exc:  # noqa: BLE001 — a hook must never take the session down
        return f"(reseed failed: {type(exc).__name__}: {str(exc)[:80]}; showing cached)"


def _newest_first(rows: list) -> list:
    """Most recently decided first; undated rows last, newest id first within a tie."""
    return sorted(rows, key=lambda p: (p.decided_on is not None, p.decided_on, p.id), reverse=True)


def render(rig: str | None, reseed_note: str = "") -> str:
    with db.session() as s:
        # ORDER BY, always: an unordered SELECT returns Postgres heap order, and
        # a heap reorders every time a row is UPDATEd. A reseed was silently
        # changing WHICH three where-to-play positions this block printed.
        positions = list(s.scalars(select(Position).order_by(Position.id)))
        forcings = [f for f in s.scalars(select(Forcing).order_by(Forcing.id)) if not f.resolved_on]
        # Candidate cascades hold hypotheses. This block is pushed into the top
        # of every session by the gt prime hook, so a speculative where-to-play
        # reaching it would read as decided direction (solutions-tx3.1).
        speculative = queries.speculative_ids(s)
        # Answerable without leaving this database, so computed NOW rather than
        # read from the cache: a stale answer that could have been current is a
        # stale answer for no reason.
        from . import beadlink as _bl

        free_drift = _bl.free_findings(s)
    superseded = {p.id for p in positions if p.superseded_by_id}
    current = [p for p in positions if p.id not in superseded and p.id not in speculative]
    age, stale = freshness(positions)
    out = [
        f"## Direction (strategy store, {age}{' — STALE' if stale else ''}) {reseed_note}".rstrip()
    ]

    # Box 1 holds the aspiration AND the proposals circling it (win conditions,
    # master-family calls). The aspiration is the stable row — the one that was
    # there first — so age, not recency, picks it; the rest are counted, not hidden.
    asp = sorted([p for p in current if p.element_key == "ptw.box1"], key=lambda p: p.id)
    for p in asp[:1]:
        out.append(f"Aspiration: {p.title}")
    if len(asp) > 1:
        n_con = sum(1 for p in asp[1:] if p.status == "contested")
        out.append(
            f"  (+{len(asp) - 1} more in box 1"
            + (f", {n_con} contested" if n_con else "")
            + " → `strategy list --element ptw.box1`)"
        )
    where = _newest_first(
        [p for p in current if p.element_key == "ptw.box2" and p.status == "resolved"]
    )
    for p in where[:3]:
        out.append(f"Where to play: {p.title[:90]}  ({p.source_system}:{p.source_ref})")
    if len(where) > 3:
        # Say what was cut. Three of eight, chosen by whatever order the database
        # felt like, read as "these are the where-to-play positions".
        out.append(
            f"  (+{len(where) - 3} more where-to-play → `strategy list --element ptw.box2`)"
        )

    # Recent fleet-scope decisions in ANY box — the newest direction, before the
    # rig-specific rows. (A research-rig agent on 2026-08-17 read a purr docfile
    # from Aug 5 as current strategy because prime only printed box1/box2.)
    recent_cut = date.today() - timedelta(days=14)
    recent = [
        p
        for p in current
        if p.scope in ("fleet", "fleet+external")
        and p.decided_on
        and p.decided_on >= recent_cut
        and p.element_key != "ptw.box1"
    ]
    if recent:
        out.append(f"Recent fleet-scope decisions (last 14d): {len(recent)}")
        for p in sorted(recent, key=lambda p: (p.decided_on, p.id), reverse=True)[:6]:
            out.append(f"  {MARK[p.status]} {p.element_key or '—':<8} {p.title[:84]}")

    mine = [p for p in current if rig and p.venture == rig]
    # Contested rows in the same boxes as this rig's rows are this rig's business too.
    boxes = {p.element_key for p in mine if p.element_key}
    mine += [
        p for p in current if p.status == "contested" and p.element_key in boxes and p not in mine
    ]
    if rig:
        n_con = sum(1 for p in mine if p.status == "contested")
        out.append(f"This rig ({rig}): {len(mine)} current positions · {n_con} contested")
        for p in sorted(mine, key=lambda p: (p.status != "contested", p.id))[:6]:
            out.append(
                f"  {MARK[p.status]} {p.source_ref:<14} {p.title[:70]}  [{p.status}/{p.scope}]"
            )

    today = date.today()
    soon = sorted((f for f in forcings if f.due_on), key=lambda f: f.due_on)
    my_gates = [f for f in soon if rig and f.venture == rig]
    fleet_gates = [f for f in soon if (f.due_on - today).days <= 30 and f not in my_gates]
    if my_gates:
        out.append(
            "Gates (this rig): "
            + " · ".join(
                f"{f.due_on:%b %-d} ({(f.due_on - today).days:+d}d) {f.title[:45]}"
                for f in my_gates[:3]
            )
        )
    if fleet_gates:
        out.append(
            "Fleet gates ≤30d: "
            + " · ".join(f"{f.due_on:%b %-d} {f.title[:40]}" for f in fleet_gates[:3])
        )
    missed = [f for f in soon if (f.due_on - today).days < 0]
    undated = [f for f in forcings if not f.due_on and (not rig or f.venture in (rig, None))]
    if missed or undated:
        where = " on this rig/portfolio" if rig else ""
        out.append(
            f"Missed gates: {len(missed)} · undated gates{where}: {len(undated)}"
            "  →  `strategy forcing`"
        )
    out += gate_drift_lines(free_drift)
    out += _goal_lines()
    out += _drops_lines()
    out.append("Full picture: `strategy list` · `strategy report` · store rules in sea-mii7")
    return "\n".join(out)


def gate_drift_lines(free_drift: list) -> list[str]:
    """Gate drift: the free classes live, the rig-reading ones from the cache.

    This block opens EVERY session in EVERY rig, so what it may cost decides its
    shape. A position satisfier is one local SELECT and is therefore computed
    here and now. A bead satisfier is a `bd show` — ~0.5s, and ~5s for an id no
    rig has, because a miss searches all fourteen. That read belongs in
    `strategy forcing` and `strategy reconcile`, which a person invokes on
    purpose; those leave their answer in the cache and this reads it.

    THE AGE IS PART OF THE FINDING, and only for the cached half. A stale answer
    presented as a current one is the same failure this block exists to surface,
    one level up — so a check older than a week says so instead of quietly looking
    clean, and a store that has never been checked says that rather than nothing.
    Live findings carry no age because they have none.
    """
    from . import beadlink

    lines: list[str] = []
    settled = [f for f in free_drift if f.kind in (beadlink.SETTLED, beadlink.MISSING)]
    stale = [f for f in free_drift if f.kind == beadlink.STALE]
    for f in settled:
        lines.append(
            f"GATE SETTLED: #{f.gate_id} {f.gate_title[:50]} — {f.detail[:80]}"
            f"  →  `strategy gate resolve {f.gate_id}`"
        )
    if stale:
        ids = ", ".join(f"#{f.gate_id}" for f in stale[:4])
        lines.append(
            f"Gates due soon naming nothing that would satisfy them: {ids}"
            f"{' …' if len(stale) > 4 else ''}  →  `strategy gate link`"
        )
    return lines + _cached_drift_lines()


def _cached_drift_lines() -> list[str]:
    """The bead classes, which cannot be recomputed here at an acceptable cost."""
    from . import beadlink

    findings, checked_at = beadlink.read_cache()
    if checked_at is None:
        return ["Gate drift: never checked  →  `strategy forcing`"]
    age_h = _hours(checked_at.replace(tzinfo=None))
    age = _span(age_h)
    if not findings:
        # Only worth a line once it is old enough to be worth re-running.
        return [f"Gate drift: none as of {age} ago"] if age_h > 24 * 7 else []

    # The free classes are computed live by the caller; showing the cached copy
    # too would print the same finding twice, with two different ages.
    real = [f for f in findings if f.kind in (beadlink.SHIPPED, beadlink.SUPERSEDED)]
    if not real:
        return []
    head = real[0]
    extra = f" (+{len(real) - 1} more)" if len(real) > 1 else ""
    return [
        f"GATE DRIFT ({age} ago): #{head.gate_id} {head.gate_title[:52]} — "
        f"{head.detail[:70]}{extra}  →  `strategy forcing`"
    ]


def _goal_lines() -> list[str]:
    """The thermometer: every metric with gates, latest reading, next gate, 7-day delta."""
    from .metrics import goal_lines
    from .models import Metric

    db.init_db()
    with db.session() as s:
        ms = list(s.scalars(select(Metric).order_by(Metric.beat, Metric.name)))
        return goal_lines(ms)


def _drops_lines() -> list[str]:
    """Scoreboard + the manual chore it depends on (LinkedIn stats are a hand export)."""
    from .drops import needs_stats, scoreboard
    from .models import Drop

    db.init_db()  # additive schema changes land here too, not only via `strategy drop`
    with db.session() as s:
        drops = list(s.scalars(select(Drop)))
    if not drops:
        return []
    sb = scoreboard(drops)
    lines = [
        f"Drops: last 7d dropped {sb['dropped']}, on base {sb['on_base']}/{sb['scored']} scored"
        f" · last hit {sb['last_hit'] or 'never'} · followers gained {sb['followers']}"
    ]
    due = needs_stats(drops)
    if due:
        lines.append(
            f"DOWNLOAD STATS for {len(due)} drop(s) → `strategy drop ingest --downloads "
            f"{env.env_value('STRATEGY_DOWNLOADS_DIR') or '<downloads-dir>'} --archive` : "
            + " · ".join(f"{d.posted_on} {d.title[:30]}" for d, _ in due[:3])
        )
    return lines
