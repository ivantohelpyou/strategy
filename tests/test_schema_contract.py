"""The ONE TABLE SET contract, checked from the side that writes the schema.

Every drift in sea-zrbp's history was introduced by a commit in THIS repo, and
was caught (late, or not at all) by a check living in the consumer. These tests
put the check where the writer is. Two layers:

* the mechanism, against synthetic model files — a missing column, a retype, a
  table renamed away upstream, a table nobody has claimed. These are the
  assertions that keep the checker honest, and they never depend on what happens
  to be on this laptop.
* the live standing of each registered consumer, skipped when its checkout is
  absent (CI and other machines have this repo without follow-whee). It fails on
  an UNRECORDED violation and on a recorded one that has been fixed — not on the
  known-open list, which is the consumer's to clear.
"""

from __future__ import annotations

import textwrap

import pytest

from strategy_store import schema_contract as sc

STORE_SRC = '''
from sqlalchemy.orm import Mapped, mapped_column


class Position(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(300))
    superseded_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("positions.id"), nullable=True
    )
    evidence: Mapped[list["Evidence"]] = relationship(back_populates="position")


class Drop(Base):
    __tablename__ = "drops"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)


class Cascade(Base):
    __tablename__ = "cascades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
'''


def _write(tmp_path, name: str, src: str):
    p = tmp_path / name
    p.write_text(textwrap.dedent(src))
    return p


@pytest.fixture
def store(tmp_path):
    return sc.tables(_write(tmp_path, "store.py", STORE_SRC))


def _consumer(tmp_path, name: str, src: str, borrows=("positions", "drops"), owns=()):
    _write(tmp_path, "copy.py", src)
    return sc.Consumer(
        name=name,
        env="TEST_CONSUMER_SRC",
        default_root=tmp_path,
        models_path="copy.py",
        borrows=borrows,
        owns=owns,
        why="test fixture",
    )


# --- the reader -----------------------------------------------------------


def test_only_mapped_columns_count_as_columns(store):
    """`relationship(...)` is not a column, and must not be compared as one."""
    assert "evidence" not in store["positions"]
    assert set(store["positions"]) == {"id", "title", "superseded_by_id"}


def test_the_declared_type_is_the_first_positional_argument(store):
    assert store["positions"]["title"] == "String(300)"
    assert store["positions"]["superseded_by_id"] == "ForeignKey('positions.id')"


def test_a_keyword_only_column_has_no_comparable_type(tmp_path):
    """Guessing a type from the annotation would fail on things that are fine."""
    t = sc.tables(
        _write(
            tmp_path,
            "kw.py",
            '''
            class P(Base):
                __tablename__ = "p"
                x: Mapped[int] = mapped_column(primary_key=True)
            ''',
        )
    )
    assert t["p"]["x"] is None


# --- the violations -------------------------------------------------------


def test_a_column_written_here_and_missing_from_the_copy_is_a_violation(tmp_path, store):
    """The drift this bead is about: a nullable add nothing raises on.

    follow-whee reads the same Postgres. The column is there; its models cannot
    see it, so the app is blind to data the CLI is writing.
    """
    c = _consumer(
        tmp_path,
        "blind",
        '''
        class Position(Base):
            __tablename__ = "positions"
            id: Mapped[int] = mapped_column(Integer, primary_key=True)
            title: Mapped[str] = mapped_column(String(300))

        class Drop(Base):
            __tablename__ = "drops"
            id: Mapped[int] = mapped_column(Integer, primary_key=True)
        ''',
    )
    r = sc.check(c, store)
    assert [(v.key, v.kind) for v in r.violations] == [
        ("positions.superseded_by_id", "missing-column")
    ]
    assert not r.ok


def test_the_same_column_at_a_different_width_is_a_violation(tmp_path, store):
    c = _consumer(
        tmp_path,
        "narrow",
        '''
        class Position(Base):
            __tablename__ = "positions"
            id: Mapped[int] = mapped_column(Integer, primary_key=True)
            title: Mapped[str] = mapped_column(String(60))
            superseded_by_id: Mapped[int | None] = mapped_column(
                ForeignKey("positions.id"), nullable=True
            )

        class Drop(Base):
            __tablename__ = "drops"
            id: Mapped[int] = mapped_column(Integer, primary_key=True)
        ''',
    )
    r = sc.check(c, store)
    assert [(v.key, v.kind) for v in r.violations] == [("positions.title", "retyped-column")]


def test_a_borrowed_table_renamed_away_upstream_is_a_violation(tmp_path, store):
    """The direction an intersection diff cannot see.

    Compute the shared set as `set(store) & set(copy)` and a table renamed here
    quietly leaves it — so the check goes green on the change that breaks the
    consumer worst. `borrows` is declared, so its absence is caught.
    """
    c = _consumer(
        tmp_path,
        "orphaned",
        '''
        class Forcing(Base):
            __tablename__ = "forcing"
            id: Mapped[int] = mapped_column(Integer, primary_key=True)
        ''',
        borrows=("forcing",),
    )
    r = sc.check(c, store)
    assert [(v.key, v.kind) for v in r.violations] == [("forcing", "vanished-upstream")]


def test_a_borrowed_table_the_copy_never_declared_is_a_violation(tmp_path, store):
    c = _consumer(
        tmp_path,
        "partial",
        '''
        class Position(Base):
            __tablename__ = "positions"
            id: Mapped[int] = mapped_column(Integer, primary_key=True)
            title: Mapped[str] = mapped_column(String(300))
            superseded_by_id: Mapped[int | None] = mapped_column(
                ForeignKey("positions.id"), nullable=True
            )
        ''',
    )
    r = sc.check(c, store)
    assert [(v.key, v.kind) for v in r.violations] == [("drops", "missing-table")]


def test_a_table_in_neither_borrows_nor_owns_is_undeclared(tmp_path, store):
    """The state that precedes a silent merge: nobody has said who owns it."""
    c = _consumer(
        tmp_path,
        "unclaimed",
        '''
        class Position(Base):
            __tablename__ = "positions"
            id: Mapped[int] = mapped_column(Integer, primary_key=True)
            title: Mapped[str] = mapped_column(String(300))
            superseded_by_id: Mapped[int | None] = mapped_column(
                ForeignKey("positions.id"), nullable=True
            )

        class Drop(Base):
            __tablename__ = "drops"
            id: Mapped[int] = mapped_column(Integer, primary_key=True)

        class Beat(Base):
            __tablename__ = "beats"
            id: Mapped[int] = mapped_column(Integer, primary_key=True)
        ''',
    )
    r = sc.check(c, store)
    assert r.violations == []
    assert r.undeclared == ["beats"]
    assert not r.ok


def test_a_table_the_consumer_owns_is_not_undeclared(tmp_path, store):
    c = _consumer(
        tmp_path,
        "declared",
        '''
        class Position(Base):
            __tablename__ = "positions"
            id: Mapped[int] = mapped_column(Integer, primary_key=True)
            title: Mapped[str] = mapped_column(String(300))
            superseded_by_id: Mapped[int | None] = mapped_column(
                ForeignKey("positions.id"), nullable=True
            )

        class Drop(Base):
            __tablename__ = "drops"
            id: Mapped[int] = mapped_column(Integer, primary_key=True)

        class Beat(Base):
            __tablename__ = "beats"
            id: Mapped[int] = mapped_column(Integer, primary_key=True)
        ''',
        owns=("beats",),
    )
    r = sc.check(c, store)
    assert r.ok
    assert r.undeclared == []


def test_a_store_only_table_is_lag_not_a_violation(tmp_path, store):
    """Strategy-only tables are permitted. Lag is reported, never failed."""
    c = _consumer(
        tmp_path,
        "lagging",
        '''
        class Position(Base):
            __tablename__ = "positions"
            id: Mapped[int] = mapped_column(Integer, primary_key=True)
            title: Mapped[str] = mapped_column(String(300))
            superseded_by_id: Mapped[int | None] = mapped_column(
                ForeignKey("positions.id"), nullable=True
            )

        class Drop(Base):
            __tablename__ = "drops"
            id: Mapped[int] = mapped_column(Integer, primary_key=True)
        ''',
    )
    r = sc.check(c, store)
    assert r.lag == ["cascades"]
    assert r.ok


# --- recording an open drift ---------------------------------------------


def test_a_recorded_violation_does_not_fail_the_build(tmp_path, store, monkeypatch):
    """Applying an added column is the copy's commit, in the copy's repo."""
    monkeypatch.setitem(
        sc.KNOWN_DRIFT, "blind", {"positions.superseded_by_id": "sea-zrbp — reported"}
    )
    c = _consumer(
        tmp_path,
        "blind",
        '''
        class Position(Base):
            __tablename__ = "positions"
            id: Mapped[int] = mapped_column(Integer, primary_key=True)
            title: Mapped[str] = mapped_column(String(300))

        class Drop(Base):
            __tablename__ = "drops"
            id: Mapped[int] = mapped_column(Integer, primary_key=True)
        ''',
    )
    r = sc.check(c, store)
    assert len(r.violations) == 1
    assert r.unrecorded == []
    assert r.ok


def test_recording_one_drift_does_not_excuse_the_next(tmp_path, store, monkeypatch):
    monkeypatch.setitem(sc.KNOWN_DRIFT, "blind", {"positions.title": "unrelated"})
    c = _consumer(
        tmp_path,
        "blind",
        '''
        class Position(Base):
            __tablename__ = "positions"
            id: Mapped[int] = mapped_column(Integer, primary_key=True)
            title: Mapped[str] = mapped_column(String(300))

        class Drop(Base):
            __tablename__ = "drops"
            id: Mapped[int] = mapped_column(Integer, primary_key=True)
        ''',
    )
    r = sc.check(c, store)
    assert [v.key for v in r.unrecorded] == ["positions.superseded_by_id"]
    assert not r.ok


def test_a_record_that_no_longer_reproduces_is_itself_a_failure(tmp_path, store, monkeypatch):
    """Or the list becomes prose again — the failure that produced this module."""
    monkeypatch.setitem(sc.KNOWN_DRIFT, "fixed", {"positions.superseded_by_id": "sea-zrbp"})
    c = _consumer(
        tmp_path,
        "fixed",
        '''
        class Position(Base):
            __tablename__ = "positions"
            id: Mapped[int] = mapped_column(Integer, primary_key=True)
            title: Mapped[str] = mapped_column(String(300))
            superseded_by_id: Mapped[int | None] = mapped_column(
                ForeignKey("positions.id"), nullable=True
            )

        class Drop(Base):
            __tablename__ = "drops"
            id: Mapped[int] = mapped_column(Integer, primary_key=True)
        ''',
    )
    r = sc.check(c, store)
    assert r.violations == []
    assert r.stale_records == ["positions.superseded_by_id"]
    assert not r.ok


# --- the registry, and the live consumers --------------------------------


def test_the_registry_names_tables_this_store_actually_has():
    """The store's own half of the contract: do not rename a borrowed table.

    This one runs everywhere — it needs no consumer checkout, because it is
    about what THIS repo did.
    """
    have = set(sc.tables(sc.STORE_MODELS))
    for c in sc.CONSUMERS:
        missing = sorted(set(c.borrows) - have)
        assert not missing, (
            f"{c.name} borrows {missing}, which this store no longer defines. "
            f"Renaming or dropping a borrowed table breaks it silently."
        )


def test_every_consumer_has_a_reason_and_a_way_to_be_found():
    for c in sc.CONSUMERS:
        assert c.why.strip(), f"{c.name} has no recorded reason for holding a copy"
        assert c.env and c.env.isupper()
        assert set(c.borrows) & set(c.owns) == set(), f"{c.name} both borrows and owns a table"


@pytest.mark.parametrize("consumer", sc.CONSUMERS, ids=lambda c: c.name)
def test_the_live_consumer_has_no_new_drift(consumer):
    if not consumer.present:
        pytest.skip(f"no {consumer.name} checkout at {consumer.models} (set {consumer.env})")
    r = sc.check(consumer)
    assert r.unrecorded == [], "\n".join(v.message for v in r.unrecorded)
    assert r.undeclared == [], (
        f"{consumer.name} declares tables nobody has claimed: {r.undeclared}"
    )
    assert r.stale_records == [], (
        f"fixed in {consumer.name} — delete from schema_contract.KNOWN_DRIFT: {r.stale_records}"
    )
