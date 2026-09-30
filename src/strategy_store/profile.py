"""Public surfaces as a query over the store (sea-mii7.2).

A profile rewrite drifted from the store twice in one pass on 2026-08-20 — a
cadence claim the drop count did not support, and strategy-memo shorthand pasted
into public copy. The store already held the positions, the evidence and the
drops; the profile was the one public surface that was not checked against them.

Each check is a query with a stated reason, never a style opinion. The rules that
name real segments and real phrases live in a private YAML file
(STRATEGY_PROFILE_RULES), not in this source — same discipline as the forcing and
seed rows, so the repo can be published. See examples/profile-rules.yaml.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from . import env
from .models import Drop, Position, ProfileClaim, ProfileField

DEFAULT_RULES_PATH = Path.home() / ".strategy" / "profile-rules.yaml"
# LinkedIn's own limits, so export can say what will be cut before it is pasted.
FIELD_LIMITS = {"headline": 220, "about": 2600, "experience.title": 100, "skill": 100}
CADENCE_WORDS = ("daily", "every day", "each day", "most days", "weekly", "a day", "a week")


def rules_path() -> Path:
    override = env.env_value("STRATEGY_PROFILE_RULES")
    return Path(override).expanduser() if override else DEFAULT_RULES_PATH


def load_rules() -> dict:
    """Private rules: which words name a not-playing segment, which phrases are
    memo shorthand, how many drops a cadence word claims. Missing file → the
    checks that need it report that they could not run, rather than passing."""
    p = rules_path()
    if not p.exists():
        return {}
    import yaml

    return yaml.safe_load(p.read_text()) or {}


def _apps() -> dict:
    """The registry of what exists, so the profile cannot name a product that
    does not, or omit one that does."""
    gt_root = env.path_value("GT_ROOT", Path.home() / "gt")
    reg = gt_root / "apps.json"
    if not reg.exists():
        return {}
    try:
        data = json.loads(reg.read_text())
    except ValueError:
        return {}
    return data.get("apps", data) if isinstance(data, dict) else {}


def _finding(check: str, why: str, rows: list[str], ran: bool = True) -> dict:
    return {"check": check, "why": why, "rows": rows, "ran": ran}


def _fields(session, surface: str) -> list[ProfileField]:
    return sorted(
        session.scalars(select(ProfileField).where(ProfileField.surface == surface)),
        key=lambda f: (f.ordinal, f.field),
    )


def check(session, surface: str = "linkedin", today: date | None = None) -> list[dict]:
    """Every check, in the order a reader wants them: what is unsupported, what
    contradicts a decision, what the numbers do not bear out, what is stale."""
    today = today or date.today()
    rules = load_rules()
    fields = _fields(session, surface)
    if not fields:
        # Nothing recorded is not the same as nothing wrong. Reporting "seven
        # apps you never mention" against an empty profile would be a finding
        # about this store, not about the surface.
        return [
            _finding(
                "Nothing recorded for this surface",
                f"No fields yet. `strategy profile set <field> --surface {surface} "
                "--text ...` with what the surface says today, then check it.",
                [],
                ran=False,
            )
        ]
    by_id = {f.id: f for f in fields}
    claims = [
        c
        for c in session.scalars(select(ProfileClaim))
        if c.field_id in by_id
    ]
    positions = list(session.scalars(select(Position)))
    superseded = {p.supersedes_id for p in positions if p.supersedes_id}

    out = [
        _finding(
            "Claims with nothing behind them",
            "Every factual claim on a public surface should point at a position, a piece "
            "of evidence, a drop, a metric reading or a registered app. One that points "
            "at nothing is one you cannot defend when asked.",
            [
                f"{by_id[c.field_id].field}: {c.claim_text}"
                for c in claims
                if c.licensed_by_kind == "none" or not c.licensed_by_id
            ],
        ),
        _not_playing(fields, rules, positions),
        _cadence(session, fields, rules, today),
        _shorthand(fields, rules),
        _stale(fields, claims, by_id, superseded, positions),
        _registry(fields, rules),
    ]
    return out


def _not_playing(fields, rules, positions) -> dict:
    """A public surface advertising a segment the strategy decided not to play in."""
    terms = (rules.get("not_playing") or {})
    if not terms:
        return _finding(
            "Copy that advertises a not-playing segment",
            "Needs the private rules file: a not-playing decision names a segment in "
            "prose, and only a term list can connect 'AI Productivity' on a skills list "
            "to 'corporate AI-enablement training — explicitly declined'. "
            f"Write {rules_path()} (see examples/profile-rules.yaml).",
            [],
            ran=False,
        )
    titles = {}
    for p in positions:
        titles[f"{p.source_system}:{p.source_ref}"] = p.title
        titles[f"position:{p.id}"] = p.title
    rows = []
    for f in fields:
        low = f.text.lower()
        for term, ref in terms.items():
            if term.lower() in low:
                where = titles.get(str(ref), str(ref))
                rows.append(f"{f.field}: “{term}” — but {ref} says not playing ({where[:60]})")
    return _finding(
        "Copy that advertises a not-playing segment",
        "The profile is selling something the strategy decided to decline. One of the "
        "two has to change, and the profile is the cheaper one.",
        rows,
    )


def _cadence(session, fields, rules, today) -> dict:
    """A cadence word is a promise the drop count either keeps or does not."""
    window = int((rules.get("cadence") or {}).get("window_days", 30))
    since = today - timedelta(days=window)
    drops = [
        d
        for d in session.scalars(select(Drop))
        if d.posted_on and d.posted_on >= since
    ]
    rows = []
    for f in fields:
        low = f.text.lower()
        for word in CADENCE_WORDS:
            if word not in low:
                continue
            per_day = len(drops) / window if window else 0
            claimed = word in ("daily", "every day", "each day", "a day")
            if claimed and per_day < 0.7:
                rows.append(
                    f"{f.field}: “{word}” — {len(drops)} drops in {window} days "
                    f"({per_day:.2f}/day). The word claims about one a day."
                )
            elif not claimed and len(drops) < window / 7:
                rows.append(
                    f"{f.field}: “{word}” — {len(drops)} drops in {window} days is under "
                    "one a week."
                )
    return _finding(
        "Cadence words the drop count does not support",
        "“daily” and “weekly” are checkable claims, not adjectives. The drops table "
        "already knows the answer.",
        rows,
    )


def _shorthand(fields, rules) -> dict:
    """Memo shorthand reads as a slogan in public and means something else."""
    deny = rules.get("shorthand") or {}
    if not deny:
        return _finding(
            "Strategy-memo shorthand in public copy",
            f"Needs the private rules file ({rules_path()}): the denylist is a handful "
            "of real phrases from real memos.",
            [],
            ran=False,
        )
    phrases = deny if isinstance(deny, dict) else {p: "" for p in deny}
    rows = []
    for f in fields:
        low = f.text.lower()
        for phrase, why in phrases.items():
            if phrase.lower() in low:
                rows.append(f"{f.field}: “{phrase}”" + (f" — {why}" if why else ""))
    return _finding(
        "Strategy-memo shorthand in public copy",
        "Internal shorthand carries a meaning the reader does not have. It reads as a "
        "claim about them.",
        rows,
    )


def _stale(fields, claims, by_id, superseded, positions) -> dict:
    """A field still citing a position that has since been replaced."""
    pos_by_id = {p.id: p for p in positions}
    rows = []
    for c in claims:
        if c.licensed_by_kind != "position" or not c.licensed_by_id:
            continue
        try:
            pid = int(str(c.licensed_by_id).lstrip("#"))
        except ValueError:
            continue
        if pid in superseded:
            newer = next((p for p in positions if p.supersedes_id == pid), None)
            f = by_id[c.field_id]
            rows.append(
                f"{f.field}: “{c.claim_text[:60]}” cites #{pid} "
                f"({(pos_by_id.get(pid).title[:40] + '…') if pos_by_id.get(pid) else '?'}), "
                f"replaced by #{newer.id if newer else '?'}"
            )
    return _finding(
        "Claims licensed by a decision that has since been replaced",
        "The claim may still be true, but its licence is not. Re-point it or reword it.",
        rows,
    )


def _registry(fields, rules) -> dict:
    """Products named that do not exist, and shipped ones never mentioned."""
    apps = _apps()
    if not apps:
        return _finding(
            "Products named vs products that exist",
            "No app registry found at $GT_ROOT/apps.json.",
            [],
            ran=False,
        )
    shipped = {
        key: a
        for key, a in apps.items()
        if isinstance(a, dict) and a.get("status") == "stable"
    }
    # "Never named" is only a finding for something a reader could actually go
    # and use. A stable internal tool (no prod URL) has no business on a public
    # profile, and reporting it as missing is how a check earns being ignored.
    public = {k for k, a in shipped.items() if (a.get("prod") or {}).get("url")}
    text = " ".join(f.text for f in fields).lower()

    def _named(key: str) -> bool:
        # Word boundaries, not substrings: registry keys are short ("sos",
        # "hib", "elevator") and `in` matches them inside ordinary prose. The
        # human name counts too — the profile says "Survey of Software", not "sos".
        aliases = [key] + [str((apps.get(key) or {}).get("name", "")).split("—")[0].strip()]
        return any(
            a and re.search(rf"\b{re.escape(a.lower())}\b", text) for a in aliases
        )

    named = {k for k in apps if _named(k)}
    unknown = named - set(shipped)
    missing = public - named
    rows = [f"named but not stable/active in apps.json: {n}" for n in sorted(unknown)]
    rows += [f"stable/active but never named: {n}" for n in sorted(missing)]
    caveat = rules.get("registry_incomplete")
    if caveat:
        rows.append(f"(registry is known incomplete: {caveat})")
    return _finding(
        "Products named vs products that exist",
        "The registry is what can vouch for the profile. A name with no entry cannot be "
        "checked; an entry with no mention is range the reader never sees.",
        rows,
    )


def export(session, surface: str = "linkedin") -> str:
    """The fields in surface order with character counts, ready to paste."""
    lines = []
    for f in _fields(session, surface):
        limit = f.char_limit or FIELD_LIMITS.get(f.field)
        n = len(f.text)
        flag = ""
        if limit:
            flag = f" [{n}/{limit}]" + ("  ⚠ OVER" if n > limit else "")
        else:
            flag = f" [{n} chars]"
        lines.append(f"## {f.field}{flag}")
        lines.append("")
        lines.append(f.text)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def touch(field: ProfileField) -> None:
    field.updated_at = datetime.now(timezone.utc)


def split_license(spec: str) -> tuple[str, str | None]:
    """`position:20` → ("position", "20"); a bare word → (word, None)."""
    kind, _, ident = spec.partition(":")
    return kind.strip(), (ident.strip() or None)


_WORD = re.compile(r"[a-z0-9]+")


def claim_appears_in(claim_text: str, field_text: str) -> bool:
    """Loose containment — a claim recorded against a field it is no longer in
    is its own kind of drift."""
    words = _WORD.findall(claim_text.lower())
    hay = field_text.lower()
    return bool(words) and all(w in hay for w in words[:6])
