"""Seed / sync positions from the two systems that already hold them.

* A Vikunja instance — the projects named by STRATEGY_VIKUNJA_PROJECTS.
  Status is encoded there as a TITLE PREFIX (DECIDED:,
  CONFLICT:, WATCH:, FIX:, DONE:) because the tool has no column for it;
  this is where that convention gets lifted into a real column.
* beads — every `type: decision` issue in every registered rig.

Ownership rule (see models.py): sources own title/statement/source_status;
the store owns status/scope/element_key/supersedes after the FIRST insert.
Re-running seed refreshes text and reports — never rewrites — the rest.
"""

from __future__ import annotations

import html
import json
import re
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import yaml
from sqlalchemy import select

from . import env
from .models import SCOPE_RANK, Position

GT_ROOT = env.path_value("GT_ROOT", Path.home() / "gt")
VIKUNJA_CONFIG = Path.home() / ".vikunja-mcp" / "config.yaml"
def _vikunja_projects() -> dict[int, str]:
    """Which Vikunja projects hold positions. Set STRATEGY_VIKUNJA_PROJECTS as
    a comma-separated list of `id:Name` pairs, e.g. "381:2026 Goals,375:Exec"."""
    raw = (env.env_value("STRATEGY_VIKUNJA_PROJECTS") or "").strip()
    if not raw:
        return {}
    out: dict[int, str] = {}
    for part in raw.split(","):
        pid, _, name = part.partition(":")
        if pid.strip().isdigit():
            out[int(pid.strip())] = name.strip() or pid.strip()
    return out


VIKUNJA_PROJECTS = _vikunja_projects()

# Title-prefix convention → position.status. Anything unprefixed is holding.
PREFIX_STATUS = [
    ("DECIDED", "resolved"),
    ("DONE", "resolved"),
    ("CONFLICT", "contested"),
    ("UNRESOLVED", "contested"),
    ("WATCH", "holding"),
    ("FIX", "holding"),
]
BEADS_STATUS = {
    "open": "holding",
    "in_progress": "holding",
    "blocked": "holding",
    "closed": "resolved",
    "deferred": "retired",
}

# Framework slot heuristic and stated supersessions are DATA, not code: they
# name real decisions. Loaded from a file outside the repo — point
# STRATEGY_SEED_MAP at your own, or drop one at ~/.strategy/seed-map.yaml.
# See examples/seed-map.example.yaml. Anything unlisted gets NULL on purpose:
# an orphan is a report finding.

SEED_MAP_PATH = Path.home() / ".strategy" / "seed-map.yaml"


def _seed_map() -> dict:
    override = env.env_value("STRATEGY_SEED_MAP")
    p = Path(override).expanduser() if override else SEED_MAP_PATH
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text()) or {}


_MAP = _seed_map()
ELEMENT_HEURISTIC: dict[str, str] = dict(_MAP.get("element_heuristic") or {})
SEED_SUPERSEDES: list[tuple[str, str]] = [tuple(x) for x in (_MAP.get("supersedes") or [])]


def _strip_html(s: str) -> str:
    s = re.sub(r"<br\s*/?>|</p>|</li>|</h\d>", "\n", s or "")
    s = re.sub(r"<li>", "- ", s)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s).strip()


def _prefix_status(title: str) -> tuple[str, str | None]:
    head = title.strip().upper()
    for prefix, status in PREFIX_STATUS:
        if head.startswith(prefix):
            return status, prefix
    return "holding", None


def _date(s: str | None) -> date | None:
    if not s or s.startswith("0001"):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        return None


# ---------------------------------------------------------------- Vikunja


def fetch_vikunja() -> list[dict]:
    cfg = yaml.safe_load(VIKUNJA_CONFIG.read_text())
    inst = cfg["instances"]["business"]
    base = inst["url"].rstrip("/") + "/api/v1"
    headers = {"Authorization": f"Bearer {inst['token']}"}
    rows = []
    with httpx.Client(timeout=60) as c:
        for pid, pname in VIKUNJA_PROJECTS.items():
            page, total = 1, 1
            while page <= total:
                r = c.get(
                    f"{base}/projects/{pid}/tasks",
                    headers=headers,
                    params={"page": page, "per_page": 50, "filter": "done = false"},
                )
                r.raise_for_status()
                total = int(r.headers.get("x-pagination-total-pages") or 1)
                for t in r.json():
                    status, prefix = _prefix_status(t["title"])
                    rows.append(
                        {
                            "source_system": "vikunja",
                            "source_ref": f"#{t['id']}",
                            "title": t["title"].strip(),
                            "statement": _strip_html(t.get("description", "")),
                            "status": status,
                            "source_status": prefix or f"open/{pname}",
                            # Both projects were populated from chat strategy sessions
                            # (Aug 10 / Aug 12) with no dev-environment visibility.
                            "scope": "web-chat",
                            "decided_on": _date(t.get("created")),
                            "venture": None,
                        }
                    )
                page += 1
    return rows


# ------------------------------------------------------------------ beads


def _rigs() -> list[str]:
    reg = GT_ROOT / "rigs.json"
    if not reg.exists():
        return []
    names = list(json.loads(reg.read_text()).get("rigs", {}).keys())
    return [n for n in names if (GT_ROOT / n / ".beads").exists() or (GT_ROOT / n).is_dir()]


def _bd(rig: str, *args: str) -> list[dict] | dict | None:
    try:
        out = subprocess.run(
            ["bd", *args, "--json"], cwd=GT_ROOT / rig, capture_output=True, text=True, timeout=120
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return None


def fetch_beads() -> list[dict]:
    rows = []
    for rig in _rigs():
        listing = _bd(rig, "list", "-t", "decision", "--status", "all")
        if not listing:
            continue
        for full in listing:
            # `bd list --json` already carries description/created_at — no per-item show.
            rows.append(
                {
                    "source_system": "beads",
                    "source_ref": full["id"],
                    "title": (full.get("title") or "").strip(),
                    "statement": (full.get("description") or "").strip(),
                    "status": BEADS_STATUS.get(full.get("status", "open"), "holding"),
                    "source_status": full.get("status"),
                    # A bead decision is made inside one rig with that rig's files
                    # and beads in view.
                    "scope": "single-rig",
                    "decided_on": _date(full.get("created_at")),
                    "venture": rig,
                }
            )
    return rows


# ------------------------------------------------------------------ upsert


def upsert(session, rows: list[dict]) -> dict:
    """Insert new positions; refresh source-owned text on existing ones.
    Returns counts and a drift list (source status changed since we last mapped it)."""
    now = datetime.now(timezone.utc)
    counts = {"inserted": 0, "updated": 0, "drift": []}
    for r in rows:
        key = f"{r['source_system']}:{r['source_ref']}"
        existing = session.scalar(
            select(Position).where(
                Position.source_system == r["source_system"], Position.source_ref == r["source_ref"]
            )
        )
        if existing is None:
            p = Position(**r, element_key=ELEMENT_HEURISTIC.get(key), synced_at=now)
            session.add(p)
            counts["inserted"] += 1
            continue
        # Source-owned columns refresh; store-owned columns are left alone.
        existing.title = r["title"]
        existing.statement = r["statement"]
        if existing.source_status != r["source_status"]:
            counts["drift"].append(
                (key, existing.source_status, r["source_status"], existing.status, r["status"])
            )
            existing.source_status = r["source_status"]
        existing.synced_at = now
        counts["updated"] += 1
    session.flush()

    for new_key, old_key in SEED_SUPERSEDES:
        new = _by_key(session, new_key)
        old = _by_key(session, old_key)
        if new and old and old.superseded_by_id is None:
            ok, why = supersede_allowed(new, old, evidence_note=None)
            if ok:
                old.superseded_by_id = new.id
            else:
                counts.setdefault("refused", []).append((new_key, old_key, why))
    session.commit()
    return counts


def _by_key(session, key: str) -> Position | None:
    sys_, ref = key.split(":", 1)
    return session.scalar(
        select(Position).where(Position.source_system == sys_, Position.source_ref == ref)
    )


def supersede_allowed(new: Position, old: Position, evidence_note: str | None) -> tuple[bool, str]:
    """The scope rule: a narrower-scope decision may not silently supersede a
    wider one. It may if it names the new evidence that justifies it."""
    if SCOPE_RANK[new.scope] >= SCOPE_RANK[old.scope]:
        return True, "scope >= superseded"
    if evidence_note:
        return True, "narrower scope, but new evidence named"
    return False, (
        f"refused: {new.scope} < {old.scope} and no new evidence named "
        f"(pass --evidence to override)"
    )
