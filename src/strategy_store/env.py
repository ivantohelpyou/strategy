r"""One place where configuration is resolved (sea-mii7.3).

Before this module there were three separate .env parsers and five keys that
read ``os.environ`` only, so ``strategy ...`` and
``set -a; . ~/.env; set +a; strategy ...`` were two different programs: the bare
shell seeded no Vikunja projects at all and wrote the JSONL backup to a
different file, both without a word. Precedence is process environment first,
then the .env files below, then the caller's default — and :func:`resolve`
reports which one won, so ``strategy doctor`` can show it instead of leaving the
caller to guess.

Values that come from a file are read the way a shell that sourced it would
read them: ``~/.env`` holds lines like ``export STRATEGY_EXPORT_PATH="$HOME/gt/..."``
and, since the town-wide credentials consolidation, lines like
``export STRATEGY_DB_URL="${FOLLOWWHEE_DB_URL}"`` — one URL composed from parts
defined further up the same file so that rotating a password edits one line.
``os.path.expandvars`` resolved neither of those, because the parts are defined
in the file and not in ``os.environ``, so a bare ``strategy`` read the literal
text ``${FOLLOWWHEE_DB_URL}`` as its connection string and the store was
unreachable from any shell that had not sourced ``~/.env`` (hq-f8za).

python-dotenv does that resolution without executing the file, and the files are
resolved as one stream so a reference in either can reach a part defined in the
other — the same read qrcards' ``scripts/workshop.py`` does.

Two consequences worth knowing before editing a .env file:

* **Quoting does not protect ``${``.** python-dotenv (1.2.3) interpolates inside
  single quotes as well as double, and does not honour ``\${`` as an escape — it
  drops the backslash and eats the reference anyway. There is therefore no way to
  write a literal ``${`` in these files. A secret that contains one has to reach
  the process some other way: export it in the shell, where the process
  environment wins over every file. ``test_env_resolution.py`` pins that
  behaviour so a change in python-dotenv is noticed rather than discovered.
* **A reference nothing defines drops the key**, with one line on stderr naming
  the key, the file and the reference (``strategy doctor`` prints the same line).
  dotenv substitutes an unresolvable ``${VAR}`` with the empty string, so
  ``postgresql://u:p@${SHARED_PG_HOST}/db`` with no such part defined comes back
  as a URL with no host — truthy, plausible, and it dials the local socket. Half
  a connection string is the failure this module exists to prevent.
"""

from __future__ import annotations

import io
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

# ~/.env is the town-wide file; the qrcards one predates it and still holds the
# analytics credentials metrics.py needs. First file that carries the key wins.
ENV_FILES: tuple[Path, ...] = (
    Path.home() / ".env",
    Path.home() / "gt" / "qrcards" / "crew" / "ivan" / ".env",
)


@dataclass(frozen=True)
class Resolved:
    """A value and where it came from — the origin is the point of this module."""

    value: str | None
    origin: str  # "env" | "<path to .env>" | "default" | "unset"

    def __bool__(self) -> bool:
        return bool(self.value)


#: ``${NAME}``, ``${NAME:-default}`` (python-dotenv) and the bare ``$NAME`` that
#: the :func:`os.path.expandvars` pass below still handles.
_REF = re.compile(r"\$\{(?P<braced>[^{}:]*)(?P<default>:-[^{}]*)?\}|\$(?P<bare>\w+)")

#: Every dropped-key line already written to stderr. The files are re-read on
#: every lookup by design, and one message per problem is the useful number.
_WARNED: set[str] = set()


def _read(path: Path) -> str | None:
    try:
        return path.read_text()
    except OSError:
        return None


def _missing_refs(raw: str, defined: set[str]) -> list[str]:
    """The names `raw` references that nothing defines.

    A reference written with a default (``${NAME:-x}``) is not missing — the
    default is the author saying what to do when the part is absent."""
    out = []
    for m in _REF.finditer(raw):
        if m.group("default") is not None:
            continue
        name = m.group("braced") if m.group("braced") is not None else m.group("bare")
        if name and name not in defined and name not in out:
            out.append(name)
    return out


def _warn(message: str) -> None:
    """One line, once, on stderr. Silence is what made this a two-week outage."""
    if message not in _WARNED:
        _WARNED.add(message)
        print(message, file=sys.stderr)


def _parse(path: Path, context: str = "", problems: list[str] | None = None) -> dict[str, str]:
    """The keys `path` defines, with ``${...}`` references already resolved.

    `context` is the other .env files' text, parsed ahead of this one so a
    reference can reach a part they define; `path` comes last, so its own lines
    win for its own keys and the caller still learns which file defined what.

    A key whose raw value references something nothing defines is dropped and
    reported, so it falls through to the next file or the default. Checking the
    raw text rather than the result is the point: dotenv substitutes the empty
    string, which leaves a plausible-looking URL with a hole where the host was.

    Deliberately uncached: these files are a couple of KB, and a cache keyed on
    mtime returns a stale answer on any filesystem whose timestamps are coarser
    than the edit that just happened — which is the class of bug this module
    exists to end."""
    text = _read(path)
    if text is None:
        return {}
    stream = context + "\n" + text
    raw = dotenv_values(stream=io.StringIO(text), interpolate=False)
    resolved = dotenv_values(stream=io.StringIO(stream))
    everything = dotenv_values(stream=io.StringIO(stream), interpolate=False)
    defined = {k for k, v in everything.items() if v} | set(os.environ)

    out: dict[str, str] = {}
    for key, text_of in raw.items():
        if not text_of:
            continue  # a deliberately empty line, not an unresolved reference
        missing = _missing_refs(text_of, defined)
        if missing:
            message = (
                f"env: {key} in {path} references undefined "
                f"{', '.join('$' + n for n in missing)}; key dropped"
            )
            _warn(message)
            if problems is not None:
                problems.append(message)
            continue
        # python-dotenv resolves ``${VAR}`` only; ~/.env also writes the bare
        # ``$HOME/gt/...`` form, which a shell would have expanded.
        value = os.path.expandvars(resolved.get(key) or "")
        if value:
            out[key] = value
    return out


def _context(path: Path) -> str:
    others = (_read(p) for p in ENV_FILES if p != path)
    return "\n".join(t for t in others if t is not None)


def _file_values(path: Path) -> dict[str, str]:
    """One file's keys, resolved against every other file in :data:`ENV_FILES`."""
    return _parse(path, _context(path))


def problems() -> list[str]:
    """Every key dropped for a reference nothing defines, one line each.

    `strategy doctor` prints these: a key that silently is not there looks
    exactly like a key that was never set, and the difference is a typo."""
    found: list[str] = []
    for f in ENV_FILES:
        _parse(f, _context(f), found)
    return found


def resolve(key: str, default: str | None = None) -> Resolved:
    """Look `key` up once, the same way for every caller."""
    if os.environ.get(key):
        return Resolved(os.environ[key], "env")
    for f in ENV_FILES:
        raw = _file_values(f).get(key)
        if raw:
            return Resolved(os.path.expanduser(raw), str(f))
    return Resolved(default, "default" if default is not None else "unset")


def env_value(key: str, default: str | None = None) -> str | None:
    return resolve(key, default).value


def path_value(key: str, default: Path | str) -> Path:
    """Same resolution, returned as an expanded Path (defaults get expanded too)."""
    r = resolve(key)
    return Path(os.path.expanduser(os.path.expandvars(r.value or str(default))))


def mask(url: str | None) -> str:
    """A connection string safe to print: the password becomes ***."""
    if not url:
        return "(none)"
    return re.sub(r"(://[^:/@]+):[^@]*@", r"\1:***@", url)
