"""Read-side queries, in one place, so the CLI and the web surface answer the
same question the same way.

Position #67 (2026-08-24) settles that the CLI is not the eventual surface and a
second reader of the same store is coming, and says where the shared logic
belongs: "in code both surfaces call, not in the CLI's command handlers." This
module is the read half of that. The write half — the supersession scope rule,
store-owned columns, claim contest — stays in the CLI until a second WRITER
exists to justify moving it; the web surface is read-only precisely so that
refactor is not a prerequisite for having a surface at all.

Everything here returns data, never prints. `report.build_context` is the third
shared reader and already had this shape.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from . import beadlink, db, env
from .models import (
    Assessment,
    Cascade,
    CascadePosition,
    Drop,
    Evidence,
    Factor,
    Forcing,
    Metric,
    Position,
    Question,
)

STATUS_MARK = {"holding": "○", "contested": "◐", "resolved": "✓", "retired": "❄"}


def speculative_ids(session) -> set[int]:
    """Positions that exist ONLY inside candidate cascades.

    A candidate cascade is one Ivan is trying on — "given today's capabilities,
    where else could I play?" — and the box2 rows it generates are hypotheses,
    not claims he holds. `prime` prints box1 and box2 into the top of every
    session through the gt prime hook, so without this set a speculative
    where-to-play would arrive in the next agent's context indistinguishable
    from a decided one. That is the same failure the assessment matrix avoids by
    refusing to default an empty cell to "pass": an unanswered question must not
    manufacture an opinion nobody holds (solutions-tx3.1).

    Three states, and the first is the one that must not regress:

      no memberships at all       visible — every position in the store today
      >=1 membership to actual    visible
      memberships, all candidate  hidden

    Standing is the only axis here. A RETIRED actual cascade still counts as
    actual: it holds positions that were genuinely held, and conflating "no
    longer current" with "never real" would quietly delete history. Retirement
    is `cascade show`'s business, not this gate's.
    """
    held: set[int] = set()
    tried: set[int] = set()
    for pid, standing in session.execute(
        select(CascadePosition.position_id, Cascade.standing).join(
            Cascade, Cascade.id == CascadePosition.cascade_id
        )
    ):
        (held if standing == "actual" else tried).add(pid)
    return tried - held


def current_positions(session) -> list[Position]:
    """Every position a reader should be shown: not superseded, not retired, not
    speculative.

    prime and report both need exactly this and used to each write their own
    supersession filter. One reader, so the two surfaces cannot disagree about
    what is current (position #67).

    Retired joined the hidden set on 2026-08-30. A retired position is a decision
    NOT to hold something — real, dated, worth keeping, and not part of the
    current cascade. Leaving them in meant a box that had been cut to five still
    listed seven, which is the opposite of what the cut was for."""
    rows = list(session.scalars(select(Position).order_by(Position.id)))
    hidden = (
        {p.id for p in rows if p.superseded_by_id}
        | {p.id for p in rows if p.status == "retired"}
        | speculative_ids(session)
    )
    return [p for p in rows if p.id not in hidden]


def superseded_ids(session) -> set[int]:
    """Ids that some other row replaced — the rows `list` hides by default."""
    return {
        pid
        for (pid,) in session.execute(
            select(Position.id).where(Position.superseded_by_id.is_not(None))
        )
    }


def positions(
    session,
    *,
    status: str | None = None,
    element: str | None = None,
    source: str | None = None,
    venture: str | None = None,
    show_all: bool = False,
) -> tuple[list[Position], set[int]]:
    """Positions with the CLI's filters. `element='none'` means the orphans —
    a position in no framework box, which is itself a report finding."""
    q = select(Position)
    if status:
        q = q.where(Position.status == status)
    if element == "none":
        q = q.where(Position.element_key.is_(None))
    elif element:
        q = q.where(Position.element_key == element)
    if source:
        q = q.where(Position.source_system == source)
    if venture == "none":
        q = q.where(Position.venture.is_(None))
    elif venture:
        q = q.where(Position.venture == venture)
    supers = superseded_ids(session)
    rows = list(session.scalars(q))
    if not show_all:
        # Candidate-only rows are hypotheses; `--all` is the deliberate way to
        # see them, the same escape hatch superseded rows already had. Both sets
        # are read ONCE — a membership lookup per row is how the per-position
        # dossier call made the admin page take 27 seconds.
        hidden = supers | speculative_ids(session)
        # A retired position is a decision not to hold something: history, not
        # cascade. `--all` and an explicit `--status retired` still reach it.
        if status != "retired":
            hidden = hidden | {p.id for p in rows if p.status == "retired"}
        rows = [p for p in rows if p.id not in hidden]
    rows.sort(key=lambda p: (p.element_key or "~", p.id))
    return rows, supers


def pending(session, element: str | None = None) -> list[Position]:
    """Drafted but never adopted. 'Drafted' is `decided_on IS NULL`: a claim
    nobody has dated is a claim nobody has decided."""
    q = select(Position).where(Position.decided_on.is_(None))
    if element:
        q = q.where(Position.element_key == element)
    return sorted(session.scalars(q), key=lambda p: (p.element_key or "~", p.id))


def gates(session, *, show_all: bool = False, today: date | None = None) -> list[dict[str, Any]]:
    """Dated gates soonest first; undated last — they are the finding, not a
    tail. Each row carries `days` (None when undated) and `missed`."""
    today = today or date.today()
    rows = list(session.scalars(select(Forcing).options(selectinload(Forcing.satisfiers))))
    if not show_all:
        rows = [r for r in rows if r.resolved_on is None]
    rows.sort(key=lambda r: (r.due_on is None, r.due_on or date.max))
    out = []
    for r in rows:
        days = (r.due_on - today).days if r.due_on else None
        out.append(
            {
                "row": r,
                "days": days,
                "missed": days is not None and days < 0,
                "undated": r.due_on is None,
                # What would close it: a bead that closes, an event that happens,
                # both, or nothing recorded yet. UNCLASSIFIED means nobody has
                # looked — deliberately not the same as "nothing could satisfy it".
                "satisfaction": beadlink.classify(r.satisfiers),
                "satisfiers": sorted(r.satisfiers, key=lambda x: (x.kind, x.bead_id or "")),
            }
        )
    return out


def drift(session) -> list[dict[str, Any]]:
    """Where the source system's status and the store's disagree.

    Reports only; the store is authoritative for status by design, so this is a
    list to read, never a list to apply."""
    from . import seed as seed_mod

    out = []
    for p in session.scalars(select(Position)):
        if p.source_system == "vikunja":
            mapped, _ = seed_mod._prefix_status(p.title)
        elif p.source_system == "beads":
            mapped = seed_mod.BEADS_STATUS.get(p.source_status or "open", "holding")
        else:
            continue
        if mapped != p.status:
            out.append({"position": p, "store": p.status, "source_maps_to": mapped})
    return out


def evidence_counts(session) -> dict[int, int]:
    return dict(
        session.execute(
            select(Evidence.position_id, func.count()).group_by(Evidence.position_id)
        ).all()
    )


def orphans(session) -> list[Position]:
    """Current positions in no framework box."""
    rows, supers = positions(session, element="none")
    return rows


SETTING_KEYS = (
    "STRATEGY_VIKUNJA_PROJECTS",
    "STRATEGY_EXPORT_PATH",
    "STRATEGY_DOWNLOADS_DIR",
    "STRATEGY_FORCING_SEED",
    "STRATEGY_SEED_MAP",
    "STRATEGY_PROFILE_RULES",
    "GT_ROOT",
)


def store_config() -> dict[str, Any]:
    """What `doctor` reports about configuration — resolved without touching
    the database, so it still answers when the store is unreachable."""
    cfg = db.store()
    return {
        "url": env.mask(cfg.value),
        "origin": cfg.origin,
        "env_files": [(str(f), f.exists()) for f in env.ENV_FILES],
        "settings": [
            (k, r.origin, env.mask(r.value) if r.value else "—")
            for k, r in ((k, env.resolve(k)) for k in SETTING_KEYS)
        ],
    }


def store_health(session) -> dict[str, Any]:
    """Row counts and per-source sync age. STALE is >48h, the same threshold
    `doctor` prints."""
    from .prime import sync_ages

    rows = list(session.scalars(select(Position)))
    ages = sync_ages(rows)
    counts: dict[str, int] = {}
    for p in rows:
        counts[p.source_system] = counts.get(p.source_system, 0) + 1
    sources = []
    for src in sorted(counts):
        hours = ages.get(src)
        sources.append(
            {
                "source": src,
                "rows": counts[src],
                "hours": hours,
                "stale": hours is not None and hours > 48,
            }
        )
    return {
        "positions": len(rows),
        "sources": sources,
        # Sources drifting apart by >12h means a reseed refreshed only some.
        "split": bool(ages) and (max(ages.values()) - min(ages.values()) > 12),
        "evidence": session.scalar(select(func.count()).select_from(Evidence)) or 0,
        "gates": session.scalar(select(func.count()).select_from(Forcing)) or 0,
        "assessments": session.scalar(select(func.count()).select_from(Assessment)) or 0,
        "drops": session.scalar(select(func.count()).select_from(Drop)) or 0,
        "metrics": session.scalar(select(func.count()).select_from(Metric)) or 0,
    }


# ------------------------------------------------------------------- dossier


def dossier(session, pid: int) -> dict[str, Any]:
    """Everything needed to DECIDE one position, in one read.

    The reason positions stalled in `holding` was not indecision — it was that a
    position carried its claim and nothing else. The reader had to reconstruct
    the case for, the case against, and what would settle it, every time, from
    scratch. So they didn't (2026-08-28). This is that reconstruction, done once
    by the store: the claim, the arguments either side, the observations either
    side, and what is still unknown.

    Shared by the triage loop and (later) the web card, so the two cannot show a
    different case for the same position.
    """
    p = session.get(Position, pid)
    if p is None:
        return {}
    factors = list(session.scalars(select(Factor).where(Factor.position_id == pid)))
    ev = sorted(
        session.scalars(select(Evidence).where(Evidence.position_id == pid)),
        key=lambda e: (e.observed_on or date.min, e.id),
    )
    qs = sorted(
        session.scalars(select(Question).where(Question.position_id == pid)),
        key=lambda q: (q.answered_on is not None, q.id),
    )
    gates = sorted(
        session.scalars(select(Forcing).where(Forcing.position_id == pid)),
        key=lambda f: (f.due_on is None, f.due_on or date.max),
    )
    by_kind = {k: [f for f in factors if f.kind == k] for k in ("for", "against", "test")}
    by_stance = {
        "supports": [e for e in ev if e.stance == "supports"],
        "opposes": [e for e in ev if e.stance == "opposes"],
        "complicates": [e for e in ev if e.stance == "complicates"],
        # Written before stance existed, or recorded without one. Kept separate
        # rather than folded into "supports": an unstated stance is not agreement.
        "unstated": [e for e in ev if e.stance is None],
    }
    return {
        "position": p,
        "factors": by_kind,
        "factors_n": len(factors),
        "evidence": by_stance,
        "evidence_n": len(ev),
        "questions": qs,
        "open_questions": [q for q in qs if q.answered_on is None],
        "gates": gates,
        # A position nobody has argued either way about — the state that made the
        # pile unreadable, and the one worth counting.
        "bare": not factors and not any(by_stance[k] for k in ("supports", "opposes")),
    }


def open_issues(
    session, element: str | None = None, *, pending_only: bool = False
) -> list[dict[str, Any]]:
    """Every OPEN issue, in the order it is worth working, with the flags the
    queue needs — in four queries, not four per position.

    Open is deliberately wider than `pending`. The pile is positions nobody has
    dated, but an undated position is not the only kind of unresolved question:

      * a CONTESTED position carries a date and is still an open dispute — #49
        was decided on 2026-08-23, is contested, and has a gate three days out.
        Walking only the pending pile skipped the most urgent row in the store.
      * a position with an OPEN QUESTION is unresolved by construction, whatever
        its date says.

    Order: whatever a dated gate forces, soonest first; then disputes; then rows
    that already have a case (they can actually be decided now); bare ones last,
    because deciding them needs research rather than judgement.

    Built from bulk reads on purpose. Calling `dossier` per row cost ~280 round
    trips against a hosted store and made the page take 27 seconds.
    """
    supers = superseded_ids(session)
    rows = [
        p
        for p in session.scalars(select(Position))
        if p.id not in supers and p.status != "retired"
    ]
    if element:
        rows = [p for p in rows if p.element_key == element]

    open_q: dict[int, int] = {}
    for pid, n in session.execute(
        select(Question.position_id, func.count())
        .where(Question.answered_on.is_(None))
        .group_by(Question.position_id)
    ):
        open_q[pid] = n
    factor_n: dict[int, int] = dict(
        session.execute(
            select(Factor.position_id, func.count()).group_by(Factor.position_id)
        ).all()
    )
    # Only a stated stance counts as a case; an unstated one is not agreement.
    stanced: dict[int, int] = dict(
        session.execute(
            select(Evidence.position_id, func.count())
            .where(Evidence.stance.in_(("supports", "opposes")))
            .group_by(Evidence.position_id)
        ).all()
    )
    due: dict[int, date] = {}
    for f in session.scalars(select(Forcing).where(Forcing.position_id.is_not(None))):
        if f.due_on and f.resolved_on is None:
            cur = due.get(f.position_id)
            if cur is None or f.due_on < cur:
                due[f.position_id] = f.due_on

    out = []
    for p in rows:
        is_open = p.decided_on is None or p.status == "contested" or p.id in open_q
        if not is_open or (pending_only and p.decided_on is not None):
            continue
        bare = not factor_n.get(p.id) and not stanced.get(p.id)
        out.append(
            {
                "position": p,
                "due_on": due.get(p.id),
                "open_questions": open_q.get(p.id, 0),
                "factors_n": factor_n.get(p.id, 0),
                "bare": bare,
                "contested": p.status == "contested",
            }
        )
    out.sort(
        key=lambda r: (
            r["due_on"] or date.max,
            not r["contested"],
            not r["open_questions"],
            r["bare"],
            r["position"].element_key or "~",
            r["position"].id,
        )
    )
    return out


def triage_queue(
    session, element: str | None = None, *, pending_only: bool = False
) -> list[Position]:
    """The same queue as :func:`open_issues`, as bare positions."""
    return [r["position"] for r in open_issues(session, element, pending_only=pending_only)]


def shared_positions(
    session, *, include_candidates: bool = False, element: str | None = None
) -> dict[str, Any]:
    """Which positions more than one cascade rests on — the layer/vertical report.

    The reason cascades share positions rather than copying them. A position
    several cascades hold is load-bearing across brands: it is the horizontal
    platform, and it is why contesting it is expensive. A position exactly one
    cascade holds is that brand's own — the vertical.

    Candidates are excluded by default and counted separately. A hypothesis
    "sharing" a position with a held cascade is not evidence of anything, and
    letting it raise a position's count would inflate the platform with
    speculation (solutions-tx3.1).

    UNASSIGNED is reported, not dropped, and split by REASON, because the three
    are different findings. A retired position is deliberately out. A position
    with no framework box cannot be placed without inventing one. But a current,
    boxed position in no cascade at all is the only one of the three that means
    somebody missed it — so it is counted on its own rather than hidden inside a
    total that always looks large.
    """
    live = [c for c in session.scalars(select(Cascade)) if c.retired_on is None]
    counted = [c for c in live if include_candidates or c.standing == "actual"]
    by_id = {c.id: c for c in counted}
    live_ids = {c.id for c in live}
    holders: dict[int, list[Cascade]] = {}
    # Held by ANY live cascade, counted or not — the difference between "nobody
    # placed this" and "this is deliberately speculative".
    placed: set[int] = set()
    for m in session.scalars(select(CascadePosition)):
        if m.cascade_id in live_ids:
            placed.add(m.position_id)
        c = by_id.get(m.cascade_id)
        if c is not None:
            holders.setdefault(m.position_id, []).append(c)

    supers = superseded_ids(session)
    current = [p for p in session.scalars(select(Position)) if p.id not in supers]
    if element:
        current = [p for p in current if p.element_key == element]

    rows = []
    for p in current:
        held = holders.get(p.id, [])
        if not held:
            continue
        rows.append(
            {
                "position": p,
                "cascades": sorted(c.key for c in held),
                "n": len(held),
            }
        )
    rows.sort(key=lambda r: (-r["n"], r["position"].element_key or "~", r["position"].id))

    distribution: dict[int, int] = {}
    for r in rows:
        distribution[r["n"]] = distribution.get(r["n"], 0) + 1

    unplaced = [p for p in current if not holders.get(p.id)]
    return {
        "cascades": sorted(counted, key=lambda c: c.key),
        "rows": rows,
        "distribution": dict(sorted(distribution.items(), reverse=True)),
        "unassigned": {
            "total": len(unplaced),
            "retired": sum(1 for p in unplaced if p.status == "retired"),
            "no_box": sum(
                1 for p in unplaced if p.status != "retired" and not p.element_key
            ),
            # In a cascade, just not a counted one. Deliberately speculative, so
            # not a gap — reporting it as missed would send someone to place a
            # position that is already exactly where it belongs.
            "candidate_only": sum(
                1
                for p in unplaced
                if p.status != "retired" and p.element_key and p.id in placed
            ),
            # The only bucket that means somebody missed one.
            "missed": [
                p
                for p in unplaced
                if p.status != "retired" and p.element_key and p.id not in placed
            ],
        },
    }
