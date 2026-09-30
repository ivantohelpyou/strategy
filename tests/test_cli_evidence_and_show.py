"""CLI wiring for `show --full` and the `evidence` group (st-knn).

`strategy show` used to cut every evidence summary at 100 characters and the
statement at 40 lines, and `strategy evidence` was write-only, so a decision
row longer than a sentence could not be read back through the CLI at all.
These tests go through Click, not core, because the bug was in the wiring.
"""

from datetime import date

import pytest
from click.testing import CliRunner
from sqlalchemy import select

from strategy_store import cli, core, db
from strategy_store.models import Evidence


@pytest.fixture
def store(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 's.db'}"
    monkeypatch.setenv("STRATEGY_DB_URL", url)
    db._engine = None
    db._Session = None
    db.init_db()
    yield db
    db._engine = None
    db._Session = None


LONG = "DECIDED, option C: " + "two customers, the ally and the convener. " * 6
assert len(LONG) > 100


def _position(statement="x"):
    r = CliRunner().invoke(
        cli.main,
        ["add", "--title", "t", "--statement", statement, "--scope", "fleet",
         "--source", "manual:test", "--status", "holding"],
    )
    assert r.exit_code == 0, r.output
    return int(r.output.split()[1])


def test_evidence_add_writes_every_option(store):
    pid = _position()
    r = CliRunner().invoke(
        cli.main,
        ["evidence", "add", str(pid), "--source", "room", "--summary", LONG,
         "--scope", "fleet", "--on", "2026-09-25", "--stance", "opposes"],
    )
    assert r.exit_code == 0, r.output
    with db.session() as s:
        e = s.scalar(select(Evidence).where(Evidence.position_id == pid))
        assert (e.source, e.summary, e.scope, e.observed_on, e.stance) == (
            "room", LONG, "fleet", date(2026, 9, 25), "opposes")


def test_bare_evidence_is_the_group_and_names_its_subcommands(store):
    r = CliRunner().invoke(cli.main, ["evidence"])
    assert "add" in r.output and "list" in r.output


def test_evidence_list_prints_rows_in_full_and_filters_on_date(store):
    pid = _position()
    with db.session() as s:
        core.add_evidence(s, pid, source="a", summary=LONG,
                          observed_on=date(2026, 9, 25), stance="supports")
        core.add_evidence(s, pid, source="b", summary="other day",
                          observed_on=date(2026, 9, 26))
        core.add_evidence(s, pid, source="c", summary="undated row")
        s.commit()
    r = CliRunner().invoke(cli.main, ["evidence", "list", str(pid)])
    assert r.exit_code == 0, r.output
    assert LONG in r.output and "other day" in r.output and "undated row" in r.output
    assert "stance: supports" in r.output
    r = CliRunner().invoke(cli.main, ["evidence", "list", str(pid), "--on", "2026-09-25"])
    assert r.exit_code == 0, r.output
    assert LONG in r.output
    assert "other day" not in r.output and "undated row" not in r.output


def test_show_truncates_by_default_and_full_prints_everything(store):
    statement = "\n".join(f"line {i}" for i in range(45))
    pid = _position(statement)
    with db.session() as s:
        core.add_evidence(s, pid, source="a", summary=LONG)
        s.commit()
    brief = CliRunner().invoke(cli.main, ["show", str(pid)])
    assert brief.exit_code == 0, brief.output
    assert LONG not in brief.output and LONG[:100] in brief.output
    assert "line 44" not in brief.output
    full = CliRunner().invoke(cli.main, ["show", str(pid), "--full"])
    assert full.exit_code == 0, full.output
    assert LONG in full.output and "line 44" in full.output
