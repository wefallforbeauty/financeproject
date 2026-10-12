"""Engine, sessions, schema creation and seed data."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from financeproject import __version__
from financeproject.core.kinds import EQUITY, EXPENSE, INCOME
from financeproject.errors import FinanceError
from financeproject.ledger.models import Account, Base, Commodity, Meta

SCHEMA_VERSION = "2"
"""1: version 00 (Turkish account names). 2: English account names and kinds."""

DEFAULT_COMMODITIES = (
    # code, name, kind, decimals, price source
    ("TRY", "Turkish lira", "currency", 2, "fixed"),
    ("USD", "US dollar", "currency", 2, "tcmb"),
    ("EUR", "Euro", "currency", 2, "tcmb"),
    ("XAU_G", "Gold, gram (24 carat, fine)", "gold", 4, "gold"),
)

OPENING_ACCOUNT = "Equity:Opening Balances"
TRADING_ROOT = "Equity:Trading"
UNCATEGORIZED_EXPENSE = "Expenses:Uncategorized"
UNCATEGORIZED_INCOME = "Income:Uncategorized"

DEFAULT_ACCOUNTS = (
    (OPENING_ACCOUNT, EQUITY),
    (UNCATEGORIZED_EXPENSE, EXPENSE),
    (UNCATEGORIZED_INCOME, INCOME),
)


def make_engine(path: Path | str, echo: bool = False) -> Engine:
    """SQLite engine with foreign keys enforced. ``":memory:"`` for tests."""
    if str(path) == ":memory:":
        url = "sqlite+pysqlite:///:memory:"
    else:
        p = Path(path).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite+pysqlite:///{p}"
    engine = create_engine(url, echo=echo)

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    return engine


def init_db(engine: Engine) -> None:
    """Create the tables and seed data. Safe to run again."""
    Base.metadata.create_all(engine)
    with session_scope(engine) as s:
        if s.get(Meta, "schema_version") is None:
            s.add(Meta(key="schema_version", value=SCHEMA_VERSION))
            s.add(Meta(key="created_with", value=__version__))
            s.add(Meta(key="created_at", value=datetime.now().isoformat(timespec="seconds")))
        for code, name, kind, decimals, source in DEFAULT_COMMODITIES:
            if s.get(Commodity, code) is None:
                s.add(Commodity(code=code, name=name, kind=kind, decimals=decimals, price_source=source))
        s.flush()
        existing = set(s.scalars(select(Account.path)))
        for path, account_type in DEFAULT_ACCOUNTS:
            if path not in existing:
                s.add(Account(path=path, type=account_type))


def is_initialised(engine: Engine) -> bool:
    with engine.connect() as conn:
        rows = conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table' AND name='meta'")
        return rows.first() is not None


def schema_version(engine: Engine) -> str | None:
    with session_scope(engine) as s:
        meta = s.get(Meta, "schema_version")
        return meta.value if meta else None


def open_ledger(engine: Engine) -> None:
    """Check that an existing ledger can be used, and add tables that newer versions need."""
    version = schema_version(engine)
    if version != SCHEMA_VERSION:
        raise FinanceError(
            f"This ledger has schema version {version}, this program needs {SCHEMA_VERSION}. "
            "Ledgers made by version 00 (Turkish account names) cannot be opened; create a new one."
        )
    Base.metadata.create_all(engine)


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    """Commit on success, roll back on any error."""
    factory = sessionmaker(engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def backup(db_path: Path, target_dir: Path) -> Path:
    """Consistent copy through SQLite's online backup API."""
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = target_dir / f"{db_path.stem}-{stamp}.sqlite"
    with closing(sqlite3.connect(db_path)) as src, closing(sqlite3.connect(target)) as dst:
        src.backup(dst)
    target.chmod(0o600)
    return target
