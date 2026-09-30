"""Read beads on behalf of gates. Reads only — the rig owns bead state.

A gate says work is pending. A bead in some rig says that work shipped. Nothing
overwrites anything; the two simply drift apart, because nothing ever reads one
on behalf of the other. `Forcing.source_ref` has carried bead ids by convention
for months and nothing parsed them, which is how gate #6 held back the QR handout
while `sea-btz.6` sat closed since 2026-07-03 — eight weeks.

This module is the read. It is deliberately the only thing in the store that
knows `bd` exists, and it has no write path at all:

    THE STORE NEVER WRITES BEAD STATUS, AND NEVER CACHES IT.

There is no `status` column on `gate_satisfiers` for the same reason. A cached
status is a second copy of a fact the rig owns, and a second copy is the failure
this whole epic is named after (docs/two-writers-one-row.md). Status is read at
check time, so the store and the rig cannot disagree — they can only be asked at
different moments, which is honest.

RESOLVING AN ID TO A RIG IS A SEARCH, NOT A PREFIX LOOKUP. `rigs.json` gives each
rig a bead prefix, but the mapping is not one-to-one in practice: `sea-btz.6`
lives in the `sea` rig and `sea-zrbp` lives in the `strategy` rig, both under the
prefix `sea`. So prefix matches are tried FIRST, then every other rig. Getting
this wrong is silent — a miss looks exactly like a bead that does not exist,
which would read as "no drift" on a gate that is in fact satisfied.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path

from . import env

GT_ROOT = env.path_value("GT_ROOT", Path.home() / "gt")

# Bead statuses that mean the work is not going to happen under this id. `closed`
# is the ordinary one. A superseded bead is also not coming back — the work moved
# — and a gate waiting on it is waiting on nothing, which is drift either way.
DONE_STATUSES = frozenset({"closed", "superseded", "done", "resolved"})


@dataclass(frozen=True)
class BeadState:
    """What a rig says about one bead, at the moment it was asked."""

    bead_id: str
    rig: str
    status: str
    title: str
    closed_at: date | None
    close_reason: str

    @property
    def done(self) -> bool:
        return self.status in DONE_STATUSES


@lru_cache(maxsize=1)
def _rig_prefixes() -> dict[str, list[str]]:
    """{prefix: [rig, ...]} from rigs.json. A prefix may name more than one rig."""
    reg = GT_ROOT / "rigs.json"
    if not reg.exists():
        return {}
    try:
        rigs = json.loads(reg.read_text()).get("rigs", {})
    except (json.JSONDecodeError, OSError):
        return {}
    out: dict[str, list[str]] = {}
    for name, cfg in rigs.items():
        prefix = ((cfg or {}).get("beads") or {}).get("prefix")
        if prefix:
            out.setdefault(prefix, []).append(name)
    return out


def rig_candidates(bead_id: str) -> list[str]:
    """Rigs to search for `bead_id`: prefix matches first, then everything else.

    Prefix-first is an ordering, never a filter. A prefix that resolves to the
    wrong rig would make a satisfied gate look unsatisfied, and nothing would say
    so — the exact silence this epic exists to end.
    """
    prefixes = _rig_prefixes()
    prefix = bead_id.split("-", 1)[0]
    first = prefixes.get(prefix, [])
    rest = [r for rigs in prefixes.values() for r in rigs if r not in first]
    seen: set[str] = set()
    ordered = []
    for r in [*first, *sorted(rest)]:
        if r not in seen and (GT_ROOT / r).is_dir():
            seen.add(r)
            ordered.append(r)
    return ordered


def _bd_show(rig: str, bead_id: str) -> dict | None:
    try:
        out = subprocess.run(
            ["bd", "show", bead_id, "--json"],
            cwd=GT_ROOT / rig,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        return None
    if isinstance(data, list):
        data = data[0] if data else None
    return data if isinstance(data, dict) and data.get("id") else None


def _as_date(value) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def read_state(bead_id: str) -> BeadState | None:
    """Ask the rigs about one bead. None means no rig has it — itself a finding."""
    for rig in rig_candidates(bead_id):
        data = _bd_show(rig, bead_id)
        if data is None:
            continue
        return BeadState(
            bead_id=data["id"],
            rig=rig,
            status=(data.get("status") or "open"),
            title=(data.get("title") or "").strip(),
            closed_at=_as_date(data.get("closed_at")),
            close_reason=(data.get("close_reason") or "").strip(),
        )
    return None


# ------------------------------------------------------------- classification

# A gate's satisfaction class, DERIVED from its rows rather than stored, so it
# cannot disagree with them. The four values are not a preference ordering; each
# one implies a different next action.
UNCLASSIFIED = "unclassified"  # nobody has looked. Not a claim that it has none.
BEAD = "bead"  # checkable by reading a rig — one subprocess per link
POSITION = "position"  # checkable by one local SELECT: the cheapest kind
EVENT = "event"  # a human has to be there on the day. Never checkable.
MIXED = "mixed"  # more than one kind — part checkable, part not

# The kinds a machine can answer. EVENT is deliberately absent and always will be.
CHECKABLE = (BEAD, POSITION)


def classify(satisfiers) -> str:
    kinds = {sat.kind for sat in satisfiers}
    if not kinds:
        return UNCLASSIFIED
    if len(kinds) > 1:
        return MIXED
    return kinds.pop()


# A closed bead whose close reason says the work MOVED rather than shipped. The
# fleet writes this in caps at the head of the reason (sea-btz.8: "SUPERSEDED by
# explainer 138"; sea-btz.9: "SUPERSEDED on both halves"). It is a text signal
# and it is treated as one: it never changes what happens to the gate, only which
# sentence the report prints, because "the work moved" and "the work shipped"
# want different reading from a person.
SUPERSEDED_MARKERS = ("SUPERSEDED", "SUPERCEDED", "RE-HOMED", "REHOMED", "MOVED TO")

# The three classes this reports, from three cases hit by hand:
SHIPPED = "shipped-but-gated"  # every satisfying bead closed, gate still open
SETTLED = "settled-but-gated"  # the position the gate waits on has already moved
SUPERSEDED = "superseded"  # a satisfying bead's work moved — needs a human read
MISSING = "missing-bead"  # a linked id no rig has: the gate looks watched and is not
STALE = "premise-stale"  # nothing moved, and nothing has been touched either


@dataclass(frozen=True)
class GateFinding:
    """One gate that disagrees with the world, and what kind of disagreement.

    Never an instruction. `strategy gate resolve`'s own docstring says resolving a
    gate whose question is still open is the failure it makes easy — so this
    reports, and Ivan decides. A bead closing is evidence a gate CAN close, not
    authority to close it: the work can ship and the thing it gated still not be
    true.
    """

    kind: str
    gate_id: int
    gate_title: str
    due_on: date | None
    detail: str
    bead_id: str | None = None
    rig: str | None = None
    closed_at: date | None = None

    @property
    def stale_days(self) -> int | None:
        """How long the gate has been asking a question already answered."""
        if self.closed_at is None:
            return None
        return (date.today() - self.closed_at).days

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "gate_id": self.gate_id,
            "gate_title": self.gate_title,
            "due_on": self.due_on.isoformat() if self.due_on else None,
            "detail": self.detail,
            "bead_id": self.bead_id,
            "rig": self.rig,
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "GateFinding":
        return cls(
            kind=d["kind"],
            gate_id=d["gate_id"],
            gate_title=d["gate_title"],
            due_on=date.fromisoformat(d["due_on"]) if d.get("due_on") else None,
            detail=d["detail"],
            bead_id=d.get("bead_id"),
            rig=d.get("rig"),
            closed_at=date.fromisoformat(d["closed_at"]) if d.get("closed_at") else None,
        )


def _looks_superseded(state: BeadState) -> bool:
    head = state.close_reason[:200].upper()
    return any(m in head for m in SUPERSEDED_MARKERS)


def _open_gates(session):
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from .models import Forcing

    return (
        session.execute(
            select(Forcing)
            .where(Forcing.resolved_on.is_(None))
            .options(selectinload(Forcing.satisfiers))
            .order_by(Forcing.id)
        )
        .scalars()
        .all()
    )


def bead_findings(session, reader=None) -> list[GateFinding]:
    """The two classes that need the rigs read. Costs one `bd show` per link.

    `reader` is injected so the fixtures need no rig, and so a caller can swap in
    a batched reader later without this function changing.
    """
    reader = reader or read_state
    out: list[GateFinding] = []
    for gate in _open_gates(session):
        links = [sat for sat in gate.satisfiers if sat.kind == BEAD]
        if not links:
            continue
        states = {sat.bead_id: reader(sat.bead_id) for sat in links}
        for bead_id, st in states.items():
            if st is None:
                out.append(
                    GateFinding(
                        MISSING, gate.id, gate.title, gate.due_on,
                        f"{bead_id} is linked to this gate and no rig has it — the gate "
                        f"reads as watched and is not",
                        bead_id=bead_id,
                    )
                )
        known = [st for st in states.values() if st is not None]
        if not known or not all(st.done for st in known):
            continue
        moved = [st for st in known if _looks_superseded(st)]
        if moved:
            st = moved[0]
            out.append(
                GateFinding(
                    SUPERSEDED, gate.id, gate.title, gate.due_on,
                    f"{st.bead_id} closed as superseded — the work MOVED rather than "
                    f"shipped, so what satisfies this gate may have moved with it: "
                    f"{st.close_reason[:120]}",
                    bead_id=st.bead_id, rig=st.rig, closed_at=st.closed_at,
                )
            )
            continue
        st = min(known, key=lambda x: x.closed_at or date.max)
        names = ", ".join(sorted(states))
        out.append(
            GateFinding(
                SHIPPED, gate.id, gate.title, gate.due_on,
                f"every satisfying bead is closed ({names}) and the gate is still open",
                bead_id=st.bead_id, rig=st.rig, closed_at=st.closed_at,
            )
        )
    return out


def position_findings(session) -> list[GateFinding]:
    """Gates waiting on a position this store has already moved. Pure SELECT.

    No rig, no subprocess, no cache — which is why `prime` can afford to run this
    live on every session while the bead classes have to come from a file. The
    cheapest class was also the last one noticed, because the vocabulary started
    with "bead or event" and this is neither.

    A predicate naming a position that does not exist is reported, not skipped:
    a link to a deleted row reads as "not satisfied yet" forever, so the gate
    looks watched and is not — the same failure as a bead id no rig has.
    """
    from .models import Position

    out: list[GateFinding] = []
    for gate in _open_gates(session):
        for sat in gate.satisfiers:
            if sat.kind != POSITION:
                continue
            pos = session.get(Position, sat.position_id)
            if pos is None:
                out.append(
                    GateFinding(
                        MISSING, gate.id, gate.title, gate.due_on,
                        f"this gate waits on position #{sat.position_id}, which this "
                        f"store does not have — the gate reads as watched and is not",
                    )
                )
                continue
            if sat.predicate == "superseded" and pos.superseded_by_id:
                out.append(
                    GateFinding(
                        SETTLED, gate.id, gate.title, gate.due_on,
                        f"position #{pos.id} has been superseded by #{pos.superseded_by_id} "
                        f"({pos.title[:60]}) and the gate is still open",
                    )
                )
            elif sat.predicate == "decided" and pos.decided_on:
                out.append(
                    GateFinding(
                        SETTLED, gate.id, gate.title, gate.due_on,
                        f"position #{pos.id} was decided on {pos.decided_on} "
                        f"({pos.title[:60]}) and the gate is still open",
                        closed_at=pos.decided_on,
                    )
                )
    return out


# How near a gate has to be before silence about it is a finding.
STALE_HORIZON_DAYS = 30


def stale_findings(session, today: date | None = None,
                   horizon: int = STALE_HORIZON_DAYS) -> list[GateFinding]:
    """Gates coming due that name nothing that would satisfy them. No rigs read.

    The third case (gate #23, FHIA) is not mechanically detectable as the bead
    describes it: "the condition names a counterparty who has not been touched" is
    a fact about the world, not about this database. So this does not claim to
    know why.

    THE AGE SIGNAL THE BEAD PROPOSES DOES NOT WORK, and the data says so rather
    than an argument. `Forcing.created_at` is when the ROW was written, not when
    the commitment was made or last examined: on 2026-09-03 the seeded gates all
    read 17d old because that is when the store was built, and #25/#26/#28 read 0d
    because they were added that morning. Gate #23 itself was 3 days old when it
    went stale. Filtering on that age would have hidden the case the bead names.

    So the honest signal is the one thing the store genuinely knows: a dated gate
    is close, and nothing recorded here can tell whether its premise still holds.
    Row age is reported as CONTEXT, labelled as row age, never used as a filter.

    Gates that HAVE a link are silent here — the live classes cover them, and
    saying it twice trains the reader to skim. A gate whose text carries an
    `[edited …]` note is silent too: someone has looked at it since it was written.
    """
    today = today or date.today()
    out = []
    for gate in _open_gates(session):
        if gate.due_on is None or gate.satisfiers:
            continue
        days = (gate.due_on - today).days
        if days < 0 or days > horizon:
            continue
        if "[edited " in (gate.gates or ""):
            continue
        created = gate.created_at.date() if gate.created_at else None
        age = f", row written {(today - created).days}d ago" if created else ""
        out.append(
            GateFinding(
                STALE, gate.id, gate.title, gate.due_on,
                f"due in {days}d and names nothing that would satisfy it{age} — "
                f"nothing here can tell whether the premise still holds "
                f"(`strategy gate link {gate.id} …`)",
            )
        )
    return out


# ------------------------------------------------------------------- the cache
#
# `prime` opens EVERY session in EVERY rig. One `bd show` is ~0.5s and an id no
# rig has costs ~5s, because a miss searches all of them — so the live classes
# can never be computed in that path, and a drift signal that only `reconcile`
# prints is a drift signal nobody sees.
#
# So the live read happens in the two commands a person actually invokes
# (`forcing`, `reconcile`), and it leaves its answer here. prime reads this file
# and ALWAYS shows how old it is: a stale finding presented as current is the
# same failure one level up.
#
# A FILE, NOT A TABLE, ON PURPOSE. This is a cached copy of facts the rigs own.
# Putting it in the store would make the store a second holder of bead state,
# which is the thing this whole epic is named after (docs/two-writers-one-row.md).
# A cache next to the export is obviously derived and can be deleted at any time.

CACHE_PATH = env.path_value(
    "STRATEGY_GATE_DRIFT_CACHE", Path.home() / ".strategy" / "gate-drift.json"
)


def write_cache(findings: list[GateFinding], path: Path | None = None) -> None:
    path = path or CACHE_PATH
    payload = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "findings": [f.as_dict() for f in findings],
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2))
    except OSError:
        # Never fail a command because the cache is unwritable. The report the
        # caller asked for is already on their screen.
        pass


def read_cache(path: Path | None = None) -> tuple[list[GateFinding], datetime | None]:
    """(findings, when they were checked). ([], None) when never checked."""
    path = path or CACHE_PATH
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return [], None
    try:
        when = datetime.fromisoformat(payload["checked_at"])
        return [GateFinding.from_dict(d) for d in payload["findings"]], when
    except (KeyError, TypeError, ValueError):
        return [], None


def check(session, reader=None, cache: Path | None = None) -> list[GateFinding]:
    """Read the rigs, add the store-only staleness signal, and cache the answer."""
    findings = (
        bead_findings(session, reader=reader)
        + position_findings(session)
        + stale_findings(session)
    )
    write_cache(findings, cache)
    return findings


def free_findings(session) -> list[GateFinding]:
    """Everything answerable without leaving this database.

    `prime` uses this instead of the cached copy for these two classes: they cost
    one query, and a cached answer that could have been computed now is a stale
    answer for no reason.
    """
    return position_findings(session) + stale_findings(session)


@dataclass(frozen=True)
class GateDrift:
    """An open gate whose satisfying bead is already done, or has gone missing."""

    gate_id: int
    gate_title: str
    due_on: date | None
    bead_id: str
    kind: str  # satisfied | missing
    state: BeadState | None

    @property
    def stale_days(self) -> int | None:
        """How long the gate has been asking a question the bead already answered."""
        if self.state is None or self.state.closed_at is None:
            return None
        return (date.today() - self.state.closed_at).days


def drift(session, reader=read_state) -> list[GateDrift]:
    """Open gates whose bead satisfiers say the work is finished, or gone.

    `reader` is injected so this is testable without a rig — and so a caller can
    substitute a batched reader later without this function changing. It is the
    only knowledge of `bd` that leaves this module.

    Reports. Resolves nothing: a bead closing is evidence that a gate can close,
    not authority to close it. `NEVER auto-resolve a gate` is the epic's rule, and
    the reason is that the bead and the gate are not the same claim — the work can
    ship and the thing it gated still not be true.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from .models import Forcing

    rows = (
        session.execute(
            select(Forcing)
            .where(Forcing.resolved_on.is_(None))
            .options(selectinload(Forcing.satisfiers))
            .order_by(Forcing.id)
        )
        .scalars()
        .all()
    )
    out: list[GateDrift] = []
    for gate in rows:
        for sat in gate.satisfiers:
            if sat.kind != BEAD:
                continue
            state = reader(sat.bead_id)
            if state is None:
                out.append(
                    GateDrift(gate.id, gate.title, gate.due_on, sat.bead_id, "missing", None)
                )
            elif state.done:
                out.append(
                    GateDrift(gate.id, gate.title, gate.due_on, sat.bead_id, "satisfied", state)
                )
    return out
