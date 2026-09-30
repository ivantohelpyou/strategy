"""The candidate-cascade gate (solutions-tx3.1).

The failure this prevents: `strategy prime` prints box1/box2 into the top of
every session through the gt prime hook. A where-to-play generated inside a
speculative cascade — "given today's capabilities, where ELSE could I play?" —
must not arrive in the next agent's context looking like a decided position.
"""

from datetime import date

from sqlalchemy import select
import pytest

from strategy_store import db, prime, queries
from strategy_store.models import Cascade, CascadePosition, Position


def _position(s, ref: str, *, element="ptw.box2", status="resolved") -> Position:
    p = Position(
        title=f"position {ref}",
        element_key=element,
        status=status,
        scope="fleet",
        decided_on=date(2026, 8, 1),
        source_system="manual",
        source_ref=ref,
    )
    s.add(p)
    s.flush()
    return p


def _cascade(s, key: str, standing: str) -> Cascade:
    c = Cascade(key=key, name=key.upper(), standing=standing, question="q?")
    s.add(c)
    s.flush()
    return c


@pytest.fixture
def three_states(store):
    """One position in each of the three membership states."""
    with db.session() as s:
        unassigned = _position(s, "unassigned")
        held = _position(s, "held")
        tried = _position(s, "tried")
        actual = _cascade(s, "mcd", "actual")
        cand = _cascade(s, "mcd-if-fleet-fixed", "candidate")
        s.add_all(
            [
                CascadePosition(cascade_id=actual.id, position_id=held.id,
                                element_key="ptw.box2", mode="fixed"),
                CascadePosition(cascade_id=cand.id, position_id=tried.id,
                                element_key="ptw.box2", mode="derived"),
            ]
        )
        s.commit()
        return {"unassigned": unassigned.id, "held": held.id, "tried": tried.id}


def test_speculative_ids_is_exactly_the_candidate_only_set(three_states):
    with db.session() as s:
        assert queries.speculative_ids(s) == {three_states["tried"]}


def test_a_position_in_both_an_actual_and_a_candidate_is_held(store):
    """Membership in a candidate does not taint a position held elsewhere."""
    with db.session() as s:
        p = _position(s, "both")
        actual = _cascade(s, "ccs", "actual")
        cand = _cascade(s, "ccs-reverse", "candidate")
        s.add_all(
            [
                CascadePosition(cascade_id=actual.id, position_id=p.id,
                                element_key="ptw.box2", mode="fixed"),
                CascadePosition(cascade_id=cand.id, position_id=p.id,
                                element_key="ptw.box3", mode="open"),
            ]
        )
        s.commit()
        assert queries.speculative_ids(s) == set()


def test_a_retired_actual_cascade_still_counts_as_held(store):
    """Retired is not the same as never-real; conflating them deletes history."""
    with db.session() as s:
        p = _position(s, "retired-home")
        c = _cascade(s, "old-brand", "actual")
        c.retired_on = date(2026, 1, 1)
        s.add(CascadePosition(cascade_id=c.id, position_id=p.id,
                              element_key="ptw.box2", mode="fixed"))
        s.commit()
        assert queries.speculative_ids(s) == set()


def test_list_hides_candidate_only_and_keeps_unassigned(three_states):
    with db.session() as s:
        rows, _ = queries.positions(s)
        ids = {p.id for p in rows}
    assert three_states["unassigned"] in ids, "a position in NO cascade must still show"
    assert three_states["held"] in ids
    assert three_states["tried"] not in ids


def test_list_all_shows_the_candidate_only_row(three_states):
    """--all is the deliberate escape hatch, as it already is for superseded rows."""
    with db.session() as s:
        rows, _ = queries.positions(s, show_all=True)
        assert three_states["tried"] in {p.id for p in rows}


def test_prime_does_not_print_a_candidate_only_position(three_states):
    out = prime.render(None)
    assert "position held" in out
    assert "position unassigned" in out
    assert "position tried" not in out


def test_prime_still_works_when_no_cascades_exist(store):
    """The pre-cascade store: every position unassigned, nothing hidden."""
    with db.session() as s:
        _position(s, "solo")
        s.commit()
    assert "position solo" in prime.render(None)


def test_current_positions_applies_both_filters(store):
    with db.session() as s:
        old = _position(s, "old")
        new = _position(s, "new")
        old.superseded_by_id = new.id
        tried = _position(s, "tried")
        cand = _cascade(s, "cand", "candidate")
        s.add(CascadePosition(cascade_id=cand.id, position_id=tried.id,
                              element_key="ptw.box2", mode="open"))
        s.commit()
        ids = {p.id for p in queries.current_positions(s)}
        assert ids == {new.id}


def test_the_list_COMMAND_hides_candidate_only_rows(three_states):
    """Regression: queries.positions was gated while `strategy list` kept its
    own SELECT and went on printing hypotheses (solutions-tx3.1)."""
    from click.testing import CliRunner

    from strategy_store import cli

    out = CliRunner().invoke(cli.main, ["list"]).output
    assert "position held" in out
    assert "position unassigned" in out
    assert "position tried" not in out

    all_out = CliRunner().invoke(cli.main, ["list", "--all"]).output
    assert "position tried" in all_out


def test_the_triage_queue_is_NOT_gated(store):
    """Deliberate: the gate keeps hypotheses out of DIRECTION, not out of the
    work queue. An open box in a candidate cascade is real work — it is meant to
    produce questions — and hiding it would make speculation unworkable."""
    with db.session() as s:
        p = _position(s, "spec", status="holding")
        p.decided_on = None
        c = _cascade(s, "trial", "candidate")
        s.add(CascadePosition(cascade_id=c.id, position_id=p.id,
                              element_key="ptw.box2", mode="open"))
        s.commit()
        assert p.id in {r.id for r in queries.triage_queue(s)}


def test_retired_positions_are_hidden_from_the_cascade(store):
    """A retired position is a decision NOT to hold something — real and worth
    keeping, but not part of the current cascade. Leaving them in meant a box cut
    to five still listed seven."""
    with db.session() as s:
        live = _position(s, "live")
        gone = _position(s, "gone")
        gone.status = "retired"
        s.commit()
        ids = {p.id for p in queries.current_positions(s)}
        assert ids == {live.id}


def test_retired_is_still_reachable_by_asking_for_it(store):
    with db.session() as s:
        gone = _position(s, "gone")
        gone.status = "retired"
        s.commit()
        rows, _ = queries.positions(s, status="retired")
        assert [p.id for p in rows] == [gone.id]
        rows_all, _ = queries.positions(s, show_all=True)
        assert gone.id in {p.id for p in rows_all}


# --------------------------------------------------- gate edit + reseed safety

def _seed_rows(resolved=None, gates="g", due=None):
    """One seed entry in load_seed()'s tuple shape (forcing_seed.FIELDS order)."""
    from datetime import date as _d
    return [("doc", "F.md#a", "seeded gate", "cond", due or _d(2026, 10, 9),
             gates, "purr", None, resolved)]


def test_reseed_keeps_a_hand_resolution_and_its_note(store):
    """The bug this guards: `gate resolve` writes to the store, the seed file
    knows nothing about it, and a plain overwrite reopened six closed gates and
    destroyed the notes explaining why they closed."""
    from datetime import date
    from strategy_store.forcing_seed import upsert_forcing
    from strategy_store.models import Forcing

    with store.session() as s:
        upsert_forcing(s, _seed_rows())
        row = s.scalar(select(Forcing))
        row.resolved_on = date(2026, 8, 30)
        row.gates = row.gates + "\n[resolved 2026-08-30] premise withdrawn"
        s.commit()

        upsert_forcing(s, _seed_rows())          # the file still says nothing
        row = s.scalar(select(Forcing))
        assert row.resolved_on == date(2026, 8, 30), "a reseed reopened a closed gate"
        assert "[resolved 2026-08-30] premise withdrawn" in row.gates
        assert row.gates.startswith("g"), "the file's own text should still win"


def test_reseed_still_refreshes_text_and_can_record_a_resolution(store):
    from datetime import date
    from strategy_store.forcing_seed import upsert_forcing
    from strategy_store.models import Forcing

    with store.session() as s:
        upsert_forcing(s, _seed_rows())
        upsert_forcing(s, _seed_rows(resolved=date(2026, 9, 1), gates="rewritten"))
        row = s.scalar(select(Forcing))
        assert row.gates == "rewritten" and row.resolved_on == date(2026, 9, 1)


def test_gate_edit_refuses_a_seeded_gate(store, tmp_path, monkeypatch):
    import yaml
    from click.testing import CliRunner
    from strategy_store import cli
    from strategy_store.forcing_seed import upsert_forcing

    seed_file = tmp_path / "forcing-seed.yaml"
    seed_file.write_text(yaml.safe_dump({"forcing": [
        {"source_system": "doc", "source_ref": "F.md#a", "title": "seeded gate",
         "condition": "cond", "gates": "g", "due_on": "2026-10-09"}]}))
    monkeypatch.setenv("STRATEGY_FORCING_SEED", str(seed_file))
    with store.session() as s:
        upsert_forcing(s)

    out = CliRunner().invoke(cli.main, ["gate", "edit", "1", "--title", "x"])
    assert out.exit_code != 0
    assert "seeded" in out.output and "strategy seed" in out.output


def test_gate_edit_needs_a_note_to_move_a_date_then_logs_it(store, monkeypatch):
    from datetime import date
    from click.testing import CliRunner
    from strategy_store.models import Forcing

    monkeypatch.setenv("STRATEGY_FORCING_SEED", "/nonexistent/seed.yaml")
    with store.session() as s:
        s.add(Forcing(title="t", condition="c", gates="holds the deck",
                      source_system="manual", source_ref="m1", due_on=date(2026, 10, 9)))
        s.commit()

    from strategy_store import cli
    run = CliRunner()
    bare = run.invoke(cli.main, ["gate", "edit", "1", "--due", "2026-10-10"])
    assert bare.exit_code != 0 and "needs a reason" in bare.output

    ok = run.invoke(cli.main, ["gate", "edit", "1", "--due", "2026-10-10",
                               "--note", "the session is a Saturday"])
    assert ok.exit_code == 0, ok.output
    with store.session() as s:
        row = s.get(Forcing, 1)
        assert row.due_on == date(2026, 10, 10)
        assert "[edited" in row.gates and "the session is a Saturday" in row.gates
        assert row.gates.startswith("holds the deck"), "the original text survived"


def test_gate_edit_refuses_a_resolved_gate_and_a_noop(store, monkeypatch):
    from datetime import date
    from click.testing import CliRunner
    from strategy_store import cli
    from strategy_store.models import Forcing

    monkeypatch.setenv("STRATEGY_FORCING_SEED", "/nonexistent/seed.yaml")
    with store.session() as s:
        s.add(Forcing(title="live", condition="c", gates="g",
                      source_system="manual", source_ref="m1"))
        s.add(Forcing(title="done", condition="c", gates="g", source_system="manual",
                      source_ref="m2", resolved_on=date(2026, 8, 30)))
        s.commit()

    run = CliRunner()
    closed = run.invoke(cli.main, ["gate", "edit", "2", "--title", "x"])
    assert closed.exit_code != 0 and "reopen" in closed.output

    noop = run.invoke(cli.main, ["gate", "edit", "1", "--title", "live"])
    assert noop.exit_code != 0 and "nothing to change" in noop.output
