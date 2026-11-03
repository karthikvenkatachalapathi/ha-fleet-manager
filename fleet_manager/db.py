from __future__ import annotations
from fastapi import Request
from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from .settings import DATABASE_URL

connect_args = {'check_same_thread': False, 'timeout': 30} if DATABASE_URL.startswith('sqlite') else {}
engine = create_engine(DATABASE_URL, future=True, connect_args=connect_args)

if DATABASE_URL.startswith('sqlite'):
    @event.listens_for(engine, 'connect')
    def _set_sqlite_pragmas(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute('PRAGMA journal_mode=WAL')
        cursor.execute('PRAGMA busy_timeout=30000')
        cursor.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

class Base(DeclarativeBase):
    pass

def get_db(request: Request = None):
    db = SessionLocal()
    try:
        principal=getattr(request.state,'api_principal',None) if request else None
        if principal is not None:
            from .services.api_policy import bind_scope
            bind_scope(db,principal)
        yield db
    finally:
        db.close()
