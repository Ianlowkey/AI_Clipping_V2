import subprocess
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.storage import PROJECT_DIRECTORIES


def make_video(tmp_path: Path, settings) -> Path:
    ffmpeg = settings.resolve_ffmpeg()
    if not ffmpeg:
        pytest.skip("FFmpeg is not installed")
    path = tmp_path / "sample.mp4"
    result = subprocess.run(
        [ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=64x48:d=1", "-c:v", "mpeg4", "-y", str(path)],
        capture_output=True, timeout=30,
    )
    if result.returncode:
        pytest.skip("Local FFmpeg cannot generate the test video")
    return path


def setup_test_data(tmp_path, monkeypatch):
    from app.core import config

    monkeypatch.setattr(config.settings, "data_dir", tmp_path / "AIClippingData")
    monkeypatch.setattr(config.settings, "database_path", tmp_path / "AIClippingData" / "database" / "aiclipping.db")
    monkeypatch.setattr(config.settings, "max_upload_size_bytes", 20 * 1024 * 1024)


def create_project(client):
    response = client.post("/api/projects", json={"name": "Upload test"})
    assert response.status_code == 201
    return response.json()["id"]


def test_video_upload_prepares_and_stores_file(tmp_path, monkeypatch):
    from app.core import config

    setup_test_data(tmp_path, monkeypatch)
    video = make_video(tmp_path, config.settings)
    with TestClient(app) as client:
        project_id = create_project(client)
        response = client.post(f"/api/projects/{project_id}/source-video", files={"file": ("..\\..\\sample video.mp4", video.read_bytes(), "video/mp4")})
        second = client.post(f"/api/projects/{project_id}/source-video", files={"file": ("sample.mp4", video.read_bytes(), "video/mp4")})
        projects = client.get("/api/projects").json()

    assert response.status_code == 201, response.text
    result = response.json()
    stored = Path(result["source_video"]["local_path"])
    assert stored.is_file() and stored.read_bytes() == video.read_bytes()
    assert result["preparation"]["readable"] is True
    assert result["preparation"]["width"] == 64
    assert result["preparation"]["height"] == 48
    assert result["preparation"]["audio_available"] is False
    assert projects[0]["source_videos"][0]["metadata_json"]
    assert second.status_code == 201
    assert Path(second.json()["source_video"]["local_path"]).name != stored.name
    assert ".." not in stored.name and "\\" not in stored.name
    project_root = stored.parent.parent
    assert {path.name for path in project_root.iterdir()} == set(PROJECT_DIRECTORIES)


def test_upload_rejects_invalid_extension_and_missing_project(tmp_path, monkeypatch):
    setup_test_data(tmp_path, monkeypatch)
    with TestClient(app) as client:
        project_id = create_project(client)
        invalid = client.post(f"/api/projects/{project_id}/source-video", files={"file": ("notes.txt", b"hello", "text/plain")})
        missing = client.post(f"/api/projects/{project_id}/source-video")
        no_project = client.post("/api/projects/not-a-project/source-video", files={"file": ("clip.mp4", b"x", "video/mp4")})
        from app.core import config
        monkeypatch.setattr(config.settings, "max_upload_size_bytes", 2)
        too_large = client.post(f"/api/projects/{project_id}/source-video", files={"file": ("clip.mp4", b"123", "video/mp4")})
    assert invalid.status_code == 415
    assert missing.status_code == 422
    assert no_project.status_code == 404
    assert too_large.status_code == 413


def test_upload_reports_ffmpeg_preparation_failure_without_storing(tmp_path, monkeypatch):
    from app import main

    setup_test_data(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "prepare_video", lambda _: (_ for _ in ()).throw(main.VideoPreparationError("Test preparation failure")))
    with TestClient(app) as client:
        project_id = create_project(client)
        failed = client.post(f"/api/projects/{project_id}/source-video", files={"file": ("broken.mp4", b"not a video", "video/mp4")})
        project = client.get(f"/api/projects/{project_id}").json()
    assert failed.status_code == 422
    assert "Test preparation failure" in failed.json()["detail"]
    assert project["source_videos"] == []
    assert not list((tmp_path / "AIClippingData" / "projects" / project_id / "source").glob("*"))


def test_database_migration_preserves_legacy_project_records(tmp_path, monkeypatch):
    from app.core import config
    from app.database.session import create_database
    from sqlalchemy import text

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    new_root = tmp_path / "Local" / "AIClippingData"
    old_db = tmp_path / "Local" / "AIClipping" / "database" / "aiclipping.db"
    old_db.parent.mkdir(parents=True)
    with sqlite3.connect(old_db) as connection:
        connection.executescript("""
          CREATE TABLE projects (id VARCHAR(36) PRIMARY KEY, name VARCHAR(200) NOT NULL, description TEXT, status VARCHAR(32) NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL);
          CREATE TABLE source_videos (id VARCHAR(36) PRIMARY KEY, project_id VARCHAR(36) NOT NULL, source_type VARCHAR(32) NOT NULL, source_uri TEXT NOT NULL, local_path TEXT, title VARCHAR(500), duration_seconds FLOAT, created_at DATETIME NOT NULL);
          INSERT INTO projects VALUES ('legacy-id', 'Keep me', 'Existing row', 'created', '2025-01-01', '2025-01-01');
          INSERT INTO source_videos VALUES ('legacy-source', 'legacy-id', 'url', 'https://example.test/video', NULL, 'Legacy source', NULL, '2025-01-01');
        """)
    monkeypatch.setattr(config.settings, "data_dir", new_root)
    monkeypatch.setattr(config.settings, "database_path", new_root / "database" / "aiclipping.db")
    engine = create_database()
    with engine.connect() as connection:
        project = connection.execute(text("SELECT name, description FROM projects WHERE id='legacy-id'")).one()
        source = connection.execute(text("SELECT source_uri, metadata_json FROM source_videos WHERE id='legacy-source'")).one()
    assert project == ("Keep me", "Existing row")
    assert source == ("https://example.test/video", None)
    assert old_db.exists()
