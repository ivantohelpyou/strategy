"""sea-mii7.3 — one resolution path, and a store that says what it is reading.

The bug: `strategy X` and `set -a; . ~/.env; set +a; strategy X` were two
different programs. Five keys read os.environ only, so a bare shell (which is
what the session hook runs in) seeded no Vikunja projects and wrote the JSONL
backup somewhere else — silently, while `prime` still reported "synced <1h ago"
because it looked at the freshest source instead of the stalest.
"""

from datetime import datetime, timedelta, timezone

import pytest
from click.testing import CliRunner

from strategy_store import cli, db, env, prime, seed
from strategy_store.models import Position

# Every key that has ever been read from the environment. The parity test below
# is the guard: add a key here when you add one to the code.
KEYS = (
    "STRATEGY_DB_URL",
    "STRATEGY_VIKUNJA_PROJECTS",
    "STRATEGY_EXPORT_PATH",
    "STRATEGY_DOWNLOADS_DIR",
    "STRATEGY_FORCING_SEED",
    "STRATEGY_SEED_MAP",
    "GT_ROOT",
)


@pytest.fixture
def envfile(tmp_path, monkeypatch):
    """A stand-in ~/.env, in the shape the real one has (export, quotes, $HOME)."""
    f = tmp_path / ".env"
    f.write_text(
        "# comment\n"
        "export STRATEGY_DB_URL=postgresql://u:p@example.invalid/db?sslmode=require\n"
        'export STRATEGY_VIKUNJA_PROJECTS="381:2026 Business Goals,375:Exec"\n'
        'export STRATEGY_EXPORT_PATH="$HOME/gt/solutions/positions.jsonl"\n'
        "STRATEGY_DOWNLOADS_DIR=/mnt/c/Downloads\n"
        "EMPTY=\n"
    )
    monkeypatch.setattr(env, "ENV_FILES", (f,))
    for k in KEYS:
        monkeypatch.delenv(k, raising=False)
    return f


def _source_the_envfile(f, monkeypatch):
    """What `set -a; . ~/.env; set +a` does to the process environment."""
    import os
    import re

    for line in f.read_text().splitlines():
        m = re.match(r"^\s*(?:export\s+)?(\w+)=(.*)$", line)
        if m and m.group(2).strip():
            monkeypatch.setenv(m.group(1), os.path.expandvars(m.group(2).strip().strip("'\"")))


def test_bare_and_sourced_shells_resolve_identically(envfile, monkeypatch):
    """The repro from the bead, as an assertion."""
    bare = {k: env.env_value(k) for k in KEYS}
    _source_the_envfile(envfile, monkeypatch)
    sourced = {k: env.env_value(k) for k in KEYS}
    assert bare == sourced
    # ...and not by both being empty: the file's keys actually resolved.
    assert bare["STRATEGY_VIKUNJA_PROJECTS"] == "381:2026 Business Goals,375:Exec"


def test_the_keys_that_had_no_env_file_fallback(envfile):
    """Each of these read os.environ only before the fix; a bare shell got
    nothing and carried on as if that were the configuration."""
    assert seed._vikunja_projects() == {381: "2026 Business Goals", 375: "Exec"}
    assert str(env.path_value("STRATEGY_EXPORT_PATH", "/unused")).endswith(
        "gt/solutions/positions.jsonl"
    )
    assert env.env_value("STRATEGY_DOWNLOADS_DIR") == "/mnt/c/Downloads"


def test_process_environment_wins_over_the_file(envfile, monkeypatch):
    monkeypatch.setenv("STRATEGY_DOWNLOADS_DIR", "/from/shell")
    assert env.env_value("STRATEGY_DOWNLOADS_DIR") == "/from/shell"
    assert env.resolve("STRATEGY_DOWNLOADS_DIR").origin == "env"
    assert env.resolve("STRATEGY_VIKUNJA_PROJECTS").origin == str(envfile)


def test_home_is_expanded_the_way_a_shell_would(envfile):
    from pathlib import Path

    assert str(Path.home()) in env.env_value("STRATEGY_EXPORT_PATH")
    assert "$HOME" not in env.env_value("STRATEGY_EXPORT_PATH")


def test_blank_and_missing_keys_report_their_origin(envfile):
    assert env.resolve("EMPTY").value is None
    assert env.resolve("NOT_SET").origin == "unset"
    assert env.resolve("NOT_SET", "fallback") == env.Resolved("fallback", "default")


# --- hq-f8za: the town-wide credentials file composes its URLs from parts --------
# `export STRATEGY_DB_URL="${FOLLOWWHEE_DB_URL}"` so that rotating one password
# edits one line. os.path.expandvars resolved that against os.environ only, so a
# `strategy` run from any shell that had not sourced ~/.env got the literal text
# `${FOLLOWWHEE_DB_URL}` as its connection string — reported from qrcards as "the
# store is down", and a whole rig fell back to a two-week-old export.


@pytest.fixture
def composed_envfile(tmp_path, monkeypatch):
    """~/.env in the shape the consolidation left it: parts, then URLs built
    from the parts, then one key that is nothing but a reference to another."""
    f = tmp_path / ".env"
    f.write_text(
        "export SHARED_PG_HOST=db.example.invalid\n"
        "export SHARED_PG_SSLMODE=require\n"
        "export SHARED_PG_PASSWORD_FOLLOWWHEE=s3cret\n"
        "export FOLLOWWHEE_DB_URL="
        "postgresql://fw:${SHARED_PG_PASSWORD_FOLLOWWHEE}@${SHARED_PG_HOST}"
        "/matrix?sslmode=${SHARED_PG_SSLMODE}\n"
        'export STRATEGY_DB_URL="${FOLLOWWHEE_DB_URL}"\n'
    )
    monkeypatch.setattr(env, "ENV_FILES", (f,))
    for k in (*KEYS, "FOLLOWWHEE_DB_URL", "SHARED_PG_HOST"):
        monkeypatch.delenv(k, raising=False)
    return f


def test_a_reference_to_another_key_in_the_same_file_resolves(composed_envfile):
    """The repro from the bead. No shell sourced anything; the CLI still has to
    read what a shell would have read."""
    url = env.env_value("STRATEGY_DB_URL")
    assert "${" not in url, "the literal reference reached the connection string"
    assert url == "postgresql://fw:s3cret@db.example.invalid/matrix?sslmode=require"


def test_the_store_is_the_composed_url_not_the_reference(composed_envfile):
    """db.store() is what actually gets dialled, and it must never be handed a
    URL with a `${...}` in it — nor silently fall back to the local SQLite."""
    assert db.store().value.startswith("postgresql://fw:")
    assert db.store().origin == str(composed_envfile)


def test_bare_and_sourced_shells_agree_on_a_composed_url(composed_envfile, monkeypatch):
    """`set -a; . ~/.env; set +a; strategy ...` and bare `strategy ...` are one
    program — the property sea-mii7.3 established, now for composed values."""
    bare = env.env_value("STRATEGY_DB_URL")
    import subprocess

    sourced = subprocess.run(
        ["bash", "-c", f'set -a; . "{composed_envfile}"; set +a; printf %s "$STRATEGY_DB_URL"'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert bare == sourced


def test_a_reference_reaches_a_part_defined_in_the_other_env_file(tmp_path, monkeypatch):
    """Two files, read as one: the qrcards .env predates ~/.env and may compose
    its values from the shared parts. Read separately, those resolve to empty."""
    home = tmp_path / "home.env"
    home.write_text("export SHARED_PG_HOST=db.example.invalid\n")
    repo = tmp_path / "repo.env"
    repo.write_text("export STRATEGY_DOWNLOADS_DIR=/d/${SHARED_PG_HOST}\n")
    monkeypatch.setattr(env, "ENV_FILES", (home, repo))
    monkeypatch.delenv("STRATEGY_DOWNLOADS_DIR", raising=False)
    assert env.env_value("STRATEGY_DOWNLOADS_DIR") == "/d/db.example.invalid"


def test_the_first_file_still_wins_for_a_key_both_define(tmp_path, monkeypatch):
    """Precedence did not move: ENV_FILES order decides, and doctor names the
    file that actually won."""
    home = tmp_path / "home.env"
    home.write_text("export STRATEGY_DOWNLOADS_DIR=/from/home\n")
    repo = tmp_path / "repo.env"
    repo.write_text("export STRATEGY_DOWNLOADS_DIR=/from/repo\n")
    monkeypatch.setattr(env, "ENV_FILES", (home, repo))
    monkeypatch.delenv("STRATEGY_DOWNLOADS_DIR", raising=False)
    assert env.resolve("STRATEGY_DOWNLOADS_DIR") == env.Resolved("/from/home", str(home))


@pytest.fixture(autouse=True)
def _forget_warnings():
    """The stderr line is emitted once per message per process; tests are many
    processes' worth of first times."""
    env._WARNED.clear()
    yield
    env._WARNED.clear()


def test_an_unresolvable_reference_is_not_half_a_connection_string(tmp_path, monkeypatch):
    """Nothing defines the part. The answer is "unset" — which db.py reports as
    an unconfigured store — not a URL with a hole in it that dials somewhere."""
    f = tmp_path / ".env"
    f.write_text("export STRATEGY_VIKUNJA_PROJECTS=${NOTHING_DEFINES_THIS}\n")
    monkeypatch.setattr(env, "ENV_FILES", (f,))
    monkeypatch.delenv("STRATEGY_VIKUNJA_PROJECTS", raising=False)
    monkeypatch.delenv("NOTHING_DEFINES_THIS", raising=False)
    assert env.resolve("STRATEGY_VIKUNJA_PROJECTS").origin == "unset"


def test_a_missing_part_inside_a_url_drops_the_key_and_says_so(tmp_path, monkeypatch, capsys):
    """The review finding: dotenv substitutes an unresolvable ${VAR} with '',
    so the value stays truthy and the URL loses only its host — which then
    dials the local socket and reports a Postgres that is not the store."""
    f = tmp_path / ".env"
    f.write_text(
        "export SHARED_PG_PASSWORD_FOLLOWWHEE=s3cret\n"
        "export STRATEGY_DB_URL="
        "postgresql://fw:${SHARED_PG_PASSWORD_FOLLOWWHEE}@${SHARED_PG_HOST}/matrix\n"
    )
    monkeypatch.setattr(env, "ENV_FILES", (f,))
    for k in ("STRATEGY_DB_URL", "SHARED_PG_HOST"):
        monkeypatch.delenv(k, raising=False)

    r = env.resolve("STRATEGY_DB_URL")
    assert r.value is None and r.origin == "unset", "a URL with no host was returned"

    message = capsys.readouterr().err
    assert "STRATEGY_DB_URL" in message
    assert str(f) in message
    assert "SHARED_PG_HOST" in message
    assert "s3cret" not in message, "the message must not print the parts it did resolve"


def test_doctor_names_the_dropped_key(tmp_path, monkeypatch):
    """Absent and never-set look identical in `doctor`'s settings block, and the
    difference between them is usually a typo in one part name."""
    f = tmp_path / ".env"
    f.write_text("export STRATEGY_DOWNLOADS_DIR=/d/${SHARED_PG_HOSTT}\n")
    monkeypatch.setattr(env, "ENV_FILES", (f,))
    monkeypatch.delenv("STRATEGY_DOWNLOADS_DIR", raising=False)
    monkeypatch.setenv("STRATEGY_DB_URL", f"sqlite:///{tmp_path / 'd.db'}")
    out = CliRunner().invoke(cli.main, ["doctor"]).output
    assert "STRATEGY_DOWNLOADS_DIR" in out and "SHARED_PG_HOSTT" in out
    assert "key dropped" in out


def test_a_reference_with_a_default_is_not_a_missing_part(tmp_path, monkeypatch):
    """`${X:-y}` is the author saying what to do when X is absent."""
    f = tmp_path / ".env"
    f.write_text("export STRATEGY_DOWNLOADS_DIR=${NOT_DEFINED_ANYWHERE:-/fallback}\n")
    monkeypatch.setattr(env, "ENV_FILES", (f,))
    monkeypatch.delenv("STRATEGY_DOWNLOADS_DIR", raising=False)
    assert env.env_value("STRATEGY_DOWNLOADS_DIR") == "/fallback"


def test_quoting_does_not_protect_a_literal_dollar_brace(tmp_path, monkeypatch):
    r"""Pinned, not fixed (hq-f8za review finding 2). python-dotenv interpolates
    inside single quotes and does not honour `\${` as an escape, so a secret
    containing `${` cannot be written in these files at all — it has to come
    from the process environment, which wins over every file. If this test ever
    fails, python-dotenv changed and the README paragraph is now wrong."""
    f = tmp_path / ".env"
    f.write_text(
        "export A='lit ${NOT_DEFINED_ANYWHERE} end'\n"
        "export B=esc \\${NOT_DEFINED_ANYWHERE} end\n"
    )
    monkeypatch.setattr(env, "ENV_FILES", (f,))
    monkeypatch.delenv("NOT_DEFINED_ANYWHERE", raising=False)
    # Both are dropped as unresolved references rather than kept as literals:
    # there is no in-file spelling that survives.
    assert env.resolve("A").origin == "unset"
    assert env.resolve("B").origin == "unset"
    # And the escape really is not honoured — the reference is eaten, not kept.
    import io as _io

    from dotenv import dotenv_values

    assert dotenv_values(stream=_io.StringIO("B=esc \\${NOT_DEFINED_ANYWHERE} end\n")) == {
        "B": "esc \\ end"
    }
    # The escape hatch that does work: the process environment.
    monkeypatch.setenv("A", "lit ${NOT_DEFINED_ANYWHERE} end")
    assert env.resolve("A") == env.Resolved("lit ${NOT_DEFINED_ANYWHERE} end", "env")


def test_an_edited_env_file_is_picked_up(envfile):
    assert env.env_value("STRATEGY_DOWNLOADS_DIR") == "/mnt/c/Downloads"
    envfile.write_text("export STRATEGY_DOWNLOADS_DIR=/elsewhere\n")
    assert env.env_value("STRATEGY_DOWNLOADS_DIR") == "/elsewhere"


def test_a_configured_store_that_is_unreachable_fails_loudly(envfile, monkeypatch):
    """Never the local SQLite copy in disguise: a stale answer the caller cannot
    tell from a live one is the failure this store exists to surface.

    The connection is faked rather than dialled — the assertion is about what
    the store does with a refusal, not about how fast a socket refuses."""
    from sqlalchemy.exc import OperationalError

    monkeypatch.setenv("STRATEGY_DB_URL", "postgresql://u:p@db.example.invalid/nope")

    class Refusing:
        def connect(self):
            raise OperationalError("connect", {}, Exception("could not connect to server"))

        def dispose(self):
            pass

    monkeypatch.setattr(db, "create_engine", lambda *a, **k: Refusing())
    db.reset()
    with pytest.raises(db.StoreUnreachable) as e:
        db.engine()
    msg = str(e.value)
    assert "not falling back" in msg
    assert str(db.DEFAULT_PATH) in msg
    assert ":p@" not in msg and "***" in msg  # the password is not printed
    assert db._engine is None  # and nothing was left cached to be used anyway


def test_doctor_names_the_store_its_origin_and_the_stale_source(
    tmp_path, store, envfile, monkeypatch
):
    # envfile's STRATEGY_DB_URL points at a Postgres that must never be dialled;
    # pin resolution back to this test's SQLite file.
    monkeypatch.setenv("STRATEGY_DB_URL", f"sqlite:///{tmp_path / 'isolated.db'}")
    now = datetime.now(timezone.utc)
    with store.session() as s:
        s.add(
            Position(
                title="fresh", source_system="beads", source_ref="b1", scope="single-rig",
                synced_at=now,
            )
        )
        s.add(
            Position(
                title="old", source_system="vikunja", source_ref="#1", scope="web-chat",
                synced_at=now - timedelta(hours=90),
            )
        )
        s.commit()
    out = CliRunner().invoke(cli.main, ["doctor"]).output
    assert "sqlite:" in out
    assert "STRATEGY_VIKUNJA_PROJECTS" in out
    assert "vikunja" in out and "STALE" in out
    assert "sources disagree" in out


def test_prime_reports_the_stalest_source_not_the_freshest(store):
    """`synced <1h ago` while 26 Vikunja rows sat at 85h is how this hid for
    three days — the daily reseed was refreshing beads only."""
    now = datetime.now(timezone.utc)
    rows = [
        Position(title="a", source_system="beads", source_ref="b1", scope="single-rig",
                 synced_at=now),
        Position(title="b", source_system="vikunja", source_ref="#1", scope="web-chat",
                 synced_at=now - timedelta(hours=85)),
        Position(title="c", source_system="manual", source_ref="m1", scope="fleet"),
    ]
    text, stale = prime.freshness(rows)
    assert text == "synced 3d ago (vikunja)"
    assert prime.sync_ages(rows).keys() == {"beads", "vikunja"}  # manual has no upstream
    text, stale = prime.freshness([r for r in rows if r.source_system != "vikunja"])
    assert text == "synced <1h ago" and not stale


def test_reseed_triggers_on_the_stalest_source(store, monkeypatch):
    """A fresh beads sync used to hold the whole reseed below the threshold."""
    now = datetime.now(timezone.utc)
    with store.session() as s:
        s.add(Position(title="a", source_system="beads", source_ref="b1", scope="single-rig",
                       synced_at=now))
        s.add(Position(title="b", source_system="vikunja", source_ref="#1", scope="web-chat",
                       synced_at=now - timedelta(hours=85)))
        s.commit()
    monkeypatch.setattr(prime.db, "session", store.session)
    monkeypatch.setenv("STRATEGY_VIKUNJA_PROJECTS", "381:Goals")
    called = []
    monkeypatch.setattr(seed, "fetch_vikunja", lambda: called.append("v") or [])
    monkeypatch.setattr(seed, "fetch_beads", lambda: called.append("b") or [])
    note = prime.maybe_reseed(24)
    assert called == ["v", "b"], "the 85h Vikunja rows must force the reseed"
    assert "reseeded" in note


# --- Found while verifying the above: the block also has to be the same block
# --- twice. An unordered SELECT returns Postgres heap order, which a reseed
# --- reshuffles, so `prime` was quietly printing a different three of eight
# --- where-to-play positions after every refresh.


def _box(store, key, n, status="resolved", start=0):
    from datetime import date

    with store.session() as s:
        for i in range(start, start + n):
            s.add(
                Position(
                    title=f"{key} row {i}",
                    element_key=key,
                    status=status,
                    scope="fleet",
                    source_system="manual",
                    source_ref=f"{key}-{i}",
                    decided_on=date(2026, 8, 1 + i),
                )
            )
        s.commit()


def test_where_to_play_is_the_same_three_every_time_and_says_what_it_cut(store):
    _box(store, "ptw.box2", 8)
    first = prime.render(None)
    second = prime.render(None)
    assert first == second
    assert first.count("Where to play:") == 3
    assert "+5 more where-to-play" in first
    # Newest decided first — the three shown are not an accident of row order.
    assert "ptw.box2 row 7" in first and "ptw.box2 row 0" not in first


def test_aspiration_is_the_stable_row_not_the_latest_proposal(store):
    """Box 1 holds the aspiration and the proposals circling it; the aspiration
    is the row that was there first, and the rest are counted, not dropped."""
    _box(store, "ptw.box1", 1)
    _box(store, "ptw.box1", 3, status="contested", start=1)
    text = prime.render(None)
    assert "Aspiration: ptw.box1 row 0" in text
    assert "(+3 more in box 1, 3 contested" in text
