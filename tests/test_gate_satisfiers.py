"""Gate -> bead links: what would satisfy a gate, and how the drift is detected.

The failure being closed here is the third case in docs/two-writers-one-row.md —
no illegal write happens, both sides stay internally consistent, and they are
jointly false. Gate #6 held back the QR handout while sea-btz.6 sat closed for
eight weeks. Nothing was wrong; nothing was ever asked.

The drift tests inject their own bead reader. That is not only for speed: a test
that shells out to `bd` asserts the state of this laptop's rigs, which changes
under it, and it would be asserting the thing the store must never own.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from click.testing import CliRunner
from sqlalchemy import select

from strategy_store import beadlink, cli
from strategy_store.models import Forcing, GateSatisfier


def _gate(store, title="a gate", due=None, resolved=None, ref=None):
    with store.session() as s:
        f = Forcing(
            title=title,
            condition="checkable on a date",
            gates="something",
            due_on=due,
            resolved_on=resolved,
            source_system="manual",
            source_ref=ref or f"test:{title}",
        )
        s.add(f)
        s.commit()
        return f.id


def _state(bead_id, status="open", closed_at=None, rig="sea", title="the work"):
    return beadlink.BeadState(
        bead_id=bead_id,
        rig=rig,
        status=status,
        title=title,
        closed_at=closed_at,
        close_reason="",
    )


def _reader(mapping):
    """A stand-in for the rigs. Unknown ids return None, as a real miss does."""
    return lambda bead_id: mapping.get(bead_id)


# --- the shape of the link ------------------------------------------------


def test_a_gate_may_name_more_than_one_satisfying_bead(store):
    """The reason this is a join table and not a delimited column.

    `supersedes_id` taught the same lesson from the other side: it could hold
    exactly one row, so consolidating five positions into one kept the last and
    lost four.
    """
    gid = _gate(store)
    with store.session() as s:
        for b in ("sea-a1", "purr-b2", "qrcards-c3"):
            s.add(GateSatisfier(forcing_id=gid, kind="bead", bead_id=b))
        s.commit()
    with store.session() as s:
        f = s.get(Forcing, gid)
        assert sorted(sat.bead_id for sat in f.satisfiers) == ["purr-b2", "qrcards-c3", "sea-a1"]


def test_the_same_bead_cannot_be_linked_to_one_gate_twice(store):
    gid = _gate(store)
    with store.session() as s:
        s.add(GateSatisfier(forcing_id=gid, kind="bead", bead_id="sea-a1"))
        s.commit()
    with store.session() as s:
        s.add(GateSatisfier(forcing_id=gid, kind="bead", bead_id="sea-a1"))
        with pytest.raises(Exception):
            s.commit()


def test_one_bead_may_satisfy_more_than_one_gate(store):
    """The query the whole table exists for: which gates does this bead satisfy."""
    a, b = _gate(store, "gate a"), _gate(store, "gate b")
    with store.session() as s:
        s.add(GateSatisfier(forcing_id=a, kind="bead", bead_id="sea-shared"))
        s.add(GateSatisfier(forcing_id=b, kind="bead", bead_id="sea-shared"))
        s.commit()
    with store.session() as s:
        gates = s.execute(
            select(GateSatisfier.forcing_id).where(GateSatisfier.bead_id == "sea-shared")
        ).scalars().all()
        assert sorted(gates) == sorted([a, b])


def test_a_bead_row_must_carry_an_id_and_an_event_row_must_not(store):
    """The two halves of `kind` are not interchangeable, and the DB says so."""
    gid = _gate(store)
    with store.session() as s:
        s.add(GateSatisfier(forcing_id=gid, kind="bead", bead_id=None))
        with pytest.raises(Exception):
            s.commit()
    with store.session() as s:
        s.add(GateSatisfier(forcing_id=gid, kind="event", bead_id="sea-a1"))
        with pytest.raises(Exception):
            s.commit()


def test_nothing_in_the_link_table_mirrors_bead_state(store):
    """One writer per fact. The rig owns whether a bead is closed.

    A cached status is a second copy of someone else's fact, which is the failure
    this epic is named after. Pinned as a test because the convenient thing to do
    on the next feature is add `status` here and refresh it "just for reporting".
    """
    columns = set(GateSatisfier.__table__.columns.keys())
    forbidden = {"status", "closed_on", "closed_at", "bead_status", "state", "title"}
    assert columns & forbidden == set(), (
        f"gate_satisfiers must not mirror bead state; found {sorted(columns & forbidden)}"
    )


# --- classification -------------------------------------------------------


def test_no_satisfiers_is_unclassified_not_event(store):
    """"Nobody has looked" and "an event satisfies it" are different facts.

    Defaulting one to the other is how a gate gets quietly reclassified by a
    schema default instead of by a person.
    """
    assert beadlink.classify([]) == beadlink.UNCLASSIFIED


def test_classification_splits_bead_event_and_mixed():
    bead = GateSatisfier(kind="bead", bead_id="sea-a1")
    event = GateSatisfier(kind="event", note="the room happens")
    assert beadlink.classify([bead]) == beadlink.BEAD
    assert beadlink.classify([event]) == beadlink.EVENT
    assert beadlink.classify([bead, event]) == beadlink.MIXED


# --- the detection this bead is DONE WHEN ---------------------------------


def test_a_closed_bead_under_an_open_gate_is_detected(store):
    """The bead's DONE WHEN, and gate #6's real history.

    sea-btz.6 closed 2026-07-03 'SHIPPED + LIVE-VERIFIED' while the gate it
    satisfied stayed open for eight weeks. Nothing raised, because nothing asked.
    """
    gid = _gate(store, "the QR handout", due=date(2026, 9, 22))
    with store.session() as s:
        s.add(GateSatisfier(forcing_id=gid, kind="bead", bead_id="sea-btz.6"))
        s.commit()
    closed_on = date.today() - timedelta(days=62)
    with store.session() as s:
        found = beadlink.drift(
            s, reader=_reader({"sea-btz.6": _state("sea-btz.6", "closed", closed_on)})
        )
    assert len(found) == 1
    d = found[0]
    assert (d.gate_id, d.bead_id, d.kind) == (gid, "sea-btz.6", "satisfied")
    assert d.stale_days == 62


def test_an_open_bead_under_an_open_gate_is_not_drift(store):
    gid = _gate(store)
    with store.session() as s:
        s.add(GateSatisfier(forcing_id=gid, kind="bead", bead_id="sea-open"))
        s.commit()
    with store.session() as s:
        assert beadlink.drift(s, reader=_reader({"sea-open": _state("sea-open", "open")})) == []


def test_a_closed_bead_under_a_RESOLVED_gate_is_not_drift(store):
    """The gate was answered. Agreement is not drift."""
    gid = _gate(store, "answered", resolved=date(2026, 8, 30))
    with store.session() as s:
        s.add(GateSatisfier(forcing_id=gid, kind="bead", bead_id="sea-done"))
        s.commit()
    with store.session() as s:
        found = beadlink.drift(
            s, reader=_reader({"sea-done": _state("sea-done", "closed", date(2026, 8, 1))})
        )
    assert found == []


def test_a_superseded_bead_counts_as_done(store):
    """The work moved. A gate waiting on that id is waiting on nothing."""
    gid = _gate(store)
    with store.session() as s:
        s.add(GateSatisfier(forcing_id=gid, kind="bead", bead_id="sea-moved"))
        s.commit()
    with store.session() as s:
        found = beadlink.drift(
            s, reader=_reader({"sea-moved": _state("sea-moved", "superseded", date(2026, 8, 1))})
        )
    assert [d.kind for d in found] == ["satisfied"]


def test_a_bead_no_rig_has_is_reported_as_missing_not_as_quiet(store):
    """A dead id reads as 'not closed yet' forever — the gate looks watched."""
    gid = _gate(store)
    with store.session() as s:
        s.add(GateSatisfier(forcing_id=gid, kind="bead", bead_id="sea-ghost"))
        s.commit()
    with store.session() as s:
        found = beadlink.drift(s, reader=_reader({}))
    assert [(d.bead_id, d.kind) for d in found] == [("sea-ghost", "missing")]


def test_an_event_satisfier_is_never_reported_as_drift(store):
    """An event happening is not a bead closing, so there is nothing to read.

    Auto-checking one would manufacture a green light on a Tuesday nobody was
    watching, which is worse than the gate simply still asking.
    """
    gid = _gate(store, "the room sells seats", due=date(2026, 9, 22))
    with store.session() as s:
        s.add(GateSatisfier(forcing_id=gid, kind="event", note="MCD LIVE happens Sept 22"))
        s.commit()
    with store.session() as s:
        assert beadlink.drift(s, reader=_reader({})) == []


def test_drift_resolves_nothing(store):
    """Reports. A closed bead is evidence a gate can close, not authority."""
    gid = _gate(store)
    with store.session() as s:
        s.add(GateSatisfier(forcing_id=gid, kind="bead", bead_id="sea-x"))
        s.commit()
    with store.session() as s:
        beadlink.drift(s, reader=_reader({"sea-x": _state("sea-x", "closed", date(2026, 7, 3))}))
    with store.session() as s:
        assert s.get(Forcing, gid).resolved_on is None


# --- rig resolution -------------------------------------------------------


def test_a_prefix_may_name_more_than_one_rig(monkeypatch, tmp_path):
    """'sea-btz.6' is in the sea rig; 'sea-zrbp' is in the strategy rig.

    So the prefix orders the search and never filters it. A prefix treated as an
    answer makes a satisfied gate look unsatisfied, silently.
    """
    (tmp_path / "sea").mkdir()
    (tmp_path / "strategy").mkdir()
    (tmp_path / "purr").mkdir()
    (tmp_path / "rigs.json").write_text(
        '{"rigs": {"sea": {"beads": {"prefix": "sea"}},'
        ' "strategy": {"beads": {"prefix": "sea"}},'
        ' "purr": {"beads": {"prefix": "purr"}}}}'
    )
    monkeypatch.setattr(beadlink, "GT_ROOT", tmp_path)
    beadlink._rig_prefixes.cache_clear()
    try:
        order = beadlink.rig_candidates("sea-zrbp")
        assert set(order[:2]) == {"sea", "strategy"}, "both prefix matches come first"
        assert "purr" in order, "and every other rig is still searched"
    finally:
        beadlink._rig_prefixes.cache_clear()


# --- the CLI --------------------------------------------------------------


def test_gate_link_refuses_an_id_no_rig_has(store, monkeypatch):
    gid = _gate(store)
    monkeypatch.setattr(beadlink, "read_state", lambda b: None)
    r = CliRunner().invoke(cli.main, ["gate", "link", str(gid), "sea-nope"])
    assert r.exit_code != 0
    assert "no rig has a bead 'sea-nope'" in r.output
    with store.session() as s:
        assert s.get(Forcing, gid).satisfiers == []


def test_gate_link_flags_a_bead_that_is_already_closed(store, monkeypatch):
    """Linking work that already shipped is the finding, on the spot."""
    gid = _gate(store, "the QR handout")
    monkeypatch.setattr(
        beadlink, "read_state", lambda b: _state(b, "closed", date(2026, 7, 3))
    )
    r = CliRunner().invoke(cli.main, ["gate", "link", str(gid), "sea-btz.6"])
    assert r.exit_code == 0, r.output
    assert "ALREADY CLOSED" in r.output


def test_gate_link_records_an_event_with_its_reason(store):
    gid = _gate(store, "MCD LIVE")
    r = CliRunner().invoke(
        cli.main, ["gate", "link", str(gid), "--event", "the room happens on Sept 22"]
    )
    assert r.exit_code == 0, r.output
    with store.session() as s:
        sats = s.get(Forcing, gid).satisfiers
        assert [(x.kind, x.note) for x in sats] == [("event", "the room happens on Sept 22")]


def test_gate_link_refuses_a_resolved_gate(store):
    """Attaching work to an answered question implies that work is why it closed."""
    gid = _gate(store, "answered", resolved=date(2026, 8, 30))
    r = CliRunner().invoke(cli.main, ["gate", "link", str(gid), "--event", "x"])
    assert r.exit_code != 0
    assert "--historical" in r.output
    assert "gate reopen" in r.output


def test_historical_permits_a_resolved_gate_for_the_backfill(store):
    """Half the table is already resolved; a blanket refusal makes it
    permanently unclassifiable. The flag makes the claim deliberate."""
    gid = _gate(store, "answered", resolved=date(2026, 8, 30))
    r = CliRunner().invoke(
        cli.main, ["gate", "link", str(gid), "--historical", "--event", "the day passed"]
    )
    assert r.exit_code == 0, r.output
    with store.session() as s:
        assert len(s.get(Forcing, gid).satisfiers) == 1


def test_a_historical_link_is_still_never_reported_as_drift(store, monkeypatch):
    """Inert either way — the escape hatch cannot manufacture a report."""
    gid = _gate(store, "answered", resolved=date(2026, 8, 30))
    monkeypatch.setattr(beadlink, "read_state", lambda b: _state(b, "closed", date(2026, 7, 3)))
    CliRunner().invoke(cli.main, ["gate", "link", str(gid), "--historical", "sea-btz.6"])
    with store.session() as s:
        assert beadlink.drift(
            s, reader=_reader({"sea-btz.6": _state("sea-btz.6", "closed", date(2026, 7, 3))})
        ) == []


def test_gate_link_needs_something_to_link(store):
    gid = _gate(store)
    r = CliRunner().invoke(cli.main, ["gate", "link", str(gid)])
    assert r.exit_code != 0
    assert "--event" in r.output


def test_linking_the_same_bead_again_is_a_no_op_not_an_error(store, monkeypatch):
    gid = _gate(store)
    monkeypatch.setattr(beadlink, "read_state", lambda b: _state(b))
    run = CliRunner()
    assert run.invoke(cli.main, ["gate", "link", str(gid), "sea-a1"]).exit_code == 0
    r = run.invoke(cli.main, ["gate", "link", str(gid), "sea-a1"])
    assert r.exit_code == 0
    assert "already linked" in r.output
    with store.session() as s:
        assert len(s.get(Forcing, gid).satisfiers) == 1


def test_gate_unlink_removes_the_link_and_leaves_the_gate(store, monkeypatch):
    gid = _gate(store)
    monkeypatch.setattr(beadlink, "read_state", lambda b: _state(b))
    run = CliRunner()
    run.invoke(cli.main, ["gate", "link", str(gid), "sea-a1"])
    r = run.invoke(cli.main, ["gate", "unlink", str(gid), "sea-a1"])
    assert r.exit_code == 0, r.output
    with store.session() as s:
        assert s.get(Forcing, gid) is not None
        assert s.get(Forcing, gid).satisfiers == []


def test_gate_add_can_name_its_satisfier_up_front(store, monkeypatch):
    monkeypatch.setattr(beadlink, "read_state", lambda b: _state(b))
    r = CliRunner().invoke(
        cli.main,
        ["gate", "add", "--title", "new gate", "--condition", "c", "--gates", "g",
         "--source", "session-x", "--satisfied-by", "sea-a1", "--satisfied-by", "purr-b2"],
    )
    assert r.exit_code == 0, r.output
    with store.session() as s:
        f = s.scalar(select(Forcing).where(Forcing.title == "new gate"))
        assert sorted(x.bead_id for x in f.satisfiers) == ["purr-b2", "sea-a1"]


def test_gate_satisfiers_reports_the_three_way_split(store, monkeypatch):
    """The finding the bead asks for: which gates can be checked, and which cannot."""
    a = _gate(store, "bead-satisfiable", ref="r1")
    b = _gate(store, "event-satisfiable", ref="r2")
    _gate(store, "nobody has looked", ref="r3")
    monkeypatch.setattr(beadlink, "read_state", lambda x: _state(x))
    run = CliRunner()
    run.invoke(cli.main, ["gate", "link", str(a), "sea-a1"])
    run.invoke(cli.main, ["gate", "link", str(b), "--event", "the room happens"])
    r = run.invoke(cli.main, ["gate", "satisfiers"])
    assert r.exit_code == 0, r.output
    assert "BEAD (1)" in r.output
    assert "EVENT (1)" in r.output
    assert "UNCLASSIFIED (1)" in r.output
    assert "1 bead" in r.output and "1 event" in r.output and "1 unclassified" in r.output
    # Never claim an event can be checked. It is the one kind that never can be.
    assert "event never can be" in r.output


def test_gate_satisfiers_check_flags_the_open_gate_over_a_closed_bead(store, monkeypatch):
    gid = _gate(store, "the QR handout")
    monkeypatch.setattr(beadlink, "read_state", lambda b: _state(b))
    CliRunner().invoke(cli.main, ["gate", "link", str(gid), "sea-btz.6"])
    monkeypatch.setattr(
        beadlink, "read_state", lambda b: _state(b, "closed", date.today() - timedelta(days=62))
    )
    r = CliRunner().invoke(cli.main, ["gate", "satisfiers", "--check"])
    assert r.exit_code == 0, r.output
    assert "DRIFT: gate is open" in r.output
    assert "62d ago" in r.output


def test_gate_satisfiers_has_a_bucket_for_every_kind_classify_can_return(store, monkeypatch):
    """A kind added to the model used to fall out of this report with a KeyError.

    The buckets are now keyed off classify()'s own vocabulary, so the report and
    the model cannot disagree about what kinds exist.
    """
    from strategy_store.models import Position

    with store.session() as s:
        p = Position(title="a claim", statement="", status="resolved", scope="fleet",
                     source_system="manual", source_ref="test:p", decided_on=date(2026, 8, 30))
        s.add(p)
        s.commit()
        pid = p.id
    a = _gate(store, "position-satisfiable", ref="p1")
    b = _gate(store, "event-satisfiable", ref="p2")
    run = CliRunner()
    run.invoke(cli.main, ["gate", "link", str(a), "--position", str(pid), "--when", "decided"])
    run.invoke(cli.main, ["gate", "link", str(b), "--event", "the room happens"])
    r = run.invoke(cli.main, ["gate", "satisfiers"])
    assert r.exit_code == 0, r.output
    assert "POSITION (1)" in r.output
    assert f"pos:   #{pid} decided" in r.output
    assert "1 position" in r.output
