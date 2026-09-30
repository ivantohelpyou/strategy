"""The coverage map: every CLI command, and where (if anywhere) it is surfaced.

The list is walked out of the click group at runtime rather than typed here, so
a command added to the CLI cannot quietly go missing from the map — it shows up
as `unclassified`, which is the finding. Only the two judgments click cannot
supply are hand-held below: does the command WRITE, and which page shows it.

Writes are deliberately absent from the web surface. Position #67: the
invariants that protect judgment (the supersession scope rule, store-owned
columns a sync must not overwrite, a claim being contested when its members
disagree) live in the command handlers, and a second writer must call them
rather than re-implement them. Until that extraction happens, read-only is the
honest shape.
"""

from __future__ import annotations

from dataclasses import dataclass

import click


@dataclass(frozen=True)
class Command:
    name: str
    what: str
    writes: bool
    surface: str = ""


# name -> (writes, surface). A read command with no surface is a gap worth
# seeing; a write command with no surface is the design.
CLASSIFY: dict[str, tuple[bool, str]] = {
    "init": (True, ""),
    "seed": (True, ""),
    "seed-forcing": (True, ""),
    "list": (False, "/positions"),
    "show": (False, "/positions"),
    "revisions": (False, "/positions"),
    "clarity": (False, "/positions"),
    "set": (True, ""),
    "gate add": (True, ""),
    "gate edit": (True, ""),
    "gate resolve": (True, ""),
    "gate reopen": (True, ""),
    "gate link": (True, ""),
    "gate unlink": (True, ""),
    # A read, so it needs a page: /gates carries the same three-way split in its
    # "satisfied by" column. The CLI's --check reads the rigs and the page does
    # not — a page request must not shell out to `bd` once per gate.
    "gate satisfiers": (False, "/gates"),
    "add": (True, ""),
    "pending": (False, "/pending"),
    "adopt": (True, "/triage"),
    "bless": (True, ""),
    "return": (True, ""),
    "supersede": (True, ""),
    "evidence add": (True, ""),
    "evidence list": (False, "/positions"),
    "reconcile": (False, "/drift"),
    "claim": (True, ""),
    "assess": (True, "/matrix"),
    "forcing": (False, "/gates"),
    "gate": (True, ""),
    "report": (False, "/report"),
    "prime": (False, "/"),
    "doctor": (False, "/store"),
    "export": (True, ""),
    "import": (True, ""),
    "web": (False, "/"),
    "reject": (True, "/triage"),
    "triage": (True, "/triage"),
    "factor": (True, "/triage"),
    "ask": (True, "/triage"),
    "answer": (True, ""),
    "questions": (False, "/questions"),
    "profile set": (True, ""),
    "profile claim": (True, ""),
    "profile list": (False, "/profile"),
    "profile export": (False, "/profile"),
    "profile check": (False, "/profile"),
    "metric add": (True, ""),
    "metric read": (True, ""),
    "metric pull": (True, ""),
    "metric list": (False, "/metrics"),
    "drop log": (True, ""),
    "drop ingest": (True, ""),
    "drop stats": (True, ""),
    "drop note": (True, ""),
    "drop backfill-ids": (True, ""),
    "cascade create": (True, ""),
    "cascade add": (True, ""),
    "cascade rm": (True, ""),
    "cascade promote": (True, ""),
    "cascade list": (False, "/cascades"),
    "cascade show": (False, "/cascades"),
    "cascade shared": (False, "/cascades"),
    "drop list": (False, "/drops"),
    "drop funnel": (False, "/funnel"),
}


def _walk(group: click.Group, prefix: str = "") -> list[Command]:
    out: list[Command] = []
    ctx = click.Context(group)
    for name in sorted(group.list_commands(ctx)):
        c = group.get_command(ctx, name)
        if c is None:
            continue
        full = f"{prefix}{name}"
        if isinstance(c, click.Group):
            out += _walk(c, prefix=f"{full} ")
            continue
        writes, surface = CLASSIFY.get(full, (False, ""))
        what = (c.get_short_help_str(limit=110) or "").strip()
        if full not in CLASSIFY:
            what = f"UNCLASSIFIED — {what}"
        out.append(Command(name=f"strategy {full}", what=what, writes=writes, surface=surface))
    return out


def _load() -> list[Command]:
    from ..cli import main

    return _walk(main)


class _Lazy(list):
    """Built on first read: importing the CLI at module import time would drag
    click's whole command tree into every web request path."""

    def _fill(self):
        if not list.__len__(self):
            self.extend(_load())
        return self

    def __iter__(self):
        return list.__iter__(self._fill())

    def __len__(self):
        return list.__len__(self._fill())

    def __getitem__(self, i):
        return list.__getitem__(self._fill(), i)


COMMANDS = _Lazy()
