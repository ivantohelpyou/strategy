"""What must never be in this repo, checked rather than remembered (st-lhu).

The repo is public; the store it manages is not. The two keep leaking into each
other the same way: a close reason, a gate's text or a Vikunja task gets quoted
into a docstring or a fixture as evidence, and it carries a real task id, a
patent number or a person's name with it. A scrub before the flip fixes the tree
once. This makes it a property that stays true.

Three kinds of residue:

* **Vikunja task ids.** Real ids point into a private task list. Examples and
  fixtures use the placeholders in :data:`PLACEHOLDER_TASK_IDS` — add to that
  set rather than borrowing a real id because it was handy.
* **Patent application numbers**, by shape.
* **Private terms**: names, counterparties, anything that cannot be matched by
  shape. They live in a file OUTSIDE the repo (:func:`private_terms_path`),
  because a list of names to keep out of a public repo cannot itself be in it.
  Absent that file, the check is skipped, not passed — the caller says so.

`scan_tree` reads the tracked files. `scan_history` reads every commit's added
lines and message, because making a repo public publishes its history too, and a
term removed from HEAD is still one `git log -p` away.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

PLACEHOLDER_TASK_IDS = frozenset({"0000", "0001", "0002", "0003", "1234", "1235"})

# Two spellings. Named: "vikunja:#N", "Vikunja #N", "Vikunja task #N", any
# length. Bare: a four-digit "#N" — real task ids are four digits and store
# position ids are not, so a bare "#112" is a position and passes. The
# lookbehind keeps HTML entities ("&#8212;") and anchors ("x#1234") out.
TASK_ID = re.compile(r"vikunja\W{0,2}(?:task\s*)?#(\d+)|(?<![\w&])#(\d{4})\b", re.IGNORECASE)

# Application numbers by shape: US provisional 63/NNN,NNN and GB NNNNNNN.
PATENT = re.compile(r"\b6[0-3]/\d{3},\d{3}\b|\bGB ?\d{7}\b")


@dataclass(frozen=True)
class Finding:
    where: str  # path:line, or commit:path for history
    kind: str  # task-id | patent | private-term
    match: str

    def __str__(self) -> str:
        return f"{self.where}: {self.kind} {self.match!r}"


def private_terms_path() -> Path:
    return Path(
        os.environ.get("STRATEGY_PRIVATE_TERMS", "~/.strategy/private-terms.txt")
    ).expanduser()


def load_private_terms(path: Path | None = None) -> list[str] | None:
    """One term per line, `#` comments. None when the file does not exist."""
    path = path or private_terms_path()
    if not path.exists():
        return None
    terms = []
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            terms.append(line)
    return terms


def scan_text(
    text: str, where: str, terms: list[str] | None = None, numbered: bool = True
) -> list[Finding]:
    """Findings in one text. `where` gets `:lineno` added when the text is a whole file."""
    lowered_terms = [(t, t.lower()) for t in terms or ()]
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        loc = f"{where}:{n}" if numbered else where
        for m in TASK_ID.finditer(line):
            if (m.group(1) or m.group(2)) not in PLACEHOLDER_TASK_IDS:
                out.append(Finding(loc, "task-id", m.group(0)))
        for m in PATENT.finditer(line):
            out.append(Finding(loc, "patent", m.group(0)))
        low = line.lower()
        for term, t in lowered_terms:
            if t in low:
                out.append(Finding(loc, "private-term", term))
    return out


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout


def scan_tree(root: Path = REPO_ROOT, terms: list[str] | None = None) -> list[Finding]:
    out = []
    for rel in _git(root, "ls-files", "-z").split("\0"):
        if not rel:
            continue
        p = root / rel
        try:
            text = p.read_text()
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue  # binary, or deleted in the working tree
        out.extend(scan_text(text, rel, terms))
    return out


def _added_lines(diff: str) -> dict[str, list[str]]:
    """Added lines of one commit's patch, by the path they were added to.

    Header lines are only read between `diff --git` and the first `@@`, so an
    added line that happens to begin `++ ` is content, not a file header.
    """
    by_path: dict[str, list[str]] = {}
    current: list[str] = []
    in_header = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            in_header = True
            current = by_path.setdefault(line.rsplit(" b/", 1)[-1], [])
        elif in_header:
            if line.startswith("+++ b/"):
                current = by_path.setdefault(line[6:], [])
            elif line.startswith("@@"):
                in_header = False
        elif line.startswith("+"):
            current.append(line[1:])
    return {path: lines for path, lines in by_path.items() if lines}


def scan_history(root: Path = REPO_ROOT, terms: list[str] | None = None) -> list[Finding]:
    """Every commit's message and added lines, on every ref.

    A line that survives many commits is added once, so each finding is the
    commit that introduced it.
    """
    out = []
    for commit in _git(root, "rev-list", "--all").split():
        short = commit[:7]
        message = _git(root, "log", "-1", "--format=%B", commit)
        out.extend(scan_text(message, f"{short}:message", terms, numbered=False))
        diff = _git(root, "show", "--format=", "--no-color", "--diff-merges=first-parent", commit)
        for path, added in _added_lines(diff).items():
            out.extend(scan_text("\n".join(added), f"{short}:{path}", terms, numbered=False))
    return out
