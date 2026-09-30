"""Strategy store — SQLAlchemy 2.0 models (SEA-evolvable).

v1 holds the two tables the report needs first: `positions` and `evidence`.
Everything else scoped on sea-mii7 (venture, objective, forcing, constraint,
framework/element/test/assessment) is deliberately deferred — the plan is to
add them through `sea do` against this file, so the store's own evolution is
the SEA case study.

Write rules the app enforces (SQLite cannot compare across rows in a CHECK):

* supersedes is valid only when the new row's scope >= the old row's scope,
  OR the call names new evidence. Otherwise refuse (see `SCOPE_RANK`).
* Source systems (Vikunja, beads) own a position's TEXT; the store owns
  status / scope / element / supersedes after first insert. Sync never
  silently rewrites store-owned columns — `strategy reconcile` reports drift.

THIS FILE IS COPIED. follow-whee carries its own declaration of six of these
tables (positions, evidence, forcing, drops, metrics, metric_readings) and writes
the same Postgres schema, because the hosted app cannot pip-install a private
repo. Adding a column to one of those six is a change to someone else's program.
It is permitted — the contract lets a copy lag on additions — but it is not free,
and it has drifted silently three times:

    uv run python scripts/check_schema_contract.py

The registry of who copies what, the contract, and the open items live in
`schema_contract.py`; `tests/test_schema_contract.py` fails on a NEW drift. Run
the check after touching a borrowed table. See docs/two-writers-one-row.md.
"""

from datetime import date, datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

STATUSES = ("holding", "contested", "resolved", "retired")
SCOPES = ("web-chat", "single-rig", "fleet", "fleet+external")
SCOPE_RANK = {s: i for i, s in enumerate(SCOPES)}
SOURCE_SYSTEMS = ("vikunja", "beads", "doc", "manual")
VERDICTS = ("pass", "fail", "irrelevant")
# Which way a piece of evidence cuts. NULL is allowed and is the honest default for
# the 53 rows written before this existed: "observed, stance not recorded" is not
# the same as "neutral", and back-filling them with a guess would manufacture an
# argument nobody made.
STANCES = ("supports", "opposes", "complicates")
# A factor is an ARGUMENT; evidence is an OBSERVATION. "test" is the one that ends
# a stalemate — the thing that would settle the question if someone went and did it.
FACTOR_KINDS = ("for", "against", "test")
# What can license a public claim. "none" is a legal value and is the finding.
LICENSE_KINDS = ("position", "evidence", "drop", "metric", "app", "none")
SURFACES = ("linkedin", "one-sheet", "pmi-bio", "site-about")
# What would satisfy a forcing gate. A BEAD is work that closes — it can be read
# from the rig and checked without a human. An EVENT happens on a day — the room
# either sold seats or it did not, and no bead closing proves it either way. The
# distinction is the whole point of recording it: only the first kind can be
# auto-checked, and calling the second kind checkable would manufacture a green
# light on a Tuesday nobody was watching (sea-mii7.5.1).
SATISFIER_KINDS = ("bead", "event", "position")
# A POSITION satisfier is the cheapest kind and was the last one noticed. Several
# gates close when a claim IN THIS STORE reaches a state — "a position exists that
# supersedes #22" (gate #20), "#88 carries a target and is adopted out of
# contested" (gate #18). Those are queries, not judgements, and answering one
# costs a local SELECT: no rig, no subprocess, no person present on a day. They
# read as "needs a human" only because the vocabulary started with two words in it.
#
# The predicate is a closed list rather than free text, because a gate that names
# a condition nothing can evaluate is the deferral-without-a-trigger failure this
# whole table exists to catch.
POSITION_PREDICATES = (
    # The named position has been replaced by another. Gate #20's "a position
    # exists that supersedes #22" is this, asked of #22.
    "superseded",
    # The named position has been decided — adopted or blessed, carrying a date.
    # Deliberately NOT "is no longer contested": retiring a position also leaves
    # contested, and that is not the same as answering it.
    "decided",
)

# A cascade's STANDING, not its "stance" — Evidence.stance is already taken in this
# file and means something else entirely (which way an observation cuts). A cascade
# is either one Ivan actually holds or one he is trying on. The default is
# `candidate` on purpose: a cascade must be promoted deliberately, because `actual`
# is what `prime` prints into the top of every session.
CASCADE_STANDINGS = ("actual", "candidate")
# Which way the cascade was reasoned. forward = 1->5, aspiration-led (the normal
# read). reverse = 5->1, capability-led ("given this floor, what can it carry?").
CASCADE_DIRECTIONS = ("forward", "reverse")
# A position's role IN THIS CASCADE — orthogonal to its status. `fixed` is a given
# here (it may still be contested in its home cascade); `open` is what this cascade
# is solving for; `derived` fell out of what was fixed rather than being chosen.
MEMBERSHIP_MODES = ("fixed", "open", "derived")
# Brand is deliberately NOT constrained, for the same reason `venture` is not: the
# list grows, and a CHECK that has to be edited to name a new brand would be edited
# late. Brand is also NOT venture — qrcards is one venture serving both CCS (the
# dispatch tenant) and I2HU (ivantohelpyou.com is its prod URL).


class Base(DeclarativeBase):
    pass


class Position(Base):
    """A strategic claim: what was decided (or is still open), by whom, seen at what scope."""

    __tablename__ = "positions"
    __table_args__ = (
        UniqueConstraint("source_system", "source_ref", name="uq_position_source"),
        CheckConstraint(f"status IN {STATUSES}", name="ck_position_status"),
        CheckConstraint(f"scope IN {SCOPES}", name="ck_position_scope"),
        CheckConstraint(f"source_system IN {SOURCE_SYSTEMS}", name="ck_position_source_system"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(300))
    statement: Mapped[str] = mapped_column(Text, default="")
    # Framework slot, e.g. "ptw.box3". NULL is allowed and is itself a report
    # finding (orphaned position) — do not default it.
    element_key: Mapped[str | None] = mapped_column(String(40), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="holding")
    # Declared by the deciding session, never inferred later.
    scope: Mapped[str] = mapped_column(String(20), default="web-chat")
    decided_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Rig / product the claim is about; NULL = portfolio-level.
    venture: Mapped[str | None] = mapped_column(String(60), nullable=True)
    source_system: Mapped[str] = mapped_column(String(20))
    source_ref: Mapped[str] = mapped_column(String(60))
    # Raw status as the source system had it at last sync (e.g. "DECIDED", "open").
    source_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # DEPRECATED, kept so existing rows still read: the single position this one
    # replaced. It could only ever hold ONE, so consolidating five positions into
    # one silently kept the last and lost the other four (2026-08-30).
    supersedes_id: Mapped[int | None] = mapped_column(ForeignKey("positions.id"), nullable=True)
    # AUTHORITATIVE. Lives on the position being REPLACED and points at its
    # successor, so many rows can name the same one. Supersession is many-to-one
    # in practice — a box collapsing from eighteen positions to five is the normal
    # case, not an edge one.
    superseded_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("positions.id"), nullable=True
    )
    # Rows in different source systems that answer the SAME question share a
    # claim_key, so the report can render one contested claim with each source
    # as evidence instead of three positions that look like three arguments.
    # Supersession says "this replaced that"; a claim says "these are one
    # question, still open" (sea-mii7.1).
    claim_key: Mapped[str | None] = mapped_column(String(60), nullable=True)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )

    # p.superseded_by -> the one position that replaced p (or None)
    # p.supersedes    -> the list of positions p replaced
    superseded_by: Mapped["Position | None"] = relationship(
        remote_side=[id], foreign_keys=[superseded_by_id], backref="supersedes"
    )
    evidence: Mapped[list["Evidence"]] = relationship(back_populates="position")


class Evidence(Base):
    """Something observed that bears on a position — where, when, at what scope."""

    __tablename__ = "evidence"
    __table_args__ = (CheckConstraint(f"scope IN {SCOPES}", name="ck_evidence_scope"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("positions.id"))
    source: Mapped[str] = mapped_column(String(200))
    scope: Mapped[str] = mapped_column(String(20), default="single-rig")
    observed_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    summary: Mapped[str] = mapped_column(Text)
    # Which way it cuts, if the person recording it said. NULL = not recorded.
    stance: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )

    position: Mapped["Position"] = relationship(back_populates="evidence")


class Revision(Base):
    """A position's text as it stood before someone overwrote it.

    Statements drift in two directions. They start as the bare claim and grow to
    absorb the reasoning, the consequences and every later correction, until a
    reader cannot tell at a glance what the position actually is — and then they
    get cut back, which is a lossy edit made in a hurry. The first direction is
    what Factor and Evidence exist to prevent; this table covers the second.

    Written automatically by core.set_fields whenever title or statement changes,
    so a compression pass over the whole cascade is reversible and a bad
    summary is recoverable rather than silently authoritative (2026-08-30)."""

    __tablename__ = "revisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("positions.id"))
    # The text as it was BEFORE the write that created this row.
    title: Mapped[str] = mapped_column(String(300), default="")
    statement: Mapped[str] = mapped_column(Text, default="")
    # Which fields the write actually changed, comma-separated.
    changed: Mapped[str] = mapped_column(String(60), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    replaced_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )

    position: Mapped["Position"] = relationship(backref="revisions")


class Factor(Base):
    """A reason to adopt a position, a reason not to, or the test that would settle it.

    Distinct from Evidence on purpose. Evidence is something that HAPPENED — dated,
    sourced, backward-looking. A factor is an ARGUMENT, and it needs no date because
    it was not observed; it was reasoned.

    Why the store grew this: positions sat in `holding` for weeks not because nobody
    had an opinion but because the position carried the claim and nothing else — no
    case for, no case against, and no statement of what would end the argument. A
    reader had to reconstruct the decision from scratch every time, so they didn't
    (2026-08-28)."""

    __tablename__ = "factors"
    __table_args__ = (CheckConstraint(f"kind IN {FACTOR_KINDS}", name="ck_factor_kind"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("positions.id"))
    kind: Mapped[str] = mapped_column(String(20))
    text: Mapped[str] = mapped_column(Text)
    # Who reasoned it — "ivan" or a session ref. Not who typed it; who holds it.
    source: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )

    position: Mapped["Position"] = relationship(backref="factors")


class Question(Base):
    """Something that must be answered before the position can be decided.

    The third triage outcome, and the one the store had no room for: a draft is not
    always a yes or a no — often it is "I would decide this if I knew X". Without
    somewhere to put X the position goes back on the pile unchanged and the next
    reader re-derives the same doubt.

    An open question (answered_on IS NULL) is what makes a pending position BLOCKED
    rather than merely untouched, and the difference matters: blocked work has an
    owner and a next action, untouched work has neither."""

    __tablename__ = "questions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("positions.id"))
    text: Mapped[str] = mapped_column(Text)
    asked_on: Mapped[date] = mapped_column(Date)
    answer: Mapped[str] = mapped_column(Text, default="")
    answered_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )

    position: Mapped["Position"] = relationship(backref="questions")

    @property
    def open(self) -> bool:
        return self.answered_on is None


class Forcing(Base):
    """A dated gate: a condition that can be true on a specific Tuesday, and what
    it gates. The fix for "deferral without a trigger" — and the one table whose
    rows are inherently cross-rig (Aug 26 lives in three systems today).

    Scaffolded by `sea do "add forcing table with title, condition, due_on, gates,
    resolved_on, position_id"` (add_table_with_fk, 2026-08-17). SEA placed the
    class and spliced the file cleanly; it typed every column String(255)
    nullable and did not wire position_id as a ForeignKey — the types and FK
    below are hand-corrected. Filed as a SEA finding (sea-mii7 notes).
    """

    __tablename__ = "forcing"
    __table_args__ = (
        UniqueConstraint("source_system", "source_ref", name="uq_forcing_source"),
        CheckConstraint(f"source_system IN {SOURCE_SYSTEMS}", name="ck_forcing_source_system"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(300))
    # The condition in words — must be checkable on a date, not a vibe.
    condition: Mapped[str] = mapped_column(Text, default="")
    due_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    # What the gate holds back: a decision, a publication, a build, a payment.
    gates: Mapped[str] = mapped_column(Text, default="")
    resolved_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    position_id: Mapped[int | None] = mapped_column(ForeignKey("positions.id"), nullable=True)
    source_system: Mapped[str] = mapped_column(String(20), default="manual")
    source_ref: Mapped[str] = mapped_column(String(60))
    venture: Mapped[str | None] = mapped_column(String(60), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )

    position: Mapped["Position | None"] = relationship(backref="forcings")
    satisfiers: Mapped[list["GateSatisfier"]] = relationship(
        back_populates="gate", cascade="all, delete-orphan"
    )


class GateSatisfier(Base):
    """One thing that would satisfy a gate: a bead that would close, or an event
    that would happen.

    Distinct from `Forcing.source_ref`, which is PROVENANCE — where the gate came
    from ('vikunja:#1234', 'beads:purr-u8s.29'). Provenance answers "who asked";
    satisfaction answers "what would make this stop asking", and the two are
    routinely different rows in different systems. source_ref has carried bead ids
    by convention for months and nothing parsed them, which is how gate #6 held
    back the QR handout while sea-btz.6 sat closed for eight weeks.

    A JOIN TABLE, not a comma-separated column on `forcing`, for two reasons:

    * the question this exists to answer is "which gates does this bead satisfy",
      and a delimited string cannot be asked that. Supersession already taught
      this file the same lesson from the other direction — `supersedes_id` could
      hold exactly one row, so consolidating five positions into one silently
      kept the last and lost four (2026-08-30).
    * `forcing` is one of the six tables follow-whee copies. A column here widens
      that drift the day it is added; a new table is store-only, which the copy
      contract permits as lag. `scripts/check_schema_contract.py` says so rather
      than someone remembering it.

    NOTHING HERE MIRRORS BEAD STATE. There is no `status`, no `closed_on`, no
    cached title. The rig owns whether a bead is closed and this store never
    writes it — it reads at check time, so the two cannot disagree. One writer per
    fact (docs/two-writers-one-row.md, sea-zrbp).

    `kind` is not derivable from emptiness. A gate with no rows means NOBODY HAS
    LOOKED; a gate with an `event` row means someone looked and decided a human
    has to be there on the day. Reading absence as a decision is how a gate gets
    quietly reclassified by a schema default.
    """

    __tablename__ = "gate_satisfiers"
    __table_args__ = (
        # Dedupes bead links, which is the pair that must never double. Event rows
        # hold a NULL bead_id and are deduped on their note by the CLI, since two
        # different events can legitimately gate one row (#23 is both the FHIA
        # obligation and the Sept 22 room).
        UniqueConstraint("forcing_id", "bead_id", name="uq_gate_satisfier_bead"),
        CheckConstraint(f"kind IN {SATISFIER_KINDS}", name="ck_gate_satisfier_kind"),
        CheckConstraint(
            "(kind = 'bead' AND bead_id IS NOT NULL AND position_id IS NULL) "
            "OR (kind = 'event' AND bead_id IS NULL AND position_id IS NULL) "
            "OR (kind = 'position' AND position_id IS NOT NULL AND predicate IS NOT NULL "
            "AND bead_id IS NULL)",
            name="ck_gate_satisfier_shape",
        ),
        CheckConstraint(
            f"predicate IS NULL OR predicate IN {POSITION_PREDICATES}",
            name="ck_gate_satisfier_predicate",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    forcing_id: Mapped[int] = mapped_column(ForeignKey("forcing.id"), index=True)
    kind: Mapped[str] = mapped_column(String(10), default="bead")
    # Fully qualified and cross-rig on purpose — 'sea-btz.6', 'purr-u8s.29'. Gates
    # living in more than one rig is mii7's first reason for this store existing,
    # so a bare local id would be the one thing this column must not accept. The
    # prefix does NOT identify the rig on its own: 'sea-btz.6' is in the sea rig
    # and 'sea-zrbp' is in the strategy rig, so resolution searches (beadlink.py).
    bead_id: Mapped[str | None] = mapped_column(String(60), nullable=True)
    # For an event: WHAT event. For a bead: why this bead is the one. Required for
    # an event, because 'event' with no note records a judgement without its reason
    # and the next reader cannot tell it from a shrug.
    note: Mapped[str] = mapped_column(Text, default="")
    # For a POSITION satisfier: which position, and what has to become true of it.
    # A local FK, so this kind is answerable in one SELECT — which is why prime can
    # check it live while the bead kinds have to come from a cache.
    position_id: Mapped[int | None] = mapped_column(ForeignKey("positions.id"), nullable=True)
    predicate: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # When the judgement was made, not when the bead closes. A link is a claim by
    # a person on a day, and a stale one should be visibly stale.
    linked_on: Mapped[date] = mapped_column(Date, default=date.today)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )

    gate: Mapped["Forcing"] = relationship(back_populates="satisfiers")
    position: Mapped["Position | None"] = relationship()


class Assessment(Base):
    """One framework test, answered for one venture.

    The cell is deliberately absent until someone answers it: seven tests over a
    dozen ventures is 84 answers, and a matrix that defaults empty cells to
    "pass" would manufacture 84 opinions nobody holds (sea-mii7). Unanswered
    renders blank, and the report counts how much of the grid is filled."""

    __tablename__ = "assessments"
    __table_args__ = (
        UniqueConstraint("framework", "test_key", "venture", name="uq_assessment_cell"),
        CheckConstraint(f"verdict IN {VERDICTS}", name="ck_assessment_verdict"),
        CheckConstraint(f"scope IN {SCOPES}", name="ck_assessment_scope"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    framework: Mapped[str] = mapped_column(String(20), default="ptw")
    test_key: Mapped[str] = mapped_column(String(20))
    venture: Mapped[str] = mapped_column(String(60))
    verdict: Mapped[str] = mapped_column(String(20))
    note: Mapped[str] = mapped_column(Text, default="")
    # Same rule as a position: the answer declares what the answerer could see.
    scope: Mapped[str] = mapped_column(String(20), default="single-rig")
    decided_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )


class ProfileField(Base):
    """One field of one public surface, holding what that surface CURRENTLY says.

    The store already holds the positions, the evidence and the drops; the
    profile is the one public surface that is not a query over them. Keeping the
    live wording here is what makes drift diffable (sea-mii7.2)."""

    __tablename__ = "profile_fields"
    __table_args__ = (
        UniqueConstraint("surface", "field", name="uq_profile_field"),
        CheckConstraint(f"surface IN {SURFACES}", name="ck_profile_field_surface"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    surface: Mapped[str] = mapped_column(String(20), default="linkedin")
    field: Mapped[str] = mapped_column(String(60))
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text, default="")
    # LinkedIn's own limit for the field, so export can say what will be cut.
    char_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )

    claims: Mapped[list["ProfileClaim"]] = relationship(
        back_populates="profile_field", cascade="all, delete-orphan"
    )


class ProfileClaim(Base):
    """A factual claim inside a profile field, and what licenses it.

    A claim with no licence is not an error to be prevented — it is the finding.
    Recording it unlicensed is how `strategy profile check` can say so."""

    __tablename__ = "profile_claims"
    __table_args__ = (
        CheckConstraint(f"licensed_by_kind IN {LICENSE_KINDS}", name="ck_profile_claim_kind"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    field_id: Mapped[int] = mapped_column(ForeignKey("profile_fields.id"))
    claim_text: Mapped[str] = mapped_column(Text)
    licensed_by_kind: Mapped[str] = mapped_column(String(20), default="none")
    licensed_by_id: Mapped[str | None] = mapped_column(String(60), nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )

    profile_field: Mapped["ProfileField"] = relationship(back_populates="claims")


class Drop(Base):
    """One public drop (a LinkedIn post/article carrying a live app) and its stats.

    Scaffolded by `sea do "add drops table with posted_on, url, title, format, …"`
    (2026-08-17; same String(255)-everywhere result as `forcing`, see sea-70a3) —
    types hand-corrected. Stats come from LinkedIn's per-post export
    (`strategy drop ingest <xlsx>`), which Ivan must download by hand — so a drop
    with no stats, or stats younger than the post's settle window, is surfaced by
    `strategy prime` as "download stats".
    """

    __tablename__ = "drops"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    posted_on: Mapped[date] = mapped_column(Date)
    url: Mapped[str] = mapped_column(String(300), unique=True)
    # One LinkedIn post has TWO 19-digit ids: the activity URN (in the browser URL
    # as `-activity-<id>-`, and in the export's FILENAME) and the share/ugcPost URN
    # (`-share-<id>-` / `-ugcPost-<id>-`, and in the export's Post URL cell). A
    # hand-logged URL may carry either; matching on URL alone made duplicates
    # (qrcards-pd6v, 2026-08-20). Columns via `sea insert add_nullable_field drops
    # activity_id --type string:20` (and share_id), moved up beside url by hand.
    activity_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    share_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    title: Mapped[str] = mapped_column(String(300), default="")
    # What the media IS: article (newsletter) | post | carousel | video.
    # Until 2026-08-24 this column also carried "comment-link", which is a link
    # PLACEMENT, not a medium — so #13 (a native video with the app link in my
    # first comment) recorded the placement and erased the video. The two facts
    # are independent; link_placement below carries the second one.
    format: Mapped[str] = mapped_column(String(20), default="post")
    # Where the app link lives: body | first-comment | none. Null = not recorded
    # (drops logged before the split). Columns via
    # `sea insert add_nullable_field drops link_placement --type string:20`.
    link_placement: Mapped[str | None] = mapped_column(String(20), nullable=True)
    app_url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    impressions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reached: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The export's "Video performance" block, present only on video posts. Watch
    # time arrives as display text ("18m 34s"); both are stored as seconds so they
    # sum and compare. Added 2026-08-24 — before that the ingest read the video
    # block and threw it away (qrcards drop #13 lost 56 views / 18m34s that way).
    video_views: Mapped[int | None] = mapped_column(Integer, nullable=True)
    watch_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    avg_watch_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    article_views: Mapped[int | None] = mapped_column(Integer, nullable=True)
    email_sends: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reactions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    comments: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reposts: Mapped[int | None] = mapped_column(Integer, nullable=True)
    saves: Mapped[int | None] = mapped_column(Integer, nullable=True)
    link_clicks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    followers_gained: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # My own comments on the post (the "link in the comments" comment, replies to
    # commenters). LinkedIn's export counts them in `comments`; they are not
    # engagement. `comment-link` drops default to 1; set by hand otherwise.
    own_comments: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # My own reposts (the main-feed share of a page post). LinkedIn counts them in
    # `reposts`; a share of my own post is not engagement. Column via
    # `sea insert add_nullable_field drops own_reposts` (2026-08-20), tightened to
    # default 0 by hand to mirror own_comments (the not-null pattern has no templates).
    own_reposts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    stats_as_of: Mapped[date | None] = mapped_column(Date, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )

    # Base hit = any ONE of: a follower gained, a comment or repost (neither my
    # own), or >=5 link clicks.
    BASE_HIT_CLICKS = 5

    @property
    def has_stats(self) -> bool:
        return self.stats_as_of is not None

    @property
    def others_comments(self) -> int | None:
        """Comments minus my own — the number that counts as engagement."""
        if self.comments is None:
            return None
        return max(0, self.comments - (self.own_comments or 0))

    @property
    def others_reposts(self) -> int | None:
        """Reposts minus my own — the number that counts as engagement."""
        if self.reposts is None:
            return None
        return max(0, self.reposts - (self.own_reposts or 0))

    @property
    def base_hit(self) -> bool | None:
        if not self.has_stats:
            return None
        return bool(
            (self.followers_gained or 0) >= 1
            or (self.others_comments or 0) >= 1
            or (self.others_reposts or 0) >= 1
            or (self.link_clicks or 0) >= self.BASE_HIT_CLICKS
        )


METRIC_SOURCES = ("buttondown-api", "linkedin-export", "page_views", "manual")


class Metric(Base):
    """A number worth chasing, per beat: where it comes from, how often, and the
    gates that make it a goal. Ties to the position that states the goal (e.g.
    #49, the 300 → 1,000 win condition) so the cascade and the thermometer are
    one object. Readings are rows in metric_readings — a time series, never an
    overwrite (the drops table's mistake, fixed by drop_stats-style history).

    Hand-written 2026-08-19 (SEA-evolvable shape; sea-70a3 still open, so the
    scaffold step was skipped rather than hand-corrected twice).
    """

    __tablename__ = "metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # short handle used on the CLI: li-newsletter-subs, li-followers, bd-method …
    name: Mapped[str] = mapped_column(String(60), unique=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    # ivantohelpyou-linkedin | mcd-linkedin-page | mcd-buttondown | dispatch-buttondown …
    beat: Mapped[str] = mapped_column(String(60), default="")
    source: Mapped[str] = mapped_column(String(20), default="manual")
    # buttondown-api: env var holding the list's token. linkedin-export: the
    # export column to lift (email_sends). manual: empty.
    source_key: Mapped[str] = mapped_column(String(60), default="")
    cadence: Mapped[str] = mapped_column(String(20), default="weekly")
    # "300,600,1000" — ascending; empty = tracked, not a goal
    gates: Mapped[str] = mapped_column(String(100), default="")
    due_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    position_id: Mapped[int | None] = mapped_column(ForeignKey("positions.id"), nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )

    position: Mapped["Position | None"] = relationship(backref="metrics")
    readings: Mapped[list["MetricReading"]] = relationship(
        back_populates="metric", cascade="all, delete-orphan", order_by="MetricReading.as_of"
    )

    @property
    def gate_list(self) -> list[int]:
        return [int(g) for g in self.gates.split(",") if g.strip()]

    @property
    def latest(self) -> "MetricReading | None":
        return self.readings[-1] if self.readings else None

    def reading_on_or_before(self, day: date) -> "MetricReading | None":
        prior = [r for r in self.readings if r.as_of <= day]
        return prior[-1] if prior else None

    def next_gate(self) -> int | None:
        if not self.latest:
            return self.gate_list[0] if self.gate_list else None
        for g in self.gate_list:
            if self.latest.value < g:
                return g
        return None


class MetricReading(Base):
    __tablename__ = "metric_readings"
    __table_args__ = (UniqueConstraint("metric_id", "as_of", name="uq_metric_reading_day"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    metric_id: Mapped[int] = mapped_column(ForeignKey("metrics.id"))
    as_of: Mapped[date] = mapped_column(Date)
    value: Mapped[int] = mapped_column(Integer)
    # where this number came from: "api", "ivan", "export:SinglePostAnalytics_…"
    origin: Mapped[str] = mapped_column(String(120), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )

    metric: Mapped["Metric"] = relationship(back_populates="readings")



class Cascade(Base):
    """One Playing to Win cascade, held from one point of view.

    Until now a cascade was implicit — "every position carrying a box key" — so
    there was exactly one, the fleet's, and no way to ask the question from
    somewhere else. Two things needed it to become an object: the repos group
    into brands (MCD, CCS, I2HU, hobbies) that each have their own aspiration and
    their own where-to-play, and the generative PtW move is to hold part of the
    cascade constant and search the rest ("given boxes 4-5, what options arise in
    2-3?"). Neither could be asked, let alone kept.

    A cascade owns no positions. Membership is many-to-many through
    `cascade_positions`, so a capability that is load-bearing for three brands is
    ONE row that three cascades point at — evidence lands in one place, contesting
    it contests it everywhere, and the shared set is queryable. That query is the
    point: positions in every cascade are the horizontal platform, positions in one
    are the vertical (solutions-tx3).

    Scaffolded by `sea do "add cascades table with key, name, brand, framework,
    stance, direction, question, retired_on, forked_from_id"` (2026-08-29). SEA
    spliced the class cleanly and, as with `forcing` and `drops` (sea-70a3), typed
    every column String(255) nullable, wired no ForeignKey, and emitted no
    created_at. New this time: it silently split the field list on the SUBSTRING
    "and", so `brand` arrived as a column named `br`. Types, FK, constraints,
    defaults and the `stance` -> `standing` rename below are hand-corrected."""

    __tablename__ = "cascades"
    __table_args__ = (
        UniqueConstraint("key", name="uq_cascade_key"),
        CheckConstraint(f"standing IN {CASCADE_STANDINGS}", name="ck_cascade_standing"),
        CheckConstraint(f"direction IN {CASCADE_DIRECTIONS}", name="ck_cascade_direction"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Slug used on the CLI: "mcd", "ccs", "i2hu", "mcd-if-fleet-fixed".
    key: Mapped[str] = mapped_column(String(60))
    name: Mapped[str] = mapped_column(String(200), default="")
    # MCD | CCS | I2HU | hobby. NULL = portfolio-level, mirroring Position.venture.
    brand: Mapped[str | None] = mapped_column(String(40), nullable=True)
    framework: Mapped[str] = mapped_column(String(20), default="ptw")
    standing: Mapped[str] = mapped_column(String(20), default="candidate")
    direction: Mapped[str] = mapped_column(String(20), default="forward")
    # The point of view in one line: "given today's fleet, where else could I play?"
    # A cascade without one is a copy, not a question.
    question: Mapped[str] = mapped_column(Text, default="")
    forked_from_id: Mapped[int | None] = mapped_column(ForeignKey("cascades.id"), nullable=True)
    retired_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )

    forked_from: Mapped["Cascade | None"] = relationship(
        remote_side=[id], foreign_keys=[forked_from_id], backref="forks"
    )
    memberships: Mapped[list["CascadePosition"]] = relationship(
        back_populates="cascade", cascade="all, delete-orphan"
    )

    @property
    def is_actual(self) -> bool:
        return self.standing == "actual"


class CascadePosition(Base):
    """A position's membership in a cascade, and its role there.

    `mode` is the flip mechanic: fixing boxes 4-5 and opening 2-3 is a cascade
    whose box4/box5 memberships are `fixed` and whose box2/box3 slots are empty
    and `open`. Status stays on the Position because status is a property of the
    CLAIM; mode lives here because it is a property of the claim's role in THIS
    cascade. Keeping them apart is what lets a capability be settled for MCD and
    an open question for CCS — the case worth being able to see.

    element_key is denormalised off Position on purpose: a candidate cascade may
    slot a position into a different box than its home one, and that reframing is
    exactly the kind of move worth recording.

    Scaffolded by `sea do "add cascade_positions table with cascade_id,
    position_id, element_key, mode, note"` (2026-08-29); both FKs, the unique
    constraint and the types are hand-corrected (sea-70a3)."""

    __tablename__ = "cascade_positions"
    __table_args__ = (
        UniqueConstraint("cascade_id", "position_id", name="uq_cascade_position"),
        CheckConstraint(f"mode IN {MEMBERSHIP_MODES}", name="ck_cascade_position_mode"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cascade_id: Mapped[int] = mapped_column(ForeignKey("cascades.id"))
    position_id: Mapped[int] = mapped_column(ForeignKey("positions.id"))
    element_key: Mapped[str] = mapped_column(String(40))
    mode: Mapped[str] = mapped_column(String(20), default="fixed")
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )

    cascade: Mapped["Cascade"] = relationship(back_populates="memberships")
    position: Mapped["Position"] = relationship(backref="cascade_memberships")
