import os
import shutil

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import main
from app.core import config
from app.database import session as database
from app.database.models import (
    Clip,
    ClipMetadata,
    HighlightCandidate,
    ProcessingJob,
    ProcessingLog,
    Project,
    SourceVideo,
    Transcript,
)
from app.storage import project_directory


@pytest.fixture
def client(tmp_path, monkeypatch):
    data_dir = tmp_path / "AIClippingData"
    monkeypatch.setattr(config.settings, "data_dir", data_dir)
    monkeypatch.setattr(config.settings, "database_path", data_dir / "database" / "aiclipping.db")
    with TestClient(main.app) as test_client:
        yield test_client


def create_project(client, name="Deletion test"):
    response = client.post("/api/projects", json={"name": name})
    assert response.status_code == 201
    return response.json()["id"]


def test_delete_project_removes_only_its_files_and_related_rows(client, tmp_path):
    project_id = create_project(client)
    other_id = create_project(client, "Keep this project")
    project_dir = project_directory(project_id)
    other_dir = project_directory(other_id)
    (project_dir / "source" / "video.mp4").write_bytes(b"video")
    other_marker = other_dir / "source" / "keep.mp4"
    other_marker.write_bytes(b"preserve")

    with database.SessionLocal() as db:
        db.add(SourceVideo(project_id=project_id, source_type="local_file", source_uri="local", local_path=str(project_dir / "source" / "video.mp4")))
        db.add(Transcript(project_id=project_id, text_path="text", srt_path="srt", words_path="words"))
        candidate = HighlightCandidate(project_id=project_id, start_seconds=0, end_seconds=1, score=1)
        job = ProcessingJob(project_id=project_id, job_type="transcription", status="completed")
        db.add_all([candidate, job])
        db.flush()
        clip = Clip(project_id=project_id, candidate_id=candidate.id, file_path="clip")
        db.add(clip)
        db.flush()
        db.add(ClipMetadata(clip_id=clip.id, platform="test"))
        db.add(ProcessingLog(job_id=job.id, level="info", message="complete"))
        db.commit()

    response = client.delete(f"/api/projects/{project_id}")

    assert response.status_code == 200
    assert response.json() == {"deleted": True, "cleanup_pending": False}
    assert not project_dir.exists()
    assert other_marker.read_bytes() == b"preserve"
    with database.SessionLocal() as db:
        assert db.get(Project, project_id) is None
        assert db.scalar(select(func.count()).select_from(SourceVideo).where(SourceVideo.project_id == project_id)) == 0
        assert db.scalar(select(func.count()).select_from(Transcript).where(Transcript.project_id == project_id)) == 0
        assert db.scalar(select(func.count()).select_from(ProcessingJob).where(ProcessingJob.project_id == project_id)) == 0
        assert db.scalar(select(func.count()).select_from(ProcessingLog)) == 0
        assert db.scalar(select(func.count()).select_from(Clip).where(Clip.project_id == project_id)) == 0
        assert db.scalar(select(func.count()).select_from(ClipMetadata)) == 0
        assert db.scalar(select(func.count()).select_from(HighlightCandidate).where(HighlightCandidate.project_id == project_id)) == 0


def test_delete_project_handles_missing_storage_directory(client):
    project_id = create_project(client)
    shutil.rmtree(project_directory(project_id))
    assert client.delete(f"/api/projects/{project_id}").status_code == 200
    assert client.get(f"/api/projects/{project_id}").status_code == 404


@pytest.mark.parametrize("project_id", ["not-a-uuid", "../outside", "..%2F..%2Foutside"])
def test_delete_rejects_invalid_and_traversal_ids_without_touching_files(client, tmp_path, project_id):
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    marker = outside / "keep.txt"
    marker.write_text("preserve", encoding="utf-8")

    response = client.delete(f"/api/projects/{project_id}")

    assert response.status_code in (404, 422)
    assert marker.read_text(encoding="utf-8") == "preserve"


def test_delete_rejects_symlink_storage_escape(client, tmp_path):
    project_id = create_project(client)
    project_dir = project_directory(project_id)
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("preserve", encoding="utf-8")
    shutil.rmtree(project_dir)
    try:
        os.symlink(outside, project_dir, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")

    response = client.delete(f"/api/projects/{project_id}")

    assert response.status_code == 422
    assert marker.read_text(encoding="utf-8") == "preserve"
    assert client.get(f"/api/projects/{project_id}").status_code == 200


def test_delete_refuses_active_jobs_and_keeps_project_files(client):
    project_id = create_project(client)
    marker = project_directory(project_id) / "source" / "keep.mp4"
    marker.write_bytes(b"preserve")
    with database.SessionLocal() as db:
        db.add(ProcessingJob(project_id=project_id, job_type="transcription", status="processing", active_project_id=project_id))
        db.commit()

    response = client.delete(f"/api/projects/{project_id}")

    assert response.status_code == 409
    assert "active processing job" in response.json()["detail"]
    assert marker.read_bytes() == b"preserve"
    assert client.get(f"/api/projects/{project_id}").status_code == 200


def test_delete_unknown_project_returns_not_found(client):
    response = client.delete("/api/projects/9d7ebf00-8a79-44b5-b6d5-ec370b8b6a82")
    assert response.status_code == 404


def test_browser_cors_preflight_allows_project_delete(client):
    response = client.options("/api/projects/9d7ebf00-8a79-44b5-b6d5-ec370b8b6a82", headers={
        "Origin": "http://127.0.0.1:1420",
        "Access-Control-Request-Method": "DELETE",
    })
    assert response.status_code == 200
    assert "DELETE" in response.headers["access-control-allow-methods"]


def test_database_failure_restores_staged_project_storage(client, monkeypatch):
    project_id = create_project(client)
    marker = project_directory(project_id) / "source" / "keep.mp4"
    marker.write_bytes(b"preserve")
    monkeypatch.setattr(main, "delete", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("simulated database failure")))

    response = client.delete(f"/api/projects/{project_id}")

    assert response.status_code == 500
    assert marker.read_bytes() == b"preserve"
    assert client.get(f"/api/projects/{project_id}").status_code == 200


def test_startup_recovers_staged_deletion_for_existing_and_deleted_projects(client, tmp_path):
    project_id = create_project(client)
    root = project_directory(project_id).parent
    project_dir = project_directory(project_id)
    existing_stage = root / f".deleting-{project_id}-{'a' * 32}"
    os.replace(project_dir, existing_stage)

    deleted_id = "12c2ea30-94f8-42c5-97f9-b4366843f921"
    deleted_stage = root / f".deleting-{deleted_id}-{'b' * 32}"
    deleted_stage.mkdir()
    (deleted_stage / "stale.txt").write_text("staged", encoding="utf-8")

    main.recover_project_deletion_staging()

    assert project_dir.is_dir()
    assert not existing_stage.exists()
    assert not deleted_stage.exists()
