import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app import transcription, transcription_jobs


def fake_model():
    words = [
        SimpleNamespace(start=0.12, end=0.4, word=" Hello", probability=0.98),
        SimpleNamespace(start=0.41, end=0.9, word=" world.", probability=0.93),
    ]
    segment = SimpleNamespace(start=0.12, end=0.9, text=" Hello world. ", words=words)

    class Model:
        def transcribe(self, path, **options):
            assert options["word_timestamps"] is True
            assert options["vad_filter"] is True
            return iter([segment]), SimpleNamespace(language="en", language_probability=0.99, duration=1.0)

    return Model()


def fake_audio_chunks(_source, work_dir, source_offset_seconds=0.0):
    path = work_dir / "audio_00000.flac"
    path.write_bytes(b"audio")
    return [transcription.AudioChunk(path, 1.0, source_offset_seconds)]


def test_transcription_writes_txt_srt_and_word_json(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"mock video")
    monkeypatch.setattr(transcription, "prepare_video", lambda _: {"audio_available": True, "duration_seconds": 1.0})
    monkeypatch.setattr(transcription, "_source_audio_offset", lambda *_: 0.0)
    monkeypatch.setattr(transcription, "_extract_audio_chunks", fake_audio_chunks)
    monkeypatch.setattr(transcription, "load_model", lambda *args: fake_model())
    monkeypatch.setattr(transcription.settings, "whisper_device", "cpu")
    monkeypatch.setattr(transcription.settings, "whisper_compute_type", "int8")
    stages = []
    result = transcription.transcribe_video(
        source, "project-id", "source.mp4", tmp_path / "transcript",
        lambda stage, detail, device: stages.append((stage, device)),
    )
    assert Path(result["text_path"]).read_text(encoding="utf-8") == "Hello world."
    assert "00:00:00,120 --> 00:00:00,900" in Path(result["srt_path"]).read_text(encoding="utf-8")
    payload = json.loads(Path(result["words_path"]).read_text(encoding="utf-8"))
    assert payload["project_id"] == "project-id"
    assert payload["language"] == "en"
    assert payload["segments"][0]["words"][1]["text"] == " world."
    assert payload["words"][0]["start"] == 0.12
    assert payload["diarization_status"] == "unavailable"
    assert payload["diarization_detail"] == "model_not_configured"
    assert payload["words"][0].get("speaker_id") is None
    assert [stage for stage, _ in stages] == ["preparing_audio", "loading_model", "transcribing", "saving_transcript"]


def test_diarization_availability_diagnostics_are_explicit(tmp_path, monkeypatch):
    monkeypatch.setattr(transcription.diarization.settings, "diarization_model_path", None)
    state = transcription.diarization.availability()
    assert state["detail"] == "model_not_configured"

    missing = tmp_path / "missing-model"
    monkeypatch.setattr(transcription.diarization.settings, "diarization_model_path", missing)
    state = transcription.diarization.availability()
    assert state["detail"] == "model_path_missing"

    incomplete = tmp_path / "incomplete-model"
    incomplete.mkdir()
    monkeypatch.setattr(transcription.diarization.settings, "diarization_model_path", incomplete)
    state = transcription.diarization.availability()
    assert state["detail"] == "model_bundle_incomplete"

    (incomplete / "config.yaml").write_text("pipeline: test\n", encoding="utf-8")
    monkeypatch.setattr(transcription.diarization.importlib.util, "find_spec",
                        lambda _name: None)
    state = transcription.diarization.availability()
    assert state["detail"] == "dependency_not_installed"


def test_optional_diarization_aligns_stable_labels_and_leaves_overlap_unknown():
    words = [
        {"start": 0.0, "end": 0.8, "text": " one"},
        {"start": 1.0, "end": 2.0, "text": " overlapping"},
        {"start": 2.0, "end": 3.0, "text": " two"},
    ]
    segments = [{"start": 0.0, "end": 3.0, "text": "one overlapping two", "words": words}]
    aligned_words, aligned_segments, status = transcription.diarization.assign_speakers(
        words, segments, [(0.0, 1.2, "raw-a"), (1.7, 3.0, "raw-b")]
    )
    assert status == "complete"
    assert [word["speaker_id"] for word in aligned_words] == ["speaker_1", None, "speaker_2"]
    assert aligned_segments[0]["speaker_id"] is None
    assert [speaker["speaker_id"] for speaker in transcription.diarization.assign_speakers(
        words, segments, [(0.0, 1.2, "raw-a"), (1.7, 3.0, "raw-b")]
    )[0]] == ["speaker_1", None, "speaker_2"]


def test_diarize_file_prepares_mono_wav_and_applies_source_offset(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"mock video")
    wav = tmp_path / "diarization.wav"
    wav.write_bytes(b"RIFF")
    monkeypatch.setattr(transcription.diarization.settings, "diarization_model_path", tmp_path)
    monkeypatch.setattr(transcription.diarization, "availability", lambda: {"available": True, "status": "ready", "message": "ready"})
    seen = {}

    class Pipeline:
        def __call__(self, path):
            seen["path"] = path
            return [
                types.SimpleNamespace(start=1.0, end=2.0, speaker_diarization=None),
            ]

    monkeypatch.setattr(transcription.diarization, "_load_pipeline", lambda _path: Pipeline())
    monkeypatch.setattr(transcription.diarization, "_prepare_audio", lambda _source: (wav, tmp_path))
    monkeypatch.setattr(transcription.diarization, "_annotation_turns", lambda _output: [(1.0, 2.0, "raw-a")])

    words = [{"start": 1.5, "end": 2.5, "text": " hello"}]
    segments = [{"start": 1.0, "end": 2.5, "text": "hello", "words": words}]
    result = transcription.diarization.diarize_file(source, words, segments, source_offset_seconds=0.25)
    assert seen["path"] == str(wav)
    assert result["status"] == "complete"
    assert result["speaker_turns"][0]["start"] == 1.25
    assert result["speaker_turns"][0]["end"] == 2.25


def test_diarization_unavailable_and_failure_are_nonfatal(tmp_path, monkeypatch):
    words = [{"start": 0.0, "end": 1.0, "text": " hello"}]
    segments = [{"start": 0.0, "end": 1.0, "text": "hello", "words": words}]
    monkeypatch.setattr(transcription.diarization.settings, "diarization_model_path", None)
    unavailable = transcription.diarization.diarize_file(tmp_path / "source.mp4", words, segments)
    assert unavailable["status"] == "unavailable"
    assert unavailable["words"] == words
    monkeypatch.setattr(transcription.diarization.settings, "diarization_model_path", tmp_path)
    monkeypatch.setattr(transcription.diarization, "_configured_model_path", lambda: tmp_path)
    monkeypatch.setattr(transcription.diarization, "availability", lambda: {"available": True, "status": "ready", "message": "ready"})
    monkeypatch.setattr(transcription.diarization, "_load_pipeline", lambda _path: (_ for _ in ()).throw(OSError("blocked native dependency")))
    fallback = transcription.diarization.diarize_file(tmp_path / "source.mp4", words, segments)
    assert fallback["status"] == "incomplete"
    assert fallback["words"] == words
    assert "still available" in fallback["message"]


def test_optional_pipeline_uses_local_offline_bundle_without_telemetry(tmp_path, monkeypatch):
    import os

    seen = {}
    class Pipeline:
        @classmethod
        def from_pretrained(cls, path):
            seen.update(path=path, offline=os.environ.get("HF_HUB_OFFLINE"), metrics=os.environ.get("PYANNOTE_METRICS_ENABLED"))
            return cls()
        def to(self, device):
            seen["device"] = device
    pyannote = types.ModuleType("pyannote")
    pyannote.__path__ = []
    audio = types.ModuleType("pyannote.audio")
    audio.Pipeline = Pipeline
    torch = types.ModuleType("torch")
    torch.device = lambda value: value
    monkeypatch.setitem(sys.modules, "pyannote", pyannote)
    monkeypatch.setitem(sys.modules, "pyannote.audio", audio)
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.delenv("PYANNOTE_METRICS_ENABLED", raising=False)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.setattr(transcription.diarization, "_pipeline", None)
    monkeypatch.setattr(transcription.diarization, "_pipeline_path", None)
    pipeline = transcription.diarization._load_pipeline(tmp_path)
    assert isinstance(pipeline, Pipeline)
    assert seen == {"path": str(tmp_path), "offline": "1", "metrics": "0", "device": "cpu"}
    assert os.environ["PYANNOTE_METRICS_ENABLED"] == "0"
    assert "HF_HUB_OFFLINE" not in os.environ


def test_transcript_formats_and_persists_optional_speaker_labels(tmp_path):
    payload = {
        "segments": [{"start": 0.0, "end": 1.0, "text": "Hello.", "speaker_id": "speaker_1",
                      "words": [{"start": 0.0, "end": 1.0, "text": " Hello.", "speaker_id": "speaker_1"}]}],
        "words": [{"start": 0.0, "end": 1.0, "text": " Hello.", "speaker_id": "speaker_1"}],
        "speakers": [{"speaker_id": "speaker_1", "label": "Speaker 1"}],
        "diarization_status": "complete", "diarization_message": "estimated locally",
    }
    paths = transcription._write_outputs(tmp_path / "transcript", payload)
    assert "Speaker 1: Hello." in Path(paths["text_path"]).read_text(encoding="utf-8")
    assert "Speaker 1: Hello." in Path(paths["srt_path"]).read_text(encoding="utf-8")
    saved = json.loads(Path(paths["words_path"]).read_text(encoding="utf-8"))
    assert saved["speakers"][0]["speaker_id"] == "speaker_1"
    assert saved["words"][0]["speaker_id"] == "speaker_1"
    unknown = {"segments": [{"start": 0.0, "end": 1.0, "text": "Unclear overlap.", "words": [
        {"start": 0.0, "end": 1.0, "text": " Unclear overlap.", "speaker_id": None},
    ]}], "words": [], "speakers": [], "diarization_status": "incomplete"}
    unknown_paths = transcription._write_outputs(tmp_path / "unknown", unknown)
    assert "Unknown speaker: Unclear overlap." in Path(unknown_paths["text_path"]).read_text(encoding="utf-8")


def test_no_audio_and_model_failures_are_actionable(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"mock video")
    monkeypatch.setattr(transcription, "prepare_video", lambda _: {"audio_available": False})
    monkeypatch.setattr(transcription, "load_model", lambda *args: pytest.fail("model should not load without audio"))
    with pytest.raises(transcription.TranscriptionError, match="no audio stream"):
        transcription.transcribe_video(source, "p", "source.mp4", tmp_path, lambda *_: None)

    monkeypatch.setattr(transcription, "prepare_video", lambda _: {"audio_available": True, "duration_seconds": 1})
    monkeypatch.setattr(transcription, "_source_audio_offset", lambda *_: 0.0)
    monkeypatch.setattr(transcription, "_extract_audio_chunks", fake_audio_chunks)
    monkeypatch.setattr(transcription, "load_model", lambda *args: (_ for _ in ()).throw(transcription.TranscriptionError("model unavailable")))
    monkeypatch.setattr(transcription.settings, "whisper_device", "cpu")
    with pytest.raises(transcription.TranscriptionError, match="model unavailable"):
        transcription.transcribe_video(source, "p", "source.mp4", tmp_path, lambda *_: None)


def test_explicit_cuda_cpu_fallback_reports_actual_device(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"mock video")
    monkeypatch.setattr(transcription, "prepare_video", lambda _: {"audio_available": True, "duration_seconds": 1})
    monkeypatch.setattr(transcription, "_source_audio_offset", lambda *_: 0.0)
    monkeypatch.setattr(transcription, "_extract_audio_chunks", fake_audio_chunks)
    monkeypatch.setattr(transcription.settings, "whisper_device", "cuda")
    monkeypatch.setattr(transcription.settings, "whisper_compute_type", "float16")
    monkeypatch.setattr(transcription.settings, "whisper_cpu_fallback", True)
    calls = []

    def load(model, device, compute):
        calls.append((device, compute))
        if device == "cuda":
            raise transcription.TranscriptionError("CUDA could not initialize Faster-Whisper")
        return fake_model()

    monkeypatch.setattr(transcription, "load_model", load)
    result = transcription.transcribe_video(source, "p", "source.mp4", tmp_path / "out", lambda *_: None)
    assert calls == [("cuda", "float16"), ("cpu", "int8")]
    assert result["device"] == "cpu"
    assert result["compute_type"] == "int8"


def setup_client(tmp_path, monkeypatch, *, with_source=True):
    from app.core import config
    from app.database import session as database
    from app.database.models import Project, SourceVideo
    from app.storage import ensure_project_directories

    data_root = tmp_path / "AIClippingData"
    monkeypatch.setattr(config.settings, "data_dir", data_root)
    monkeypatch.setattr(config.settings, "database_path", data_root / "database" / "aiclipping.db")
    monkeypatch.setattr(config.settings, "whisper_device", "cpu")
    monkeypatch.setattr(config.settings, "whisper_compute_type", "int8")
    client = TestClient(app)
    client.__enter__()
    project_response = client.post("/api/projects", json={"name": "Transcription test"})
    project_id = project_response.json()["id"]
    if with_source:
        project_dir = ensure_project_directories(project_id)
        source_path = project_dir / "source" / "source.mp4"
        source_path.write_bytes(b"test video")
        with database.SessionLocal() as db:
            db.add(SourceVideo(
                project_id=project_id, source_type="local_file", source_uri=str(source_path),
                local_path=str(source_path), title="source.mp4", metadata_json=json.dumps({"readable": True}),
            ))
            db.commit()
    return client, project_id


def test_transcription_api_status_and_duplicate_prevention(tmp_path, monkeypatch):
    from sqlalchemy import select
    client, project_id = setup_client(tmp_path, monkeypatch)
    import threading
    started = threading.Event()
    release = threading.Event()
    calls = []

    def slow_job(*, project_id, source_filename, transcript_dir, stage_callback, **kwargs):
        calls.append(len(calls) + 1)
        text = f"mock-{calls[-1]}"
        stage_callback("transcribing", "mock transcription", "cpu")
        started.set()
        release.wait(timeout=3)
        transcript_dir.mkdir(parents=True, exist_ok=True)
        paths = {}
        for key, name in (("text_path", "transcript.txt"), ("srt_path", "transcript.srt"), ("words_path", "words.json")):
            path = transcript_dir / name
            path.write_text(json.dumps({"language": "en", "segments": [{"text": text}], "words": [{"text": text}]}) if name.endswith(".json") else text, encoding="utf-8")
            paths[key] = str(path)
        return {**paths, "language": "en", "source_filename": source_filename, "model": "small", "device": "cpu", "compute_type": "int8", "language_probability": 0.9, "duration_seconds": 1.0, "segments": [{"text": "mock"}], "words": [{"text": "mock"}]}

    monkeypatch.setattr(transcription_jobs, "transcribe_video", slow_job)
    first = client.post(f"/api/projects/{project_id}/transcription", json={})
    assert first.status_code == 202
    assert started.wait(timeout=2)
    duplicate = client.post(f"/api/projects/{project_id}/transcription", json={})
    assert duplicate.status_code == 409
    release.set()
    import time
    for _ in range(30):
        status = client.get(f"/api/projects/{project_id}/transcription/jobs/{first.json()['id']}")
        if status.json()["status"] == "completed":
            break
        time.sleep(0.05)
    assert status.json()["status"] == "completed"
    assert status.json()["stage"] == "completed"
    assert client.get(f"/api/projects/{project_id}/transcript").json()["metadata"]["language"] == "en"
    # The API's response streams the output; verify its bytes rather than relying on stored path metadata.
    assert client.get(f"/api/projects/{project_id}/transcript/txt").text == "mock-1"
    blocked_regeneration = client.post(f"/api/projects/{project_id}/transcription", json={})
    assert blocked_regeneration.status_code == 409
    assert client.get(f"/api/projects/{project_id}/transcript/txt").text == "mock-1"
    second = client.post(f"/api/projects/{project_id}/transcription", json={"regenerate": True})
    assert second.status_code == 202
    for _ in range(30):
        status = client.get(f"/api/projects/{project_id}/transcription/jobs/{second.json()['id']}")
        if status.json()["status"] == "completed":
            break
        time.sleep(0.05)
    assert status.json()["status"] == "completed"
    assert client.get(f"/api/projects/{project_id}/transcript/txt").text == "mock-2"
    assert client.get(f"/api/projects/{project_id}/transcript/json").headers["content-disposition"].endswith('filename="words.json"')
    folder = client.get(f"/api/projects/{project_id}/transcript-folder")
    assert folder.status_code == 200 and Path(folder.json()["path"]).is_dir()
    from app.database import session as database
    from app.database.models import Transcript
    with database.SessionLocal() as db:
        record = db.scalar(select(Transcript).where(Transcript.project_id == project_id))
        missing_path = Path(record.srt_path)
        missing_path.unlink()
    assert client.get(f"/api/projects/{project_id}/transcript/srt").status_code == 404
    assert client.get(f"/api/projects/{project_id}/transcript").status_code == 404
    client.__exit__(None, None, None)


def test_transcription_api_rejects_invalid_and_missing_source(tmp_path, monkeypatch):
    client, project_id = setup_client(tmp_path, monkeypatch, with_source=False)
    assert client.post("/api/projects/not-a-project/transcription", json={}).status_code == 404
    response = client.post(f"/api/projects/{project_id}/transcription", json={})
    assert response.status_code == 422
    client.__exit__(None, None, None)


def test_missing_source_file_and_worker_failure_are_reported(tmp_path, monkeypatch):
    client, project_id = setup_client(tmp_path, monkeypatch)
    from app.database import session as database
    from app.database.models import SourceVideo
    from sqlalchemy import select
    with database.SessionLocal() as db:
        source = db.scalar(select(SourceVideo).where(SourceVideo.project_id == project_id))
        Path(source.local_path).unlink()
    missing = client.post(f"/api/projects/{project_id}/transcription", json={})
    assert missing.status_code == 422
    client.__exit__(None, None, None)

    client, project_id = setup_client(tmp_path / "failure", monkeypatch)
    monkeypatch.setattr(transcription_jobs, "transcribe_video", lambda **kwargs: (_ for _ in ()).throw(transcription.TranscriptionError("The video has no audio stream.")))
    started = client.post(f"/api/projects/{project_id}/transcription", json={})
    assert started.status_code == 202
    import time
    for _ in range(30):
        current = client.get(f"/api/projects/{project_id}/transcription/jobs/{started.json()['id']}").json()
        if current["status"] == "failed":
            break
        time.sleep(0.05)
    assert current["status"] == "failed"
    assert current["error_message"] == "The video has no audio stream."
    retry = client.post(f"/api/projects/{project_id}/transcription", json={})
    assert retry.status_code == 202
    for _ in range(30):
        current = client.get(f"/api/projects/{project_id}/transcription/jobs/{retry.json()['id']}").json()
        if current["status"] == "failed":
            break
        time.sleep(0.05)
    assert current["status"] == "failed"
    client.__exit__(None, None, None)


def test_windows_launcher_sets_cpu_and_sqlalchemy_defaults_process_locally():
    script = Path(__file__).resolve().parents[2] / "start-aiclipping.ps1"
    source = script.read_text(encoding="utf-8")
    assert 'DISABLE_SQLALCHEMY_CEXT_RUNTIME = "1"' in source
    assert 'AICLIPPING_WHISPER_DEVICE = "cpu"' in source
    assert 'AICLIPPING_WHISPER_COMPUTE_TYPE = "int8"' in source
    assert 'if ($null -eq $current)' in source
    assert 'SetEnvironmentVariable($name, $originalBackendEnvironment[$name], "Process")' in source
