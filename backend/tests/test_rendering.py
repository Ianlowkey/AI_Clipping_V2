import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import rendering
from app.core import config
from app.database import session as database
from app.database.models import Clip, HighlightCandidate, ProcessingJob, Project, SourceVideo
from app.main import app
from app.storage import ensure_project_directories


def setup_project(tmp_path, monkeypatch, *, approved=True, selected=True, with_audio=False):
    data = tmp_path / "render-data"
    monkeypatch.setattr(config.settings, "data_dir", data)
    monkeypatch.setattr(config.settings, "database_path", data / "database" / "aiclipping.db")
    client = TestClient(app)
    client.__enter__()
    created = client.post("/api/projects", json={"name": "Render test"})
    project_id = created.json()["id"]
    project_dir = ensure_project_directories(project_id)
    source = project_dir / "source" / "source.mp4"
    source.write_bytes(b"synthetic")
    with database.SessionLocal() as db:
        db.add(SourceVideo(
            project_id=project_id,
            source_type="local_file",
            source_uri=str(source),
            local_path=str(source),
            title="source.mp4",
            duration_seconds=90,
            metadata_json=json.dumps({"readable": True, "audio_available": with_audio}),
        ))
        candidate = HighlightCandidate(
            project_id=project_id,
            start_seconds=20,
            end_seconds=50,
            duration_seconds=30,
            score=80,
            title="Podcast moment",
            hook="A strong exchange",
            approved=approved,
            review_status="approved" if approved else "suggested",
            selected_for_render=selected,
        )
        db.add(candidate)
        db.commit()
        db.refresh(candidate)
        candidate_id = candidate.id
    return client, project_id, candidate_id, source


def test_render_profile_validation_and_candidate_eligibility(tmp_path, monkeypatch):
    client, project_id, candidate_id, _source = setup_project(tmp_path, monkeypatch)
    invalid_profile = client.post(f"/api/projects/{project_id}/render", json={
        "candidate_ids": [candidate_id],
        "output_profile": "portrait",
    })
    assert invalid_profile.status_code == 422

    with database.SessionLocal() as db:
        candidate = db.get(HighlightCandidate, candidate_id)
        candidate.approved = False
        candidate.review_status = "suggested"
        db.commit()
    unapproved = client.post(f"/api/projects/{project_id}/render", json={
        "candidate_ids": [candidate_id],
        "output_profile": "vertical",
    })
    assert unapproved.status_code == 422
    assert "approved" in unapproved.json()["detail"].lower()
    client.__exit__(None, None, None)


def test_approve_select_reload_then_render_uses_persisted_candidate(tmp_path, monkeypatch):
    client, project_id, candidate_id, _source = setup_project(
        tmp_path, monkeypatch, approved=False, selected=False
    )
    monkeypatch.setattr(rendering._executor, "submit", lambda *_args, **_kwargs: None)

    approved = client.patch(
        f"/api/projects/{project_id}/highlights/candidates/{candidate_id}/review",
        json={"status": "approved"},
    )
    assert approved.status_code == 200
    selected = client.patch(
        f"/api/projects/{project_id}/highlights/candidates/{candidate_id}/selection",
        json={"selected": True},
    )
    assert selected.status_code == 200
    reloaded = client.get(f"/api/projects/{project_id}/highlights/candidates")
    assert reloaded.status_code == 200
    assert reloaded.json()[0]["review_status"] == "approved"
    assert reloaded.json()[0]["selected_for_render"] is True

    response = client.post(f"/api/projects/{project_id}/render", json={
        "candidate_ids": [candidate_id],
        "output_profile": "vertical",
    })
    assert response.status_code == 202
    assert response.json()["candidate_count"] == 1
    with database.SessionLocal() as db:
        job = db.get(ProcessingJob, response.json()["id"])
        assert json.loads(job.candidate_ids_json) == [candidate_id]
    client.__exit__(None, None, None)


def test_selection_without_approval_is_rejected(tmp_path, monkeypatch):
    client, project_id, candidate_id, _source = setup_project(
        tmp_path, monkeypatch, approved=False, selected=False
    )
    response = client.patch(
        f"/api/projects/{project_id}/highlights/candidates/{candidate_id}/selection",
        json={"selected": True},
    )
    assert response.status_code == 409
    assert "approved" in response.json()["detail"].lower()
    client.__exit__(None, None, None)


def test_render_diagnostics_distinguish_missing_selection_and_unapproved(tmp_path, monkeypatch):
    client, project_id, candidate_id, _source = setup_project(
        tmp_path, monkeypatch, approved=False, selected=False
    )
    no_selection = client.post(f"/api/projects/{project_id}/render", json={
        "candidate_ids": [],
        "output_profile": "vertical",
    })
    assert no_selection.status_code == 422
    assert "no candidates are selected" in no_selection.json()["detail"].lower()

    with database.SessionLocal() as db:
        candidate = db.get(HighlightCandidate, candidate_id)
        candidate.selected_for_render = True
        db.commit()
    unapproved = client.post(f"/api/projects/{project_id}/render", json={
        "candidate_ids": [candidate_id],
        "output_profile": "vertical",
    })
    assert unapproved.status_code == 422
    assert "not approved" in unapproved.json()["detail"].lower()
    client.__exit__(None, None, None)


def test_approval_without_selection_and_rejection_after_selection_are_not_renderable(tmp_path, monkeypatch):
    client, project_id, candidate_id, _source = setup_project(
        tmp_path, monkeypatch, approved=False, selected=False
    )
    approved_only = client.patch(
        f"/api/projects/{project_id}/highlights/candidates/{candidate_id}/review",
        json={"status": "approved"},
    )
    assert approved_only.status_code == 200
    no_selection = client.post(f"/api/projects/{project_id}/render", json={
        "candidate_ids": [],
        "output_profile": "vertical",
    })
    assert "no candidates are selected" in no_selection.json()["detail"].lower()

    assert client.patch(
        f"/api/projects/{project_id}/highlights/candidates/{candidate_id}/selection",
        json={"selected": True},
    ).status_code == 200
    rejected = client.patch(
        f"/api/projects/{project_id}/highlights/candidates/{candidate_id}/review",
        json={"status": "rejected"},
    )
    assert rejected.json()["selected_for_render"] is False
    after_rejection = client.post(f"/api/projects/{project_id}/render", json={
        "candidate_ids": [],
        "output_profile": "vertical",
    })
    assert after_rejection.status_code == 422
    assert "no candidates are selected" in after_rejection.json()["detail"].lower()
    client.__exit__(None, None, None)


def test_render_accepts_multiple_approved_selected_candidates(tmp_path, monkeypatch):
    client, project_id, first_id, _source = setup_project(tmp_path, monkeypatch)
    with database.SessionLocal() as db:
        second = HighlightCandidate(
            project_id=project_id,
            start_seconds=40,
            end_seconds=70,
            duration_seconds=30,
            score=75,
            approved=True,
            review_status="approved",
            selected_for_render=True,
        )
        db.add(second)
        db.commit()
        db.refresh(second)
        second_id = second.id
    monkeypatch.setattr(rendering._executor, "submit", lambda *_args, **_kwargs: None)

    response = client.post(f"/api/projects/{project_id}/render", json={
        "candidate_ids": [first_id, second_id],
        "output_profile": "square",
    })
    assert response.status_code == 202
    assert response.json()["candidate_count"] == 2
    client.__exit__(None, None, None)


@pytest.mark.parametrize("candidate_scope", ["invalid", "another_project"])
def test_render_rejects_invalid_or_foreign_candidate_ids(tmp_path, monkeypatch, candidate_scope):
    client, project_id, _candidate_id, _source = setup_project(tmp_path, monkeypatch)
    candidate_id = "not-a-candidate"
    if candidate_scope == "another_project":
        other_project = client.post("/api/projects", json={"name": "Other project"}).json()["id"]
        with database.SessionLocal() as db:
            foreign = HighlightCandidate(
                project_id=other_project,
                start_seconds=20,
                end_seconds=50,
                duration_seconds=30,
                score=80,
                approved=True,
                review_status="approved",
                selected_for_render=True,
            )
            db.add(foreign)
            db.commit()
            db.refresh(foreign)
            candidate_id = foreign.id

    response = client.post(f"/api/projects/{project_id}/render", json={
        "candidate_ids": [candidate_id],
        "output_profile": "vertical",
    })
    assert response.status_code == 404
    assert "invalid or do not belong" in response.json()["detail"].lower()
    client.__exit__(None, None, None)


def test_mocked_ffmpeg_success_and_export_verification(tmp_path, monkeypatch):
    client, project_id, candidate_id, source = setup_project(tmp_path, monkeypatch)
    monkeypatch.setattr(config.settings, "ffprobe_path", "ffprobe")
    monkeypatch.setattr(config.settings, "ffmpeg_path", "ffmpeg")

    def fake_run(args, **kwargs):
        if args[0] == "ffprobe":
            if str(args[-1]).endswith("source.mp4"):
                return SimpleNamespace(returncode=0, stdout=json.dumps({
                    "format": {"duration": "90", "size": "1000"},
                    "streams": [{"codec_type": "video", "width": 1920, "height": 1080}],
                }), stderr="")
            return SimpleNamespace(returncode=0, stdout=json.dumps({
                "format": {"duration": "30", "size": "3000"},
                "streams": [{"codec_type": "video", "width": 1080, "height": 1920}],
            }), stderr="")
        output = Path(args[-1])
        output.write_bytes(b"\x00" * 200)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(rendering.subprocess, "run", fake_run)
    final = tmp_path / "candidate.mp4"
    with database.SessionLocal() as db:
        candidate = db.get(HighlightCandidate, candidate_id)
    result = rendering.render_candidate(source, candidate, "vertical", final)
    assert final.is_file()
    assert result["width"] == 1080
    assert result["height"] == 1920
    assert result["duration_seconds"] == 30


def test_mocked_ffmpeg_failure_and_invalid_output_cleanup(tmp_path, monkeypatch):
    client, project_id, candidate_id, source = setup_project(tmp_path, monkeypatch)
    monkeypatch.setattr(config.settings, "ffprobe_path", "ffprobe")
    monkeypatch.setattr(config.settings, "ffmpeg_path", "ffmpeg")
    monkeypatch.setattr(rendering, "_probe", lambda _path: {
        "format": {"duration": "90"},
        "streams": [{"codec_type": "video", "width": 1920, "height": 1080}],
    })

    def failed(args, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="encoder unavailable")

    monkeypatch.setattr(rendering.subprocess, "run", failed)
    with database.SessionLocal() as db:
        candidate = db.get(HighlightCandidate, candidate_id)
    final = tmp_path / "failed.mp4"
    with pytest.raises(rendering.RenderError, match="FFmpeg rendering failed"):
        rendering.render_candidate(source, candidate, "square", final)
    assert not final.exists()
    assert not list(tmp_path.glob("*.rendering.mp4"))
    client.__exit__(None, None, None)


def test_render_arguments_use_output_seek_sar_and_vfr_passthrough(tmp_path, monkeypatch):
    client, project_id, candidate_id, source = setup_project(tmp_path, monkeypatch)
    monkeypatch.setattr(config.settings, "ffprobe_path", "ffprobe")
    monkeypatch.setattr(config.settings, "ffmpeg_path", "ffmpeg")
    captured = {}

    def fake_run(args, **kwargs):
        if args[0] == "ffprobe":
            if str(args[-1]).endswith("source.mp4"):
                return SimpleNamespace(returncode=0, stdout=json.dumps({
                    "format": {"duration": "90"},
                    "streams": [{"codec_type": "video", "width": 720, "height": 480,
                                 "sample_aspect_ratio": "8:9"}],
                }), stderr="")
            return SimpleNamespace(returncode=0, stdout=json.dumps({
                "format": {"duration": "30"},
                "streams": [{"codec_type": "video", "width": 1080, "height": 1920}],
            }), stderr="")
        captured["args"] = args
        output = Path(args[-1])
        output.write_bytes(b"\x00" * 200)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(rendering.subprocess, "run", fake_run)
    final = tmp_path / "candidate.mp4"
    with database.SessionLocal() as db:
        candidate = db.get(HighlightCandidate, candidate_id)
    rendering.render_candidate(source, candidate, "vertical", final)
    args = captured["args"]
    input_index = args.index("-i")
    seek_index = args.index("-ss")
    assert seek_index > input_index
    assert "-fps_mode" in args and args[args.index("-fps_mode") + 1] == "passthrough"
    vf = args[args.index("-vf") + 1]
    assert "iw*sar" in vf
    client.__exit__(None, None, None)


def test_retry_skips_existing_export_for_candidate(tmp_path, monkeypatch):
    client, project_id, candidate_id, source = setup_project(tmp_path, monkeypatch)
    project_dir = source.parent.parent
    existing = project_dir / "exports" / "existing.mp4"
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_bytes(b"\x00" * 200)
    with database.SessionLocal() as db:
        db.add(Clip(
            project_id=project_id,
            candidate_id=candidate_id,
            file_path=str(existing),
            duration_seconds=30,
            output_profile="vertical",
        ))
        job = ProcessingJob(
            project_id=project_id,
            job_type="render",
            active_project_id=project_id,
            status="processing",
            stage="rendering",
            candidate_ids_json=json.dumps([candidate_id]),
            output_profile="vertical",
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    rendering._run_render(job_id, project_id, [candidate_id], "vertical")
    with database.SessionLocal() as db:
        assert db.query(Clip).filter_by(project_id=project_id, candidate_id=candidate_id).count() == 1
    client.__exit__(None, None, None)


def test_source_replacement_blocked_after_transcription_or_review(tmp_path, monkeypatch):
    client, project_id, _candidate_id, _source = setup_project(tmp_path, monkeypatch)
    with database.SessionLocal() as db:
        candidate = db.query(HighlightCandidate).filter_by(project_id=project_id).one()
        candidate.approved = True
        candidate.review_status = "approved"
        db.commit()
    replacement = tmp_path / "replacement.mp4"
    replacement.write_bytes(b"replacement")
    response = client.post(
        f"/api/projects/{project_id}/source-video",
        files={"file": ("replacement.mp4", replacement.read_bytes(), "video/mp4")},
    )
    assert response.status_code == 409
    client.__exit__(None, None, None)


def test_recovery_cleans_stale_render_temps(tmp_path, monkeypatch):
    data = tmp_path / "recovery-data"
    monkeypatch.setattr(config.settings, "data_dir", data)
    monkeypatch.setattr(config.settings, "database_path", data / "database" / "aiclipping.db")
    with TestClient(app) as client:
        project_id = client.post("/api/projects", json={"name": "Recovery"}).json()["id"]
        project_dir = ensure_project_directories(project_id)
        stale = project_dir / "exports" / ".abc.rendering.mp4"
        stale.write_bytes(b"partial")
        with database.SessionLocal() as db:
            db.add(ProcessingJob(
                project_id=project_id,
                job_type="render",
                active_project_id=project_id,
                status="processing",
                stage="rendering",
            ))
            db.commit()
        rendering.recover_interrupted_jobs()
        with database.SessionLocal() as db:
            job = db.query(ProcessingJob).filter_by(project_id=project_id).one()
            assert job.status == "failed"
            assert job.stage == "interrupted"
    assert not stale.exists()


def test_real_synthetic_render_with_ffmpeg_when_available(tmp_path, monkeypatch):
    ffmpeg = config.settings.resolve_ffmpeg()
    ffprobe = config.settings.resolve_ffprobe()
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg and FFprobe are required")
    source = tmp_path / "synthetic.mp4"
    create = subprocess.run([
        ffmpeg, "-v", "error", "-f", "lavfi",
        "-i", "color=c=blue:s=320x180:d=2:r=30",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
        "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-y", str(source),
    ], capture_output=True, timeout=60)
    if create.returncode != 0:
        pytest.skip("Local FFmpeg cannot encode a synthetic fixture")
    candidate = SimpleNamespace(start_seconds=0.0, end_seconds=2.0)
    output = tmp_path / "synthetic_vertical.mp4"
    result = rendering.render_candidate(source, candidate, "vertical", output)
    assert output.is_file() and output.stat().st_size > 100
    assert result["width"] == 1080 and result["height"] == 1920
