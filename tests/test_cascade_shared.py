"""The layer-vs-vertical report (solutions-tx3.5).

Why it exists: cascades share positions rather than copying them, so what
several cascades rest on is discoverable rather than declared. A position three
brands hold is the horizontal platform; one exactly one brand holds is that
brand's vertical.
"""

from datetime import date

import pytest
from click.testing import CliRunner

from strategy_store import cli, core, db, queries
from strategy_store.models import Position


def _p(s, ref, *, element="ptw.box4", status="resolved") -> Position:
    p = Position(title=f"position {ref}", element_key=element, status=status,
                 scope="fleet", decided_on=date(2026, 8, 1),
                 source_system="manual", source_ref=ref)
    s.add(p)
    s.flush()
    return p


@pytest.fixture
def portfolio(store):
    """Three actual brands, one candidate, and a position at each sharing level."""
    with db.session() as s:
        mcd = core.create_cascade(s, "mcd", standing="actual", brand="MCD")
        ccs = core.create_cascade(s, "ccs", standing="actual", brand="CCS")
        i2hu = core.create_cascade(s, "i2hu", standing="actual", brand="I2HU")
        cand = core.create_cascade(s, "trial", question="what if?")

        plat = _p(s, "platform")
        two = _p(s, "two")
        one = _p(s, "one")
        spec = _p(s, "spec")
        retired = _p(s, "retired", status="retired")
        boxless = _p(s, "boxless")
        boxless.element_key = None
        missed = _p(s, "missed")

        for c in (mcd, ccs, i2hu):
            core.add_member(s, c, plat)
        core.add_member(s, mcd, two)
        core.add_member(s, ccs, two)
        core.add_member(s, mcd, one)
        core.add_member(s, cand, spec)
        s.commit()
        return {k: v.id for k, v in
                dict(plat=plat, two=two, one=one, spec=spec,
                     retired=retired, boxless=boxless, missed=missed).items()}


def test_spread_counts_each_sharing_level(portfolio):
    with db.session() as s:
        d = queries.shared_positions(s)
    assert d["distribution"] == {3: 1, 2: 1, 1: 1}


def test_a_candidate_does_not_raise_a_positions_count(portfolio):
    """A hypothesis 'sharing' a position is not evidence of a platform."""
    with db.session() as s:
        core.add_member(s, core.cascade(s, "trial"), s.get(Position, portfolio["two"]))
        s.commit()
        d = queries.shared_positions(s)
    row = next(r for r in d["rows"] if r["position"].id == portfolio["two"])
    assert row["n"] == 2, "the candidate must not count"
    assert "trial" not in row["cascades"]


def test_include_candidates_counts_them(portfolio):
    with db.session() as s:
        d = queries.shared_positions(s, include_candidates=True)
    assert any(r["position"].id == portfolio["spec"] for r in d["rows"])


def test_unassigned_is_split_by_reason(portfolio):
    """The three are different findings and must not be summed into one number."""
    with db.session() as s:
        d = queries.shared_positions(s)
    u = d["unassigned"]
    assert u["retired"] == 1
    assert u["no_box"] == 1
    assert u["candidate_only"] == 1, (
        "a position in a candidate cascade is deliberately speculative, not missed"
    )
    assert [p.id for p in u["missed"]] == [portfolio["missed"]], (
        "a current, boxed position in no cascade is the only bucket that means "
        "somebody missed one"
    )


def test_element_filter_narrows_to_one_box(portfolio):
    with db.session() as s:
        d = queries.shared_positions(s, element="ptw.box2")
    assert d["rows"] == []


def test_cli_names_the_top_tier_the_platform(portfolio):
    out = CliRunner().invoke(cli.main, ["cascade", "shared"]).output
    assert "held by 3 — the platform" in out
    assert "held by 2 — shared" in out
    assert "held by 1 — the verticals" in out
    assert "position platform" in out


def test_cli_summarises_verticals_until_asked(portfolio):
    out = CliRunner().invoke(cli.main, ["cascade", "shared"]).output
    assert "position one" not in out
    full = CliRunner().invoke(cli.main, ["cascade", "shared", "--verticals"]).output
    assert "position one" in full


def test_cli_flags_a_missed_position(portfolio):
    out = CliRunner().invoke(cli.main, ["cascade", "shared"]).output
    assert "MISSED" in out and "position missed" in out


def test_the_page_renders_the_shared_report(portfolio):
    from fastapi.testclient import TestClient

    from strategy_store.web import create_app

    r = TestClient(create_app()).get("/cascades")
    assert r.status_code == 200
    assert "the platform" in r.text
    assert "position platform" in r.text
    assert "boxed and current" in r.text
