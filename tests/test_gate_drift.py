"""The three gate-drift classes, as the three real cases that produced them.

Each fixture below is a thing that actually happened, dated. They are written out
rather than summarised because the point of each class is a distinction that only
shows up in a real case — "the work shipped" and "the work moved somewhere else"
produce the same closed bead and want different reading from a person.

Nothing here reads a rig. The reader is injected, so these assert the store's
logic and not the state of this laptop's beads, which changes underneath them.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest
from click.testing import CliRunner

from strategy_store import beadlink, cli, prime
from strategy_store.models import Forcing, GateSatisfier

TODAY = date(2026, 9, 3)


def _gate(store, title, due=None, ref=None, created=None, gates="what it holds back"):
    with store.session() as s:
        f = Forcing(
            title=title,
            condition="checkable on a date",
            gates=gates,
            due_on=due,
            source_system="manual",
            source_ref=ref or f"test:{title[:30]}",
            created_at=created or datetime(2026, 8, 17, tzinfo=timezone.utc),
        )
        s.add(f)
        s.commit()
        return f.id


def _link(store, gate_id, bead_id=None, event=None):
    with store.session() as s:
        s.add(
            GateSatisfier(
                forcing_id=gate_id,
                kind="event" if event else "bead",
                bead_id=bead_id,
                note=event or "",
            )
        )
        s.commit()


def _state(bead_id, status="open", closed_at=None, reason="", rig="sea", title="w"):
    return beadlink.BeadState(bead_id, rig, status, title, closed_at, reason)


def _reader(mapping):
    return lambda b: mapping.get(b)


# --- case 1: SHIPPED-BUT-GATED -------------------------------------------


def test_shipped_but_gated_is_the_qr_handout_case(store):
    """Gate #6 gated the QR handout. sea-btz.6 closed 2026-07-03 'SHIPPED +
    LIVE-VERIFIED'. The gate stayed open for eight weeks. Nothing was wrong and
    nothing had gone wrong since — it was simply never asked."""
    gid = _gate(store, "SEA UX-generation demo date", due=date(2026, 9, 22))
    _link(store, gid, "sea-btz.6")
    reader = _reader(
        {"sea-btz.6": _state("sea-btz.6", "closed", date(2026, 7, 3), "SHIPPED + LIVE-VERIFIED")}
    )
    with store.session() as s:
        found = beadlink.bead_findings(s, reader=reader)
    assert [f.kind for f in found] == [beadlink.SHIPPED]
    f = found[0]
    assert f.gate_id == gid and f.bead_id == "sea-btz.6"
    assert f.stale_days == (date.today() - date(2026, 7, 3)).days


def test_one_bead_still_open_is_not_shipped_but_gated(store):
    """"Every satisfying bead is closed" is the whole claim. Two links, one open,
    and the gate is still waiting on something real."""
    gid = _gate(store, "two halves", due=date(2026, 9, 22))
    _link(store, gid, "sea-done")
    _link(store, gid, "sea-open")
    reader = _reader(
        {
            "sea-done": _state("sea-done", "closed", date(2026, 7, 3)),
            "sea-open": _state("sea-open", "open"),
        }
    )
    with store.session() as s:
        assert beadlink.bead_findings(s, reader=reader) == []


# --- case 2: SUPERSEDED ---------------------------------------------------


def test_a_bead_closed_as_superseded_reads_differently_from_one_that_shipped(store):
    """sea-btz.9, closed 2026-09-03: 'SUPERSEDED on both halves; nothing unique
    left ... sea-tvw2 replaced it' (the named archive is in the bead, not here —
    this repo is due to go public 2026-10-09, gate #13).

    The bead is closed either way. But work that MOVED may have taken what
    satisfies the gate with it, so this is a different sentence and a human read,
    never an auto-resolve."""
    gid = _gate(store, "Brownfield demo on stage", due=date(2026, 9, 22))
    _link(store, gid, "sea-btz.9")
    reader = _reader(
        {
            "sea-btz.9": _state(
                "sea-btz.9", "closed", date(2026, 9, 3),
                "SUPERSEDED on both halves; nothing unique left. sea-tvw2 replaced it",
            )
        }
    )
    with store.session() as s:
        found = beadlink.bead_findings(s, reader=reader)
    assert [f.kind for f in found] == [beadlink.SUPERSEDED]
    assert "sea-tvw2 replaced it" in found[0].detail


def test_superseded_wins_over_shipped_when_a_gate_has_both(store):
    """The weaker claim must not hide the one that needs a person."""
    gid = _gate(store, "mixed", due=date(2026, 9, 22))
    _link(store, gid, "sea-btz.8")
    _link(store, gid, "sea-plain")
    reader = _reader(
        {
            "sea-btz.8": _state(
                "sea-btz.8", "closed", date(2026, 9, 3), "SUPERSEDED by explainer 138"
            ),
            "sea-plain": _state("sea-plain", "closed", date(2026, 8, 1), "shipped"),
        }
    )
    with store.session() as s:
        found = beadlink.bead_findings(s, reader=reader)
    assert [f.kind for f in found] == [beadlink.SUPERSEDED]


def test_the_supersession_marker_is_read_from_the_head_of_the_reason(store):
    """A close reason that merely MENTIONS an older superseded thing further down
    is not itself a supersession. The fleet writes the verdict at the front."""
    gid = _gate(store, "plain ship", due=date(2026, 9, 22))
    _link(store, gid, "sea-x")
    tail = "x" * 400 + " SUPERSEDED something unrelated"
    reader = _reader({"sea-x": _state("sea-x", "closed", date(2026, 8, 1), tail)})
    with store.session() as s:
        assert [f.kind for f in beadlink.bead_findings(s, reader=reader)] == [beadlink.SHIPPED]


# --- case 3: PREMISE-STALE ------------------------------------------------


def test_premise_stale_is_a_dated_gate_naming_nothing(store):
    """Gate #23: 'On 2026-09-22, FHIA people are in the room.' No bead moved, and
    no bead could — nothing in the store can say whether the premise still holds.
    This claims only that, and does not pretend to know why."""
    gid = _gate(store, "FHIA and neighborhood associations at MCD LIVE", due=date(2026, 9, 22))
    with store.session() as s:
        found = beadlink.stale_findings(s, today=TODAY)
    assert [(f.kind, f.gate_id) for f in found] == [(beadlink.STALE, gid)]
    assert "due in 19d" in found[0].detail


def test_row_age_is_context_and_never_a_filter(store):
    """The age signal the bead proposes does not work, and the data said so.

    `created_at` is when the ROW was written. Gate #23 was 3 days old when it went
    stale, because it had just been re-added; filtering on age would have hidden
    the case the bead names it for.
    """
    gid = _gate(store, "just added", due=date(2026, 9, 22),
                created=datetime(2026, 9, 3, tzinfo=timezone.utc))
    with store.session() as s:
        found = beadlink.stale_findings(s, today=TODAY)
    assert [f.gate_id for f in found] == [gid], "a 0-day-old row must still be reported"
    assert "row written 0d ago" in found[0].detail


def test_a_gate_that_names_something_is_not_premise_stale(store):
    """Covered by the live classes. Saying it twice trains the reader to skim."""
    gid = _gate(store, "linked", due=date(2026, 9, 22))
    _link(store, gid, event="the room happens")
    with store.session() as s:
        assert beadlink.stale_findings(s, today=TODAY) == []


def test_an_edited_gate_is_not_premise_stale(store):
    """An `[edited …]` note means someone has looked at the text since it was written."""
    _gate(store, "corrected", due=date(2026, 9, 22),
          gates="what it holds back\n[edited 2026-09-01] condition rewritten")
    with store.session() as s:
        assert beadlink.stale_findings(s, today=TODAY) == []


def test_a_gate_beyond_the_horizon_is_not_yet_a_finding(store):
    _gate(store, "far off", due=date(2027, 4, 2))
    with store.session() as s:
        assert beadlink.stale_findings(s, today=TODAY) == []


def test_a_missed_gate_is_not_reported_as_premise_stale(store):
    """`forcing` already shouts MISSED. A second name for it is noise."""
    _gate(store, "overdue", due=date(2026, 8, 1))
    with store.session() as s:
        assert beadlink.stale_findings(s, today=TODAY) == []


# --- a linked id no rig has ----------------------------------------------


def test_a_linked_bead_no_rig_has_is_its_own_finding(store):
    gid = _gate(store, "typo'd link", due=date(2026, 9, 22))
    _link(store, gid, "sea-ghost")
    with store.session() as s:
        found = beadlink.bead_findings(s, reader=_reader({}))
    assert [(f.kind, f.bead_id) for f in found] == [(beadlink.MISSING, "sea-ghost")]


# --- nothing is ever resolved --------------------------------------------


def test_no_class_of_drift_ever_resolves_a_gate(store):
    """`gate resolve`'s own docstring: resolving one whose question is still open
    is the failure the command makes easy. The tool reports; Ivan decides."""
    shipped = _gate(store, "shipped", due=date(2026, 9, 22), ref="r1")
    moved = _gate(store, "moved", due=date(2026, 9, 22), ref="r2")
    _gate(store, "stale", due=date(2026, 9, 22), ref="r3")
    _link(store, shipped, "sea-a")
    _link(store, moved, "sea-b")
    reader = _reader(
        {
            "sea-a": _state("sea-a", "closed", date(2026, 7, 3)),
            "sea-b": _state("sea-b", "closed", date(2026, 9, 3), "SUPERSEDED by x"),
        }
    )
    with store.session() as s:
        assert len(beadlink.bead_findings(s, reader=reader)) == 2
        assert len(beadlink.stale_findings(s, today=TODAY)) == 1
    with store.session() as s:
        assert all(f.resolved_on is None for f in s.query(Forcing).all())


# --- the cache, and what prime does with it ------------------------------


def test_the_cache_round_trips_every_field(tmp_path, store):
    gid = _gate(store, "the QR handout", due=date(2026, 9, 22))
    _link(store, gid, "sea-btz.6")
    path = tmp_path / "gate-drift.json"
    reader = _reader({"sea-btz.6": _state("sea-btz.6", "closed", date(2026, 7, 3))})
    with store.session() as s:
        written = beadlink.bead_findings(s, reader=reader)
    beadlink.write_cache(written, path)
    back, when = beadlink.read_cache(path)
    assert when is not None
    assert [f.as_dict() for f in back] == [f.as_dict() for f in written]


def test_a_corrupt_cache_is_no_findings_not_a_crash(tmp_path):
    """prime must never fail a session start over a cache file."""
    path = tmp_path / "gate-drift.json"
    path.write_text("{ not json")
    assert beadlink.read_cache(path) == ([], None)
    path.write_text(json.dumps({"checked_at": "not-a-date", "findings": []}))
    assert beadlink.read_cache(path) == ([], None)


def test_prime_says_never_checked_rather_than_looking_clean(monkeypatch):
    monkeypatch.setattr(beadlink, "read_cache", lambda *a, **k: ([], None))
    assert prime.gate_drift_lines([]) == ["Gate drift: never checked  →  `strategy forcing`"]


def test_prime_shows_the_age_with_the_finding(monkeypatch, store):
    """A stale answer presented as a current one is this failure one level up."""
    gid = _gate(store, "the QR handout", due=date(2026, 9, 22))
    _link(store, gid, "sea-btz.6")
    reader = _reader({"sea-btz.6": _state("sea-btz.6", "closed", date(2026, 7, 3))})
    with store.session() as s:
        found = beadlink.bead_findings(s, reader=reader)
    when = datetime.now(timezone.utc) - timedelta(hours=30)
    monkeypatch.setattr(beadlink, "read_cache", lambda *a, **k: (found, when))
    lines = prime.gate_drift_lines([])
    assert lines and lines[0].startswith("GATE DRIFT (30h ago): #")
    assert "strategy forcing" in lines[0]


def test_prime_stays_quiet_about_a_fresh_clean_check(monkeypatch):
    """No news is not worth a line in a block that opens every session."""
    monkeypatch.setattr(
        beadlink, "read_cache", lambda *a, **k: ([], datetime.now(timezone.utc))
    )
    assert prime.gate_drift_lines([]) == []


def test_prime_speaks_up_when_a_clean_check_has_gone_stale(monkeypatch):
    old = datetime.now(timezone.utc) - timedelta(days=9)
    monkeypatch.setattr(beadlink, "read_cache", lambda *a, **k: ([], old))
    assert prime.gate_drift_lines([]) == ["Gate drift: none as of 9d ago"]


def test_prime_reads_the_cache_and_never_a_rig(monkeypatch, store):
    """This block opens every session in every rig. A `bd show` is ~0.5s and a
    miss is ~5s, so a live read here is a new way for every session everywhere to
    get slower."""
    def boom(*a, **k):  # pragma: no cover - the assertion is that it never runs
        raise AssertionError("prime read a rig")

    monkeypatch.setattr(beadlink, "read_state", boom)
    monkeypatch.setattr(beadlink, "_bd_show", boom)
    monkeypatch.setattr(beadlink, "read_cache", lambda *a, **k: ([], None))
    assert prime.gate_drift_lines([])


# --- the surfaces ---------------------------------------------------------


@pytest.fixture
def drifting(store, tmp_path, monkeypatch):
    monkeypatch.setattr(beadlink, "CACHE_PATH", tmp_path / "gate-drift.json")
    gid = _gate(store, "the QR handout", due=date(2026, 9, 22), ref="r1")
    _link(store, gid, "sea-btz.6")
    monkeypatch.setattr(
        beadlink,
        "read_state",
        lambda b: _state(b, "closed", date(2026, 7, 3), "SHIPPED + LIVE-VERIFIED"),
    )
    return gid


def test_forcing_marks_the_drifting_gate_inline(drifting):
    """The signal has to be in the thing that IS read. A report you have to
    remember to run reproduces the failure it was built to fix."""
    r = CliRunner().invoke(cli.main, ["forcing"])
    assert r.exit_code == 0, r.output
    assert "⚠ DRIFT" in r.output
    assert "SHIPPED-BUT-GATED" in r.output
    assert "never auto-resolved" in r.output


def test_forcing_can_be_run_without_reading_the_rigs(drifting):
    r = CliRunner().invoke(cli.main, ["forcing", "--no-check"])
    assert r.exit_code == 0, r.output
    assert "⚠ DRIFT" not in r.output
    assert "rigs not read" in r.output


def test_forcing_leaves_its_answer_in_the_cache_for_prime(drifting, tmp_path):
    CliRunner().invoke(cli.main, ["forcing"])
    found, when = beadlink.read_cache(tmp_path / "gate-drift.json")
    assert when is not None
    assert [f.kind for f in found] == [beadlink.SHIPPED]


def test_reconcile_reports_positions_and_gates_as_two_halves(drifting):
    r = CliRunner().invoke(cli.main, ["reconcile"])
    assert r.exit_code == 0, r.output
    assert "POSITIONS" in r.output and "GATES" in r.output
    assert "SHIPPED-BUT-GATED" in r.output


def test_reconcile_resolves_nothing(drifting, store):
    CliRunner().invoke(cli.main, ["reconcile"])
    with store.session() as s:
        assert s.get(Forcing, drifting).resolved_on is None


def test_a_clean_check_says_so_rather_than_saying_nothing(store, tmp_path, monkeypatch):
    monkeypatch.setattr(beadlink, "CACHE_PATH", tmp_path / "gate-drift.json")
    monkeypatch.setattr(beadlink, "read_state", lambda b: _state(b, "open"))
    r = CliRunner().invoke(cli.main, ["forcing"])
    assert "no gate drift" in r.output


# --- the position class: the cheapest one, and the last one noticed ------


def _position(store, title="a claim", status="contested", decided=None, superseded_by=None):
    from strategy_store.models import Position

    with store.session() as s:
        p = Position(
            title=title, statement="", status=status, scope="fleet",
            source_system="manual", source_ref=f"test:{title[:30]}",
            decided_on=decided, superseded_by_id=superseded_by,
        )
        s.add(p)
        s.commit()
        return p.id


def test_a_gate_waiting_on_a_superseded_position_is_settled(store):
    """Gate #20: 'A position exists that supersedes #22.' Asked of #22, that is
    one local SELECT — no rig, no subprocess, nobody present on a day."""
    replacement = _position(store, "STRATEGY v4")
    old = _position(store, "STRATEGY v3", superseded_by=replacement)
    gid = _gate(store, "STRATEGY v4 after MCD LIVE", due=date(2026, 9, 24))
    with store.session() as s:
        s.add(GateSatisfier(forcing_id=gid, kind="position",
                            position_id=old, predicate="superseded"))
        s.commit()
    with store.session() as s:
        found = beadlink.position_findings(s)
    assert [(f.kind, f.gate_id) for f in found] == [(beadlink.SETTLED, gid)]
    assert f"superseded by #{replacement}" in found[0].detail


def test_a_position_not_yet_superseded_is_not_a_finding(store):
    pid = _position(store, "STRATEGY v3")
    gid = _gate(store, "waiting", due=date(2026, 9, 24))
    with store.session() as s:
        s.add(GateSatisfier(forcing_id=gid, kind="position",
                            position_id=pid, predicate="superseded"))
        s.commit()
    with store.session() as s:
        assert beadlink.position_findings(s) == []


def test_a_gate_waiting_on_a_decided_position_is_settled(store):
    """Gate #18: '#88 ... is adopted out of contested.'"""
    pid = _position(store, "win condition", decided=date(2026, 8, 30))
    gid = _gate(store, "Win condition target number", due=date(2026, 9, 4))
    with store.session() as s:
        s.add(GateSatisfier(forcing_id=gid, kind="position",
                            position_id=pid, predicate="decided"))
        s.commit()
    with store.session() as s:
        found = beadlink.position_findings(s)
    assert [f.kind for f in found] == [beadlink.SETTLED]
    assert found[0].closed_at == date(2026, 8, 30)


def test_a_link_to_a_position_this_store_does_not_have_is_reported(store):
    """Same failure as a bead id no rig has: it reads as 'not yet' forever."""
    gid = _gate(store, "dangling", due=date(2026, 9, 24))
    with store.session() as s:
        s.add(GateSatisfier(forcing_id=gid, kind="position",
                            position_id=9999, predicate="decided"))
        s.commit()
    with store.session() as s:
        found = beadlink.position_findings(s)
    assert [f.kind for f in found] == [beadlink.MISSING]


def test_the_position_class_never_reads_a_rig(store, monkeypatch):
    """The whole reason prime can run this live on every session."""
    def boom(*a, **k):  # pragma: no cover
        raise AssertionError("position_findings read a rig")

    pid = _position(store, "decided one", decided=date(2026, 8, 30))
    gid = _gate(store, "g", due=date(2026, 9, 24))
    with store.session() as s:
        s.add(GateSatisfier(forcing_id=gid, kind="position",
                            position_id=pid, predicate="decided"))
        s.commit()
    monkeypatch.setattr(beadlink, "read_state", boom)
    monkeypatch.setattr(beadlink, "_bd_show", boom)
    with store.session() as s:
        assert len(beadlink.free_findings(s)) == 1


def test_a_position_satisfier_resolves_nothing(store):
    pid = _position(store, "decided one", decided=date(2026, 8, 30))
    gid = _gate(store, "g", due=date(2026, 9, 24))
    with store.session() as s:
        s.add(GateSatisfier(forcing_id=gid, kind="position",
                            position_id=pid, predicate="decided"))
        s.commit()
    with store.session() as s:
        beadlink.position_findings(s)
    with store.session() as s:
        assert s.get(Forcing, gid).resolved_on is None


def test_prime_shows_a_settled_gate_live_with_no_age(store, monkeypatch):
    """Computed now, so it carries no 'as of'. A cached answer that could have
    been current is a stale answer for no reason."""
    replacement = _position(store, "v4")
    old = _position(store, "v3", superseded_by=replacement)
    gid = _gate(store, "STRATEGY v4", due=date(2026, 9, 24))
    with store.session() as s:
        s.add(GateSatisfier(forcing_id=gid, kind="position",
                            position_id=old, predicate="superseded"))
        s.commit()
    monkeypatch.setattr(beadlink, "read_cache", lambda *a, **k: ([], datetime.now(timezone.utc)))
    with store.session() as s:
        lines = prime.gate_drift_lines(beadlink.free_findings(s))
    assert lines and lines[0].startswith(f"GATE SETTLED: #{gid}")
    assert "ago" not in lines[0]
    assert f"gate resolve {gid}" in lines[0]


def test_a_position_predicate_must_be_one_the_store_can_evaluate(store):
    """A condition nothing can evaluate is the deferral-without-a-trigger
    failure this table exists to catch."""
    pid = _position(store, "p")
    gid = _gate(store, "g", due=date(2026, 9, 24))
    with store.session() as s:
        s.add(GateSatisfier(forcing_id=gid, kind="position",
                            position_id=pid, predicate="vibes"))
        with pytest.raises(Exception):
            s.commit()


def test_a_position_satisfier_must_say_what_has_to_become_true(store):
    pid = _position(store, "p")
    gid = _gate(store, "g", due=date(2026, 9, 24))
    with store.session() as s:
        s.add(GateSatisfier(forcing_id=gid, kind="position", position_id=pid))
        with pytest.raises(Exception):
            s.commit()


def test_gate_link_refuses_a_position_without_a_predicate(store):
    gid = _gate(store, "g", due=date(2026, 9, 24))
    r = CliRunner().invoke(cli.main, ["gate", "link", str(gid), "--position", "1"])
    assert r.exit_code != 0
    assert "--position and --when go together" in r.output


def test_gate_link_records_a_position_and_flags_one_already_moved(store):
    pid = _position(store, "already decided", decided=date(2026, 8, 30))
    gid = _gate(store, "g", due=date(2026, 9, 24))
    r = CliRunner().invoke(
        cli.main,
        ["gate", "link", str(gid), "--position", str(pid), "--when", "decided"],
    )
    assert r.exit_code == 0, r.output
    assert "ALREADY DECIDED" in r.output


def test_classify_names_the_position_kind(store):
    from strategy_store.models import GateSatisfier as GS

    assert beadlink.classify([GS(kind="position", position_id=1, predicate="decided")]) == (
        beadlink.POSITION
    )
    assert beadlink.classify(
        [GS(kind="position", position_id=1, predicate="decided"), GS(kind="event", note="x")]
    ) == beadlink.MIXED
    assert beadlink.EVENT not in beadlink.CHECKABLE
