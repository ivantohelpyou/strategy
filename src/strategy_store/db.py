"""Engine/session factory. Default is a local SQLite file — no new paid
infrastructure while cash flow is negative (sea-mii7). STRATEGY_DB_URL points at
the hosted store (a schema on factumerit-db since 2026-08-20, solutions-6zs);
resolution goes through env.resolve so a bare shell, a sourced shell and a
session hook all land on the same database (sea-mii7.3)."""

from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from . import env
from .models import Base

DEFAULT_PATH = Path.home() / ".strategy" / "store.db"
ENV_FILE = env.ENV_FILES[0]  # kept for callers that referenced it


class StoreUnreachable(RuntimeError):
    """A store was configured and could not be opened.

    Raised instead of quietly using the local SQLite copy: a stale answer the
    caller cannot distinguish from a live one is the failure this store exists
    to surface, so it must not be the failure mode of the store itself."""


def store() -> env.Resolved:
    """The configured store URL and where the setting came from."""
    r = env.resolve("STRATEGY_DB_URL")
    if r.value:
        return env.Resolved(r.value.replace("postgres://", "postgresql://", 1), r.origin)
    DEFAULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    return env.Resolved(f"sqlite:///{DEFAULT_PATH}", "default")


def db_url() -> str:
    return store().value


_engine = None
_Session = None


def engine():
    global _engine, _Session
    if _engine is None:
        cfg = store()
        eng = create_engine(cfg.value, future=True)
        try:
            with eng.connect():
                pass
        except SQLAlchemyError as exc:
            eng.dispose()
            raise StoreUnreachable(
                f"cannot open the strategy store at {env.mask(cfg.value)}\n"
                f"  configured by: {cfg.origin}\n"
                f"  {type(exc).__name__}: {str(exc).splitlines()[0][:160]}\n"
                f"  not falling back to {DEFAULT_PATH} — that copy would answer "
                f"confidently and be wrong."
            ) from exc
        _engine = eng
        _Session = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def reset() -> None:
    """Forget the cached engine — for tests and for `doctor` re-checks."""
    global _engine, _Session
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _Session = None


def session() -> Session:
    engine()
    return _Session()


def init_db() -> None:
    """Create missing tables, then add any columns the models have grown since.

    The store has no migration tool; additive column changes are the only kind
    we make, and ALTER TABLE ADD COLUMN covers them (idempotent — only columns
    absent from the live table are added)."""
    eng = engine()
    Base.metadata.create_all(eng)
    insp = inspect(eng)
    with eng.begin() as conn:
        for table in Base.metadata.sorted_tables:
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                ddl = f"ALTER TABLE {table.name} ADD COLUMN {col.name} {col.type.compile(eng.dialect)}"
                if col.server_default is not None:
                    ddl += f" DEFAULT {col.server_default.arg}"
                conn.execute(text(ddl))
