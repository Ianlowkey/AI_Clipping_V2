from fastapi.testclient import TestClient

from app.main import app


def test_health_endpoint():
    with TestClient(app) as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_create_and_list_project(tmp_path, monkeypatch):
    from app.core import config

    monkeypatch.setattr(config.settings, "database_path", tmp_path / "database" / "aiclipping.db")
    with TestClient(app) as client:
        created = client.post("/api/projects", json={
            "name": "  Demo project  ",
            "description": "Local test project",
            "source_video_name": "sample.mp4",
        })
        listed = client.get("/api/projects")
        clips = client.get("/api/clips")
        jobs = client.get("/api/jobs")

    assert created.status_code == 201
    assert created.json()["name"] == "Demo project"
    assert created.json()["description"] == "Local test project"
    assert created.json()["source_videos"][0]["source_uri"] == "sample.mp4"
    assert len(listed.json()) == 1
    assert clips.json() == []
    assert jobs.json() == []


def test_project_creation_rejects_blank_name_and_two_sources(tmp_path, monkeypatch):
    from app.core import config

    monkeypatch.setattr(config.settings, "database_path", tmp_path / "database" / "aiclipping.db")
    with TestClient(app) as client:
        blank = client.post("/api/projects", json={"name": "   "})
        both = client.post("/api/projects", json={
            "name": "Project", "video_url": "https://example.com/video", "source_video_name": "video.mp4",
        })
    assert blank.status_code == 422
    assert both.status_code == 422
