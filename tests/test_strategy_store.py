"""Strategy store: prefix→status mapping, scope rule, idempotent upsert, export/import."""

import json
from datetime import date

import pytest
from click.testing import CliRunner
from sqlalchemy import select

from strategy_store import cli, db, seed
from strategy_store.models import Base, Position


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


def _rows():
    return [
        {
            "source_system": "vikunja",
            "source_ref": "#1",
            "title": "DECIDED: fee schedule",
            "statement": "x",
            "status": "resolved",
            "source_status": "DECIDED",
            "scope": "web-chat",
            "decided_on": date(2026, 8, 12),
            "venture": None,
        },
        {
            "source_system": "beads",
            "source_ref": "purr-1",
            "title": "Reposition",
            "statement": "y",
            "status": "holding",
            "source_status": "open",
            "scope": "single-rig",
            "decided_on": date(2026, 8, 16),
            "venture": "purr",
        },
    ]


@pytest.mark.parametrize(
    "title,status",
    [
        ("DECIDED: x", "resolved"),
        ("DONE: y", "resolved"),
        ("CONFLICT: z", "contested"),
        ("Unresolved: q", "contested"),
        ("WATCH: w", "holding"),
        ("FIX: f", "holding"),
        ("📐 STRATEGY v3", "holding"),
    ],
)
def test_prefix_status(title, status):
    assert seed._prefix_status(title)[0] == status


def test_scope_rule_refuses_narrower_without_evidence():
    wide = Position(scope="single-rig", title="a", source_system="beads", source_ref="b-1")
    narrow = Position(scope="web-chat", title="b", source_system="vikunja", source_ref="#2")
    ok, _ = seed.supersede_allowed(narrow, wide, evidence_note=None)
    assert ok is False
    ok, _ = seed.supersede_allowed(narrow, wide, evidence_note="read the receipt")
    assert ok is True
    ok, _ = seed.supersede_allowed(wide, narrow, evidence_note=None)
    assert ok is True


def test_upsert_is_idempotent_and_store_owns_status(store):
    with store.session() as s:
        c1 = seed.upsert(s, _rows())
        assert (c1["inserted"], c1["updated"]) == (2, 0)
    # A manual status edit must survive a re-seed; text refreshes; drift is reported not applied.
    with store.session() as s:
        p = s.scalar(select(Position).where(Position.source_ref == "purr-1"))
        p.status = "contested"
        s.commit()
    rows = _rows()
    rows[1]["title"] = "Reposition (edited)"
    rows[1]["source_status"] = "closed"
    rows[1]["status"] = "resolved"
    with store.session() as s:
        c2 = seed.upsert(s, rows)
        assert (c2["inserted"], c2["updated"]) == (0, 2)
        p = s.scalar(select(Position).where(Position.source_ref == "purr-1"))
        assert p.title == "Reposition (edited)"
        assert p.status == "contested"  # store-owned, untouched
        assert p.source_status == "closed"  # source-owned, refreshed
        assert c2["drift"] and c2["drift"][0][0] == "beads:purr-1"


def test_export_import_round_trip(store, tmp_path):
    with store.session() as s:
        seed.upsert(s, _rows())
        a = s.scalar(select(Position).where(Position.source_ref == "#1"))
        b = s.scalar(select(Position).where(Position.source_ref == "purr-1"))
        a.superseded_by_id = b.id
        b.element_key = "ptw.box2"
        s.commit()
    out = tmp_path / "positions.jsonl"
    r = CliRunner().invoke(cli.main, ["export", "--out", str(out)])
    assert r.exit_code == 0, r.output
    lines = [json.loads(ln) for ln in out.read_text().splitlines()]
    assert len(lines) == 2
    # wipe and restore into a fresh DB
    Base.metadata.drop_all(store.engine())
    Base.metadata.create_all(store.engine())
    r = CliRunner().invoke(cli.main, ["import", "--src", str(out)])
    assert r.exit_code == 0, r.output
    with store.session() as s:
        b = s.scalar(select(Position).where(Position.source_ref == "purr-1"))
        assert b.element_key == "ptw.box2"
        assert [q.source_ref for q in b.supersedes] == ["#1"]


def test_forcing_buckets_and_report_findings(store, tmp_path):
    from datetime import timedelta

    from strategy_store.models import Forcing
    from strategy_store.report import build_context, render

    with store.session() as s:
        seed.upsert(s, _rows())
        contested = s.scalar(select(Position).where(Position.source_ref == "purr-1"))
        contested.status = "contested"
        contested.element_key = "ptw.box2"
        s.add(
            Forcing(
                title="soon",
                condition="c",
                gates="g",
                source_system="manual",
                source_ref="f1",
                due_on=date.today() + timedelta(days=3),
            )
        )
        s.add(
            Forcing(
                title="missed",
                condition="c",
                gates="g",
                source_system="manual",
                source_ref="f2",
                due_on=date.today() - timedelta(days=3),
            )
        )
        s.add(
            Forcing(
                title="undated", condition="c", gates="g", source_system="manual", source_ref="f3"
            )
        )
        s.commit()
    ctx = build_context("ptw")
    assert [f["title"] for f in ctx["forcing"]["dated"]] == ["soon"]
    assert [f["title"] for f in ctx["forcing"]["missed"]] == ["missed"]
    assert [f["title"] for f in ctx["forcing"]["undated"]] == ["undated"]
    by_title = {f["title"]: f["rows"] for f in ctx["findings"]}
    assert len(by_title["Decisions not filed under any box"]) == 1  # the vikunja row
    assert "Box 4 — Capabilities" in by_title["Boxes with nothing decided"]
    assert any("purr-1" in r for r in by_title["Disputes with no date to settle them by"])
    assert by_title["Deadlines with no date"] == ["undated (portfolio)"]
    text = render("ptw")
    assert "## 2. Where to play" in text and "Bottom line" in text
    detail = render("ptw", detail=True)
    assert "in dispute" in detail
    assert "a printout, not the source" in text


def test_prime_detects_rig_and_never_raises(store, tmp_path, monkeypatch):
    from strategy_store import prime

    monkeypatch.setattr(prime, "GT_ROOT", tmp_path)
    (tmp_path / "purr" / "crew").mkdir(parents=True)
    (tmp_path / "mayor").mkdir()
    assert prime.detect_rig(tmp_path / "purr" / "crew") == "purr"
    assert prime.detect_rig(tmp_path / "mayor") is None
    assert prime.detect_rig(tmp_path.parent) is None
    # Empty store: renders a header and the footer, no exception.
    text = prime.render("purr")
    assert text.startswith("## Direction") and "never synced" in text
    # Reseed with a broken source must not raise — it reports and falls back.
    monkeypatch.setattr(prime, "db", store)
    from strategy_store import seed as seed_mod

    monkeypatch.setenv("STRATEGY_VIKUNJA_PROJECTS", "381:Goals")
    monkeypatch.setattr(
        seed_mod, "fetch_vikunja", lambda: (_ for _ in ()).throw(RuntimeError("no net"))
    )
    monkeypatch.setattr(seed_mod, "fetch_beads", lambda: [])
    note = prime.maybe_reseed(0)
    assert note.startswith("(reseed failed: RuntimeError")
    # With no projects configured the Vikunja half is skipped — and says so,
    # instead of silently reseeding beads only (sea-mii7.3).
    monkeypatch.delenv("STRATEGY_VIKUNJA_PROJECTS")
    note = prime.maybe_reseed(0)
    assert "VIKUNJA SKIPPED" in note
    r = CliRunner().invoke(cli.main, ["prime", "--rig", "purr"])
    assert r.exit_code == 0 and "## Direction" in r.output


def test_drops_ingest_base_hit_and_needs_stats(store, tmp_path):

    import openpyxl

    from strategy_store.drops import (
        needs_stats,
        parse_linkedin_export,
        scoreboard,
        upsert_from_export,
    )
    from strategy_store.models import Drop

    def export(name, url, posted, **metrics):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Post URL", url])
        ws.append(["Post Date", posted])
        for k, v in metrics.items():
            ws.append([k, v])
        p = tmp_path / name
        wb.save(p)
        return p

    hit = export(
        "SinglePostAnalytics_a.xlsx",
        "https://www.linkedin.com/posts/x_my-app-ugcPost-1-A",
        "8/10/2026",
        **{
            "Impressions": "500",
            "Link engagements": "6",
            "Followers gained from this post": "0",
            "Comments": "0",
        },
    )
    out = export(
        "SinglePostAnalytics_b.xlsx",
        "https://www.linkedin.com/posts/x_dud-ugcPost-2-B",
        "8/12/2026",
        **{
            "Impressions": "100",
            "Link engagements": "1",
            "Followers gained from this post": "0",
            "Comments": "0",
        },
    )
    d = parse_linkedin_export(hit)
    assert d["url"].endswith("-A") and d["posted_on"] == date(2026, 8, 10) and d["link_clicks"] == 6
    with store.session() as s:
        a, created = upsert_from_export(s, hit)
        b, _ = upsert_from_export(s, out)
        assert created and a.base_hit is True and b.base_hit is False
        # hand-logged drop with no stats yet
        s.add(
            Drop(url="https://www.linkedin.com/posts/x_c", posted_on=date(2026, 8, 12), title="c")
        )
        s.commit()
        drops = list(s.scalars(select(Drop)))
        today = date(2026, 8, 20)
        due = {d.url[-1]: why for d, why in needs_stats(drops, today)}
        assert "c" in due and "no stats" in due["c"]
        # a's stats file is dated today (mtime) → settled window fine, not due; nothing else due
        sb = scoreboard(drops, days=30, today=today)
        assert sb["on_base"] == 1 and sb["scored"] == 2 and sb["dropped"] == 3
        assert sb["last_hit"] == date(2026, 8, 10)
    r = CliRunner().invoke(cli.main, ["drop", "list", "--days", "365"])
    assert r.exit_code == 0, r.output


# ── adopt / pending ────────────────────────────────────────────────────────────
# "Drafted but not adopted" used to live in the statement PROSE ("PROPOSED, needs
# blessing"), where no report could see it. `decided_on IS NULL` makes it a fact.


def _draft(store, title, **kw):
    """A position with no decided_on — i.e. a draft nobody has taken as their own."""
    with store.session() as s:
        p = Position(title=title, scope="fleet", source_system="manual",
                     source_ref=title.lower().replace(" ", "-"), **kw)
        s.add(p)
        s.commit()
        return p.id


def test_pending_lists_only_undated_positions(store):
    a = _draft(store, "Draft A", element_key="ptw.box4")
    _draft(store, "Decided B", decided_on=date(2026, 1, 1))
    out = CliRunner().invoke(cli.main, ["pending"]).output
    assert "Draft A" in out and "Decided B" not in out
    assert "1 awaiting adoption" in out
    # --element narrows to one box
    assert "Draft A" in CliRunner().invoke(cli.main, ["pending", "--element", "ptw.box4"]).output
    assert "Draft A" not in CliRunner().invoke(cli.main, ["pending", "--element", "ptw.box1"]).output
    assert a


def test_adopt_stamps_a_date_and_leaves_an_evidence_trail(store):
    pid = _draft(store, "Draft C")
    r = CliRunner().invoke(cli.main, ["adopt", str(pid), "--on", "2026-08-23", "--note", "mine now"])
    assert r.exit_code == 0
    with store.session() as s:
        p = s.get(Position, pid)
        assert p.decided_on == date(2026, 8, 23)
        assert p.status == "holding"          # status is NOT moved unless asked
        ev = p.evidence
        assert len(ev) == 1 and ev[0].source == "adopt" and ev[0].summary == "mine now"
        assert ev[0].observed_on == date(2026, 8, 23)   # evidence dated with the choice, not today


def test_adopt_can_move_status_and_bless_is_an_alias(store):
    pid = _draft(store, "Draft D")
    r = CliRunner().invoke(cli.main, ["bless", str(pid), "--status", "resolved"])
    assert r.exit_code == 0 and "holding -> resolved" in r.output
    with store.session() as s:
        assert s.get(Position, pid).status == "resolved"


def test_adopt_refuses_an_already_decided_position_unless_redated(store):
    pid = _draft(store, "Draft E", decided_on=date(2026, 1, 1))
    r = CliRunner().invoke(cli.main, ["adopt", str(pid)])
    assert r.exit_code != 0 and "already decided on 2026-01-01" in r.output
    with store.session() as s:
        assert s.get(Position, pid).decided_on == date(2026, 1, 1)   # untouched
    r = CliRunner().invoke(cli.main, ["adopt", str(pid), "--on", "2026-08-23", "--redate"])
    assert r.exit_code == 0
    with store.session() as s:
        assert s.get(Position, pid).decided_on == date(2026, 8, 23)


def test_a_bad_id_adopts_nothing_at_all(store):
    """Every id is validated before ANY write — a typo in the second argument must
    not leave the first one adopted and the run half-applied."""
    good = _draft(store, "Draft F")
    r = CliRunner().invoke(cli.main, ["adopt", str(good), "999"])
    assert r.exit_code != 0 and "no position 999" in r.output
    with store.session() as s:
        p = s.get(Position, good)
        assert p.decided_on is None and p.evidence == []


def test_return_sends_a_position_back_to_the_pile(store):
    pid = _draft(store, "Draft G", decided_on=date(2026, 8, 17))
    assert "Draft G" not in CliRunner().invoke(cli.main, ["pending"]).output
    r = CliRunner().invoke(cli.main, ["return", str(pid), "--note", "needs a satisfied-state"])
    assert r.exit_code == 0 and "was dated 2026-08-17" in r.output
    with store.session() as s:
        p = s.get(Position, pid)
        assert p.decided_on is None
        assert p.status == "holding"        # returning is not retiring, and not contesting
        assert p.evidence[0].source == "return"
        assert p.evidence[0].summary == "needs a satisfied-state"
    assert "Draft G" in CliRunner().invoke(cli.main, ["pending"]).output


def test_return_refuses_something_already_pending(store):
    pid = _draft(store, "Draft H")
    r = CliRunner().invoke(cli.main, ["return", str(pid)])
    assert r.exit_code != 0 and "already pending" in r.output


def test_adopt_and_return_round_trip(store):
    pid = _draft(store, "Draft I")
    assert CliRunner().invoke(cli.main, ["adopt", str(pid)]).exit_code == 0
    assert CliRunner().invoke(cli.main, ["return", str(pid)]).exit_code == 0
    assert CliRunner().invoke(cli.main, ["adopt", str(pid)]).exit_code == 0
    with store.session() as s:
        # every step left a trail rather than overwriting the last one
        assert [e.source for e in s.get(Position, pid).evidence] == ["adopt", "return", "adopt"]
