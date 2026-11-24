from app.database.session import create_database
from app.database.models import HighlightCandidate, Project
from app.database import session as database
from sqlalchemy import inspect, text


def test_database_creation(tmp_path, monkeypatch):
    from app.core import config

    data_dir = tmp_path / "data"
    monkeypatch.setattr(config.settings, "data_dir", data_dir)
    monkeypatch.setattr(config.settings, "database_path", data_dir / "database" / "aiclipping.db")
    engine = create_database()

    assert engine is not None
    assert (data_dir / "database" / "aiclipping.db").exists()
    candidate_fields = {column["name"] for column in inspect(engine).get_columns("highlight_candidates")}
    assert {"selected_for_render", "speaker_context", "conversational_engagement"} <= candidate_fields
    clip_fields = {column["name"] for column in inspect(engine).get_columns("clips")}
    job_fields = {column["name"] for column in inspect(engine).get_columns("processing_jobs")}
    assert {"output_profile", "file_size_bytes", "width", "height"} <= clip_fields
    assert {"output_profile", "candidate_ids_json"} <= job_fields
    project = Project(name="Legacy approval migration")
    with database.SessionLocal() as db:
        db.add(project)
        db.flush()
        candidate = HighlightCandidate(
            project_id=project.id,
            start_seconds=20,
            end_seconds=50,
            score=80,
            approved=True,
            review_status="suggested",
        )
        db.add(candidate)
        db.commit()
        candidate_id = candidate.id
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE highlight_candidates DROP COLUMN selected_for_render"))
        connection.execute(text("ALTER TABLE highlight_candidates DROP COLUMN review_status"))
        connection.execute(text("ALTER TABLE highlight_candidates DROP COLUMN speaker_context"))
        connection.execute(text("ALTER TABLE highlight_candidates DROP COLUMN conversational_engagement"))
        connection.execute(text("ALTER TABLE processing_jobs DROP COLUMN content_profile"))

    upgraded = create_database()
    candidate_fields = {column["name"] for column in inspect(upgraded).get_columns("highlight_candidates")}
    assert {"selected_for_render", "speaker_context", "conversational_engagement"} <= candidate_fields
    assert "content_profile" in {column["name"] for column in inspect(upgraded).get_columns("processing_jobs")}
    with upgraded.connect() as connection:
        legacy_candidate = connection.execute(
            text("SELECT approved, review_status, selected_for_render FROM highlight_candidates WHERE id = :id"),
            {"id": candidate_id},
        ).one()
    assert legacy_candidate == (1, "approved", 0)
