import logging
import os
import shutil
from pathlib import Path
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.core.config import default_data_dir, settings

logger = logging.getLogger(__name__)

class Base(DeclarativeBase):
    pass


engine: Engine | None = None
SessionLocal: sessionmaker | None = None


def create_database() -> Engine:
    from app.database import models  # noqa: F401

    global engine, SessionLocal

    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    # Keep Phase 2A records available while adopting the documented data-root name.
    legacy_root = Path(os.environ["LOCALAPPDATA"]) / "AIClipping" if os.environ.get("LOCALAPPDATA") else Path.home() / ".aiclipping"
    legacy_database = legacy_root / "database" / "aiclipping.db"
    if settings.data_dir == default_data_dir() and not settings.database_path.exists() and legacy_database.is_file():
        logger.info("Preserving existing AIClipping database at the new data root")
        shutil.copy2(legacy_database, settings.database_path)
    engine = create_engine(
        f"sqlite:///{settings.database_path}",
        connect_args={"check_same_thread": False},
    )
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
    # create_all does not add columns to existing SQLite tables.
    columns = {column["name"] for column in inspect(engine).get_columns("projects")}
    if "description" not in columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE projects ADD COLUMN description TEXT"))
    source_columns = {column["name"] for column in inspect(engine).get_columns("source_videos")}
    if "metadata_json" not in source_columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE source_videos ADD COLUMN metadata_json TEXT"))
    return engine
