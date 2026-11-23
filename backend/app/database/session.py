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
    job_columns = {column["name"] for column in inspect(engine).get_columns("processing_jobs")}
    job_migrations = {
        "job_type": "VARCHAR(32) NOT NULL DEFAULT 'generic'",
        "stage": "VARCHAR(40)",
        "active_project_id": "VARCHAR(36)",
        "model_name": "VARCHAR(100)",
        "device": "VARCHAR(32)",
        "compute_type": "VARCHAR(32)",
        "language": "VARCHAR(16)",
        "started_at": "DATETIME",
        "completed_at": "DATETIME",
    }
    transcript_columns = {column["name"] for column in inspect(engine).get_columns("transcripts")}
    candidate_columns = {column["name"] for column in inspect(engine).get_columns("highlight_candidates")}
    candidate_migrations = {
        "analysis_job_id": "VARCHAR(36)", "transcript_id": "VARCHAR(36)", "duration_seconds": "FLOAT",
        "excerpt": "TEXT", "title": "VARCHAR(300)", "hook": "TEXT", "category": "VARCHAR(40)",
        "selection_explanation": "TEXT", "hook_strength": "INTEGER", "engagement_potential": "INTEGER",
        "standalone_clarity": "INTEGER", "emotional_value": "INTEGER",
        "review_status": "VARCHAR(20) NOT NULL DEFAULT 'suggested'", "validation_warnings": "TEXT",
        "selected_for_render": "BOOLEAN NOT NULL DEFAULT 0",
        "created_at": "DATETIME",
    }
    transcript_migrations = {
        "words_path": "TEXT",
        "source_filename": "VARCHAR(500)",
        "model_name": "VARCHAR(100)",
        "device": "VARCHAR(32)",
        "compute_type": "VARCHAR(32)",
        "language_probability": "FLOAT",
        "duration_seconds": "FLOAT",
    }
    with engine.begin() as connection:
        for name, column_type in job_migrations.items():
            if name not in job_columns:
                connection.execute(text(f"ALTER TABLE processing_jobs ADD COLUMN {name} {column_type}"))
        for name, column_type in transcript_migrations.items():
            if name not in transcript_columns:
                connection.execute(text(f"ALTER TABLE transcripts ADD COLUMN {name} {column_type}"))
        for name, column_type in candidate_migrations.items():
            if name not in candidate_columns:
                connection.execute(text(f"ALTER TABLE highlight_candidates ADD COLUMN {name} {column_type}"))
        connection.execute(text("UPDATE highlight_candidates SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_highlight_candidates_analysis_job_id ON highlight_candidates(analysis_job_id)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_highlight_candidates_transcript_id ON highlight_candidates(transcript_id)"))
        connection.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_processing_active_transcription "
            "ON processing_jobs(active_project_id) WHERE active_project_id IS NOT NULL"
        ))
    return engine
