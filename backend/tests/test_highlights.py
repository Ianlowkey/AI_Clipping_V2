import json
import sys
import types
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import highlight_detection
from app import gemini_credentials
from app.database import session as database
from app.database.models import Project, Transcript
from app.main import app


class MemoryCredentialManager:
    def __init__(self):
        self.passwords = {}

    def get_password(self, service, username):
        return self.passwords.get((service, username))

    def set_password(self, service, username, password):
        self.passwords[(service, username)] = password

    def delete_password(self, service, username):
        self.passwords.pop((service, username), None)


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
    monkeypatch.setattr(gemini_credentials, "_windows_backend", MemoryCredentialManager)
    client = TestClient(app, client=("127.0.0.1", 50000))
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


def _comedy_candidate(*, passage=None, start=20, end=50, score=75, overall=30, explanation="The reply turns the earlier setup into a wordplay payoff."):
    candidate = _candidate()
    return candidate.model_copy(update={
        "start_seconds": start,
        "end_seconds": end,
        "passage": passage or candidate.passage,
        "overall_score": overall,
        "comedy_potential": score,
        "comedy_explanation": explanation,
        "conversational_engagement": 88,
        "storytelling_interest": 72,
        "surprise_potential": 91,
        "emotional_impact": 65,
        "clip_completeness": 95,
    })


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
        "configured": False, "model": "gemini-3.6-flash", "key_environment_variable": "AICLIPPING_GEMINI_API_KEY",
        "saved_in_credential_manager": False, "secure_storage_available": True, "source": "none",
    }
    assert client.post(f"/api/projects/{project}/highlights").status_code == 422
    client.__exit__(None, None, None)


def test_gemini_key_save_replace_status_delete_and_no_secret_leaks(tmp_path, monkeypatch, caplog):
    client, _project = _setup(tmp_path, monkeypatch, transcript=False, key=None)
    store = MemoryCredentialManager()
    monkeypatch.setattr(gemini_credentials, "_windows_backend", lambda: store)
    secret = "AIzaSy" + "Z" * 35
    replacement = "AIzaSy" + "Y" * 35

    saved = client.put("/api/settings/gemini/key", json={"api_key": secret})
    assert saved.status_code == 200
    assert saved.json()["configured"] is True
    assert saved.json()["saved_in_credential_manager"] is True
    assert saved.json()["source"] == "credential_manager"
    assert secret not in saved.text
    assert client.get("/api/settings/gemini").json()["configured"] is True

    replaced = client.put("/api/settings/gemini/key", json={"api_key": replacement})
    assert replaced.status_code == 200
    assert replacement not in replaced.text
    assert store.get_password(gemini_credentials.SERVICE_NAME, gemini_credentials.ACCOUNT_NAME) == replacement

    database_path = tmp_path / "data" / "database" / "app.db"
    assert replacement.encode() not in database_path.read_bytes()
    assert secret not in caplog.text and replacement not in caplog.text

    deleted = client.delete("/api/settings/gemini/key")
    assert deleted.status_code == 200
    assert deleted.json()["configured"] is False
    assert deleted.json()["saved_in_credential_manager"] is False
    assert replacement not in deleted.text
    client.__exit__(None, None, None)


def test_gemini_key_environment_fallback_and_unavailable_secure_storage(tmp_path, monkeypatch, caplog):
    client, _project = _setup(tmp_path, monkeypatch, transcript=False, key="explicit-environment-key")
    store = MemoryCredentialManager()
    monkeypatch.setattr(gemini_credentials, "_windows_backend", lambda: store)
    status = client.get("/api/settings/gemini").json()
    assert status["configured"] is True and status["source"] == "environment"
    assert status["saved_in_credential_manager"] is False

    def unavailable():
        raise gemini_credentials.CredentialStoreUnavailable("Windows Credential Manager is unavailable. No insecure storage fallback.")

    monkeypatch.setattr(gemini_credentials, "_windows_backend", unavailable)
    status = client.get("/api/settings/gemini").json()
    assert status["configured"] is True and status["source"] == "environment"
    assert status["secure_storage_available"] is False
    assert gemini_credentials.get_api_key() == "explicit-environment-key"
    attempted_secret = "AIzaSy" + "Q" * 35
    failed_save = client.put("/api/settings/gemini/key", json={"api_key": attempted_secret})
    assert failed_save.status_code == 503
    assert attempted_secret not in failed_save.text and attempted_secret not in caplog.text
    assert "plaintext" in failed_save.json()["detail"].lower() or "Credential Manager" in failed_save.json()["detail"]
    client.__exit__(None, None, None)


def test_gemini_key_validation_hides_invalid_input_and_management_is_loopback_only(tmp_path, monkeypatch):
    client, _project = _setup(tmp_path, monkeypatch, transcript=False, key=None)
    invalid = "short-secret-marker"
    response = client.put("/api/settings/gemini/key", json={"api_key": invalid})
    assert response.status_code == 422
    assert invalid not in response.text

    remote_client = TestClient(app, client=("192.0.2.10", 50000))
    remote = remote_client.put("/api/settings/gemini/key", json={"api_key": "AIzaSy" + "R" * 35})
    assert remote.status_code == 403
    assert remote_client.get("/api/settings/gemini").status_code == 403
    remote_client.close()
    client.__exit__(None, None, None)


def test_comedy_prompt_uses_transcript_context_and_disclaims_visual_analysis():
    prompt = highlight_detection.build_analysis_prompt("[1.00-2.00] setup\n[2.00-3.00] punchline", "comedy")
    for phrase in ("punchlines", "banter", "sarcasm", "unexpected replies", "callbacks", "spoken reactions", "group interactions"):
        assert phrase in prompt
    assert "loudness, profanity" in prompt
    assert "Do not infer facial expressions, visual gags, physical" in prompt
    assert "[1.00-2.00] setup" in prompt and "[2.00-3.00] punchline" in prompt
    assert "no video or audio is provided" in prompt
    assert "comedy_potential" in prompt and "comedy_explanation" in prompt
    assert "return up to five" in highlight_detection.build_analysis_prompt("text", "general")
    with pytest.raises(ValueError, match="Detection mode"):
        highlight_detection.build_analysis_prompt("text", "visual")


def test_podcast_profile_scores_context_and_speaker_formatting():
    transcript = "[0.0-0.5] Speaker 1: I moved across the country\n[0.5-1.0] Speaker 2: Why would you do that?"
    prompt = highlight_detection.build_analysis_prompt(transcript, "general", "podcast_conversation", True)
    for term in ("personal stories", "surprising admissions", "strong back-and-forth", "running jokes", "clip_completeness"):
        assert term in prompt
    assert "not real-world identities" in prompt
    assert "never include labels in the quoted spoken passage" in prompt
    words = [
        {"start": 0.0, "end": 0.5, "text": " I moved", "speaker_id": "speaker_1", "speaker_label": "Speaker 1"},
        {"start": 0.5, "end": 1.0, "text": " Why?", "speaker_id": "speaker_2", "speaker_label": "Speaker 2"},
    ]
    formatted = highlight_detection.format_timestamped_transcript(words)
    assert "Speaker 1:  I moved" in formatted and "Speaker 2:  Why?" in formatted
    assert highlight_detection._speaker_context(words) == "Speaker 1: I moved\nSpeaker 2: Why?"
    assert "Speaker" not in highlight_detection.format_timestamped_transcript([{"start": 0.0, "end": 1.0, "text": " hello"}])


def test_comedy_scores_context_alignment_overlap_and_duplicate_filtering():
    words = highlight_detection._validate_words(_words_payload(), 120)
    setup_and_payoff = _comedy_candidate(start=45, end=50, passage=" ".join(f"thought{i}" for i in range(20, 50)))
    aligned = highlight_detection._align(setup_and_payoff, words, 120)
    assert aligned["start"] == 20 and aligned["end"] == 50
    assert "thought20" in aligned["excerpt"] and "thought49." in aligned["excerpt"]

    first = _comedy_candidate(score=92, overall=15)
    distinct = _comedy_candidate(start=25, end=55, score=70, overall=99,
        passage="different setup punchline reply unexpected reversal group conversation")
    repeated = _comedy_candidate(start=21, end=51, score=80, overall=100)
    distinct_excerpt = "A separate joke with a different setup and a different payoff from this conversation."
    first_span = {"start": 20, "end": 50, "duration": 30, "excerpt": "The first joke has a setup and its own funny payoff here."}
    distinct_span = {"start": 25, "end": 55, "duration": 30, "excerpt": distinct_excerpt}
    repeated_span = {"start": 21, "end": 51, "duration": 30, "excerpt": first_span["excerpt"]}
    selected, rejected = highlight_detection._filter_candidates(
        [(first, first_span), (distinct, distinct_span), (repeated, repeated_span)], "comedy"
    )
    assert [candidate.comedy_potential for candidate, _ in selected] == [92, 70]
    assert rejected and "duplicate" in rejected[0]
    general, _ = highlight_detection._filter_candidates(
        [(first, first_span), (distinct, distinct_span)], "general"
    )
    assert len(general) == 1
    podcast, _ = highlight_detection._filter_candidates(
        [(first, first_span), (distinct, distinct_span)], "general", "podcast_conversation"
    )
    assert len(podcast) == 2


def test_comedy_analysis_persists_mode_score_and_explanation(tmp_path, monkeypatch):
    client, project = _setup(tmp_path, monkeypatch)
    seen = {}

    def mocked_analysis(timestamped_text, detection_mode, content_profile="podcast_conversation", speaker_labels_available=False):
        seen["text"] = timestamped_text
        seen["mode"] = detection_mode
        seen["profile"] = content_profile
        seen["speakers"] = speaker_labels_available
        return highlight_detection.GeminiResponse(candidates=[_comedy_candidate()])

    monkeypatch.setattr(highlight_detection, "analyze_with_gemini", mocked_analysis)
    started = client.post(f"/api/projects/{project}/highlights", json={"detection_mode": "comedy", "content_profile": "podcast_conversation"})
    assert started.status_code == 202
    assert started.json()["detection_mode"] == "comedy"
    assert started.json()["content_profile"] == "podcast_conversation"
    finished = _wait(client, project, started.json()["id"])
    assert finished["status"] == "completed" and finished["detection_mode"] == "comedy"
    assert seen["mode"] == "comedy" and "[20.00-21.00]  thought20" in seen["text"]
    assert seen["profile"] == "podcast_conversation" and seen["speakers"] is False
    candidate = client.get(f"/api/projects/{project}/highlights/candidates").json()[0]
    assert candidate["comedy_potential"] == 75
    assert candidate["comedy_explanation"].startswith("The reply turns")
    assert candidate["conversational_engagement"] == 88
    assert candidate["storytelling_interest"] == 72
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


def test_podcast_analysis_persists_speaker_context_and_independent_scores(tmp_path, monkeypatch):
    client, project = _setup(tmp_path, monkeypatch)
    from app.database.models import HighlightCandidate
    with database.SessionLocal() as db:
        transcript = db.query(Transcript).filter_by(project_id=project).one()
        path = Path(transcript.words_path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["speakers"] = [
            {"speaker_id": "speaker_1", "label": "Speaker 1"},
            {"speaker_id": "speaker_2", "label": "Speaker 2"},
        ]
        for index, word in enumerate(payload["words"]):
            word["speaker_id"] = "speaker_1" if index < 35 else "speaker_2"
        path.write_text(json.dumps(payload), encoding="utf-8")
        db.commit()
    called = {}

    def mocked_analysis(text, mode, profile, labels_available):
        called.update(text=text, mode=mode, profile=profile, labels_available=labels_available)
        return highlight_detection.GeminiResponse(candidates=[_comedy_candidate()])

    monkeypatch.setattr(highlight_detection, "analyze_with_gemini", mocked_analysis)
    started = client.post(f"/api/projects/{project}/highlights", json={
        "detection_mode": "comedy", "content_profile": "podcast_conversation",
    })
    assert started.status_code == 202
    assert _wait(client, project, started.json()["id"])["status"] == "completed"
    assert called["labels_available"] is True
    assert "Speaker 1:" in called["text"] and "Speaker 2:" in called["text"]
    candidate = client.get(f"/api/projects/{project}/highlights/candidates").json()[0]
    assert "Speaker 1:" in candidate["speaker_context"] and "Speaker 2:" in candidate["speaker_context"]
    assert candidate["clip_completeness"] == 95
    with database.SessionLocal() as db:
        saved = db.query(HighlightCandidate).filter_by(project_id=project).one()
        assert saved.speaker_context == candidate["speaker_context"]
    client.__exit__(None, None, None)


def test_conversation_alignment_includes_speaker_setup_and_reaction():
    words = [
        {"start": float(index), "end": float(index + 1), "text": f" word{index}",
         "speaker_id": "speaker_1" if index < 10 or index > 30 else "speaker_2"}
        for index in range(40)
    ]
    words[9]["text"] = " setup."
    words[30]["text"] = " punchline."
    words[31]["text"] = " laughed."
    start_idx, end_idx = highlight_detection._expand_conversation_boundaries(words, 20, 30)
    assert start_idx <= 10
    assert end_idx >= 31


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
    monkeypatch.setattr(gemini_credentials, "_windows_backend", MemoryCredentialManager)
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
    def slow(_text, _mode="general", _profile="podcast_conversation", _speaker_labels_available=False):
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
    monkeypatch.setattr(highlight_detection, "analyze_with_gemini", lambda _text, _mode="general", _profile="podcast_conversation", _speaker_labels_available=False: (_ for _ in ()).throw(highlight_detection.HighlightError("quota exceeded")))
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
