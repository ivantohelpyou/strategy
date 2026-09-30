"""The admin surface: it renders, it stays read-only, and its coverage map
cannot silently fall behind the CLI.

Assertions are on behaviour and on data the page must carry (a position's id,
a gate's undated-ness), not on markup strings — the styling is expected to
change and must not break the suite.
"""

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from strategy_store import db
from strategy_store.models import Evidence, Forcing, Position
from strategy_store.web import commands as cmdmap
from strategy_store.web import create_app


@pytest.fixture
def seeded(store):
    with db.session() as s:
        held = Position(
            title="Sell to teams, not individuals",
            statement="the long form",
            element_key="ptw.box2",
            status="resolved",
            scope="fleet",
            decided_on=date(2026, 1, 5),
            venture="acme",
            source_system="manual",
            source_ref="t-held",
        )
        drafted = Position(
            title="Maybe raise the price",
            element_key="ptw.box3",
            status="holding",
            scope="fleet",
            source_system="manual",
            source_ref="t-drafted",
        )
        s.add_all([held, drafted])
        s.flush()
        s.add(Evidence(position_id=held.id, source="room", scope="fleet",
                       observed_on=date(2026, 2, 1), summary="two teams bought"))
        s.add(Forcing(title="Dated gate", condition="c", gates="a decision",
                      due_on=date.today() + timedelta(days=3), source_ref="g-dated"))
        s.add(Forcing(title="Undated gate", condition="c", gates="another decision",
                      source_ref="g-undated"))
        s.commit()
        ids = (held.id, drafted.id)
    return ids


@pytest.fixture
def client(seeded):
    return TestClient(create_app())


PAGES = [
    "/triage",
    "/questions",
    "/", "/positions", "/pending", "/gates", "/findings", "/matrix", "/drift",
    "/metrics", "/drops", "/funnel", "/profile", "/report", "/store", "/commands",
]


@pytest.mark.parametrize("path", PAGES)
def test_every_page_renders(client, path):
    r = client.get(path)
    assert r.status_code == 200
    assert r.text.startswith("<!doctype html>")


def test_health_reports_the_store_it_is_on(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["store"].startswith("sqlite:")


def test_cascade_places_a_position_in_its_box(client, seeded):
    held, _ = seeded
    body = client.get("/").text
    assert "Where to play" in body and f"/positions/{held}" in body


def test_pending_shows_only_undated_positions(client, seeded):
    held, drafted = seeded
    body = client.get("/pending").text
    assert f"/positions/{drafted}" in body
    assert f'href="/positions/{held}"' not in body


def test_position_detail_carries_statement_and_evidence(client, seeded):
    held, _ = seeded
    body = client.get(f"/positions/{held}").text
    assert "the long form" in body and "two teams bought" in body


def test_missing_position_is_not_a_500(client):
    r = client.get("/positions/99999")
    assert r.status_code == 200 and "No position #99999" in r.text


def test_filters_narrow_the_position_list(client, seeded):
    held, drafted = seeded
    body = client.get("/positions?element=ptw.box3").text
    assert f"/positions/{drafted}" in body
    assert f'href="/positions/{held}"' not in body


def test_undated_gate_is_called_out_not_merely_listed(client):
    body = client.get("/gates").text
    assert "Undated gate" in body and "NO DATE" in body
    assert "carry no date" in body  # the finding, stated


def test_only_triage_writes(client):
    """Writing is confined to /triage. Every other route on the surface is a read,
    so browsing cannot change the store by accident."""
    for route in client.app.routes:
        methods = getattr(route, "methods", set()) or set()
        if methods - {"GET", "HEAD", "OPTIONS"}:
            assert route.path.startswith("/triage/"), f"{route.path} accepts {methods}"


def test_web_writes_go_through_core_rather_than_reimplementing_it(client, seeded):
    """The guarantee that replaced read-only (position #67).

    "A rejection needs a reason" exists in exactly one place — core.reject. A
    surface that built its own write path would not have it. Refusing here is
    therefore evidence that the route calls core, not merely that it validates."""
    _, drafted = seeded
    r = client.post(f"/triage/{drafted}/reject", data={"note": ""}, follow_redirects=True)
    assert r.status_code == 200
    assert "needs a reason" in r.text
    with db.session() as s:
        assert s.get(Position, drafted).status == "holding"


def test_a_cross_origin_form_post_is_refused(client, seeded):
    """The surface binds to loopback with no login. That is fine while it only
    reads; once it writes, any page in the same browser could POST a form at it."""
    _, drafted = seeded
    r = client.post(
        f"/triage/{drafted}/adopt",
        data={"note": "not me"},
        headers={"origin": "https://evil.example", "host": "testserver"},
        follow_redirects=True,
    )
    assert "cross-origin write refused" in r.text
    with db.session() as s:
        assert s.get(Position, drafted).decided_on is None


def test_adopting_from_the_surface_dates_it_and_moves_on(client, seeded):
    _, drafted = seeded
    r = client.post(f"/triage/{drafted}/adopt", data={"note": "yes"}, follow_redirects=True)
    assert r.status_code == 200
    with db.session() as s:
        p = s.get(Position, drafted)
        assert p.decided_on is not None
        assert any(e.source == "adopt" and e.summary == "yes" for e in p.evidence)


def test_asking_from_the_surface_blocks_without_deciding(client, seeded):
    from strategy_store import core

    _, drafted = seeded
    client.post(f"/triage/{drafted}/ask", data={"text": "what would it cost?"},
                follow_redirects=True)
    with db.session() as s:
        assert s.get(Position, drafted).decided_on is None
        assert [q.text for q in core.open_questions(s, drafted)] == ["what would it cost?"]


def test_the_card_shows_the_case_and_names_a_bare_one(client, seeded):
    from strategy_store import core

    _, drafted = seeded
    with db.session() as s:
        core.add_factor(s, drafted, kind="test", text="install it somewhere else")
        s.commit()
    body = client.get(f"/triage/{drafted}").text
    assert "Would settle it" in body and "install it somewhere else" in body
    assert "No case recorded either way" not in body


def test_every_cli_command_is_classified(store):
    """A command added to the CLI must be given a kind and a surface. Until it
    is, the coverage map labels it UNCLASSIFIED — this test is the reason that
    label never has to be noticed by hand."""
    missing = [c.name for c in cmdmap.COMMANDS if "UNCLASSIFIED" in c.what]
    assert not missing, f"unclassified CLI commands: {missing}"


def test_coverage_map_names_a_real_page_for_every_surfaced_command(store):
    paths = {getattr(r, "path", "") for r in create_app().routes}
    for c in cmdmap.COMMANDS:
        if c.surface:
            assert c.surface in paths, f"{c.name} points at {c.surface}, which is not a route"


def test_every_read_command_is_surfaced(store):
    """The point of the surface. A read command with no page is a gap, and a
    deliberate one has to be argued for here rather than drifting in."""
    gaps = [c.name for c in cmdmap.COMMANDS if not c.writes and not c.surface]
    assert not gaps, f"read commands with no page: {gaps}"


def test_add_refuses_an_over_long_source_ref_with_the_column_named(store):
    """A 62-character --source used to reach Postgres and come back as a
    hundred-line SQLAlchemy traceback naming no column (2026-08-28)."""
    from click.testing import CliRunner

    from strategy_store import cli

    r = CliRunner().invoke(
        cli.main,
        ["add", "--title", "t", "--scope", "fleet", "--source", "s" * 61],
    )
    assert r.exit_code != 0
    assert "--source is 61 characters" in r.output
    assert "positions.source_ref holds 60" in r.output


def test_add_still_accepts_a_source_ref_at_the_limit(store):
    from click.testing import CliRunner

    from strategy_store import cli

    r = CliRunner().invoke(
        cli.main,
        ["add", "--title", "t", "--scope", "fleet", "--source", "s" * 60],
    )
    assert r.exit_code == 0, r.output


def test_position_detail_renders_a_many_to_one_supersession(store):
    """The page read `supersedes` as a single position and `superseded_by` as a
    list. Both flipped when supersession became many-to-one, and /positions/50
    returned a 500 for every consolidated position."""
    from strategy_store import core, db
    from strategy_store.models import Position

    def _mk(title, ref):
        with db.session() as s:
            p = Position(title=title, element_key="ptw.box2", status="holding",
                         scope="fleet", source_system="manual", source_ref=ref)
            s.add(p)
            s.commit()
            return p.id

    a, b = _mk("Old A", "w-a"), _mk("Old B", "w-b")
    survivor = _mk("Consolidated", "w-s")
    with db.session() as s:
        core.supersede(s, survivor, a)
        core.supersede(s, survivor, b)
        s.commit()

    c = TestClient(create_app())
    r = c.get(f"/positions/{survivor}")
    assert r.status_code == 200, r.text[:400]
    assert "supersedes" in r.text
    r_old = c.get(f"/positions/{a}")
    assert r_old.status_code == 200
    assert "superseded by" in r_old.text
