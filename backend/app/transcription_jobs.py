import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.database import session as database
from app.database.models import ProcessingJob, Project, SourceVideo, Transcript
from app.storage import project_directory
from app.transcription import TranscriptionError, transcribe_video

logger = logging.getLogger(__name__)
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="aiclipping-transcription")


def _now():
    return datetime.now(timezone.utc)


def recover_interrupted_jobs() -> None:
    if database.SessionLocal is None:
        return
    with database.SessionLocal() as db:
        jobs = db.scalars(select(ProcessingJob).where(
            ProcessingJob.job_type == "transcription",
            ProcessingJob.status.in_(("queued", "processing")),
        )).all()
        for job in jobs:
            job.status = "failed"
            job.stage = "interrupted"
            job.error_message = "The application stopped before transcription completed. You can retry the job."
            job.completed_at = _now()
            job.active_project_id = None
            project = db.get(Project, job.project_id)
            if project and project.status == "transcribing":
                project.status = "transcription_failed"
        db.commit()


def _get_job(job_id: str) -> ProcessingJob:
    with database.SessionLocal() as db:
        job = db.get(ProcessingJob, job_id)
        if job is None:
            raise LookupError("Transcription job not found")
        db.expunge(job)
        return job


def start_transcription(project_id: str, regenerate: bool = False) -> ProcessingJob:
    if database.SessionLocal is None:
        raise RuntimeError("Database is not ready")
    with database.SessionLocal() as db:
        project = db.get(Project, project_id)
        if project is None:
            raise LookupError("Project not found")
        source = db.scalar(select(SourceVideo).where(
            SourceVideo.project_id == project_id,
            SourceVideo.source_type == "local_file",
            SourceVideo.local_path.is_not(None),
        ).order_by(SourceVideo.created_at.desc()))
        if source is None:
            raise ValueError("Import and prepare a local video before starting transcription.")
        try:
            source_path = Path(source.local_path).resolve(strict=True)
            source_root = (project_directory(project_id) / "source").resolve()
        except (OSError, ValueError) as exc:
            raise ValueError("The imported source file is missing or its project path is invalid. Re-import the video.") from exc
        if not source_path.is_file() or source_path.parent != source_root:
            raise ValueError("The imported source file is missing or its project path is invalid. Re-import the video.")
        try:
            prepared = json.loads(source.metadata_json or "{}")
        except json.JSONDecodeError:
            prepared = {}
        if not prepared.get("readable"):
            raise ValueError("The source video has not passed preparation. Re-import the video before transcribing.")
        existing = db.scalar(select(Transcript).where(Transcript.project_id == project_id))
        if existing and not regenerate:
            raise FileExistsError("A transcript already exists. Use Regenerate transcription to replace it.")
        if existing and not all(path and Path(path).is_file() for path in (existing.text_path, existing.srt_path, existing.words_path)):
            raise FileExistsError("A transcript record already exists. Request explicit regeneration to replace it.")
        job = ProcessingJob(
            project_id=project_id,
            job_type="transcription",
            active_project_id=project_id,
            status="queued",
            stage="queued",
            model_name=settings.whisper_model_size,
            device=settings.whisper_device,
            compute_type=settings.whisper_compute_type,
            language=settings.whisper_language,
        )
        db.add(job)
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise FileExistsError("A transcription is already queued or running for this project.") from exc
        db.refresh(job)
        job_id = job.id
        source_id = source.id
    _executor.submit(_run_transcription, job_id, project_id, source_id, regenerate)
    return _get_job(job_id)


def _set_stage(job_id: str, stage: str, detail: str, device: str | None = None) -> None:
    with database.SessionLocal() as db:
        job = db.get(ProcessingJob, job_id)
        if job is None or job.status not in ("queued", "processing"):
            return
        job.status = "processing"
        job.stage = stage
        if device:
            job.device = device
            if device == "cpu" and settings.whisper_cpu_fallback and "fallback" in detail.lower():
                job.compute_type = "int8"
        if stage == "preparing_audio" and job.started_at is None:
            job.started_at = _now()
        db.commit()
    logger.info("Transcription job %s stage: %s (%s)", job_id, stage, detail)


def _run_transcription(job_id: str, project_id: str, source_id: str, regenerate: bool) -> None:
    try:
        with database.SessionLocal() as db:
            job = db.get(ProcessingJob, job_id)
            project = db.get(Project, project_id)
            source = db.get(SourceVideo, source_id)
            if not job or not project or not source:
                raise TranscriptionError("Project or imported source could not be loaded.")
            project.status = "transcribing"
            job.status = "processing"
            job.stage = "preparing_audio"
            job.started_at = _now()
            source_path = Path(source.local_path)
            source_filename = source.title or source_path.name
            transcript_dir = project_directory(project_id) / "transcript"
            db.commit()

        result = transcribe_video(
            source_path=source_path,
            project_id=project_id,
            source_filename=source_filename,
            transcript_dir=transcript_dir,
            stage_callback=lambda stage, detail, device: _set_stage(job_id, stage, detail, device),
        )
        with database.SessionLocal() as db:
            transcript = db.scalar(select(Transcript).where(Transcript.project_id == project_id))
            if transcript is None:
                transcript = Transcript(project_id=project_id)
                db.add(transcript)
            transcript.language = result["language"]
            transcript.text_path = result["text_path"]
            transcript.srt_path = result["srt_path"]
            transcript.words_path = result["words_path"]
            transcript.source_filename = result["source_filename"]
            transcript.model_name = result["model"]
            transcript.device = result["device"]
            transcript.compute_type = result["compute_type"]
            transcript.language_probability = result["language_probability"]
            transcript.duration_seconds = result["duration_seconds"]
            project = db.get(Project, project_id)
            if project:
                project.status = "transcribed"
            job = db.get(ProcessingJob, job_id)
            if job:
                job.status = "completed"
                job.stage = "completed"
                job.model_name = result["model"]
                job.device = result["device"]
                job.compute_type = result["compute_type"]
                job.language = result["language"]
                job.progress_percent = 100
                job.completed_at = _now()
                job.active_project_id = None
            db.commit()
    except Exception as exc:
        logger.exception("Transcription job %s failed", job_id)
        message = str(exc) if isinstance(exc, TranscriptionError) else "Transcription failed. Check the local logs and retry."
        with database.SessionLocal() as db:
            job = db.get(ProcessingJob, job_id)
            project = db.get(Project, project_id)
            if job:
                job.status = "failed"
                job.stage = "failed"
                job.error_message = message
                job.completed_at = _now()
                job.active_project_id = None
            if project:
                project.status = "transcription_failed"
            db.commit()


def get_transcription_job(project_id: str, job_id: str | None = None) -> ProcessingJob | None:
    with database.SessionLocal() as db:
        statement = select(ProcessingJob).where(
            ProcessingJob.project_id == project_id,
            ProcessingJob.job_type == "transcription",
        )
        if job_id:
            statement = statement.where(ProcessingJob.id == job_id)
        job = db.scalar(statement.order_by(ProcessingJob.created_at.desc()))
        if job:
            db.expunge(job)
        return job

