"""Drops: LinkedIn export ingest — URL-variant matching and re-export ordering."""

import os
from datetime import date

import pytest
from click.testing import CliRunner
from sqlalchemy import select

from strategy_store import cli, db
from strategy_store.drops import normalize_url
from strategy_store.models import Drop

URL = "https://www.linkedin.com/posts/ivantohelpyou_happy-birthday-share-7495553981142679552-CJBw"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("STRATEGY_DB_URL", f"sqlite:///{tmp_path / 's.db'}")
    db._engine = None
    db._Session = None
    db.init_db()
    yield db
    db._engine = None
    db._Session = None


def _export(path, impressions, follows=0, mtime=None, url=URL):
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    for k, v in [
        ("Post URL", url),
        ("Post Date", "08/18/2026"),
        ("Impressions", impressions),
        ("Members reached", impressions // 2),
        ("Reactions", 3),
        ("Comments", 1),
        ("Reposts", 0),
        ("Saves", 0),
        ("Link engagements", 1),
        ("Followers gained from this post", follows),
    ]:
        ws.append([k, v])
    wb.save(path)
    if mtime:
        os.utime(path, (mtime, mtime))
    return path


def test_normalize_url_strips_slash_query_fragment():
    assert normalize_url(URL + "/") == URL
    assert normalize_url(URL + "/?utm_source=share#x ") == URL
    assert normalize_url(URL) == URL


def test_hand_logged_trailing_slash_matches_export(store, tmp_path):
    r = CliRunner()
    res = r.invoke(cli.main, ["drop", "log", "--url", URL + "/", "--title", "Birthday drop",
                              "--app", "https://example.test/game"])
    assert res.exit_code == 0, res.output
    _export(tmp_path / "SinglePostAnalytics_x.xlsx", 329, follows=1)
    res = r.invoke(cli.main, ["drop", "ingest", "--downloads", str(tmp_path)])
    assert res.exit_code == 0, res.output
    with db.session() as s:
        drops = list(s.scalars(select(Drop)))
    assert len(drops) == 1, [d.url for d in drops]
    d = drops[0]
    assert d.title == "Birthday drop" and d.app_url == "https://example.test/game"
    assert d.impressions == 329 and d.followers_gained == 1 and d.base_hit is True


def test_older_reexport_never_overwrites_newer_stats(store, tmp_path):
    # `X (1).xlsx` sorts before `X.xlsx` by name but is the NEWER export.
    old = _export(tmp_path / "SinglePostAnalytics_p.xlsx", 295, mtime=1_700_000_000)
    new = _export(tmp_path / "SinglePostAnalytics_p (1).xlsx", 377, mtime=1_700_000_000 + 3 * 86400)
    r = CliRunner()
    res = r.invoke(cli.main, ["drop", "ingest", "--downloads", str(tmp_path)])
    assert res.exit_code == 0, res.output
    with db.session() as s:
        d = s.scalar(select(Drop))
        assert d.impressions == 377
        assert d.stats_as_of == date.fromtimestamp(new.stat().st_mtime)
    # Explicit file args in the wrong order: the old one is reported, not applied.
    res = r.invoke(cli.main, ["drop", "ingest", str(new), str(old)])
    assert res.exit_code == 0, res.output
    assert "old SinglePostAnalytics_p.xlsx" in res.output
    with db.session() as s:
        assert s.scalar(select(Drop)).impressions == 377


def test_own_comments_do_not_put_a_drop_on_base(store, tmp_path):
    r = CliRunner()
    # The export counts my own "link in the comments" comment as 1 comment.
    _export(tmp_path / "SinglePostAnalytics_c.xlsx", 26, follows=0)  # comments=1, clicks=1
    res = r.invoke(cli.main, ["drop", "ingest", "--downloads", str(tmp_path)])
    assert res.exit_code == 0, res.output
    with db.session() as s:
        assert s.scalar(select(Drop)).base_hit is True  # naive: 1 comment = on base
    res = r.invoke(cli.main, ["drop", "log", "--url", URL, "--own-comments", "1"])
    assert res.exit_code == 0, res.output
    assert "own-comments=1" in res.output
    with db.session() as s:
        d = s.scalar(select(Drop))
        assert d.others_comments == 0 and d.base_hit is False
        assert d.format == "post"  # re-log without --format must not reset it
    res = r.invoke(cli.main, ["drop", "list"])
    assert "cmt= 0(+1 mine)" in res.output and " out " in res.output


def test_own_repost_does_not_put_a_drop_on_base(store):
    r = CliRunner()
    page = "https://www.linkedin.com/posts/model-citizen-developer_the-workshop-activity-7495975919816192000-R9JM"
    res = r.invoke(cli.main, ["drop", "log", "--url", page, "--format", "comment-link"])
    assert res.exit_code == 0, res.output
    # Page posts have no export: stats come in by hand. 1 repost, 1 comment (mine).
    res = r.invoke(
        cli.main,
        ["drop", "stats", "1", "--impressions", "106", "--clicks", "2", "--comments", "1",
         "--reposts", "1", "--as-of", "2026-08-20", "--source", "MCD page admin UI"],
    )
    assert res.exit_code == 0, res.output
    assert "→ ON BASE" in res.output  # naive: the repost counts
    with db.session() as s:
        d = s.scalar(select(Drop))
        assert d.stats_as_of == date(2026, 8, 20) and d.others_comments == 0
        assert "stats by hand from MCD page admin UI: impressions=106" in d.notes
    # The repost was my own main-feed share.
    res = r.invoke(cli.main, ["drop", "log", "--url", page, "--own-reposts", "1"])
    assert res.exit_code == 0, res.output
    assert "own-comments=1 own-reposts=1" in res.output
    with db.session() as s:
        d = s.scalar(select(Drop))
        assert d.others_reposts == 0 and d.base_hit is False
        # re-log without --format must not reset the placement
        assert d.link_placement == "first-comment" and d.format == "post"
    res = r.invoke(cli.main, ["drop", "list"])
    assert "rp= 0(+1 mine)" in res.output and " out " in res.output
    # A stranger's repost on top of mine puts it on base.
    res = r.invoke(cli.main, ["drop", "stats", "1", "--reposts", "2"])
    assert res.exit_code == 0 and "→ ON BASE" in res.output, res.output


def test_drop_stats_requires_a_stat(store):
    r = CliRunner()
    r.invoke(cli.main, ["drop", "log", "--url", URL])
    res = r.invoke(cli.main, ["drop", "stats", "1"])
    assert res.exit_code != 0 and "at least one stat" in res.output


def test_first_comment_placement_defaults_to_one_own_comment(store):
    r = CliRunner()
    res = r.invoke(cli.main, ["drop", "log", "--url", URL, "--link", "first-comment"])
    assert res.exit_code == 0, res.output
    with db.session() as s:
        d = s.scalar(select(Drop))
        assert d.link_placement == "first-comment" and d.own_comments == 1


def test_legacy_comment_link_format_maps_to_placement_and_leaves_medium(store):
    """`--format comment-link` predates the medium/placement split. It must keep
    working, set the placement, and NOT claim the medium was 'comment-link'."""
    r = CliRunner()
    res = r.invoke(cli.main, ["drop", "log", "--url", URL, "--format", "comment-link"])
    assert res.exit_code == 0, res.output
    with db.session() as s:
        d = s.scalar(select(Drop))
        assert d.link_placement == "first-comment"
        assert d.format == "post"  # the medium is untouched, not overwritten
        assert d.own_comments == 1


def test_legacy_comment_link_flag_does_not_clobber_a_real_medium(store):
    """The regression that motivated the split: drop #13 was a native video with
    the link in the first comment, and the placement erased the video."""
    r = CliRunner()
    r.invoke(cli.main, ["drop", "log", "--url", URL, "--format", "video"])
    res = r.invoke(cli.main, ["drop", "log", "--url", URL, "--format", "comment-link"])
    assert res.exit_code == 0, res.output
    with db.session() as s:
        d = s.scalar(select(Drop))
        assert d.format == "video" and d.link_placement == "first-comment"


def test_init_db_adds_new_columns_to_an_old_table(tmp_path, monkeypatch):
    import sqlite3

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("create table drops (id integer primary key, posted_on date, url varchar(300) unique)")
    con.commit(); con.close()
    monkeypatch.setenv("STRATEGY_DB_URL", f"sqlite:///{path}")
    db._engine = None
    db._Session = None
    db.init_db()
    db.init_db()  # idempotent
    con = sqlite3.connect(path)
    cols = {r[1] for r in con.execute("pragma table_info(drops)")}
    assert {"own_comments", "own_reposts", "impressions", "stats_as_of", "notes"} <= cols
    db._engine = None
    db._Session = None


def test_ingest_archive_moves_files_and_keeps_both_reexports(store, tmp_path):
    dl, arc = tmp_path / "dl", tmp_path / "arc"
    dl.mkdir()
    old = _export(dl / "SinglePostAnalytics_p.xlsx", 295, mtime=1_700_000_000)
    new = _export(dl / "SinglePostAnalytics_p (1).xlsx", 377, mtime=1_700_000_000 + 3 * 86400)
    r = CliRunner()
    res = r.invoke(cli.main, ["drop", "ingest", "--downloads", str(dl), "--archive", str(arc)])
    assert res.exit_code == 0, res.output
    assert not old.exists() and not new.exists()
    assert sorted(p.name for p in arc.iterdir()) == [
        "SinglePostAnalytics_p (1).xlsx",
        "SinglePostAnalytics_p.xlsx",
    ]
    # Same filename exported again on a later day: archived under a dated name, not clobbered.
    again = _export(dl / "SinglePostAnalytics_p.xlsx", 400, mtime=1_700_000_000 + 9 * 86400)
    res = r.invoke(cli.main, ["drop", "ingest", "--downloads", str(dl), "--archive", str(arc)])
    assert res.exit_code == 0, res.output
    assert not again.exists() and len(list(arc.iterdir())) == 3
    assert any("[" in p.name for p in arc.iterdir())
    with db.session() as s:
        assert s.scalar(select(Drop)).impressions == 400


def test_export_import_round_trip_includes_drops(store, tmp_path):
    r = CliRunner()
    res = r.invoke(cli.main, ["drop", "log", "--url", URL, "--title", "T", "--own-comments", "1",
                              "--app", "https://example.test/x"])
    assert res.exit_code == 0, res.output
    _export(tmp_path / "SinglePostAnalytics_x.xlsx", 329, follows=1)
    assert r.invoke(cli.main, ["drop", "ingest", str(tmp_path / "SinglePostAnalytics_x.xlsx")]).exit_code == 0
    out = tmp_path / "bk" / "positions.jsonl"
    res = r.invoke(cli.main, ["export", "--out", str(out)])
    assert res.exit_code == 0, res.output
    assert "exported 1 drops" in res.output
    drops_file = out.parent / "drops.jsonl"
    assert drops_file.exists()
    # wipe, restore
    from strategy_store.models import Base

    Base.metadata.drop_all(db.engine())
    db.init_db()
    res = r.invoke(cli.main, ["import", "--src", str(out)])
    assert res.exit_code == 0, res.output
    assert "imported 1 drops (1 new)" in res.output
    with db.session() as s:
        d = s.scalar(select(Drop))
        assert d.own_reposts == 0
        assert (d.title, d.own_comments, d.app_url, d.impressions, d.followers_gained) == (
            "T", 1, "https://example.test/x", 329, 1)
        assert d.base_hit is True and d.stats_as_of == date.today()
    # idempotent
    res = r.invoke(cli.main, ["import", "--src", str(out)])
    assert "imported 1 drops (0 new)" in res.output


# --- one post, two LinkedIn ids (qrcards-pd6v) ------------------------------

ACTIVITY_URL = "https://www.linkedin.com/posts/ivantohelpyou_the-workshop-activity-7495979599755956224-QWEK"
SHARE_URL = "https://www.linkedin.com/posts/ivantohelpyou_survey-of-software-is-designed-as-a-hardware-share-7495979599009587200-gC7I"
EXPORT_NAME = "SinglePostAnalytics_Ivan Schneider_7495979599755956224.xlsx"


def test_ids_from_url_and_export_name():
    from strategy_store.drops import activity_id_from_export_name, ids_from_url

    assert ids_from_url(ACTIVITY_URL) == {"activity_id": "7495979599755956224", "share_id": None}
    assert ids_from_url(SHARE_URL) == {"activity_id": None, "share_id": "7495979599009587200"}
    ugc = "https://www.linkedin.com/posts/ivantohelpyou_the-cms-behind-ugcPost-7489021861989634050-XRDE"
    assert ids_from_url(ugc)["share_id"] == "7489021861989634050"
    feed = "https://www.linkedin.com/feed/update/urn:li:activity:7495979599755956224/"
    assert ids_from_url(feed) == {"activity_id": "7495979599755956224", "share_id": None}
    assert ids_from_url("https://example.test/not-linkedin") == {"activity_id": None, "share_id": None}
    assert activity_id_from_export_name(EXPORT_NAME) == "7495979599755956224"
    reexport = "SinglePostAnalytics_Ivan Schneider_7495979599755956224 (1).xlsx"
    assert activity_id_from_export_name(reexport) == "7495979599755956224"
    assert activity_id_from_export_name("model-citizen-developer_content_1787242850611.xls") is None


def test_activity_url_logged_then_share_url_export_is_one_drop(store, tmp_path):
    r = CliRunner()
    res = r.invoke(cli.main, ["drop", "log", "--url", ACTIVITY_URL, "--title", "The Workshop"])
    assert res.exit_code == 0, res.output
    # filename = activity id, Post URL cell = share id (LinkedIn's actual export shape)
    _export(tmp_path / EXPORT_NAME, 515, follows=2, url=SHARE_URL)
    res = r.invoke(cli.main, ["drop", "ingest", "--downloads", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert "upd #1" in res.output and "new #" not in res.output
    with db.session() as s:
        drops = list(s.scalars(select(Drop)))
    assert len(drops) == 1, [d.url for d in drops]
    d = drops[0]
    assert d.url == ACTIVITY_URL  # the hand-logged URL stays
    assert d.title == "The Workshop" and d.impressions == 515 and d.followers_gained == 2
    assert (d.activity_id, d.share_id) == ("7495979599755956224", "7495979599009587200")
    # Re-logging under the share-form URL is the same drop, and says so.
    res = r.invoke(cli.main, ["drop", "log", "--url", SHARE_URL, "--own-comments", "1"])
    assert res.exit_code == 0, res.output
    assert "share-form URL matches drop #1 by id" in res.output
    with db.session() as s:
        drops = list(s.scalars(select(Drop)))
    assert len(drops) == 1 and drops[0].own_comments == 1


def test_share_url_logged_then_export_fills_activity_id(store, tmp_path):
    r = CliRunner()
    r.invoke(cli.main, ["drop", "log", "--url", SHARE_URL])
    _export(tmp_path / EXPORT_NAME, 100, url=SHARE_URL)
    res = r.invoke(cli.main, ["drop", "ingest", "--downloads", str(tmp_path)])
    assert res.exit_code == 0, res.output
    with db.session() as s:
        d = s.scalar(select(Drop))
        assert (d.activity_id, d.share_id) == ("7495979599755956224", "7495979599009587200")
    # and now the activity-form URL finds it too
    res = r.invoke(cli.main, ["drop", "log", "--url", ACTIVITY_URL])
    assert "matches drop #1 by id" in res.output
    with db.session() as s:
        assert len(list(s.scalars(select(Drop)))) == 1


def test_two_different_posts_stay_two_drops(store, tmp_path):
    other_url = "https://www.linkedin.com/posts/ivantohelpyou_happy-birthday-share-7495553981142679552-CJBw"
    r = CliRunner()
    r.invoke(cli.main, ["drop", "log", "--url", ACTIVITY_URL])
    other_export = "SinglePostAnalytics_Ivan Schneider_7495553982195539969.xlsx"
    _export(tmp_path / other_export, 329, url=other_url)
    res = r.invoke(cli.main, ["drop", "ingest", "--downloads", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert "new #2" in res.output
    with db.session() as s:
        drops = list(s.scalars(select(Drop).order_by(Drop.id)))
    assert [d.url for d in drops] == [ACTIVITY_URL, other_url]
    assert drops[1].activity_id == "7495553982195539969"
    assert drops[1].share_id == "7495553981142679552"


def test_backfill_ids_from_urls_and_archive(store, tmp_path):
    import sqlite3

    r = CliRunner()
    r.invoke(cli.main, ["drop", "log", "--url", SHARE_URL])
    page = "https://www.linkedin.com/posts/model-citizen-developer_the-workshop-activity-7495975919816192000-R9JM"
    r.invoke(cli.main, ["drop", "log", "--url", page])
    r.invoke(cli.main, ["drop", "log", "--url", "https://example.test/elsewhere"])
    # Simulate rows logged before the columns existed.
    con = sqlite3.connect(tmp_path / "s.db")
    con.execute("update drops set activity_id = null, share_id = null")
    con.commit()
    con.close()
    arc = tmp_path / "arc"
    arc.mkdir()
    _export(arc / EXPORT_NAME, 515, url=SHARE_URL)
    res = r.invoke(cli.main, ["drop", "backfill-ids", "--archive", str(arc)])
    assert res.exit_code == 0, res.output
    assert "#1 share_id=7495979599009587200, activity_id=7495979599755956224" in res.output
    assert "#2 activity_id=7495975919816192000" in res.output
    assert "backfilled 2 drops; 1 with no id: #3" in res.output
    with db.session() as s:
        d1, d2, d3 = s.scalars(select(Drop).order_by(Drop.id))
        assert (d1.activity_id, d1.share_id) == ("7495979599755956224", "7495979599009587200")
        assert (d2.activity_id, d2.share_id) == ("7495975919816192000", None)
        assert (d3.activity_id, d3.share_id) == (None, None)
        assert d1.impressions is None  # backfill reads ids only, never stats
    res = r.invoke(cli.main, ["drop", "backfill-ids", "--archive", str(arc)])
    assert "backfilled 0 drops" in res.output  # idempotent


def _video_export(path, url=URL, views=56, watch="18m 34s", avg="19s"):
    """A LinkedIn export carrying the optional 'Video performance' block."""
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    for k, v in [
        ("Post URL", url),
        ("Post Date", "08/20/2026"),
        ("Impressions", 170),
        ("Members reached", 79),
        ("Video views", views),
        ("Watch time", watch),
        ("Average watch time", avg),
        ("Reactions", 2),
        ("Comments", 1),
        ("Reposts", 0),
        ("Link engagements", 0),
        ("Followers gained from this post", 0),
    ]:
        ws.append([k, v])
    wb.save(path)
    return path


@pytest.mark.parametrize(
    "text,expected",
    [("18m 34s", 1114), ("19s", 19), ("1h 2m 3s", 3723), ("2m", 120),
     ("", None), ("—", None), ("n/a", None)],
)
def test_parse_duration(text, expected):
    from strategy_store.drops import parse_duration

    assert parse_duration(text) == expected


def test_ingest_keeps_the_video_block(store, tmp_path):
    """Before 2026-08-24 the ingest read these three cells and discarded them."""
    r = CliRunner()
    res = r.invoke(cli.main, ["drop", "ingest", str(_video_export(tmp_path / "v.xlsx"))])
    assert res.exit_code == 0, res.output
    with db.session() as s:
        d = s.scalar(select(Drop))
        assert d.video_views == 56
        assert d.watch_seconds == 1114  # "18m 34s"
        assert d.avg_watch_seconds == 19


def test_ingest_infers_video_medium_from_the_video_block(store, tmp_path):
    r = CliRunner()
    r.invoke(cli.main, ["drop", "ingest", str(_video_export(tmp_path / "v.xlsx"))])
    with db.session() as s:
        assert s.scalar(select(Drop)).format == "video"


def test_ingest_leaves_link_placement_alone(store, tmp_path):
    """LinkedIn never says where the link was, so ingest must not guess — and
    must not wipe a placement already logged by hand."""
    r = CliRunner()
    r.invoke(cli.main, ["drop", "log", "--url", URL, "--link", "first-comment"])
    r.invoke(cli.main, ["drop", "ingest", str(_video_export(tmp_path / "v.xlsx"))])
    with db.session() as s:
        d = s.scalar(select(Drop))
        assert d.link_placement == "first-comment"
        assert d.video_views == 56


def test_non_video_export_leaves_the_video_columns_null(store, tmp_path):
    r = CliRunner()
    r.invoke(cli.main, ["drop", "ingest", str(_export(tmp_path / "p.xlsx", 100))])
    with db.session() as s:
        d = s.scalar(select(Drop))
        assert d.video_views is None and d.watch_seconds is None
        assert d.format == "post"


def test_drop_list_shows_video_and_placement(store, tmp_path):
    r = CliRunner()
    r.invoke(cli.main, ["drop", "log", "--url", URL, "--link", "first-comment"])
    r.invoke(cli.main, ["drop", "ingest", str(_video_export(tmp_path / "v.xlsx"))])
    out = r.invoke(cli.main, ["drop", "list"]).output
    assert "video\u00b7fc" in out
    assert "56 views" in out and "19s avg" in out and "18m34s total" in out


def test_export_upgrades_the_default_medium_but_not_a_hand_set_one(store, tmp_path):
    """`drop log` first / stats later is the documented workflow, so a drop is
    almost always already stored as the default "post" when its export lands.
    The export must be allowed to correct that — but never to overwrite a
    medium a human actually chose."""
    r = CliRunner()
    r.invoke(cli.main, ["drop", "log", "--url", URL])  # default medium: post
    r.invoke(cli.main, ["drop", "ingest", str(_video_export(tmp_path / "v.xlsx"))])
    with db.session() as s:
        assert s.scalar(select(Drop)).format == "video"

    # A hand-set medium survives an export that guesses otherwise.
    other = URL.replace("7495553981142679552", "7495553981142679553")
    r.invoke(cli.main, ["drop", "log", "--url", other, "--format", "carousel"])
    r.invoke(cli.main, ["drop", "ingest", str(_export(tmp_path / "p.xlsx", 100, url=other))])
    with db.session() as s:
        d = s.scalar(select(Drop).where(Drop.url == other))
        assert d.format == "carousel"
