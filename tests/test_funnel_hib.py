"""funnel_for reads Heads in Beds play events (event/hib/*) alongside purr's tables."""

from datetime import date, datetime

from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, create_engine, insert

from strategy_store import funnel


def _plane(tmp_path, monkeypatch):
    db = tmp_path / "pv.sqlite"
    url = f"sqlite:///{db}"
    monkeypatch.setenv("STRATEGY_ANALYTICS_URL", url)
    md = MetaData()
    t = Table(
        "page_views", md,
        Column("id", Integer, primary_key=True),
        Column("domain", String), Column("path", String), Column("ts", DateTime),
        Column("referrer_host", String), Column("section", String), Column("slug", String),
    )
    eng = create_engine(url, future=True)
    md.create_all(eng)
    return eng, t


def _full(r):
    return {"referrer_host": None, "section": None, "slug": None, **r}


def test_hib_full_trip_and_scores(tmp_path, monkeypatch):
    eng, t = _plane(tmp_path, monkeypatch)
    dom = "dispatch.conventioncityseattle.com"
    ts = datetime(2026, 8, 18, 12, 0)
    rows = [
        dict(domain=dom, path="/game/heads-in-beds", ts=ts, referrer_host="www.linkedin.com"),
        dict(domain=dom, path="/game/heads-in-beds", ts=ts, referrer_host=None),
        dict(domain=dom, path="event/hib/start", ts=ts, section="event"),
        dict(domain=dom, path="event/hib/start", ts=ts, section="event"),
        dict(domain=dom, path="event/hib/era2", ts=ts, section="event"),
        dict(domain=dom, path="event/hib/era3", ts=ts, section="event"),
        dict(domain=dom, path="event/hib/complete", ts=ts, section="event", slug="lit-79"),
        dict(domain=dom, path="event/hib/complete", ts=ts, section="event", slug="lit-79"),
        dict(domain=dom, path="event/hib/complete", ts=ts, section="event", slug="lit-64"),
        # another domain's rows must not leak in
        dict(domain="purr.ivantohelpyou.com", path="event/table/created", ts=ts, slug="x"),
    ]
    with eng.begin() as c:
        c.execute(insert(t), [_full(r) for r in rows])
    f = funnel.funnel_for("https://dispatch.conventioncityseattle.com/game/heads-in-beds", date(2026, 8, 18))
    assert f["domain"] == dom
    assert f["landing_views"] == 2 and f["landing_from_linkedin"] == 1
    assert f["hib"] == {"start": 2, "era2": 1, "era3": 1, "complete": 3,
                        "scores": {"lit-79": 2, "lit-64": 1}}
    assert f["tables_created"] == 0
    assert f["events"]["event/hib/complete"] == 3


def test_purr_keys_still_present(tmp_path, monkeypatch):
    eng, t = _plane(tmp_path, monkeypatch)
    ts = datetime(2026, 8, 17, 12, 0)
    with eng.begin() as c:
        c.execute(insert(t), [_full(r) for r in (
            dict(domain="purr.ivantohelpyou.com", path="/one-card-schmuck", ts=ts),
            dict(domain="purr.ivantohelpyou.com", path="event/table/created", ts=ts, slug="a"),
            dict(domain="purr.ivantohelpyou.com", path="event/table/joined", ts=ts, slug="a"),
        )])
    f = funnel.funnel_for("https://purr.ivantohelpyou.com/one-card-schmuck", date(2026, 8, 17))
    assert f["landing_views"] == 1 and f["tables_created"] == 1 and f["joined"] == 1
    assert f["hib"]["complete"] == 0 and f["hib"]["scores"] == {}
