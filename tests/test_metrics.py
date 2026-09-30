"""Metrics: goals with gates, readings as a time series, Buttondown pull, export round trip."""

from datetime import date

import pytest
from click.testing import CliRunner
from sqlalchemy import select

from strategy_store import cli, db, metrics
from strategy_store.models import Metric


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("STRATEGY_DB_URL", f"sqlite:///{tmp_path / 's.db'}")
    db._engine = None
    db._Session = None
    db.init_db()
    yield db
    db._engine = None
    db._Session = None


def test_add_read_list_and_gates(store):
    r = CliRunner()
    res = r.invoke(cli.main, ["metric", "add", "--name", "li-newsletter-subs", "--beat", "ivantohelpyou-linkedin",
                              "--gates", "300,600,1000", "--due", "2026-12-31"])
    assert res.exit_code == 0, res.output
    assert r.invoke(cli.main, ["metric", "read", "li-newsletter-subs", "292", "--on", "2026-06-23"]).exit_code == 0
    assert r.invoke(cli.main, ["metric", "read", "li-newsletter-subs", "299", "--on", "2026-08-19"]).exit_code == 0
    # same day again = correction, not a second row
    res = r.invoke(cli.main, ["metric", "read", "li-newsletter-subs", "299", "--on", "2026-08-19"])
    assert "upd" in res.output
    with db.session() as s:
        m = s.scalar(select(Metric))
        assert [x.value for x in m.readings] == [292, 299]
        assert m.next_gate() == 300 and m.latest.value == 299
        assert metrics.delta(m, 7) == (7, date(2026, 6, 23))  # sparse: baseline is the only earlier reading
        lines = metrics.goal_lines([m])
    assert lines == ["Goal li-newsletter-subs: 299 → 300 → 600 → 1000 by 2026-12-31 · +7 since 2026-06-23 (as of 2026-08-19)"]
    res = r.invoke(cli.main, ["metric", "list"])
    assert "li-newsletter-subs" in res.output and "→ 300" in res.output


def test_delta_uses_reading_on_or_before_window(store):
    r = CliRunner()
    r.invoke(cli.main, ["metric", "add", "--name", "bd-dispatch", "--gates", "50"])
    for d, v in [("2026-08-01", 15), ("2026-08-10", 18), ("2026-08-19", 20)]:
        r.invoke(cli.main, ["metric", "read", "bd-dispatch", str(v), "--on", d])
    with db.session() as s:
        m = s.scalar(select(Metric))
        assert metrics.delta(m, 7) == (2, date(2026, 8, 10))   # on/before Aug 12
        assert metrics.delta(m, 30) == (5, date(2026, 8, 1))   # nothing on/before Jul 20 → earliest inside


def test_pull_reads_buttondown_with_token_from_env(store, monkeypatch):
    monkeypatch.setenv("BD_TEST_TOKEN", "secret")
    calls = []

    class Resp:
        def raise_for_status(self): pass
        def json(self): return {"count": 20, "results": []}

    class Client:
        def get(self, url, params=None, headers=None):
            calls.append((url, params["type"], headers["Authorization"]))
            return Resp()

    r = CliRunner()
    r.invoke(cli.main, ["metric", "add", "--name", "bd-dispatch", "--source", "buttondown-api", "--source-key", "BD_TEST_TOKEN"])
    r.invoke(cli.main, ["metric", "add", "--name", "bd-missing", "--source", "buttondown-api", "--source-key", "BD_NOPE"])
    with db.session() as s:
        out = metrics.pull(s, today=date(2026, 8, 19), client=Client())
    assert [(m.name, v) for m, v, _ in out] == [("bd-dispatch", 20), ("bd-missing", None)]
    assert calls == [("https://api.buttondown.com/v1/subscribers", "regular", "Token secret")]
    with db.session() as s:
        m = s.scalar(select(Metric).where(Metric.name == "bd-dispatch"))
        assert m.latest.value == 20 and m.latest.origin == "api" and m.latest.as_of == date(2026, 8, 19)


def test_article_export_records_email_sends(store, tmp_path):
    import openpyxl

    r = CliRunner()
    r.invoke(cli.main, ["metric", "add", "--name", "li-newsletter-delivered", "--source", "linkedin-export",
                        "--source-key", "email_sends"])
    wb = openpyxl.Workbook(); ws = wb.active
    for k, v in [("Post URL", "https://www.linkedin.com/posts/x_a-ugcPost-1-Z"), ("Post Date", "07/31/2026"),
                 ("Impressions", 365), ("Article views", 122), ("Email sends", 199), ("Comments", 0)]:
        ws.append([k, v])
    f = tmp_path / "SinglePostAnalytics_a.xlsx"; wb.save(f)
    res = r.invoke(cli.main, ["drop", "ingest", str(f)])
    assert res.exit_code == 0, res.output
    assert "li-newsletter-delivered" in res.output
    with db.session() as s:
        m = s.scalar(select(Metric))
        assert m.latest.value == 199 and m.latest.origin.startswith("export:")


def test_export_import_round_trip_includes_metrics(store, tmp_path):
    r = CliRunner()
    r.invoke(cli.main, ["metric", "add", "--name", "li-followers", "--beat", "ivantohelpyou-linkedin", "--gates", "2000"])
    r.invoke(cli.main, ["metric", "read", "li-followers", "1162", "--on", "2026-08-19", "--note", "profile header"])
    out = tmp_path / "bk" / "positions.jsonl"
    res = r.invoke(cli.main, ["export", "--out", str(out)])
    assert "exported 1 metrics" in res.output
    from strategy_store.models import Base
    Base.metadata.drop_all(db.engine()); db.init_db()
    res = r.invoke(cli.main, ["import", "--src", str(out)])
    assert "imported 1 metrics (1 new)" in res.output, res.output
    with db.session() as s:
        m = s.scalar(select(Metric))
        assert m.gates == "2000" and m.latest.value == 1162 and m.latest.note == "profile header"
    assert "imported 1 metrics (0 new)" in r.invoke(cli.main, ["import", "--src", str(out)]).output


def test_prime_prints_goal_line(store, tmp_path, monkeypatch):
    from strategy_store import prime
    monkeypatch.setattr(prime, "GT_ROOT", tmp_path)
    r = CliRunner()
    r.invoke(cli.main, ["metric", "add", "--name", "li-newsletter-subs", "--gates", "300,600,1000"])
    r.invoke(cli.main, ["metric", "read", "li-newsletter-subs", "299", "--on", "2026-08-19"])
    res = r.invoke(cli.main, ["prime"])
    assert res.exit_code == 0, res.output
    assert "Goal li-newsletter-subs: 299 → 300 → 600 → 1000" in res.output
