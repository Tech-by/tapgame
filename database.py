# database.py
from sqlmodel import SQLModel, create_engine, Session

# The database file will be created in your project folder
sqlite_file_name = "tapgame.db"
sqlite_url = f"sqlite:///{sqlite_file_name}"

# check_same_thread is only needed for SQLite
connect_args = {"check_same_thread": False}
engine = create_engine(sqlite_url, connect_args=connect_args)

def create_db_and_tables():
    """Creates the database tables based on our models."""
    SQLModel.metadata.create_all(engine)

def get_session():
    """Provides a database session for each request."""
    with Session(engine) as session:
        yield session
