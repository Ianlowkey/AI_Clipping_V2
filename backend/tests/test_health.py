from fastapi.testclient import TestClient

from app.main import app


def test_health_endpoint():
    with TestClient(app) as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_create_and_list_project(tmp_path, monkeypatch):
    from app.core import config

    data_dir = tmp_path / "AIClippingData"
    monkeypatch.setattr(config.settings, "data_dir", data_dir)
    monkeypatch.setattr(config.settings, "database_path", data_dir / "database" / "aiclipping.db")
    with TestClient(app) as client:
        created = client.post("/api/projects", json={
            "name": "  Demo project  ",
            "description": "Local test project",
        })
        listed = client.get("/api/projects")
        clips = client.get("/api/clips")
        jobs = client.get("/api/jobs")

    assert created.status_code == 201
    assert created.json()["name"] == "Demo project"
    assert created.json()["description"] == "Local test project"
    assert created.json()["source_videos"] == []
    assert len(listed.json()) == 1
    assert clips.json() == []
    assert jobs.json() == []


def test_project_creation_rejects_blank_name(tmp_path, monkeypatch):
    from app.core import config

    data_dir = tmp_path / "AIClippingData"
    monkeypatch.setattr(config.settings, "data_dir", data_dir)
    monkeypatch.setattr(config.settings, "database_path", data_dir / "database" / "aiclipping.db")
    with TestClient(app) as client:
        blank = client.post("/api/projects", json={"name": "   "})
    assert blank.status_code == 422
