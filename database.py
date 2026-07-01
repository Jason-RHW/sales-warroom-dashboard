"""
Database connection setup.

Defaults to a local SQLite file for development/testing without any external
service. To point this at Supabase (or any Postgres instance) later, just set
the DATABASE_URL environment variable, e.g.:

    DATABASE_URL=postgresql://postgres:<password>@<project>.supabase.co:5432/postgres

No code changes required elsewhere - SQLAlchemy handles both dialects.
"""
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.environ.get("DATABASE_URL") or "sqlite:///./sales_command_center.db"

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Create tables if they don't exist. Safe to call on every startup."""
    Base.metadata.create_all(bind=engine)
