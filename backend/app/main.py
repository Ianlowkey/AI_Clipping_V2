from contextlib import asynccontextmanager
import asyncio
import ipaddress
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import Body, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import delete, or_, select
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.api_schemas import (CandidateEdit, CandidateRead, CandidateReview, CandidateSelection, ClipRead, ExportRead, GeminiKeyWrite, GeminiSettingsRead,
    HighlightAnalysisStart, HighlightJobRead, JobRead, ProjectCreate, ProjectRead, RenderJobRead, RenderStart, TranscriptionJobRead, TranscriptionStart)
from app.database import session as database
from app.database.models import Clip, ClipMetadata, HighlightCandidate, ProcessingJob, ProcessingLog, Project, SourceVideo, Transcript
from app.storage import ensure_project_directories, project_directory, project_operation_lock, safe_video_filename
from app.video import VideoPreparationError, prepare_video
from app.transcription_jobs import get_transcription_job, recover_interrupted_jobs, start_transcription
from app import highlight_detection
from app import rendering
from app import gemini_credentials

logger = logging.getLogger(__name__)
ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mpeg", ".mpg", ".wmv"}


@asynccontextmanager
async def lifespan(_: FastAPI):
    database.create_database()
    recover_project_deletion_staging()
    recover_interrupted_jobs()
    highlight_detection.recover_interrupted_jobs()
    rendering.recover_interrupted_jobs()
    yield


app = FastAPI(
    title="AIClipping Local API",
    version="0.1.0",
    lifespan=lifespan,
)


def recover_project_deletion_staging() -> None:
    """Restore or finish only app-created deletion staging folders after a crash."""
    projects_root = (settings.data_dir / "projects").resolve()
    if not projects_root.is_dir() or database.SessionLocal is None:
        return
    for staged_dir in projects_root.glob(".deleting-*"):
        if staged_dir.is_symlink() or not staged_dir.is_dir() or staged_dir.resolve().parent != projects_root:
            continue
        suffix = staged_dir.name[len(".deleting-"):]
        project_id = suffix[:36]
        nonce = suffix[37:] if len(suffix) > 37 and suffix[36] == "-" else ""
        try:
            if str(UUID(project_id)) != project_id or len(nonce) != 32 or any(char not in "0123456789abcdef" for char in nonce):
                continue
        except ValueError:
            continue
        with database.SessionLocal() as db:
            project_exists = db.get(Project, project_id) is not None
        expected_dir = projects_root / project_id
        try:
            if project_exists and not expected_dir.exists():
                os.replace(staged_dir, expected_dir)
                logger.warning("Restored project storage after interrupted deletion: %s", project_id)
            elif not project_exists:
                shutil.rmtree(staged_dir)
                logger.info("Finished staged file cleanup after interrupted project deletion: %s", project_id)
            else:
                logger.error("Project deletion staging conflicts with an existing project folder: %s", project_id)
        except OSError:
            logger.exception("Could not recover staged project deletion for %s", project_id)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "tauri://localhost",
        "http://tauri.localhost",
        "http://127.0.0.1:1420",
        "http://localhost:1420",
    ],
    allow_credentials=True,
    allow_methods=["GET", "HEAD", "POST", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "database_ready": settings.database_path.exists(),
        "mode": "local",
    }


def get_session():
    if database.SessionLocal is None:
        raise HTTPException(status_code=503, detail="Database is not ready")
    return database.SessionLocal()


@app.get("/api/projects", response_model=list[ProjectRead])
def list_projects():
    with get_session() as db:
        return db.scalars(
            select(Project).options(selectinload(Project.source_videos)).order_by(Project.created_at.desc())
        ).all()


@app.get("/api/projects/{project_id}", response_model=ProjectRead)
def get_project(project_id: str):
    with get_session() as db:
        project = db.scalar(
            select(Project)
            .where(Project.id == project_id)
            .options(selectinload(Project.source_videos))
        )
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        return project


@app.delete("/api/projects/{project_id}")
def delete_project(project_id: str):
    try:
        expected_id = str(UUID(project_id))
        project_dir = project_directory(expected_id)
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(status_code=422, detail="Project identifier is invalid") from exc

    projects_root = (settings.data_dir / "projects").resolve()
    expected_dir = projects_root / expected_id
    if project_dir != expected_dir or expected_dir.is_symlink():
        raise HTTPException(status_code=400, detail="Project storage path is not a safe child of the projects directory")
    tombstone = projects_root / f".deleting-{expected_id}-{uuid4().hex}"
    moved = False

    # The same per-project lock protects uploads in this API process. SQLite's
    # transaction and active-job check additionally protect transcription jobs.
    with project_operation_lock(expected_id):
        try:
            with get_session() as db:
                with db.begin():
                    project = db.get(Project, expected_id)
                    if project is None:
                        raise HTTPException(status_code=404, detail="Project not found")
                    active_job = db.scalar(select(ProcessingJob.id).where(
                        ProcessingJob.project_id == expected_id,
                        or_(ProcessingJob.status.in_(("queued", "processing")), ProcessingJob.active_project_id == expected_id),
                    ).limit(1))
                    if active_job:
                        raise HTTPException(status_code=409, detail="This project has an active processing job. Wait for it to finish before deleting the project.")

                    if project_dir.exists():
                        if not project_dir.is_dir():
                            raise HTTPException(status_code=400, detail="Project storage path is not a directory")
                        project.status = "deleting"
                        db.flush()
                        os.replace(project_dir, tombstone)
                        moved = True

                    job_ids = select(ProcessingJob.id).where(ProcessingJob.project_id == expected_id)
                    clip_ids = select(Clip.id).where(Clip.project_id == expected_id)
                    db.execute(delete(ProcessingLog).where(ProcessingLog.job_id.in_(job_ids)))
                    db.execute(delete(ProcessingJob).where(ProcessingJob.project_id == expected_id))
                    db.execute(delete(ClipMetadata).where(ClipMetadata.clip_id.in_(clip_ids)))
                    db.execute(delete(Clip).where(Clip.project_id == expected_id))
                    db.execute(delete(HighlightCandidate).where(HighlightCandidate.project_id == expected_id))
                    db.execute(delete(SourceVideo).where(SourceVideo.project_id == expected_id))
                    db.execute(delete(Transcript).where(Transcript.project_id == expected_id))
                    db.delete(project)
        except HTTPException:
            if moved:
                try:
                    os.replace(tombstone, project_dir)
                except OSError:
                    logger.exception("Could not restore project storage after deletion was cancelled for %s", expected_id)
            raise
        except OSError as exc:
            if moved:
                try:
                    os.replace(tombstone, project_dir)
                except OSError:
                    logger.exception("Could not restore project storage after deletion failed for %s", expected_id)
            logger.exception("Filesystem operation failed while deleting project %s", expected_id)
            raise HTTPException(status_code=500, detail="Project files could not be safely staged for deletion. The project was kept; check local logs.") from exc
        except Exception as exc:
            if moved:
                try:
                    os.replace(tombstone, project_dir)
                except OSError:
                    logger.exception("Could not restore project storage after database deletion failed for %s", expected_id)
            logger.exception("Database operation failed while deleting project %s", expected_id)
            raise HTTPException(status_code=500, detail="Project deletion failed. The project was kept where possible; check local logs.") from exc

        cleanup_pending = False
        if moved:
            try:
                shutil.rmtree(tombstone)
            except OSError:
                cleanup_pending = True
                logger.exception("Project %s was removed from the database, but staged file cleanup is pending", expected_id)
        return {"deleted": True, "cleanup_pending": cleanup_pending}


@app.post("/api/projects", response_model=ProjectRead, status_code=201)
def create_project(payload: ProjectCreate):
    if payload.video_url and not payload.video_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=422, detail="Video URL must start with http:// or https://")

    project = Project(name=payload.name, description=payload.description or None)
    if payload.video_url:
        project.source_videos.append(
            SourceVideo(source_type="url", source_uri=payload.video_url, title=payload.video_url)
        )

    with get_session() as db:
        db.add(project)
        db.commit()
        db.refresh(project)
        db.refresh(project, attribute_names=["source_videos"])
        try:
            ensure_project_directories(project.id)
        except OSError:
            logger.exception("Could not initialize project storage for %s", project.id)
            raise HTTPException(status_code=500, detail="Project was created but its storage folders could not be initialized.")
        return project


@app.post("/api/projects/{project_id}/source-video", status_code=201)
async def upload_source_video(project_id: str, file: UploadFile = File(...)):
    lock = project_operation_lock(project_id)
    await asyncio.to_thread(lock.acquire)
    try:
        return await _upload_source_video_locked(project_id, file)
    finally:
        lock.release()


@app.get("/api/projects/{project_id}/source-video/preview")
def preview_source_video(project_id: str):
    """Stream only the validated source file recorded for this project."""
    with get_session() as db:
        project = db.get(Project, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        source = db.scalar(select(SourceVideo).where(
            SourceVideo.project_id == project_id,
            SourceVideo.source_type == "local_file",
        ).order_by(SourceVideo.created_at.desc()))
        if source is None or not source.local_path:
            raise HTTPException(status_code=404, detail="No local source video is available for preview")
        try:
            project_dir = project_directory(project_id).resolve(strict=True)
            allowed_directory = (project_dir / "source").resolve(strict=True)
            path = Path(source.local_path).resolve(strict=True)
        except (OSError, ValueError):
            raise HTTPException(status_code=404, detail="The source video is unavailable")
        if allowed_directory.parent != project_dir or path.parent != allowed_directory or not path.is_file():
            raise HTTPException(status_code=404, detail="The source video is unavailable")
        filename = path.name
    return FileResponse(path, filename=filename, content_disposition_type="inline")


async def _upload_source_video_locked(project_id: str, file: UploadFile):
    if not file.filename or not file.filename.strip():
        raise HTTPException(status_code=422, detail="Choose a video file to upload.")
    extension = Path(file.filename.replace("\\", "/").split("/")[-1]).suffix.lower()
    if extension not in ALLOWED_VIDEO_EXTENSIONS:
        raise HTTPException(status_code=415, detail=f"Unsupported video extension. Allowed: {', '.join(sorted(ALLOWED_VIDEO_EXTENSIONS))}.")

    with get_session() as db:
        project = db.get(Project, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found. Create or select a project before uploading.")
        transcript = db.scalar(select(Transcript).where(Transcript.project_id == project_id))
        candidate = db.scalar(select(HighlightCandidate).where(HighlightCandidate.project_id == project_id).limit(1))
        if transcript is not None or candidate is not None:
            raise HTTPException(status_code=409, detail="The source cannot be replaced after transcription or highlight review. Create a new project for a different source video.")

    try:
        project_dir = ensure_project_directories(project_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        logger.exception("Could not initialize source directory for %s", project_id)
        raise HTTPException(status_code=500, detail="Project storage is unavailable. Check the configured data directory.") from exc
    source_dir = project_dir / "source"
    temp_path: Path | None = None
    stored_path: Path | None = None
    size = 0
    try:
        with tempfile.NamedTemporaryFile(prefix=".upload-", suffix=extension, dir=source_dir, delete=False) as temporary:
            temp_path = Path(temporary.name)
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > settings.max_upload_size_bytes:
                    raise HTTPException(status_code=413, detail=f"Video exceeds the {settings.max_upload_size_bytes // (1024 * 1024)} MiB upload limit.")
                temporary.write(chunk)
        if size == 0:
            raise HTTPException(status_code=422, detail="The selected video file is empty.")
        try:
            preparation = prepare_video(temp_path)
        except VideoPreparationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        filename = f"{uuid4().hex}_{safe_video_filename(file.filename)}"
        stored_path = source_dir / filename
        # A hard link is atomic and fails if a name already exists; it never overwrites.
        try:
            os.link(temp_path, stored_path)
        except OSError as exc:
            logger.exception("Could not commit uploaded video to project storage %s", project_id)
            raise HTTPException(status_code=500, detail="The uploaded video could not be moved into project storage. Check disk space and permissions.") from exc
        metadata_json = json.dumps(preparation, separators=(",", ":"))
        source = SourceVideo(
            project_id=project_id,
            source_type="local_file",
            source_uri=str(stored_path),
            local_path=str(stored_path),
            title=safe_video_filename(file.filename),
            duration_seconds=preparation["duration_seconds"],
            metadata_json=metadata_json,
        )
        try:
            with get_session() as db:
                db.add(source)
                db.commit()
                db.refresh(source)
        except Exception as exc:
            # Keep the successfully imported source intact; report its local path for recovery.
            logger.exception("Video copied for project %s but metadata save failed", project_id)
            raise HTTPException(status_code=500, detail=f"Video is stored at {stored_path}, but its database record could not be saved.") from exc
        return {"source_video": source, "preparation": preparation, "size_bytes": size}
    except HTTPException:
        raise
    except OSError as exc:
        logger.exception("Disk or filesystem error while importing source video for %s", project_id)
        raise HTTPException(status_code=500, detail="The video could not be stored. Check available disk space and project-folder permissions.") from exc
    finally:
        await file.close()
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                logger.warning("Could not remove temporary upload file for project %s", project_id)


@app.get("/api/clips", response_model=list[ClipRead])
def list_clips():
    with get_session() as db:
        return db.scalars(select(Clip).order_by(Clip.created_at.desc())).all()


@app.get("/api/jobs", response_model=list[JobRead])
def list_jobs():
    with get_session() as db:
        jobs = db.scalars(
            select(ProcessingJob).options(selectinload(ProcessingJob.project)).order_by(ProcessingJob.created_at.desc())
        ).all()
        return [
            JobRead(
                id=job.id,
                project_id=job.project_id,
                project_name=job.project.name,
                job_type=job.job_type,
                stage=job.stage,
                model_name=job.model_name,
                device=job.device,
                compute_type=job.compute_type,
                language=job.language,
                started_at=job.started_at,
                completed_at=job.completed_at,
                status=job.status,
                progress_percent=job.progress_percent,
                error_message=job.error_message,
                created_at=job.created_at,
                updated_at=job.updated_at,
            )
            for job in jobs
        ]


@app.get("/api/settings/gemini", response_model=GeminiSettingsRead)
def gemini_settings(request: Request):
    _require_local_request(request)
    return GeminiSettingsRead(**gemini_credentials.configuration_status())


def _require_local_request(request: Request) -> None:
    host = request.client.host if request.client else ""
    try:
        is_local = ipaddress.ip_address(host).is_loopback
    except ValueError:
        is_local = False
    if not is_local:
        raise HTTPException(status_code=403, detail="Gemini key management is available only to the local application.")


@app.put("/api/settings/gemini/key", response_model=GeminiSettingsRead)
async def save_gemini_key(request: Request):
    _require_local_request(request)
    if request.headers.get("content-length", "0").isdigit() and int(request.headers["content-length"]) > 2048:
        raise HTTPException(status_code=413, detail="Gemini API key request is too large.")
    raw_content = await request.body()
    if len(raw_content) > 2048:
        raise HTTPException(status_code=413, detail="Gemini API key request is too large.")
    try:
        raw_body = json.loads(raw_content)
        payload = GeminiKeyWrite.model_validate(raw_body)
        api_key = payload.api_key.get_secret_value()
        gemini_credentials.validate_api_key(api_key)
    except Exception:
        raise HTTPException(status_code=422, detail="Enter a valid Gemini API key (20–256 letters, digits, underscores or hyphens).") from None
    try:
        gemini_credentials.save_api_key(api_key)
    except gemini_credentials.CredentialStoreUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    return GeminiSettingsRead(**gemini_credentials.configuration_status())


@app.delete("/api/settings/gemini/key", response_model=GeminiSettingsRead)
def delete_gemini_key(request: Request):
    _require_local_request(request)
    try:
        gemini_credentials.delete_saved_api_key()
    except gemini_credentials.CredentialStoreUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    return GeminiSettingsRead(**gemini_credentials.configuration_status())


def _highlight_job_read(job: ProcessingJob) -> HighlightJobRead:
    return HighlightJobRead(id=job.id, project_id=job.project_id, status=job.status, stage=job.stage,
        error_message=job.error_message, model_name=job.model_name, detection_mode=job.detection_mode,
        content_profile=job.content_profile, started_at=job.started_at,
        completed_at=job.completed_at, updated_at=job.updated_at)


@app.post("/api/projects/{project_id}/highlights", response_model=HighlightJobRead, status_code=202)
def begin_highlight_analysis(project_id: str, payload: HighlightAnalysisStart | None = None):
    try:
        mode = payload.detection_mode if payload is not None else "general"
        profile = payload.content_profile if payload is not None else "podcast_conversation"
        return _highlight_job_read(highlight_detection.start_analysis(project_id, mode, profile))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except highlight_detection.HighlightError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/projects/{project_id}/highlights/jobs/latest", response_model=HighlightJobRead)
def latest_highlight_job(project_id: str):
    job = highlight_detection.get_job(project_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No highlight analysis exists for this project")
    return _highlight_job_read(job)


@app.get("/api/projects/{project_id}/highlights/jobs/{job_id}", response_model=HighlightJobRead)
def read_highlight_job(project_id: str, job_id: str):
    job = highlight_detection.get_job(project_id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Highlight analysis job not found")
    return _highlight_job_read(job)


@app.get("/api/projects/{project_id}/highlights/candidates", response_model=list[CandidateRead])
def read_highlight_candidates(project_id: str):
    with get_session() as db:
        if db.get(Project, project_id) is None:
            raise HTTPException(status_code=404, detail="Project not found")
    return highlight_detection.list_candidates(project_id)


@app.get("/api/projects/{project_id}/highlights/candidates/{candidate_id}", response_model=CandidateRead)
def read_highlight_candidate(project_id: str, candidate_id: str):
    candidate = highlight_detection.list_candidates(project_id)
    result = next((item for item in candidate if item.id == candidate_id), None)
    if result is None:
        raise HTTPException(status_code=404, detail="Highlight candidate not found")
    return result


@app.patch("/api/projects/{project_id}/highlights/candidates/{candidate_id}", response_model=CandidateRead)
def update_highlight_candidate(project_id: str, candidate_id: str, payload: CandidateEdit):
    try:
        with get_session() as db:
            candidate = db.get(HighlightCandidate, candidate_id)
            if candidate is None or candidate.project_id != project_id:
                raise LookupError("Highlight candidate not found")
        return highlight_detection.edit_candidate(candidate_id, payload.start_seconds, payload.end_seconds,
            payload.title, payload.hook)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.patch("/api/projects/{project_id}/highlights/candidates/{candidate_id}/review", response_model=CandidateRead)
def review_highlight_candidate(project_id: str, candidate_id: str, payload: CandidateReview):
    with get_session() as db:
        candidate = db.get(HighlightCandidate, candidate_id)
        if candidate is None or candidate.project_id != project_id:
            raise HTTPException(status_code=404, detail="Highlight candidate not found")
        candidate.review_status = payload.status
        candidate.approved = payload.status == "approved"
        if payload.status == "rejected":
            candidate.selected_for_render = False
        db.commit()
        db.refresh(candidate)
        return candidate


@app.patch("/api/projects/{project_id}/highlights/candidates/{candidate_id}/selection", response_model=CandidateRead)
def select_highlight_candidate(project_id: str, candidate_id: str, payload: CandidateSelection):
    with get_session() as db:
        candidate = db.get(HighlightCandidate, candidate_id)
        if candidate is None or candidate.project_id != project_id:
            raise HTTPException(status_code=404, detail="Highlight candidate not found")
        if payload.selected and (not candidate.approved or candidate.review_status != "approved"):
            raise HTTPException(status_code=409, detail="Only approved candidates can be selected for rendering")
        candidate.selected_for_render = payload.selected
        db.commit()
        db.refresh(candidate)
        return candidate


def _transcription_job_response(job: ProcessingJob) -> TranscriptionJobRead:
    return TranscriptionJobRead(
        id=job.id,
        project_id=job.project_id,
        status=job.status,
        stage=job.stage,
        error_message=job.error_message,
        model_name=job.model_name,
        device=job.device,
        compute_type=job.compute_type,
        language=job.language,
        started_at=job.started_at,
        completed_at=job.completed_at,
        updated_at=job.updated_at,
    )


@app.post("/api/projects/{project_id}/transcription", response_model=TranscriptionJobRead, status_code=202)
def begin_transcription(project_id: str, payload: TranscriptionStart = Body(default=TranscriptionStart())):
    try:
        job = start_transcription(project_id, regenerate=payload.regenerate)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return _transcription_job_response(job)


@app.get("/api/projects/{project_id}/transcription/jobs/{job_id}", response_model=TranscriptionJobRead)
def read_transcription_job(project_id: str, job_id: str):
    job = get_transcription_job(project_id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Transcription job not found")
    return _transcription_job_response(job)


@app.get("/api/projects/{project_id}/transcription", response_model=TranscriptionJobRead)
def read_latest_transcription_job(project_id: str):
    job = get_transcription_job(project_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No transcription job exists for this project")
    return _transcription_job_response(job)


@app.get("/api/projects/{project_id}/transcript")
def read_transcript(project_id: str):
    with get_session() as db:
        project = db.get(Project, project_id)
        transcript = db.scalar(select(Transcript).where(Transcript.project_id == project_id))
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        if transcript is None or not transcript.words_path:
            raise HTTPException(status_code=404, detail="No completed transcript exists for this project")
        words_path = Path(transcript.words_path).resolve()
        allowed_dir = (settings.data_dir / "projects" / project.id / "transcript").resolve()
        output_paths = [Path(value or "").resolve() for value in (transcript.text_path, transcript.srt_path, transcript.words_path)]
        if any(path.parent != allowed_dir or not path.is_file() for path in output_paths) or words_path.parent != allowed_dir:
            raise HTTPException(status_code=404, detail="Transcript output files are missing")
        try:
            payload = json.loads(words_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=500, detail="Transcript data could not be read") from exc
        return {"metadata": {key: payload.get(key) for key in (
            "project_id", "source_filename", "language", "language_probability", "model", "device", "compute_type", "processed_at", "duration_seconds"
        )} | {key: payload.get(key) for key in ("diarization_status", "diarization_detail", "diarization_message", "speakers", "speaker_turns")},
            "segments": payload.get("segments", []), "words": payload.get("words", [])}


@app.get("/api/projects/{project_id}/transcript-folder")
def transcript_folder(project_id: str):
    with get_session() as db:
        project = db.get(Project, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        folder = (settings.data_dir / "projects" / project.id / "transcript").resolve()
        return {"path": str(folder)}


def _render_job_read(job: ProcessingJob) -> RenderJobRead:
    candidate_count = len(json.loads(job.candidate_ids_json or "[]"))
    return RenderJobRead(
        id=job.id,
        project_id=job.project_id,
        status=job.status,
        stage=job.stage,
        progress_percent=job.progress_percent,
        error_message=job.error_message,
        output_profile=job.output_profile,
        candidate_count=candidate_count,
        started_at=job.started_at,
        completed_at=job.completed_at,
        updated_at=job.updated_at,
    )


@app.post("/api/projects/{project_id}/render", response_model=RenderJobRead, status_code=202)
def begin_render(project_id: str, payload: RenderStart):
    try:
        job = rendering.start_render(project_id, payload.candidate_ids, payload.output_profile)
        return _render_job_read(job)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except rendering.RenderError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/projects/{project_id}/render/jobs/latest", response_model=RenderJobRead)
def latest_render_job(project_id: str):
    job = rendering.get_render_job(project_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No render job exists for this project")
    return _render_job_read(job)


@app.get("/api/projects/{project_id}/render/jobs/{job_id}", response_model=RenderJobRead)
def read_render_job(project_id: str, job_id: str):
    job = rendering.get_render_job(project_id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Render job not found")
    return _render_job_read(job)


@app.post("/api/projects/{project_id}/render/jobs/{job_id}/retry", response_model=RenderJobRead, status_code=202)
def retry_render_job(project_id: str, job_id: str):
    job = rendering.get_render_job(project_id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Render job not found")
    if job.status != "failed":
        raise HTTPException(status_code=409, detail="Only failed or interrupted render jobs can be retried")
    try:
        candidate_ids = json.loads(job.candidate_ids_json or "[]")
        retried = rendering.start_render(project_id, candidate_ids, job.output_profile or "vertical")
        return _render_job_read(retried)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/projects/{project_id}/exports", response_model=list[ExportRead])
def list_project_exports(project_id: str):
    with get_session() as db:
        if db.get(Project, project_id) is None:
            raise HTTPException(status_code=404, detail="Project not found")
    exports = rendering.list_exports(project_id)
    result = []
    for clip in exports:
        path = Path(clip.file_path or "")
        if not path.is_file():
            continue
        allowed = (project_directory(project_id) / "exports").resolve()
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved.parent != allowed:
            continue
        result.append(ExportRead(
            id=clip.id,
            project_id=clip.project_id,
            candidate_id=clip.candidate_id,
            file_path=str(resolved),
            filename=resolved.name,
            duration_seconds=clip.duration_seconds,
            output_profile=clip.output_profile,
            file_size_bytes=clip.file_size_bytes,
            width=clip.width,
            height=clip.height,
            created_at=clip.created_at,
        ))
    return result


@app.get("/api/projects/{project_id}/exports/{export_id}/download")
def download_export(project_id: str, export_id: str):
    with get_session() as db:
        clip = db.get(Clip, export_id)
    if clip is None or clip.project_id != project_id:
        raise HTTPException(status_code=404, detail="Export not found")
    path = Path(clip.file_path or "").resolve()
    allowed = (project_directory(project_id) / "exports").resolve()
    if path.parent != allowed or not path.is_file():
        raise HTTPException(status_code=404, detail="Export file is missing")
    return FileResponse(path, media_type="video/mp4", filename=path.name)


@app.get("/api/projects/{project_id}/transcript/{output_format}")
def download_transcript(project_id: str, output_format: str):
    columns = {"txt": "text_path", "srt": "srt_path", "json": "words_path"}
    if output_format not in columns:
        raise HTTPException(status_code=404, detail="Transcript format must be txt, srt, or json")
    with get_session() as db:
        project = db.get(Project, project_id)
        transcript = db.scalar(select(Transcript).where(Transcript.project_id == project_id))
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        if transcript is None:
            raise HTTPException(status_code=404, detail="No completed transcript exists for this project")
        path = Path(getattr(transcript, columns[output_format]) or "").resolve()
        allowed_dir = (settings.data_dir / "projects" / project.id / "transcript").resolve()
        if path.parent != allowed_dir or not path.is_file():
            raise HTTPException(status_code=404, detail="Transcript output file is missing")
    media_types = {"txt": ("text/plain", "transcript.txt"), "srt": ("application/x-subrip", "transcript.srt"), "json": ("application/json", "words.json")}
    media_type, filename = media_types[output_format]
    return FileResponse(path, media_type=media_type, filename=filename)
