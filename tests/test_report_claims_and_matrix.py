"""sea-mii7.1 — the two acceptance items the first report cut left open:

* one question answered in three systems must render as ONE contested claim
  with the other sources attached as evidence, not as three positions that read
  like three separate arguments;
* the tests-x-ventures matrix, where a cell nobody has answered stays blank and
  is never defaulted to a pass.
"""

from datetime import date

from click.testing import CliRunner
from sqlalchemy import select

from strategy_store import cli, report
from strategy_store.frameworks import FRAMEWORKS
from strategy_store.models import Assessment, Position

PTW = FRAMEWORKS["ptw"]


def _p(store, **kw):
    kw.setdefault("element_key", "ptw.box2")
    kw.setdefault("scope", "web-chat")
    kw.setdefault("status", "holding")
    kw.setdefault("source_system", "manual")
    with store.session() as s:
        p = Position(**kw)
        s.add(p)
        s.commit()
        return p.id


# ----------------------------------------------------------------- claims


def test_a_claim_gathers_rows_and_leaves_the_rest_alone(store):
    a = _p(store, title="speakers are the market", source_ref="a", claim_key="speakers")
    b = _p(store, title="speakers are a channel", source_ref="b", claim_key="speakers")
    c = _p(store, title="unrelated", source_ref="c")
    with store.session() as s:
        rows = sorted(s.scalars(select(Position)), key=lambda p: p.id)
    groups = report.claim_groups(rows)
    assert [[p.id for p in g] for g in groups] == [[a, b], [c]]


def test_one_question_two_answers_is_one_contested_claim(store):
    """Two rows that disagree are one dispute, whatever each row's own status
    says — the report must not present them as two settled positions."""
    lead = _p(
        store, title="Speakers: don't chase, don't hide", source_ref="fleet-row",
        scope="fleet", status="resolved", decided_on=date(2026, 8, 17), claim_key="speakers",
    )
    other = _p(
        store, title="Reposition: the competitive set is the speaker stack",
        source_ref="purr-u8s.54", source_system="beads", scope="single-rig",
        status="holding", decided_on=date(2026, 8, 16), claim_key="speakers",
    )
    ctx = report.build_context("ptw")
    box2 = next(e for e in ctx["elements"] if e["key"] == "ptw.box2")
    assert len(box2["rows"]) == 1, "two sources, one question, one rendered row"
    row = box2["rows"][0]
    assert row["id"] == lead, "the widest-scope, latest row speaks for the claim"
    assert row["status"] == "contested"
    assert row["sources_n"] == 2
    assert "beads:purr-u8s.54" in row["also_refs"]
    assert any(f"#{other}" in e["source"] for e in row["evidence"]), (
        "the other source becomes evidence under the one claim"
    )


def test_a_gathered_claim_stops_being_reported_as_an_unmerged_conflict(store):
    """The 'same question answered in two places' finding asks you to merge the
    rows. Once merged it must stop asking."""
    kw = dict(status="contested", decided_on=date(2026, 8, 16))
    _p(store, title="one way", source_ref="#1", source_system="vikunja", **kw)
    _p(store, title="other way", source_ref="b-1", source_system="beads", **kw)
    finding = _cross_finding(report.build_context("ptw"))
    assert len(finding["rows"]) == 1, "unmerged: the report asks for a merge"

    with store.session() as s:
        for p in s.scalars(select(Position)):
            p.claim_key = "same-question"
        s.commit()
    finding = _cross_finding(report.build_context("ptw"))
    assert finding["rows"] == [], "merged: the report stops asking"


def _cross_finding(ctx):
    return next(f for f in ctx["findings"] if f["title"].startswith("Same question"))


def test_claim_command_groups_ungroups_and_refuses_a_claim_of_one(store):
    a = _p(store, title="a", source_ref="a")
    b = _p(store, title="b", source_ref="b")
    run = CliRunner().invoke(cli.main, ["claim", str(a), str(b), "--key", "k"])
    assert run.exit_code == 0 and "claim 'k'" in run.output
    with store.session() as s:
        assert {p.claim_key for p in s.scalars(select(Position))} == {"k"}

    assert CliRunner().invoke(cli.main, ["claim", str(a)]).exit_code != 0

    assert CliRunner().invoke(cli.main, ["claim", str(a), str(b), "--clear"]).exit_code == 0
    with store.session() as s:
        assert {p.claim_key for p in s.scalars(select(Position))} == {None}


# ----------------------------------------------------------------- matrix


def test_an_unanswered_cell_is_blank_and_never_a_pass(store):
    _p(store, title="x", source_ref="x", venture="purr")
    _p(store, title="y", source_ref="y", venture="sea")
    m = report.build_context("ptw")["matrix"]
    assert m["ventures"] == ["purr", "sea"]
    assert m["total"] == len(PTW["tests"]) * 2
    assert m["answered"] == 0
    marks = {c["mark"] for r in m["rows"] for c in r["cells"]}
    assert marks == {""}, "an unanswered question has no opinion in it"


def test_answers_land_in_the_grid_with_their_note(store):
    _p(store, title="x", source_ref="x", venture="purr")
    runner = CliRunner()
    assert runner.invoke(
        cli.main, ["assess", "purr", "t6", "--verdict", "fail", "--note", "needs Ivan in the room"]
    ).exit_code == 0
    m = report.build_context("ptw")["matrix"]
    t6 = next(r for r in m["rows"] if r["key"] == "t6")
    assert t6["cells"][0]["verdict"] == "fail" and t6["cells"][0]["mark"] == "✗"
    assert m["answered"] == 1
    assert any(n["note"] == "needs Ivan in the room" for n in m["notes"])
    # every other cell is still blank
    assert sum(1 for r in m["rows"] for c in r["cells"] if c["mark"]) == 1


def test_reassessing_a_cell_replaces_it_and_clear_returns_it_to_blank(store):
    _p(store, title="x", source_ref="x", venture="purr")
    runner = CliRunner()
    runner.invoke(cli.main, ["assess", "purr", "t1", "--verdict", "pass"])
    runner.invoke(cli.main, ["assess", "purr", "t1", "--verdict", "irrelevant", "--note", "n/a"])
    with store.session() as s:
        rows = list(s.scalars(select(Assessment)))
    assert len(rows) == 1 and rows[0].verdict == "irrelevant", "one cell, not two"

    assert runner.invoke(cli.main, ["assess", "purr", "t1", "--clear"]).exit_code == 0
    assert report.build_context("ptw")["matrix"]["answered"] == 0


def test_an_unknown_test_key_is_refused_rather_than_stored(store):
    _p(store, title="x", source_ref="x", venture="purr")
    r = CliRunner().invoke(cli.main, ["assess", "purr", "t99", "--verdict", "pass"])
    assert r.exit_code != 0 and "t1" in r.output
    with store.session() as s:
        assert list(s.scalars(select(Assessment))) == []


def test_the_matrix_renders_one_table_row_per_test(store):
    _p(store, title="x", source_ref="x", venture="purr")
    _p(store, title="y", source_ref="y", venture="sea")
    lines = report.build_context("ptw")["matrix"]["table"].splitlines()
    assert lines[0] == "| | purr | sea |"
    assert lines[1] == "|---|---|---|"
    assert len(lines) == 2 + len(PTW["tests"])


def test_the_password_never_reaches_the_report_header(store, monkeypatch):
    monkeypatch.setenv("STRATEGY_DB_URL", "postgresql://u:hunter2@example.invalid/db")
    scope = report.build_context("ptw")["scope"]
    assert "hunter2" not in scope["db"] and "***" in scope["db"]
