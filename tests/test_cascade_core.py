"""Cascade creation, membership and promotion (solutions-tx3.2)."""

from datetime import date

import pytest
from click.testing import CliRunner
from sqlalchemy import func, select

from strategy_store import cli, core, db
from strategy_store.models import Cascade, CascadePosition, Position


def _position(s, ref, *, element="ptw.box4") -> Position:
    p = Position(title=f"position {ref}", element_key=element, status="resolved",
                 scope="fleet", decided_on=date(2026, 8, 1),
                 source_system="manual", source_ref=ref)
    s.add(p)
    s.flush()
    return p


@pytest.fixture
def run(store):
    return CliRunner().invoke


# ----------------------------------------------------------------- creation


def test_a_candidate_without_a_question_is_refused(store):
    with db.session() as s:
        with pytest.raises(core.InvariantError, match="point of view"):
            core.create_cascade(s, "mcd-what-if")


def test_an_actual_needs_no_question(store):
    with db.session() as s:
        c = core.create_cascade(s, "mcd", standing="actual", brand="MCD")
        assert c.standing == "actual"


def test_new_cascades_default_to_candidate(store):
    """The safe default is the one that cannot leak into prime."""
    with db.session() as s:
        c = core.create_cascade(s, "trial", question="what if?")
        assert c.standing == "candidate"


def test_duplicate_key_is_refused(store):
    with db.session() as s:
        core.create_cascade(s, "mcd", standing="actual")
        s.commit()
        with pytest.raises(core.InvariantError, match="already exists"):
            core.create_cascade(s, "mcd", standing="actual")


def test_a_bad_key_is_refused_with_a_sentence(store):
    with db.session() as s:
        with pytest.raises(core.InvariantError, match="slug"):
            core.create_cascade(s, "not a key!", standing="actual")


# --------------------------------------------------------------- membership


def test_membership_defaults_to_the_positions_own_box(store):
    with db.session() as s:
        c = core.create_cascade(s, "mcd", standing="actual")
        p = _position(s, "cap", element="ptw.box4")
        m = core.add_member(s, c, p)
        assert m.element_key == "ptw.box4"
        assert m.mode == "fixed"


def test_a_position_can_be_slotted_into_a_different_box(store):
    """Rereading a capability as a where-to-play is a real move; record it."""
    with db.session() as s:
        c = core.create_cascade(s, "ccs", standing="actual")
        p = _position(s, "reframed", element="ptw.box4")
        m = core.add_member(s, c, p, element_key="ptw.box2")
        assert m.element_key == "ptw.box2"
        assert p.element_key == "ptw.box4", "the position itself must not move"


def test_a_boxless_position_needs_an_explicit_element(store):
    with db.session() as s:
        c = core.create_cascade(s, "mcd", standing="actual")
        p = _position(s, "orphan")
        p.element_key = None
        with pytest.raises(core.InvariantError, match="no framework box"):
            core.add_member(s, c, p)


def test_double_add_is_refused(store):
    with db.session() as s:
        c = core.create_cascade(s, "mcd", standing="actual")
        p = _position(s, "dup")
        core.add_member(s, c, p)
        with pytest.raises(core.InvariantError, match="already in mcd"):
            core.add_member(s, c, p)


def test_removing_a_member_leaves_the_position_alone(store):
    with db.session() as s:
        c = core.create_cascade(s, "mcd", standing="actual")
        p = _position(s, "keep")
        core.add_member(s, c, p)
        s.commit()
        core.remove_member(s, c, p)
        s.commit()
        assert s.get(Position, p.id) is not None
        assert s.scalar(select(func.count()).select_from(CascadePosition)) == 0


def test_add_is_all_or_nothing_on_a_bad_id(run):
    """A typo in the third id must not leave the first two assigned."""
    with db.session() as s:
        core.create_cascade(s, "mcd", standing="actual")
        a, b = _position(s, "a"), _position(s, "b")
        s.commit()
        ids = [a.id, b.id]
    res = run(cli.main, ["cascade", "add", "mcd", str(ids[0]), str(ids[1]), "9999"])
    assert res.exit_code != 0
    assert "no position 9999" in res.output
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(CascadePosition)) == 0


# ---------------------------------------------------------------- promotion


def test_promotion_makes_a_candidate_visible(store):
    with db.session() as s:
        c = core.create_cascade(s, "trial", question="what if the floor is fixed?")
        was = core.set_standing(s, c, "actual")
        assert (was, c.standing) == ("candidate", "actual")


def test_a_questionless_fork_cannot_be_promoted(store):
    with db.session() as s:
        parent = core.create_cascade(s, "mcd", standing="actual")
        s.commit()
        child = Cascade(key="mcd-copy", name="c", standing="candidate",
                        question="", forked_from_id=parent.id)
        s.add(child)
        s.flush()
        with pytest.raises(core.InvariantError, match="say what it answers"):
            core.set_standing(s, child, "actual")


# ---------------------------------------------------------------------- CLI


def test_show_prints_empty_boxes_as_findings(run):
    with db.session() as s:
        c = core.create_cascade(s, "mcd", standing="actual", brand="MCD")
        core.add_member(s, c, _position(s, "cap", element="ptw.box4"))
        s.commit()
    out = run(cli.main, ["cascade", "show", "mcd"]).output
    assert "ptw.box4" in out and "position cap" in out
    assert "— empty —" in out, "a missing how-to-win is the finding, not an omission"
    assert "ptw.box3" in out


def test_show_flags_a_candidate_as_invisible(run):
    with db.session() as s:
        core.create_cascade(s, "trial", question="what if?")
        s.commit()
    out = run(cli.main, ["cascade", "show", "trial"]).output
    assert "not shown in prime or report" in out


def test_list_puts_actuals_first_and_counts_positions(run):
    with db.session() as s:
        a = core.create_cascade(s, "mcd", standing="actual", brand="MCD")
        core.create_cascade(s, "zz-trial", question="what if?")
        core.add_member(s, a, _position(s, "x"))
        s.commit()
    lines = [ln for ln in run(cli.main, ["cascade", "list"]).output.splitlines() if ln.strip()]
    assert lines[0].startswith("●") and "mcd" in lines[0]
    assert "1 positions" in lines[0]


def test_unknown_cascade_is_a_message_not_a_traceback(run):
    res = run(cli.main, ["cascade", "show", "nope"])
    assert res.exit_code != 0
    assert "no cascade 'nope'" in res.output


# ------------------------------------------------------------- web surface


def test_cascade_pages_render(store):
    """The read commands' page. A read command with no surface is a gap."""
    from fastapi.testclient import TestClient

    from strategy_store.web import create_app

    with db.session() as s:
        a = core.create_cascade(s, "mcd", name="MCD", standing="actual", brand="MCD")
        core.add_member(s, a, _position(s, "cap", element="ptw.box4"))
        core.create_cascade(s, "mcd-trial", question="given the fleet, where else?")
        s.commit()

    client = TestClient(create_app())
    idx = client.get("/cascades")
    assert idx.status_code == 200
    assert "mcd" in idx.text and "not in prime" in idx.text

    one = client.get("/cascades/mcd")
    assert one.status_code == 200
    assert "position cap" in one.text
    assert "empty —" in one.text, "an empty box is rendered, not dropped"

    cand = client.get("/cascades/mcd-trial")
    assert "hypotheses" in cand.text


def test_unknown_cascade_page_is_a_message(store):
    from fastapi.testclient import TestClient

    from strategy_store.web import create_app

    r = TestClient(create_app()).get("/cascades/nope")
    assert r.status_code == 200
    assert "no cascade" in r.text and "nope" in r.text
