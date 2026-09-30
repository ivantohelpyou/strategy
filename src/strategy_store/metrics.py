"""Metrics: the numbers being chased, per beat, as a time series.

`strategy metric pull` reads every metric whose source is an API (Buttondown
today) and records one reading per metric per day. Tokens are resolved by
env.py, from the process environment or the .env files it lists — never printed.
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select

from . import env
from .models import Metric, MetricReading

BUTTONDOWN_API = "https://api.buttondown.com/v1"


def record(session, metric: Metric, value: int, as_of: date | None = None,
           origin: str = "", note: str = "") -> tuple[MetricReading, bool]:
    """Upsert the reading for (metric, day). Returns (reading, created)."""
    as_of = as_of or date.today()
    r = session.scalar(
        select(MetricReading).where(MetricReading.metric_id == metric.id, MetricReading.as_of == as_of)
    )
    created = r is None
    if created:
        r = MetricReading(metric=metric, as_of=as_of)
        session.add(r)
    r.value, r.origin, r.note = value, origin or r.origin, note or r.note
    session.commit()
    return r, created


def buttondown_count(token: str, kind: str = "regular", client=None) -> int:
    import httpx

    c = client or httpx.Client(timeout=30)
    resp = c.get(
        f"{BUTTONDOWN_API}/subscribers",
        params={"type": kind, "page_size": 1},
        headers={"Authorization": f"Token {token}"},
    )
    resp.raise_for_status()
    return int(resp.json()["count"])


def pull(session, today: date | None = None, client=None) -> list[tuple[Metric, int | None, str]]:
    """Read every API-sourced metric. Returns (metric, value|None, message)."""
    out = []
    for m in session.scalars(select(Metric).where(Metric.source == "buttondown-api")):
        token = env.env_value(m.source_key)
        if not token:
            out.append((m, None, f"no token in env for {m.source_key}"))
            continue
        try:
            v = buttondown_count(token, client=client)
        except Exception as exc:  # noqa: BLE001 — one list failing must not stop the rest
            out.append((m, None, f"{type(exc).__name__}: {exc}"))
            continue
        record(session, m, v, today, origin="api")
        out.append((m, v, "ok"))
    return out


def record_from_export(session, metric_source_key: str, value: int, as_of: date, origin: str) -> list[Metric]:
    """Called by drop ingest: an article export carries email_sends etc."""
    hit = []
    for m in session.scalars(
        select(Metric).where(Metric.source == "linkedin-export", Metric.source_key == metric_source_key)
    ):
        record(session, m, value, as_of, origin=origin)
        hit.append(m)
    return hit


def delta(m: Metric, days: int = 7, today: date | None = None) -> tuple[int, date] | None:
    """Change since the window start: (diff, since). Baseline is the reading on
    or before (today - days); with sparse readings, the earliest reading inside
    the window instead. None if there is no earlier reading at all. The `since`
    date is what makes the number honest — print it."""
    if not m.latest:
        return None
    today = today or m.latest.as_of
    start = today - timedelta(days=days)
    before = m.reading_on_or_before(start)
    if before is None:
        inside = [r for r in m.readings if start < r.as_of < m.latest.as_of]
        before = inside[0] if inside else None
    if before is None or before.id == m.latest.id:
        return None
    return m.latest.value - before.value, before.as_of


def goal_lines(metrics: list[Metric], today: date | None = None) -> list[str]:
    """One line per goal (metric with gates) for prime / list."""
    lines = []
    for m in metrics:
        if not m.gate_list:
            continue
        if not m.latest:
            lines.append(f"Goal {m.name}: no reading yet · gates {'/'.join(map(str, m.gate_list))}")
            continue
        nxt = m.next_gate()
        d = delta(m, 7, today)
        dtxt = f" · {d[0]:+d} since {d[1]}" if d is not None else ""
        rest = [g for g in m.gate_list if nxt is None or g > nxt]
        tail = (f" → {nxt}" if nxt else " · all gates passed") + ("".join(f" → {g}" for g in rest))
        due = f" by {m.due_on}" if m.due_on else ""
        lines.append(
            f"Goal {m.name}: {m.latest.value}{tail}{due}{dtxt} (as of {m.latest.as_of})"
        )
    return lines
