import os
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import declarative_base, sessionmaker
from app.config import settings

_is_sqlite = settings.DATABASE_URL.startswith("sqlite")

if _is_sqlite:
    # Make sure the folder for a file-based SQLite DB exists (e.g. ./data/meetings.db).
    _db_path = settings.DATABASE_URL.split("///", 1)[-1]
    if _db_path and _db_path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(_db_path)), exist_ok=True)

# timeout=30 keeps SQLite waiting on a locked file instead of failing instantly
engine = create_engine(
    settings.DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 30} if _is_sqlite else {},
    pool_pre_ping=not _is_sqlite,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Create tables and add any columns missing from an older schema (lightweight migration)."""
    from app import models  # noqa: F401  (register models on Base)

    Base.metadata.create_all(bind=engine)

    inspector = inspect(engine)
    for table in Base.metadata.sorted_tables:
        existing = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            col_type = column.type.compile(dialect=engine.dialect)
            try:
                with engine.begin() as conn:
                    conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN "{column.name}" {col_type}'))
            except OperationalError as e:
                if "locked" in str(e).lower():
                    raise RuntimeError(
                        f"Database '{settings.DATABASE_URL}' is locked by another program "
                        "(e.g. DB Browser for SQLite with unsaved changes). Close it or click "
                        "'Write Changes'/'Close Database', then restart the server."
                    ) from None
                raise
