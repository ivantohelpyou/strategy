"""Writes, and the invariants that protect them — in one place both surfaces call.

Position #67, 2026-08-24: "the CLI is not the eventual surface, and a second
reader/writer of the same Postgres is coming. The invariants (supersession scope
rule, store-owned columns a sync must not overwrite, a claim being contested when
its members disagree) belong in code both surfaces call, not in the CLI's command
handlers." This module is that code. `queries.py` is its read-side counterpart.

Rules for everything here:

* **Raise, never print.** A refusal is an :class:`InvariantError` carrying a
  sentence a human can read. The CLI turns it into a ClickException; a web
  surface turns it into a flash. Neither gets to decide whether the rule applies.
* **Validate every target before mutating any.** A typo in the third id must not
  leave the first two adopted. This is why the functions take lists.
* **Do not commit.** The caller owns the transaction, so a surface can compose
  several operations into one unit of work.
* **Every state change that carries judgment writes Evidence.** Adopting,
  returning and rejecting are decisions; a field flipping with no trail is how a
  position ends up with a date nobody can account for.
"""

from __future__ import annotations

import re

from datetime import date

from sqlalchemy import select

from .models import (
    CASCADE_DIRECTIONS,
    CASCADE_STANDINGS,
    FACTOR_KINDS,
    MEMBERSHIP_MODES,
    SCOPE_RANK,
    STANCES,
    Cascade,
    CascadePosition,
    Evidence,
    Factor,
    Position,
    Revision,
    Question,
)


class InvariantError(Exception):
    """A write the store refused. Surfaces render it; they never work around it."""


# --------------------------------------------------------------- lookups


def position(session, pid: int) -> Position:
    p = session.get(Position, pid)
    if p is None:
        raise InvariantError(f"no position {pid}")
    return p


def positions(session, pids) -> list[Position]:
    """All of them, or none — the id check happens before any caller mutates."""
    found, missing = [], []
    for pid in pids:
        p = session.get(Position, pid)
        (found if p is not None else missing).append(p if p is not None else pid)
    if missing:
        # Wording matches what the CLI has always said; a surface may add its own
        # decoration but the sentence the store refuses with does not move.
        raise InvariantError(f"no position {', '.join(str(m) for m in missing)}")
    return found


# ------------------------------------------------------- the decision verbs
#
# adopt / send_back / reject are the three triage outcomes, and they are three
# because the store insists on the distinctions:
#   adopt      — I hold this. Dated, and the date is the decision.
#   send_back  — not ready. NOT retired (no longer held) and NOT contested
#                (disputed): a claim that simply is not ready is neither.
#   reject     — I do not hold this, and that IS a decision, so it is dated and
#                leaves the pending pile. `retired` is the status for it.


def adopt(session, pids, *, when=None, status=None, note=None, redate=False):
    """Take drafted positions as held. Returns [(position, previous_status)].

    ADOPT, not "approve": the store does not record who typed a position and
    should not. A position is a stance someone holds, and adoption is the moment
    someone starts holding it."""
    when = when or date.today()
    targets = positions(session, pids)
    for p in targets:
        if p.decided_on and not redate:
            raise InvariantError(
                f"#{p.id} already decided on {p.decided_on} — pass redate to move it"
            )
    out = []
    for p in targets:
        was = p.status
        p.decided_on = when
        if status:
            p.status = status
        session.add(
            Evidence(
                position_id=p.id,
                source="adopt",
                scope=p.scope,
                observed_on=when,
                stance="supports",
                summary=note or "Adopted — taken as a position held, not a draft.",
            )
        )
        out.append((p, was))
    return out


def send_back(session, pids, *, note=None):
    """Clear the decided date: it was dated, but it was not decided.

    Does NOT retire or contest. `retired` means no longer held; `contested` means
    disputed. Neither describes a claim that is simply not ready, and squeezing
    this into one of them loses the distinction the pile exists to keep."""
    targets = positions(session, pids)
    for p in targets:
        if p.decided_on is None:
            raise InvariantError(f"#{p.id} is already pending — nothing to return")
    out = []
    for p in targets:
        was = p.decided_on
        p.decided_on = None
        session.add(
            Evidence(
                position_id=p.id,
                source="return",
                scope=p.scope,
                observed_on=date.today(),
                summary=note or f"Returned to the pile — the {was} date was not a decision.",
            )
        )
        out.append((p, was))
    return out


def reject(session, pids, *, when=None, note=None):
    """Decide NOT to hold a position: status retired, and dated.

    Dated on purpose. Deciding you do not hold something is a decision, and an
    undated rejection would sit in the pending pile forever looking like work
    nobody had got to. The reason is required — a rejection with no reason is
    indistinguishable from an accident six weeks later."""
    when = when or date.today()
    if not note:
        raise InvariantError("a rejection needs a reason — pass a note")
    targets = positions(session, pids)
    for p in targets:
        if p.status == "retired":
            raise InvariantError(f"#{p.id} is already retired")
    out = []
    for p in targets:
        was = p.status
        p.status = "retired"
        p.decided_on = when
        session.add(
            Evidence(
                position_id=p.id,
                source="reject",
                scope=p.scope,
                observed_on=when,
                stance="opposes",
                summary=note,
            )
        )
        out.append((p, was))
    return out


# ----------------------------------------------------------- store-owned columns


STORE_OWNED = ("status", "scope", "element_key", "venture")

# Text columns. Owned by the upstream system when there IS one, because a sync
# would overwrite anything typed here. A `manual` or `doc` position has no
# upstream, so the store owns its text by default rather than by exception.
TEXT_COLUMNS = ("title", "statement")
UNSOURCED = ("manual", "doc")


def set_fields(session, pid: int, **fields) -> Position:
    """Edit the columns the STORE owns.

    The store always owns status, scope, element and venture. It owns the TEXT
    only when nothing upstream does: a vikunja- or beads-sourced position gets
    its title and statement back from the source on the next sync, so letting a
    surface edit them would silently lose the edit. A manual or doc position is
    never re-synced and has no other owner.

    Keeping the writable set here rather than in a surface's form handler is what
    stops a generic "update this row" from walking past the boundary a sync
    depends on."""
    p = position(session, pid)
    writable = set(STORE_OWNED)
    if p.source_system in UNSOURCED:
        writable |= set(TEXT_COLUMNS)
    note = fields.pop("note", None)
    unknown = set(fields) - writable
    if unknown:
        text_denied = sorted(unknown & set(TEXT_COLUMNS))
        if text_denied:
            raise InvariantError(
                f"{', '.join(text_denied)}: text belongs to {p.source_system}"
                f":{p.source_ref} and would be overwritten on the next sync — "
                f"edit it there, or record the change as evidence or a factor"
            )
        raise InvariantError(
            f"not a store-owned column: {', '.join(sorted(unknown))} "
            f"(the store owns {', '.join(STORE_OWNED)})"
        )
    changed = [
        k for k in TEXT_COLUMNS
        if fields.get(k) is not None and fields[k] != getattr(p, k)
    ]
    if changed:
        # Snapshot BEFORE the write: the row holds what is about to be lost.
        session.add(Revision(position_id=p.id, title=p.title or "",
                             statement=p.statement or "",
                             changed=",".join(changed), note=note or ""))
    for k, v in fields.items():
        if v is None:
            continue
        setattr(p, k, None if v == "none" and k in ("element_key", "venture") else v)
    return p


# ------------------------------------------------------------- supersession


def supersede_allowed(new: Position, old: Position, evidence_note: str | None):
    """The scope rule: a narrower-scope decision may not silently supersede a
    wider one. It may if it names the new evidence that justifies it."""
    if SCOPE_RANK[new.scope] >= SCOPE_RANK[old.scope]:
        return True, "scope >= superseded"
    if evidence_note:
        return True, "narrower scope, but new evidence named"
    return False, (
        f"refused: {new.scope} < {old.scope} and no new evidence named "
        f"(name the evidence to override)"
    )


def supersede(session, new_id: int, old_id: int, *, evidence_note: str | None = None):
    new, old = position(session, new_id), position(session, old_id)
    ok, why = supersede_allowed(new, old, evidence_note)
    if not ok:
        raise InvariantError(why)
    old.superseded_by_id = new.id
    if evidence_note:
        session.add(
            Evidence(
                position_id=new.id,
                source="supersede",
                scope=new.scope,
                observed_on=date.today(),
                summary=evidence_note,
            )
        )
    return new, why


# -------------------------------------------------------------------- claims


def claim_status(group) -> str:
    """One question answered two ways IS a dispute, whatever the rows say
    individually — that is the whole reason to gather them (sea-mii7.1)."""
    statuses = {p.status for p in group}
    if "contested" in statuses or len(statuses) > 1:
        return "contested"
    return statuses.pop()


def claim(session, pids, *, key: str | None = None, clear: bool = False):
    """Gather positions answering the SAME question under one claim key.

    Use `supersede` when one row REPLACED another; use this when they are all
    still live and disagree."""
    rows = positions(session, pids)
    if clear:
        for r in rows:
            r.claim_key = None
        return None, rows, False
    if len(rows) < 2 and not key:
        raise InvariantError("a claim needs at least two positions (or an explicit key)")
    key = key or f"claim-{min(pids)}"
    for r in rows:
        r.claim_key = key
    return key, rows, claim_status(rows) == "contested"


# ------------------------------------------------- evidence, factors, questions


def add_evidence(session, pid, *, source, summary, scope="single-rig",
                 observed_on=None, stance=None) -> Evidence:
    if stance is not None and stance not in STANCES:
        raise InvariantError(f"stance must be one of {', '.join(STANCES)}")
    p = position(session, pid)
    e = Evidence(
        position_id=p.id,
        source=source,
        summary=summary,
        scope=scope,
        observed_on=observed_on,
        stance=stance,
    )
    session.add(e)
    return e


def add_factor(session, pid, *, kind, text, source="") -> Factor:
    """A reason for, a reason against, or the test that would settle it."""
    if kind not in FACTOR_KINDS:
        raise InvariantError(f"kind must be one of {', '.join(FACTOR_KINDS)}")
    p = position(session, pid)
    f = Factor(position_id=p.id, kind=kind, text=text, source=source)
    session.add(f)
    return f


def ask(session, pid, *, text, on=None) -> Question:
    """Record what would have to be known before this can be decided."""
    p = position(session, pid)
    q = Question(position_id=p.id, text=text, asked_on=on or date.today())
    session.add(q)
    return q


def answer(session, qid, *, text, on=None) -> Question:
    q = session.get(Question, qid)
    if q is None:
        raise InvariantError(f"no question {qid}")
    if q.answered_on is not None:
        raise InvariantError(
            f"question {qid} was answered on {q.answered_on} — ask a new one rather "
            f"than overwriting the answer that was acted on"
        )
    q.answer = text
    q.answered_on = on or date.today()
    return q


def open_questions(session, pid: int | None = None) -> list[Question]:
    q = select(Question).where(Question.answered_on.is_(None))
    if pid is not None:
        q = q.where(Question.position_id == pid)
    return sorted(session.scalars(q), key=lambda x: (x.position_id, x.id))


# ------------------------------------------------------------------ cascades
#
# A cascade is a Playing to Win cascade held from one point of view, and a
# position's membership in one is a separate object from the position itself.
# The reason is the whole feature: a capability that is load-bearing for three
# brands must be ONE row that three cascades point at, not three copies. Copies
# would split the evidence, let a claim be contested in one place and settled in
# another, and — worst — hide the fact that it is shared, which is the thing
# worth knowing (solutions-tx3).


def cascade(session, key: str) -> Cascade:
    c = session.scalar(select(Cascade).where(Cascade.key == key))
    if c is None:
        raise InvariantError(f"no cascade {key!r}")
    return c


def create_cascade(
    session,
    key: str,
    *,
    name: str = "",
    brand: str | None = None,
    standing: str = "candidate",
    direction: str = "forward",
    question: str = "",
    forked_from: Cascade | None = None,
) -> Cascade:
    """A new cascade. Defaults to `candidate` deliberately.

    `actual` is what prime prints into the top of every session, so a cascade
    earns that standing by being promoted, never by being created. The safe
    default is the one that cannot leak.

    A question is required for a candidate. A speculative cascade without a
    stated point of view is a copy of its parent, and a copy answers nothing —
    the POV is the entire artifact.
    """
    if standing not in CASCADE_STANDINGS:
        raise InvariantError(f"standing must be one of {', '.join(CASCADE_STANDINGS)}")
    if direction not in CASCADE_DIRECTIONS:
        raise InvariantError(f"direction must be one of {', '.join(CASCADE_DIRECTIONS)}")
    if not key or not key.replace("-", "_").isidentifier():
        raise InvariantError(f"cascade key {key!r} must be a slug: letters, digits, - and _")
    if session.scalar(select(Cascade).where(Cascade.key == key)):
        raise InvariantError(f"cascade {key!r} already exists")
    if standing == "candidate" and not question.strip():
        raise InvariantError(
            "a candidate cascade needs --question: the point of view is the artifact, "
            "and one without it is a copy of its parent"
        )
    c = Cascade(
        key=key,
        name=name or key,
        brand=brand,
        standing=standing,
        direction=direction,
        question=question,
        forked_from_id=forked_from.id if forked_from else None,
    )
    session.add(c)
    session.flush()
    return c


def add_member(
    session,
    c: Cascade,
    p: Position,
    *,
    mode: str = "fixed",
    element_key: str | None = None,
    note: str = "",
) -> CascadePosition:
    """Put a position in a cascade, in a role.

    element_key defaults to the position's own box but may differ: slotting a
    position into a DIFFERENT box is a real strategic move (a capability reread
    as a where-to-play), and recording the reframing is why the column lives on
    the membership rather than being read through to the Position.
    """
    if mode not in MEMBERSHIP_MODES:
        raise InvariantError(f"mode must be one of {', '.join(MEMBERSHIP_MODES)}")
    ek = element_key or p.element_key
    if not ek:
        raise InvariantError(
            f"#{p.id} is in no framework box and none was given — pass an element "
            f"key, or set the position's box first"
        )
    existing = session.scalar(
        select(CascadePosition).where(
            CascadePosition.cascade_id == c.id, CascadePosition.position_id == p.id
        )
    )
    if existing:
        raise InvariantError(f"#{p.id} is already in {c.key} (as {existing.mode})")
    m = CascadePosition(
        cascade_id=c.id, position_id=p.id, element_key=ek, mode=mode, note=note
    )
    session.add(m)
    session.flush()
    return m


def remove_member(session, c: Cascade, p: Position) -> CascadePosition:
    m = session.scalar(
        select(CascadePosition).where(
            CascadePosition.cascade_id == c.id, CascadePosition.position_id == p.id
        )
    )
    if m is None:
        raise InvariantError(f"#{p.id} is not in {c.key}")
    session.delete(m)
    return m


def set_standing(session, c: Cascade, standing: str) -> str:
    """Promote a candidate to actual, or demote. Returns the previous standing.

    Promotion is the moment a hypothesis becomes direction, so it is a verb with
    a name rather than a field a surface writes. Demotion is allowed: a cascade
    that stops being how Ivan is playing should stop printing itself into every
    session, and pretending otherwise is what makes a store stale.
    """
    if standing not in CASCADE_STANDINGS:
        raise InvariantError(f"standing must be one of {', '.join(CASCADE_STANDINGS)}")
    if standing == "actual" and not c.question.strip() and c.forked_from_id:
        raise InvariantError(
            f"{c.key} was forked and has no question — say what it answers before "
            f"promoting it into every session's Direction block"
        )
    was, c.standing = c.standing, standing
    return was


def cascade_boxes(session, c: Cascade) -> dict[str, list[dict]]:
    """The cascade as five boxes, each carrying its memberships.

    An EMPTY box is rendered, never omitted. A cascade missing its how-to-win is
    the finding — the same reason the assessment matrix shows unanswered cells
    rather than dropping them.
    """
    from .frameworks import FRAMEWORKS

    rows = session.execute(
        select(CascadePosition, Position)
        .join(Position, Position.id == CascadePosition.position_id)
        .where(CascadePosition.cascade_id == c.id)
    ).all()
    out: dict[str, list[dict]] = {
        key: [] for key, *_ in FRAMEWORKS[c.framework]["elements"]
    }
    for m, pos in rows:
        out.setdefault(m.element_key, []).append({"membership": m, "position": pos})
    for members in out.values():
        members.sort(key=lambda r: (r["membership"].mode != "open", r["position"].id))
    return out

# ------------------------------------------------------------------- clarity


# A statement that needs a translator is not a position, it is a note to self.
# Every rule here was learned from a real failure in this store, named alongside
# it, so the bar can be argued with rather than merely enforced.
CLARITY_MAX_WORDS = 45
CLARITY_MAX_SENTENCES = 3
# Reading these back requires the store open beside you — the reader a position
# has to survive is a stranger.
_SELF_REF = re.compile(r"(?<![\w])#\d{1,3}\b|\bbox ?[1-5]\b|\bptw\.")
# Phrases this store has already had to retire for being unsayable.
_JARGON = ("is the boss", "the master family", "give/withhold/give")


def clarity(session, element: str | None = None, include_retired: bool = False) -> list[dict]:
    """Which positions a stranger could not read, and why.

    Statements drift toward absorbing their own reasoning, and the drift is
    invisible from inside: whoever wrote it can still read it. These checks are
    the ones that caught real failures — 'proving the method is the boss' (needs
    a translator), 'perform in the moment, give the method away, leave
    publications behind' (raises a question it does not answer), and statements
    grown to four hundred words with the claim buried in the middle."""
    q = select(Position).where(Position.superseded_by_id.is_(None))
    if element:
        q = q.where(Position.element_key == element)
    if not include_retired:
        q = q.where(Position.status != "retired")
    out = []
    for p in session.execute(q).scalars():
        st = (p.statement or "").strip()
        fails = []
        if not st:
            fails.append(("empty", "no statement — the title is carrying the claim alone"))
        else:
            words = len(st.split())
            if words > CLARITY_MAX_WORDS:
                fails.append(("long", f"{words} words — the claim is buried; move the case to factors"))
            sentences = len([x for x in re.split(r"[.!?]+(?:\s|$)", st) if x.strip()])
            if sentences > CLARITY_MAX_SENTENCES:
                fails.append(("sentences", f"{sentences} sentences — a position is a claim, not a paragraph"))
            if _SELF_REF.search(st):
                fails.append(("self-reference", "cites the store to explain itself — unreadable without it open"))
            for j in _JARGON:
                if j in st.lower():
                    fails.append(("jargon", f"{j!r} — retired phrasing that needed a translator"))
                    break
            title_words = {w for w in re.findall(r"[a-z]{4,}", (p.title or "").lower())}
            stmt_words = {w for w in re.findall(r"[a-z]{4,}", st.lower())}
            if title_words and len(title_words & stmt_words) / len(title_words) < 0.25:
                fails.append(("drift", "headline shares almost nothing with the statement"))
        if fails:
            out.append({"id": p.id, "title": p.title, "element": p.element_key,
                        "status": p.status, "editable": p.source_system in UNSOURCED,
                        "fails": fails})
    return sorted(out, key=lambda r: (r["element"] or "", r["id"]))

