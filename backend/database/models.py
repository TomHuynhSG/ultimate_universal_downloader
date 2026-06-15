import os
from sqlalchemy import create_engine, Column, Integer, String, Float, Boolean, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import datetime

# Create a local sqlite database
db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "downloads.db")
engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

class DownloadTask(Base):
    __tablename__ = "download_tasks"

    id = Column(String, primary_key=True, index=True)
    url = Column(String, index=True)
    title = Column(String)
    thumbnail = Column(String, nullable=True)
    status = Column(String, default="pending")  # pending, downloading, completed, error
    progress = Column(Float, default=0.0)
    details = Column(String, default="{}")
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

Base.metadata.create_all(bind=engine)
