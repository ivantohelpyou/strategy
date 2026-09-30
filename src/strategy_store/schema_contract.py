"""Who copies this store's tables, and whether their copy is still true.

ONE TABLE SET, MORE THAN ONE WRITER. `follow-whee` cannot pip-install a private
repo, so it carries its own SQLAlchemy declaration of six of this store's tables
and writes the same Postgres schema. The rule has always been:

    every column the CLI writes must exist in the copy with the same name and
    type; a copy may ADD columns (nullable or defaulted) and tables of its own,
    and may never rename or drop what the CLI expects.

For months that rule was a comment in the copy, and it drifted three times. The
third time is why this module is here rather than in the copy: **every drift was
introduced on this side.** `positions.claim_key`, `evidence.stance`, the four
`drops` view columns and `positions.superseded_by_id` were all added by a commit
in this repo, by a session that had no reason to open follow-whee at all. A check
that lives only in the consumer can report the damage; it cannot prevent it,
because the person who caused it never runs that suite.

So the store owns the contract, as data — `CONSUMERS` below — and the check runs
where the schema is written. Adding a fourth writer costs one entry here, not
another copy of this file.

Two things this catches that a consumer-side intersection diff cannot:

* a table RENAMED or DROPPED upstream. Inferring the shared set as
  `set(store) & set(copy)` means a renamed table quietly leaves the shared set
  and the check goes green on the exact change that breaks the consumer worst.
  `borrows` is declared, so its absence upstream is a violation.
* a stale registry. A table in the copy that is in neither `borrows` nor `owns`
  means nobody has decided which side owns it — the state that precedes a
  silent merge.

What this side can be held to, and what it cannot: the store may ADD a column
(the contract permits it) but cannot make the commit that applies it in someone
else's repo. So an open drift is RECORDED — `KNOWN_DRIFT` — and the suite fails
on an unrecorded one. A recorded entry that no longer reproduces is also a
failure, so a fixed drift has to be deleted rather than accumulating into a list
nobody reads.

It is an AST diff, not an import: the two projects have different dependency
sets and neither can import the other, which is the whole reason the copy
exists. Nothing here imports SQLAlchemy or opens a database.

See docs/two-writers-one-row.md (this is its schema-shaped case) and sea-zrbp.
"""

from __future__ import annotations

import ast
import os
from dataclasses import dataclass, field
from pathlib import Path

# "table.column" keys whose declared type a copy is not expected to match, because
# the copy legitimately targets a different backend for that one. Empty today, and
# named so the first real exception has an obvious home instead of a special case
# buried in the comparison.
TYPE_EXEMPT: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Consumer:
    """A program that carries its own declaration of some of these tables."""

    name: str
    env: str
    default_root: Path
    models_path: str
    borrows: tuple[str, ...]
    owns: tuple[str, ...]
    why: str

    @property
    def models(self) -> Path:
        root = Path(os.environ.get(self.env) or self.default_root)
        return root / self.models_path

    @property
    def present(self) -> bool:
        return self.models.exists()


CONSUMERS: tuple[Consumer, ...] = (
    Consumer(
        name="follow-whee",
        env="FOLLOW_WHEE_SRC",
        default_root=Path.home() / "projects/follow-whee",
        models_path="src/followwhee/models.py",
        borrows=("drops", "evidence", "forcing", "metric_readings", "metrics", "positions"),
        owns=("beats", "drop_stats"),
        why=(
            "the hosted app reads the same schema and cannot pip-install a private "
            "repo (solutions-6zs); the shared-package decision is solutions-14t, "
            "dated 2026-10-16 — a week after the publication gate that ends the "
            "reason for the copy"
        ),
    ),
)


def _column_type(node: ast.AST) -> str | None:
    """The SQL type out of `mapped_column(String(60), nullable=True)`.

    The first positional argument, rendered flat — "String(60)", "Integer",
    "ForeignKey('positions.id')". A keyword-only call (a relationship, or a
    mapped_column that leans on the annotation for its type) returns None and is
    not compared: guessing a type from an annotation would produce failures that
    are not real, and a check that cries wolf gets switched off.
    """
    if not isinstance(node, ast.Call):
        return None
    fn = node.func
    if (getattr(fn, "id", None) or getattr(fn, "attr", None)) != "mapped_column":
        return None
    if not node.args:
        return None
    return ast.unparse(node.args[0])


def tables(path: Path) -> dict[str, dict[str, str | None]]:
    """{table_name: {column_name: sql_type_or_None}} for every mapped class."""
    tree = ast.parse(path.read_text(), filename=str(path))
    found: dict[str, dict[str, str | None]] = {}
    for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        name: str | None = None
        cols: dict[str, str | None] = {}
        for stmt in cls.body:
            if (
                isinstance(stmt, ast.Assign)
                and len(stmt.targets) == 1
                and getattr(stmt.targets[0], "id", None) == "__tablename__"
                and isinstance(stmt.value, ast.Constant)
            ):
                name = stmt.value.value
            elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                # Only mapped_column(...) declares a column; relationship(...) does not.
                if isinstance(stmt.value, ast.Call):
                    fn = stmt.value.func
                    if (getattr(fn, "id", None) or getattr(fn, "attr", None)) == "mapped_column":
                        cols[stmt.target.id] = _column_type(stmt.value)
        if name:
            found[name] = cols
    return found


STORE_MODELS = Path(__file__).resolve().parent / "models.py"


@dataclass(frozen=True)
class Violation:
    """One broken clause of the contract, keyed so it can be recorded."""

    key: str  # "positions.superseded_by_id", or a bare table name
    kind: str  # missing-column | retyped-column | missing-table | vanished-upstream
    message: str

    def __str__(self) -> str:  # pragma: no cover - display only
        return self.message


# Violations that are open RIGHT NOW and are the consumer's to fix, recorded so
# the suite fails on a NEW drift instead of standing red on an old one. The store
# may add a column (the contract permits it); applying it in the copy is the
# copy's move, in the copy's repo, and this store cannot make that commit.
#
# An entry here that no longer reproduces is itself a failure. A fixed drift has
# to be deleted from this dict, or the dict becomes a list nobody reads — which
# is the failure mode that produced this module in the first place.
KNOWN_DRIFT: dict[str, dict[str, str]] = {
    # follow-whee: positions.superseded_by_id (31afc69) landed there in
    # follow-whee#3 (faa5a3a, 2026-09-13, solutions-4j4) — no open drift.
}


@dataclass
class Report:
    """One consumer's standing against the contract."""

    consumer: str
    models: Path
    violations: list[Violation] = field(default_factory=list)
    lag: list[str] = field(default_factory=list)
    undeclared: list[str] = field(default_factory=list)

    @property
    def unrecorded(self) -> list[Violation]:
        """Violations nobody has written down — the ones that fail the build."""
        known = KNOWN_DRIFT.get(self.consumer, {})
        return [v for v in self.violations if v.key not in known]

    @property
    def stale_records(self) -> list[str]:
        """Recorded drift that no longer reproduces, and must be deleted."""
        seen = {v.key for v in self.violations}
        return sorted(k for k in KNOWN_DRIFT.get(self.consumer, {}) if k not in seen)

    @property
    def ok(self) -> bool:
        return not self.unrecorded and not self.undeclared and not self.stale_records


def check(consumer: Consumer, store: dict | None = None) -> Report:
    """AST-diff one consumer's copy against this store's declaration."""
    store = tables(STORE_MODELS) if store is None else store
    copy = tables(consumer.models)
    r = Report(consumer=consumer.name, models=consumer.models)

    for t in consumer.borrows:
        if t not in store:
            # The direction an intersection diff is blind to.
            r.violations.append(
                Violation(
                    t,
                    "vanished-upstream",
                    f"{t} — declared borrowed, but this store no longer defines it. "
                    f"A rename or drop here breaks {consumer.name}, and the contract "
                    f"forbids it. Restore the name, or retire the entry in CONSUMERS "
                    f"and tell {consumer.name} first.",
                )
            )
            continue
        if t not in copy:
            r.violations.append(
                Violation(
                    t,
                    "missing-table",
                    f"{t} — declared borrowed, but the copy does not define it",
                )
            )
            continue
        for col, typ in store[t].items():
            key = f"{t}.{col}"
            if col not in copy[t]:
                r.violations.append(
                    Violation(
                        key,
                        "missing-column",
                        f"{key} — written here, MISSING from the copy"
                        + (f" (type {typ})" if typ else ""),
                    )
                )
            elif typ and copy[t][col] and typ != copy[t][col] and key not in TYPE_EXEMPT:
                r.violations.append(
                    Violation(
                        key,
                        "retyped-column",
                        f"{key} — type differs: this store says {typ}, "
                        f"the copy says {copy[t][col]}",
                    )
                )

    declared = set(consumer.borrows) | set(consumer.owns)
    r.undeclared = sorted(set(copy) - declared)
    r.lag = sorted(set(store) - set(consumer.borrows))
    return r


def check_all() -> list[Report]:
    """Every consumer whose checkout is present, in registry order."""
    store = tables(STORE_MODELS)
    return [check(c, store) for c in CONSUMERS if c.present]
