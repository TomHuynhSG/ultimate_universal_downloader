import datetime

from sqlalchemy import Column, DateTime, Float, String, create_engine, event, text
from sqlalchemy.orm import declarative_base, sessionmaker

from backend.core.paths import PROJECT_ROOT


db_path = PROJECT_ROOT / "downloads.db"
engine = create_engine(
    f"sqlite:///{db_path}",
    connect_args={"check_same_thread": False, "timeout": 30},
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, expire_on_commit=False, bind=engine)


@event.listens_for(engine, "connect")
def _configure_sqlite(dbapi_connection, _connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


Base = declarative_base()


class DownloadTask(Base):
    __tablename__ = "download_tasks"

    id = Column(String, primary_key=True, index=True)
    url = Column(String, index=True)
    title = Column(String)
    thumbnail = Column(String, nullable=True)
    status = Column(String, default="pending")
    progress = Column(Float, default=0.0)
    details = Column(String, default="{}")
    output_path = Column(String, nullable=True)
    created_at = Column(
        DateTime,
        default=lambda: datetime.datetime.now(datetime.UTC).replace(tzinfo=None),
        index=True,
    )
    updated_at = Column(
        DateTime,
        default=lambda: datetime.datetime.now(datetime.UTC).replace(tzinfo=None),
        onupdate=lambda: datetime.datetime.now(datetime.UTC).replace(tzinfo=None),
    )


def initialize_database():
    """Create the schema and apply lightweight migrations for existing databases."""
    Base.metadata.create_all(bind=engine)
    with engine.begin() as connection:
        columns = {row[1] for row in connection.execute(text("PRAGMA table_info(download_tasks)"))}
        if "output_path" not in columns:
            connection.execute(text("ALTER TABLE download_tasks ADD COLUMN output_path VARCHAR"))
        if "updated_at" not in columns:
            connection.execute(text("ALTER TABLE download_tasks ADD COLUMN updated_at DATETIME"))
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_download_tasks_status_created "
                "ON download_tasks(status, created_at DESC)"
            )
        )


initialize_database()
