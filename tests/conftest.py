"""Test isolation: no test may touch the hosted store or the real ~/.env.

The store's own bug (sea-mii7.3) was a second, invisible resolution path to a
different database. A suite that inherits the developer's environment has the
same shape — one forgotten `monkeypatch.setenv` and a test writes to the live
Postgres — so the default here is a throwaway SQLite file and an empty .env
search path, and anything that wants otherwise has to say so.
"""

import pytest

from strategy_store import db, env


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(env, "ENV_FILES", ())
    monkeypatch.setenv("STRATEGY_DB_URL", f"sqlite:///{tmp_path / 'isolated.db'}")
    db.reset()
    yield
    db.reset()


@pytest.fixture(autouse=True)
def no_hosted_store():
    """Belt and braces: fail loudly rather than open a remote database.

    Checks the engine that was actually built, not what would resolve — a test
    may legitimately point resolution at a fake Postgres URL to prove the
    unreachable-store path without ever connecting."""
    yield
    if db._engine is not None:
        url = str(db._engine.url)
        assert url.startswith("sqlite:"), f"a test opened a non-SQLite store: {env.mask(url)}"


@pytest.fixture
def store():
    """The db module, tables created, on this test's throwaway SQLite file."""
    db.init_db()
    return db
