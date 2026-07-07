import os
from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

# Configurable via the MYTUBE_DB_PATH environment variable so this isn't
# tied to any specific server's directory layout. Defaults to a local
# "data" folder next to the application for easy out-of-the-box setup.
DB_PATH = os.environ.get(
    "MYTUBE_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "mytube.db")
)
DB_PATH = os.path.abspath(DB_PATH)

os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False}
)

@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()

    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA synchronous=NORMAL;")
    cursor.execute("PRAGMA foreign_keys=ON;")

    cursor.close()

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)

Base = declarative_base()

def get_db():
    db = SessionLocal()

    try:
        yield db
    finally:
        db.close()