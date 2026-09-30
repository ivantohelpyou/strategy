"""Drops: the daily-drop scoreboard and its LinkedIn-export ingest.

Getting on base, counted. LinkedIn only gives per-post stats as a manual
export (Post → View analytics → Export), so the store tracks which drops
still need one and `strategy prime` says so.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import or_, select

from .models import Drop

# LinkedIn stats keep moving for about a week; a stats file younger than this
# (relative to the post) is provisional and worth re-pulling.
SETTLE_DAYS = 7
# Nag if a drop has no stats N days after posting.
STATS_DUE_DAYS = 2

_LABELS = {
    "Impressions": "impressions",
    "Members reached": "reached",
    "Article views": "article_views",
    "Email sends": "email_sends",
    "Reactions": "reactions",
    "Comments": "comments",
    "Reposts": "reposts",
    "Saves": "saves",
    "Link engagements": "link_clicks",
    "Followers gained from this post": "followers_gained",
    "Video views": "video_views",
}

# The same block, but LinkedIn writes these as display text ("18m 34s", "19s")
# rather than a number. Stored as seconds so they sum and compare.
_DURATION_LABELS = {
    "Watch time": "watch_seconds",
    "Average watch time": "avg_watch_seconds",
}

_DURATION_RE = re.compile(r"(\d+)\s*([hms])")


def parse_duration(text: str) -> int | None:
    """LinkedIn's watch-time display text -> seconds. "18m 34s" -> 1114,
    "19s" -> 19, "1h 2m 3s" -> 3723. Returns None if nothing parses, so a
    format change shows up as a missing value rather than a silent zero."""
    parts = _DURATION_RE.findall(str(text).strip().lower())
    if not parts:
        return None
    per = {"h": 3600, "m": 60, "s": 1}
    return sum(int(n) * per[u] for n, u in parts)


def normalize_url(url: str) -> str:
    """Canonical form for matching: strip whitespace, query/fragment, trailing slash.
    A hand-logged URL copied from the browser often carries a trailing slash or
    ?utm; LinkedIn's export never does — without this they become two drops."""
    url = url.strip().split("?", 1)[0].split("#", 1)[0]
    return url.rstrip("/")


# One post, two 19-digit ids. The browser URL and the export's filename carry
# the activity URN; the export's Post URL cell (and many copied links) carry the
# share/ugcPost URN. The slug text differs too, so the two URLs share no prefix.
_ACTIVITY_RE = re.compile(r"(?:-activity-|urn:li:activity:)(\d{15,})")
_SHARE_RE = re.compile(r"(?:-(?:share|ugcPost)-|urn:li:(?:share|ugcPost):)(\d{15,})")
_EXPORT_NAME_RE = re.compile(r"SinglePostAnalytics_.*?(\d{15,})")


def ids_from_url(url: str) -> dict:
    """{'activity_id': …, 'share_id': …} for whichever id form(s) the URL carries."""
    a = _ACTIVITY_RE.search(url or "")
    sh = _SHARE_RE.search(url or "")
    return {"activity_id": a.group(1) if a else None, "share_id": sh.group(1) if sh else None}


def activity_id_from_export_name(name: str) -> str | None:
    """LinkedIn names a single-post export after the post's ACTIVITY id
    (`SinglePostAnalytics_<Name>_<activity id>.xlsx`), even though the Post URL
    inside carries the share id — verified on every export in the archive."""
    m = _EXPORT_NAME_RE.search(name)
    return m.group(1) if m else None


def find_drop(session, url: str, activity_id: str | None = None, share_id: str | None = None):
    """Look a drop up by URL, then by either LinkedIn id, then by URL prefix
    (trailing-slash/query variants). Pass the ids you know beyond the URL's own
    (an export's filename id); the URL's are extracted here."""
    d = session.scalar(select(Drop).where(Drop.url == url))
    if d is not None:
        return d
    ids = ids_from_url(url)
    known = {activity_id, share_id, ids["activity_id"], ids["share_id"]} - {None}
    if known:
        d = session.scalar(
            select(Drop)
            .where(or_(Drop.activity_id.in_(known), Drop.share_id.in_(known)))
            .order_by(Drop.id)
        )
        if d is not None:
            return d
    n = normalize_url(url)
    for cand in session.scalars(select(Drop).where(Drop.url.like(f"{n}%"))):
        if normalize_url(cand.url) == n:
            return cand
    return None


def parse_linkedin_export(path: Path) -> dict:
    """Read one SinglePostAnalytics_*.xlsx into a dict of Drop columns."""
    import warnings

    import openpyxl

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # LinkedIn's xlsx has no default style; harmless
        wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.worksheets[0]
    out: dict = {"notes": "", "app_url": None}
    for row in ws.iter_rows(values_only=True):
        cells = [c for c in row if c is not None]
        if len(cells) < 2:
            continue
        k, v = str(cells[0]).strip(), cells[1]
        if k == "Post URL":
            out["url"] = normalize_url(str(v))
        elif k == "Post Date":
            out["posted_on"] = datetime.strptime(str(v), "%m/%d/%Y").date()
        elif k in _LABELS:
            out[_LABELS[k]] = int(str(v).replace(",", "") or 0)
        elif k in _DURATION_LABELS:
            out[_DURATION_LABELS[k]] = parse_duration(v)
        elif str(k).startswith("http") and isinstance(v, (int, float)):
            out["app_url"] = k  # LinkedIn lists clicked links under Link engagements
    if "url" not in out or "posted_on" not in out:
        raise ValueError(f"{path.name}: not a LinkedIn single-post export (no Post URL/Post Date)")
    slug = re.sub(r"^https?://www\.linkedin\.com/posts/[^_]+_", "", out["url"])
    out["title"] = slug.split("-activity-")[0].split("-ugcPost-")[0].replace("-", " ")[:200]
    # The export names the medium only by which optional block it carries.
    # link_placement is NOT inferable from the export — LinkedIn does not say
    # where the link was — so ingest leaves it alone; `drop log` sets it.
    if out.get("article_views") is not None:
        out["format"] = "article"
    elif out.get("video_views") is not None:
        out["format"] = "video"
    else:
        out["format"] = "post"
    out["stats_as_of"] = date.fromtimestamp(path.stat().st_mtime)
    out.update({k: v for k, v in ids_from_url(out["url"]).items() if v})
    if not out.get("activity_id"):
        out["activity_id"] = activity_id_from_export_name(path.name)
    return out


def upsert_from_export(session, path: Path) -> tuple[Drop, bool]:
    """Returns (drop, created). An export older than the stats already stored
    is ignored (returns the drop untouched) — re-exports of one post may sit in
    Downloads as `X.xlsx` and `X (1).xlsx`, and name order is not date order."""
    data = parse_linkedin_export(path)
    d = find_drop(session, data["url"], data.get("activity_id"), data.get("share_id"))
    created = d is None
    if created:
        d = Drop(url=data["url"], posted_on=data["posted_on"])
        session.add(d)
    # Record both ids even on an export too old to apply: the hand-logged URL may
    # have carried only one of them, and the next ingest matches on either.
    set_ids(d, data.get("activity_id"), data.get("share_id"))
    if not created and d.stats_as_of and data["stats_as_of"] < d.stats_as_of:
        session.commit()
        return d, False
    for k, v in data.items():
        if k in ("url", "activity_id", "share_id"):
            continue  # the stored (hand-logged) URL stays; ids were set above
        if k == "format" and d.format and d.format != "post":
            continue  # a hand-logged medium (e.g. carousel) wins over the guess
        if k == "format" and v == "post":
            continue  # the export learned nothing the default did not already say
        if k in ("title", "app_url") and getattr(d, k):
            continue  # a hand-logged title/app_url wins over the URL slug
        setattr(d, k, v)
    session.commit()
    return d, created


def set_ids(d: Drop, activity_id: str | None = None, share_id: str | None = None) -> list[str]:
    """Fill whichever of the drop's LinkedIn ids are still empty, from the
    arguments and from its own URL. Returns the column names that changed."""
    ids = ids_from_url(d.url)
    changed = []
    for col, val in (("activity_id", activity_id or ids["activity_id"]),
                     ("share_id", share_id or ids["share_id"])):
        if val and not getattr(d, col):
            setattr(d, col, val)
            changed.append(col)
    return changed


def backfill_ids(session, archive_dir: Path | None = None) -> list[tuple[Drop, list[str]]]:
    """One-time migration for drops logged before the id columns existed: fill
    ids from each drop's URL, then from the archived exports (filename → activity
    id, Post URL → share id). Returns (drop, changed columns) for drops touched."""
    touched: dict[int, tuple[Drop, list[str]]] = {}

    def note(d, changed):
        if changed:
            touched.setdefault(d.id, (d, []))[1].extend(changed)

    for d in session.scalars(select(Drop)):
        note(d, set_ids(d))
    session.flush()
    if archive_dir and archive_dir.is_dir():
        for f in sorted(archive_dir.glob("SinglePostAnalytics_*.xlsx")):
            try:
                data = parse_linkedin_export(f)
            except Exception:  # noqa: BLE001 — a stray non-export file
                continue
            d = find_drop(session, data["url"], data.get("activity_id"), data.get("share_id"))
            if d is not None:
                note(d, set_ids(d, data.get("activity_id"), data.get("share_id")))
    session.commit()
    return list(touched.values())


def needs_stats(drops: list[Drop], today: date | None = None) -> list[tuple[Drop, str]]:
    """Drops whose stats should be (re)downloaded, with the reason."""
    today = today or date.today()
    out = []
    for d in drops:
        age = (today - d.posted_on).days
        if not d.has_stats and age >= STATS_DUE_DAYS:
            out.append((d, f"no stats, posted {age}d ago"))
        elif d.has_stats and (d.stats_as_of - d.posted_on).days < SETTLE_DAYS <= age:
            out.append(
                (d, f"stats from day {(d.stats_as_of - d.posted_on).days}, post has settled")
            )
    return out


def scoreboard(drops: list[Drop], days: int = 7, today: date | None = None) -> dict:
    today = today or date.today()
    recent = [d for d in drops if (today - d.posted_on).days < days]
    scored = [d for d in recent if d.has_stats]
    hits = [d for d in scored if d.base_hit]
    last_follow = max((d.posted_on for d in drops if (d.followers_gained or 0) > 0), default=None)
    last_hit = max((d.posted_on for d in drops if d.base_hit), default=None)
    return {
        "days": days,
        "dropped": len(recent),
        "scored": len(scored),
        "on_base": len(hits),
        "last_hit": last_hit,
        "last_follow": last_follow,
        "followers": sum(d.followers_gained or 0 for d in drops),
    }
