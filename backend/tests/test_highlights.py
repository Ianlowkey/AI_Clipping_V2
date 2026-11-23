import json
import sys
import types
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import highlight_detection
from app.database import session as database
from app.database.models import Project, Transcript
from app.main import app


def _words_payload(duration=120):
    words = []
    for index in range(duration):
        token = f"thought{index}"
        if index in (19, 49, 79, 109):
            token += "."
        words.append({"start": float(index), "end": float(index + 1), "text": f" {token}"})
    return {"duration_seconds": duration, "words": words, "segments": []}


def _setup(tmp_path, monkeypatch, *, transcript=True, key="test-key"):
    from app.core import config
    data = tmp_path / "data"
    monkeypatch.setattr(config.settings, "data_dir", data)
    monkeypatch.setattr(config.settings, "database_path", data / "database" / "app.db")
    monkeypatch.setattr(config.settings, "gemini_api_key", key)
    client = TestClient(app)
    client.__enter__()
    response = client.post("/api/projects", json={"name": "Highlights"})
    project_id = response.json()["id"]
    if transcript:
        directory = data / "projects" / project_id / "transcript"
        directory.mkdir(parents=True, exist_ok=True)
        payload = _words_payload()
        (directory / "transcript.txt").write_text("transcript", encoding="utf-8")
        (directory / "transcript.srt").write_text("srt", encoding="utf-8")
        words_file = directory / "words.json"
        words_file.write_text(json.dumps(payload), encoding="utf-8")
        with database.SessionLocal() as db:
            db.add(Transcript(project_id=project_id, text_path=str(directory / "transcript.txt"),
                srt_path=str(directory / "transcript.srt"), words_path=str(words_file), duration_seconds=120))
            db.commit()
    return client, project_id


def _candidate():
    from app.highlight_detection import GeminiCandidate
    passage = " ".join(f"thought{i}" for i in range(20, 50))
    return GeminiCandidate(start_seconds=20, end_seconds=50, passage=passage,
        title="A clear idea", hook="Here is the thought", category="insight", explanation="Self-contained explanation.",
        overall_score=80, hook_strength=75, engagement_potential=80, standalone_clarity=90, emotional_value=60)


def _wait(client, project_id, job_id):
    for _ in range(100):
        job = client.get(f"/api/projects/{project_id}/highlights/jobs/{job_id}").json()
        if job["status"] in ("completed", "failed"):
            return job
        time.sleep(0.03)
    raise AssertionError("highlight job did not finish")


def test_missing_key_transcript_and_invalid_words(tmp_path, monkeypatch):
    client, project = _setup(tmp_path, monkeypatch, key=None)
    assert client.get("/api/settings/gemini").json() == {
        "configured": False, "model": "gemini-3.6-flash", "key_environment_variable": "AICLIPPING_GEMINI_API_KEY"
    }
    assert client.post(f"/api/projects/{project}/highlights").status_code == 422
    client.__exit__(None, None, None)
    client, project = _setup(tmp_path / "missing", monkeypatch, transcript=False)
    assert client.post(f"/api/projects/{project}/highlights").status_code == 422
    client.__exit__(None, None, None)
    client, project = _setup(tmp_path / "invalid", monkeypatch)
    with database.SessionLocal() as db:
        transcript = db.query(Transcript).filter_by(project_id=project).one()
        Path(transcript.words_path).write_text('{"words":[{"text":"bad","start":4,"end":3}]}', encoding="utf-8")
    assert client.post(f"/api/projects/{project}/highlights").status_code == 422
    client.__exit__(None, None, None)


def test_alignment_rejects_bad_times_and_duration_and_snaps_boundaries():
    from app.highlight_detection import GeminiCandidate, HighlightError, _align, _validate_words
    words = _validate_words(_words_payload(), 120)
    aligned = _align(_candidate(), words, 120)
    assert aligned["start"] == 20 and aligned["end"] == 50
    assert "thought20" in aligned["excerpt"]
    corrected = _align(_candidate().model_copy(update={"start_seconds": -10, "end_seconds": 150}), words, 120)
    assert corrected["start"] == 20 and corrected["end"] == 50
    short = _candidate().model_copy(update={"start_seconds": 40, "end_seconds": 50,
        "passage": " ".join(f"thought{i}" for i in range(40, 50))})
    with pytest.raises(HighlightError, match="20–60 seconds"):
        _align(short, words, 120)
    malformed = _candidate().model_dump()
    malformed["overall_score"] = 101
    with pytest.raises(Exception):
        GeminiCandidate.model_validate(malformed)


def test_gemini_malformed_response_and_api_error_are_actionable(monkeypatch):
    from app.core.config import settings
    from app.highlight_detection import HighlightError, analyze_with_gemini
    monkeypatch.setattr(settings, "gemini_api_key", "mock-key")
    class FakeClient:
        def __init__(self, error=None):
            self.models = types.SimpleNamespace(generate_content=lambda **_kwargs: (_ for _ in ()).throw(error) if error else types.SimpleNamespace(text="not json"))
        def close(self): pass
    module = types.ModuleType("google.genai")
    module.Client = lambda **_kwargs: FakeClient()
    module.types = types.SimpleNamespace(HttpOptions=lambda **kwargs: kwargs, GenerateContentConfig=lambda **kwargs: kwargs)
    monkeypatch.setitem(sys.modules, "google.genai", module)
    with pytest.raises(HighlightError, match="did not match"):
        analyze_with_gemini("transcript")
    class ApiError(Exception):
        code = 429
    module.Client = lambda **_kwargs: FakeClient(ApiError("private provider detail"))
    with pytest.raises(HighlightError, match="quota was exceeded"):
        analyze_with_gemini("transcript")


def test_launcher_keeps_gemini_key_out_of_frontend_process():
    script = Path(__file__).resolve().parents[2] / "start-aiclipping.ps1"
    source = script.read_text(encoding="utf-8")
    assert 'SetEnvironmentVariable("AICLIPPING_GEMINI_API_KEY", $null, "Process")' in source
    assert 'SetEnvironmentVariable("AICLIPPING_GEMINI_API_KEY", $originalGeminiKey, "Process")' in source


def test_analysis_persistence_duplicate_review_edit_retry_and_preservation(tmp_path, monkeypatch):
    client, project = _setup(tmp_path, monkeypatch)
    import threading
    ready, release = threading.Event(), threading.Event()
    def slow(_text):
        ready.set()
        release.wait(2)
        return highlight_detection.GeminiResponse(candidates=[_candidate()])
    monkeypatch.setattr(highlight_detection, "analyze_with_gemini", slow)
    first = client.post(f"/api/projects/{project}/highlights")
    assert first.status_code == 202
    assert ready.wait(2)
    assert client.post(f"/api/projects/{project}/highlights").status_code == 409
    release.set()
    assert _wait(client, project, first.json()["id"])["status"] == "completed"
    candidates = client.get(f"/api/projects/{project}/highlights/candidates").json()
    assert len(candidates) == 1 and candidates[0]["excerpt"].startswith("thought20")
    cid = candidates[0]["id"]
    invalid_edit = client.patch(f"/api/projects/{project}/highlights/candidates/{cid}", json={
        "start_seconds": 70, "end_seconds": 40, "title": "Bad edit", "hook": "Should not save"
    })
    assert invalid_edit.status_code == 422
    assert client.get(f"/api/projects/{project}/highlights/candidates").json()[0]["start_seconds"] == 20
    edited = client.patch(f"/api/projects/{project}/highlights/candidates/{cid}", json={
        "start_seconds": 21, "end_seconds": 51, "title": "Edited title", "hook": "Edited hook"
    })
    assert edited.status_code == 200 and edited.json()["review_status"] == "edited"
    assert edited.json()["start_seconds"] == 21
    blocked_selection = client.patch(f"/api/projects/{project}/highlights/candidates/{cid}/selection", json={"selected": True})
    assert blocked_selection.status_code == 409
    approved = client.patch(f"/api/projects/{project}/highlights/candidates/{cid}/review", json={"status": "approved"})
    assert approved.json()["review_status"] == "approved"
    selected = client.patch(f"/api/projects/{project}/highlights/candidates/{cid}/selection", json={"selected": True})
    assert selected.status_code == 200 and selected.json()["selected_for_render"] is True
    approved_edit = client.patch(f"/api/projects/{project}/highlights/candidates/{cid}", json={
        "start_seconds": 22, "end_seconds": 52, "title": "Still approved", "hook": "Edited after approval"
    })
    assert approved_edit.status_code == 200
    assert approved_edit.json()["review_status"] == "approved"
    assert approved_edit.json()["selected_for_render"] is True
    rejected = client.patch(f"/api/projects/{project}/highlights/candidates/{cid}/review", json={"status": "rejected"})
    assert rejected.json()["review_status"] == "rejected"
    assert rejected.json()["selected_for_render"] is False
    assert client.patch(f"/api/projects/{project}/highlights/candidates/{cid}/selection", json={"selected": True}).status_code == 409
    monkeypatch.setattr(highlight_detection, "analyze_with_gemini", lambda _text: (_ for _ in ()).throw(highlight_detection.HighlightError("quota exceeded")))
    retry = client.post(f"/api/projects/{project}/highlights")
    assert retry.status_code == 202
    assert _wait(client, project, retry.json()["id"])["status"] == "failed"
    preserved = client.get(f"/api/projects/{project}/highlights/candidates").json()
    assert len(preserved) == 1 and preserved[0]["title"] == "Still approved"
    client.__exit__(None, None, None)
    with TestClient(app) as reopened_client:
        after_restart = reopened_client.get(f"/api/projects/{project}/highlights/candidates").json()
    assert len(after_restart) == 1
    assert after_restart[0]["review_status"] == "rejected"
    assert after_restart[0]["selected_for_render"] is False
    assert after_restart[0]["title"] == "Still approved"
