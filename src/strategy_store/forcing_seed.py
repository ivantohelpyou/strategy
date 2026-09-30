"""Seed rows for `forcing` — the dated gates that live in only one system each.

The rows themselves are DATA, not code: they name real commitments, so they are
loaded at runtime from a file outside the repo rather than compiled in. Point
``STRATEGY_FORCING_SEED`` at your own file, or drop one at
``~/.strategy/forcing-seed.yaml``. See ``examples/forcing-seed.example.yaml``.

A row with ``due_on`` null is deliberate: it is a gate with no date, which is the
"deferral without a trigger" failure the report exists to surface.

Set ``resolved_on`` on a row once its condition is met. The row stays in the file
as a record of the commitment and disappears from ``forcing``, ``report`` and
``prime``, all of which already skip resolved gates.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import yaml
from sqlalchemy import select

from . import env
from .models import Forcing, Position

FIELDS = (
    "source_system",
    "source_ref",
    "title",
    "condition",
    "due_on",
    "gates",
    "venture",
    "position_ref",
    "resolved_on",
)

DEFAULT_SEED_PATH = Path.home() / ".strategy" / "forcing-seed.yaml"


def seed_path() -> Path:
    """Where the private rows live. STRATEGY_FORCING_SEED wins if set."""
    override = env.env_value("STRATEGY_FORCING_SEED")
    return Path(override).expanduser() if override else DEFAULT_SEED_PATH


def _coerce_due(value) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def load_seed(path: Path | None = None) -> list[tuple]:
    """Read the rows. Returns [] when no file is present — not an error."""
    p = path or seed_path()
    if not p.exists():
        return []
    raw = yaml.safe_load(p.read_text()) or {}
    rows = raw.get("forcing") or []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError(f"{p}: every entry under 'forcing' must be a mapping")
        missing = [f for f in ("source_system", "source_ref", "title") if not row.get(f)]
        if missing:
            raise ValueError(f"{p}: entry {row!r} is missing {', '.join(missing)}")
        out.append(
            tuple(
                _coerce_due(row.get(f)) if f in ("due_on", "resolved_on") else row.get(f)
                for f in FIELDS
            )
        )
    return out


def key_ref(ref: str, title: str, seed: list[tuple]) -> str:
    """One source ref can carry several gates (one Vikunja task can hold four), so a
    shared ref is disambiguated by title and each entry keeps its own row.

    Shared by :func:`upsert_forcing` and :func:`seeded_refs` so the two can never
    disagree about which stored row a seed entry owns — if they did, the guard on
    editing a seeded gate would pass while the reseed still overwrote it."""
    return ref if sum(1 for r in seed if r[1] == ref) == 1 else f"{ref}:{title[:40]}"


def seeded_refs(path: Path | None = None) -> set[tuple[str, str]]:
    """``(source_system, source_ref)`` for every gate the seed FILE owns.

    A gate in this set gets its title, condition, date and gates text back from
    the file on the next ``strategy seed``, so a store-side edit to those columns
    is silently lost. This is the same boundary :func:`core.set_fields` enforces
    for vikunja- and beads-sourced positions, and it exists here for the same
    reason: an edit that disappears on the next sync is worse than a refusal."""
    seed = load_seed(path)
    return {(row[0], key_ref(row[1], row[2], seed)) for row in seed}


# Markers the STORE appends to `gates`; everything from the first one onward is
# store-owned history and must survive a reseed. Kept here rather than in cli.py
# because this module is the one that would otherwise destroy it.
STORE_NOTE_MARKERS = ("\n[resolved ", "\n[edited ")


def _store_suffix(text: str | None) -> str:
    """The store-appended tail of a `gates` value — resolve and edit notes.

    Returns "" when there is none, so a seed write can always concatenate."""
    if not text:
        return ""
    cuts = [text.index(m) for m in STORE_NOTE_MARKERS if m in text]
    return text[min(cuts):] if cuts else ""


def _pos(session, ref: str | None) -> int | None:
    if not ref:
        return None
    sys_, r = ref.split(":", 1)
    p = session.scalar(
        select(Position).where(Position.source_system == sys_, Position.source_ref == r)
    )
    return p.id if p else None


def reseed_drift(session) -> list[dict]:
    """Live gates whose DATE the next `strategy seed-forcing` would silently change.

    `upsert_forcing` writes `due_on` unconditionally, because the file owns the
    date — that is the design, not an oversight. The failure is that nothing ever
    said so out loud. On 2026-09-03 gate #8 carried 2026-12-15 in the store and
    `due_on: null` in the file, so the next reseed would have turned a dated gate
    into an undated one. No error, no log, and the report would simply have
    stopped counting it as a deadline.

    That is the exact failure this table exists to catch, aimed at itself: a date
    that slides with no reason on the record. `gate edit` already refuses to move
    one without `--note`, and refuses seed-owned gates outright for this reason —
    but a refusal only guards the door the store controls. The file is the other
    door, and it needed a reconciler rather than a refusal
    (docs/two-writers-one-row.md).

    Reports only. Resolved gates are skipped: the file cannot reopen what the
    store closed, and a date on an answered question changes nothing.
    """
    seed = load_seed()
    out = []
    for sys_, ref, title, cond, due, gates, venture, pref, resolved in seed:
        key = key_ref(ref, title, seed)
        row = session.scalar(
            select(Forcing).where(Forcing.source_system == sys_, Forcing.source_ref == key)
        )
        if row is None or row.resolved_on is not None or row.due_on == due:
            continue
        out.append(
            {
                "row": row,
                "store_due": row.due_on,
                "file_due": due,
                # Clearing is worse than moving: an undated gate drops out of every
                # count of what is coming, so the gate does not move, it vanishes.
                "kind": "clear" if due is None else "move",
                "source": f"{sys_}:{key}",
            }
        )
    return out


def upsert_forcing(session, rows: list[tuple] | None = None) -> dict:
    seed = load_seed() if rows is None else rows
    counts = {"inserted": 0, "updated": 0}
    for sys_, ref, title, cond, due, gates, venture, pref, resolved in seed:
        key = key_ref(ref, title, seed)
        row = session.scalar(
            select(Forcing).where(Forcing.source_system == sys_, Forcing.source_ref == key)
        )
        if row is None:
            row = Forcing(source_system=sys_, source_ref=key)
            session.add(row)
            counts["inserted"] += 1
        else:
            counts["updated"] += 1
        # TWO WRITERS, ONE ROW. The seed file is hand-written and knows nothing
        # about `gate resolve` / `gate edit`, which append their notes to `gates`
        # and close gates in the store. A plain overwrite therefore RESURRECTED
        # closed gates and destroyed the notes saying why they closed — six rows
        # were exposed to that on 2026-09-02 (#3, #4, #5, #6, #10, #11, resolved
        # by hand after the file was last written). So: the file owns the text
        # and the date; the store owns its own appended history, and owns
        # `resolved_on` once it has set one.
        kept = _store_suffix(row.gates)
        row.title, row.condition, row.due_on, row.venture = (title, cond, due, venture)
        row.gates = (gates or "") + kept
        # A met gate stays in the file as a record and drops out of the report.
        # Only ever SETS: a file with no resolved_on must not reopen what the
        # store closed. Reopening is `gate reopen`, deliberately and by hand.
        if resolved:
            row.resolved_on = resolved
        row.position_id = _pos(session, pref)
    session.commit()
    return counts
