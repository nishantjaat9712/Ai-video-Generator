from __future__ import annotations

import os
from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from .config import DATA_DIR

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///" + (DATA_DIR / "storyorbit.db").as_posix())
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False, "timeout": 30} if DATABASE_URL.startswith("sqlite") else {}, pool_pre_ping=True)
if DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def set_sqlite_pragmas(connection, record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

class Base(DeclarativeBase):
    pass

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

def init_db():
    from . import models
    Base.metadata.create_all(engine)

def get_db():
    with SessionLocal() as session:
        yield session
