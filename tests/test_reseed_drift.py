"""What a reseed would silently change about a live gate's date.

The seed file owns a gate's date — that is the design, and `upsert_forcing`
writes `due_on` unconditionally on purpose. The failure is that nothing ever
said so out loud, so the two could disagree and only the next reseed would
find out.

Found 2026-09-03 on the live store: gate #8 carried 2026-12-15 while the file
carried `due_on: null`, so `strategy seed-forcing` would have turned a dated
gate into an undated one. No error, no log, and the gate would simply have
stopped counting as a deadline. That is this store's own founding failure —
a date that slides with no reason on the record — aimed at itself.

`gate edit` already refuses to move a date without `--note` and refuses
seed-owned gates outright. But a refusal only guards the door the store
controls; the file is the other door, and it needs a reconciler instead
(docs/two-writers-one-row.md).
"""

from __future__ import annotations

from datetime import date

import pytest
from click.testing import CliRunner

from strategy_store import cli, forcing_seed
from strategy_store.models import Forcing

# (source_system, source_ref, title, condition, due_on, gates, venture, position_ref, resolved_on)
def _row(ref, title, due, resolved=None):
    return ("vikunja", ref, title, "checkable on a date", due, "what it holds", None, None, resolved)


@pytest.fixture
def seeded(store, monkeypatch):
    """Load a two-gate file, then let the caller move the file underneath it."""
    def load(rows):
        monkeypatch.setattr(forcing_seed, "load_seed", lambda: rows)
    return load


def test_a_file_that_would_clear_a_live_date_is_reported(store, seeded):
    """Gate #8's real shape: store dated, file null."""
    seeded([_row("#0002", "Three PURR rooms", date(2026, 12, 15))])
    with store.session() as s:
        forcing_seed.upsert_forcing(s)
    seeded([_row("#0002", "Three PURR rooms", None)])
    with store.session() as s:
        drift = forcing_seed.reseed_drift(s)
    assert len(drift) == 1
    assert drift[0]["kind"] == "clear"
    assert drift[0]["store_due"] == date(2026, 12, 15)
    assert drift[0]["file_due"] is None


def test_a_file_that_would_move_a_live_date_is_reported_as_a_move(store, seeded):
    """Different severity: the gate still counts, it just counts on another day."""
    seeded([_row("#1", "a gate", date(2026, 10, 1))])
    with store.session() as s:
        forcing_seed.upsert_forcing(s)
    seeded([_row("#1", "a gate", date(2026, 11, 1))])
    with store.session() as s:
        drift = forcing_seed.reseed_drift(s)
    assert [d["kind"] for d in drift] == ["move"]


def test_agreement_is_not_drift(store, seeded):
    seeded([_row("#1", "a gate", date(2026, 10, 1))])
    with store.session() as s:
        forcing_seed.upsert_forcing(s)
        assert forcing_seed.reseed_drift(s) == []


def test_a_resolved_gate_is_not_reported(store, seeded):
    """The file cannot reopen what the store closed, so a date on an answered
    question changes nothing."""
    seeded([_row("#1", "a gate", date(2026, 10, 1))])
    with store.session() as s:
        forcing_seed.upsert_forcing(s)
        s.query(Forcing).update({"resolved_on": date(2026, 9, 1)})
        s.commit()
    seeded([_row("#1", "a gate", None)])
    with store.session() as s:
        assert forcing_seed.reseed_drift(s) == []


def test_the_reseed_really_would_do_what_the_report_says(store, seeded):
    """The report is only worth anything if it predicts the actual write.

    Asserts the behaviour, not the source: run the reseed the report warned
    about and check the date is gone.
    """
    seeded([_row("#0002", "Three PURR rooms", date(2026, 12, 15))])
    with store.session() as s:
        forcing_seed.upsert_forcing(s)
    seeded([_row("#0002", "Three PURR rooms", None)])
    with store.session() as s:
        assert forcing_seed.reseed_drift(s)[0]["kind"] == "clear"
    with store.session() as s:
        forcing_seed.upsert_forcing(s)
    with store.session() as s:
        assert s.query(Forcing).one().due_on is None, "the warning was right"
        assert forcing_seed.reseed_drift(s) == [], "and afterwards there is nothing left to warn about"


def test_reconcile_names_the_file_as_the_place_to_fix_it(store, seeded):
    """The refusal pattern from case 1: name the real owner, do not just report."""
    seeded([_row("#0002", "Three PURR rooms", date(2026, 12, 15))])
    with store.session() as s:
        forcing_seed.upsert_forcing(s)
    seeded([_row("#0002", "Three PURR rooms", None)])
    r = CliRunner().invoke(cli.main, ["reconcile", "--no-gates"])
    assert r.exit_code == 0, r.output
    assert "SEED FILE vs STORE" in r.output
    assert "UNDATE this gate" in r.output
    assert "fix in the file (vikunja:#0002), not here — it owns the date" in r.output


def test_forcing_warns_inline_because_that_is_where_dates_are_read(store, seeded):
    seeded([_row("#0002", "Three PURR rooms", date(2026, 12, 15))])
    with store.session() as s:
        forcing_seed.upsert_forcing(s)
    seeded([_row("#0002", "Three PURR rooms", None)])
    r = CliRunner().invoke(cli.main, ["forcing", "--no-check"])
    assert r.exit_code == 0, r.output
    assert "would be RE-DATED" in r.output


def test_a_clean_store_says_so_rather_than_saying_nothing(store, seeded):
    seeded([_row("#1", "a gate", date(2026, 10, 1))])
    with store.session() as s:
        forcing_seed.upsert_forcing(s)
    r = CliRunner().invoke(cli.main, ["reconcile", "--no-gates"])
    assert "the file and the store agree on every date" in r.output
