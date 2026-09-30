"""`strategy drop funnel` — the part of a drop LinkedIn can't see.

Reads the shared privacy-minimal `page_views` plane (mcd_pageviews / qrcards)
for the app a drop points at: landing views (and how many came from LinkedIn),
tables created, joined, dealt, completed. Read-only; env order
STRATEGY_ANALYTICS_URL → PAGEVIEWS_DB_URL → QRCARDS_RUNTIME_DB_URL, falling back
to `export VAR=` lines in ~/.env so it works from a bare shell.
"""

from __future__ import annotations

from datetime import date, datetime, time
from urllib.parse import urlparse

from sqlalchemy import bindparam, create_engine, text

from . import env

ENV_KEYS = ("STRATEGY_ANALYTICS_URL", "PAGEVIEWS_DB_URL", "QRCARDS_RUNTIME_DB_URL")
LANDING_PATHS = ("/one-card-schmuck", "/table/one-card-schmuck", "/table/new")
EVENTS = ("table/created", "table/joined", "table/dealt", "table/completed")


def analytics_url() -> str | None:
    for k in ENV_KEYS:
        v = env.env_value(k)
        if v:
            return v
    return None


def _norm(url: str) -> str:
    return url.replace("postgres://", "postgresql://", 1)


def funnel_for(app_url: str, since: date, until: date | None = None) -> dict | None:
    """Counts from page_views for the app's domain since a date. None if no DB configured."""
    url = analytics_url()
    if not url:
        return None
    host = urlparse(app_url).netloc.lower() or app_url
    path = urlparse(app_url).path or "/"
    landing = tuple(sorted({path, *LANDING_PATHS}))
    lo = datetime.combine(since, time.min)
    hi = datetime.combine(until, time.max) if until else datetime.max
    eng = create_engine(_norm(url), future=True)
    with eng.connect() as c:
        base = "from page_views where domain=:d and ts>=:lo and ts<=:hi"
        p = {"d": host, "lo": lo, "hi": hi}
        landing_rows = c.execute(
            text(
                f"select referrer_host, count(*) {base} and path in :paths group by referrer_host"
            ).bindparams(bindparam("paths", expanding=True)),  # portable IN (pg + sqlite)
            {**p, "paths": landing},
        ).fetchall()
        views = sum(n for _, n in landing_rows)
        from_li = sum(n for r, n in landing_rows if r and "linkedin" in r)
        # every custom event on the domain (purr's event/table/*, Heads in Beds'
        # event/hib/*, whatever the next app emits) — one query, keyed by path
        ev = dict(
            c.execute(
                text(f"select path, count(*) {base} and path like 'event/%' group by path"), p
            ).fetchall()
        )
        # completion scores: apps that finish with a score put it in slug
        # (Heads in Beds: event/hib/complete, slug lit-NN)
        scores = dict(
            c.execute(
                text(
                    f"select slug, count(*) {base} and path='event/hib/complete' "
                    "and slug is not null group by slug"
                ),
                p,
            ).fetchall()
        )
        tables = c.execute(
            text(f"select count(distinct slug) {base} and path='event/table/created'"), p
        ).scalar()
    return {
        "domain": host,
        "since": since,
        "landing_views": views,
        "landing_from_linkedin": from_li,
        "tables_created": ev.get("event/table/created", 0),
        "distinct_tables": tables or 0,
        "joined": ev.get("event/table/joined", 0),
        "dealt": ev.get("event/table/dealt", 0),
        "completed": ev.get("event/table/completed", 0),
        "events": ev,
        "hib": {
            "start": ev.get("event/hib/start", 0),
            "era2": ev.get("event/hib/era2", 0),
            "era3": ev.get("event/hib/era3", 0),
            "complete": ev.get("event/hib/complete", 0),
            "scores": scores,
        },
    }
