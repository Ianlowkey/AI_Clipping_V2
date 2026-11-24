import json
import logging
import math
import os
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.database import session as database
from app.database.models import Clip, HighlightCandidate, ProcessingJob, Project, SourceVideo
from app.storage import project_directory

logger = logging.getLogger(__name__)
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="aiclipping-render")
PROFILES = {"original", "vertical", "square"}


class RenderError(Exception):
    pass


def _now():
    return datetime.now(timezone.utc)


def _set_job(job_id: str, *, status: str, stage: str, progress: int | None = None, error: str | None = None) -> None:
    with database.SessionLocal() as db:
        job = db.get(ProcessingJob, job_id)
        if job is None or job.status not in ("queued", "processing"):
            return
        job.status = status
        job.stage = stage
        if progress is not None:
            job.progress_percent = progress
        if error is not None:
            job.error_message = error
        if status in ("completed", "failed"):
            job.completed_at = _now()
            job.active_project_id = None
        db.commit()


def recover_interrupted_jobs() -> None:
    if database.SessionLocal is None:
        return
    with database.SessionLocal() as db:
        jobs = db.scalars(select(ProcessingJob).where(
            ProcessingJob.job_type == "render",
            ProcessingJob.status.in_(("queued", "processing")),
        )).all()
        for job in jobs:
            job.status = "failed"
            job.stage = "interrupted"
            job.error_message = "The application stopped before rendering completed. Existing exports are preserved; retry the job."
            job.completed_at = _now()
            job.active_project_id = None
        db.commit()
    # Remove only stale temp render files owned by this project service.
    projects_root = (settings.data_dir / "projects").resolve()
    if projects_root.is_dir():
        for temp_path in projects_root.glob("*/temp/*.rendering.mp4"):
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                logger.warning("Could not remove stale render temp file: %s", temp_path)
        for temp_path in projects_root.glob("*/exports/*.rendering.mp4"):
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                logger.warning("Could not remove stale export temp file: %s", temp_path)


def start_render(project_id: str, candidate_ids: list[str], output_profile: str) -> ProcessingJob:
    if output_profile not in PROFILES:
        raise ValueError("Output profile must be original, vertical, or square.")
    unique_ids = list(dict.fromkeys(candidate_ids))
    with database.SessionLocal() as db:
        project = db.get(Project, project_id)
        if project is None:
            raise LookupError("Project not found")
        if not unique_ids:
            selected = db.scalars(select(HighlightCandidate).where(
                HighlightCandidate.project_id == project_id,
                HighlightCandidate.selected_for_render.is_(True),
            )).all()
            if not selected:
                raise ValueError("No candidates are selected for rendering.")
            if not any(item.approved and item.review_status == "approved" for item in selected):
                raise ValueError("Selected candidates are not approved. Approve them before rendering.")
            raise ValueError("No candidate IDs were provided for rendering.")
        source = db.scalar(select(SourceVideo).where(
            SourceVideo.project_id == project_id,
            SourceVideo.source_type == "local_file",
            SourceVideo.local_path.is_not(None),
        ).order_by(SourceVideo.created_at.desc()))
        if source is None or not source.local_path:
            raise ValueError("Import and prepare a local source video before rendering.")
        source_path = Path(source.local_path).resolve()
        source_root = (project_directory(project_id) / "source").resolve()
        if not source_path.is_file() or source_path.parent != source_root:
            raise ValueError("The project source video is missing or outside the project source directory.")
        candidates = db.scalars(select(HighlightCandidate).where(
            HighlightCandidate.project_id == project_id,
            HighlightCandidate.id.in_(unique_ids),
        )).all()
        candidate_map = {item.id: item for item in candidates}
        if len(candidate_map) != len(unique_ids):
            raise LookupError("One or more candidate IDs are invalid or do not belong to this project.")
        for candidate in candidate_map.values():
            if not candidate.approved or candidate.review_status != "approved":
                raise ValueError("One or more selected candidates are not approved. Approve them before rendering.")
            if not candidate.selected_for_render:
                raise ValueError("One or more candidates are not selected for rendering.")
            if not math.isfinite(candidate.start_seconds) or not math.isfinite(candidate.end_seconds):
                raise ValueError("Candidate timestamps are invalid.")
            if candidate.start_seconds < 0 or candidate.end_seconds <= candidate.start_seconds:
                raise ValueError("Candidate timestamps are invalid.")
        job = ProcessingJob(
            project_id=project_id,
            job_type="render",
            active_project_id=project_id,
            status="queued",
            stage="queued",
            progress_percent=0,
            output_profile=output_profile,
            candidate_ids_json=json.dumps(unique_ids),
        )
        db.add(job)
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise FileExistsError("A processing job is already queued or running for this project.") from exc
        db.refresh(job)
        job_id = job.id
    _executor.submit(_run_render, job_id, project_id, unique_ids, output_profile)
    return get_render_job(project_id, job_id)


def _probe(path: Path) -> dict:
    ffprobe = settings.resolve_ffprobe()
    if not ffprobe:
        raise RenderError("FFprobe is unavailable. Configure it or add it to PATH.")
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries",
             "format=duration,size,start_time:stream=codec_type,width,height,duration,start_time,r_frame_rate,avg_frame_rate,sample_aspect_ratio",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RenderError("FFprobe could not inspect the source or rendered clip.") from exc
    if result.returncode != 0:
        raise RenderError("FFprobe rejected the media file.")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RenderError("FFprobe returned an invalid media report.") from exc


def _video_filter(profile: str) -> str:
    # Square-pixel normalization prevents anamorphic sources from being stretched.
    normalize_sar = "scale=trunc(iw*sar/2)*2:trunc(ih/2)*2,setsar=1"
    if profile == "vertical":
        return f"{normalize_sar},scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,setpts=PTS-STARTPTS"
    if profile == "square":
        return f"{normalize_sar},scale=1080:1080:force_original_aspect_ratio=increase,crop=1080:1080,setsar=1,setpts=PTS-STARTPTS"
    return f"{normalize_sar},scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1,setpts=PTS-STARTPTS"


def _reserve_and_publish(temp_path: Path, final_path: Path) -> None:
    """Reserve the final name without overwriting an existing export, then atomically publish."""
    reservation = None
    try:
        reservation = os.open(str(final_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(reservation)
        reservation = None
        os.replace(temp_path, final_path)
    except FileExistsError as exc:
        temp_path.unlink(missing_ok=True)
        raise RenderError("An export with the generated collision-safe name already exists.") from exc
    except OSError as exc:
        if reservation is not None:
            try:
                os.close(reservation)
            except OSError:
                pass
        try:
            final_path.unlink(missing_ok=True)
        except OSError:
            pass
        temp_path.unlink(missing_ok=True)
        raise RenderError("The verified export could not be published safely. Check disk space and permissions.") from exc


def render_candidate(source: Path, candidate: HighlightCandidate, profile: str, final_path: Path) -> dict:
    ffmpeg = settings.resolve_ffmpeg()
    if not ffmpeg:
        raise RenderError("FFmpeg is unavailable. Configure it or add it to PATH.")
    source_probe = _probe(source)
    streams = source_probe.get("streams", [])
    has_audio = any(stream.get("codec_type") == "audio" for stream in streams)
    source_duration = float(source_probe.get("format", {}).get("duration") or 0)
    start = float(candidate.start_seconds)
    end = float(candidate.end_seconds)
    if end > source_duration + 0.1:
        raise RenderError("Candidate end timestamp is outside the source video duration.")
    duration = end - start
    if not 0.01 < duration <= 3600:
        raise RenderError("Candidate duration is outside the supported rendering range.")

    temp_path = final_path.with_name(f".{uuid.uuid4().hex}.rendering.mp4")
    temp_path.unlink(missing_ok=True)
    args = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source),
        "-ss", f"{start:.6f}", "-t", f"{duration:.6f}",
        "-map", "0:v:0",
    ]
    if has_audio:
        args.extend(["-map", "0:a:0"])
    args.extend([
        "-vf", _video_filter(profile),
        "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
        "-map_metadata", "-1", "-sn", "-dn",
    ])
    if has_audio:
        args.extend(["-af", "asetpts=PTS-STARTPTS", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"])
    else:
        args.extend(["-an"])
    args.extend(["-fps_mode", "passthrough", "-reset_timestamps", "1",
                 "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", str(temp_path)])
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=24 * 60 * 60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        temp_path.unlink(missing_ok=True)
        raise RenderError("FFmpeg could not start or timed out while rendering.") from exc
    if result.returncode != 0:
        temp_path.unlink(missing_ok=True)
        detail = (result.stderr or "").strip()
        raise RenderError("FFmpeg rendering failed." + (f" {detail[:500]}" if detail else ""))
    if not temp_path.is_file() or temp_path.stat().st_size < 100:
        temp_path.unlink(missing_ok=True)
        raise RenderError("FFmpeg did not produce a valid output file.")

    report = _probe(temp_path)
    video_stream = next((stream for stream in report.get("streams", []) if stream.get("codec_type") == "video"), None)
    if video_stream is None:
        temp_path.unlink(missing_ok=True)
        raise RenderError("Rendered output contains no valid video stream.")
    try:
        actual_duration = float(report.get("format", {}).get("duration") or 0)
        width = int(video_stream.get("width") or 0)
        height = int(video_stream.get("height") or 0)
    except (TypeError, ValueError) as exc:
        temp_path.unlink(missing_ok=True)
        raise RenderError("Rendered output has invalid media metadata.") from exc
    if actual_duration < max(0.05, duration - 0.5) or actual_duration > duration + 1.0:
        temp_path.unlink(missing_ok=True)
        raise RenderError("Rendered output duration does not match the candidate window.")
    if profile == "vertical" and (width, height) != (1080, 1920):
        temp_path.unlink(missing_ok=True)
        raise RenderError("Rendered vertical output has invalid dimensions.")
    if profile == "square" and (width, height) != (1080, 1080):
        temp_path.unlink(missing_ok=True)
        raise RenderError("Rendered square output has invalid dimensions.")
    if profile == "original" and (width <= 0 or height <= 0 or width % 2 or height % 2):
        temp_path.unlink(missing_ok=True)
        raise RenderError("Rendered original output has invalid dimensions.")

    audio_stream = next((stream for stream in report.get("streams", []) if stream.get("codec_type") == "audio"), None)
    if has_audio and audio_stream is None:
        temp_path.unlink(missing_ok=True)
        raise RenderError("Rendered output lost its source audio.")
    if has_audio:
        try:
            audio_duration = float(audio_stream.get("duration") or actual_duration)
        except (TypeError, ValueError):
            audio_duration = actual_duration
        if abs(audio_duration - actual_duration) > 1.0:
            temp_path.unlink(missing_ok=True)
            raise RenderError("Rendered audio and video durations are not synchronized.")

    _reserve_and_publish(temp_path, final_path)
    return {
        "duration_seconds": actual_duration,
        "file_size_bytes": final_path.stat().st_size,
        "width": width,
        "height": height,
        "media_metadata_json": json.dumps(report),
    }


def _run_render(job_id: str, project_id: str, candidate_ids: list[str], profile: str) -> None:
    try:
        with database.SessionLocal() as db:
            job = db.get(ProcessingJob, job_id)
            source = db.scalar(select(SourceVideo).where(
                SourceVideo.project_id == project_id,
                SourceVideo.source_type == "local_file",
                SourceVideo.local_path.is_not(None),
            ).order_by(SourceVideo.created_at.desc()))
            if not job or not source or not source.local_path:
                raise RenderError("Rendering job, project source, or source file could not be loaded.")
            source_path = Path(source.local_path).resolve()
            job.status = "processing"
            job.stage = "rendering"
            job.started_at = _now()
            db.commit()

        exports_dir = project_directory(project_id) / "exports"
        exports_dir.mkdir(parents=True, exist_ok=True)
        completed = 0
        with database.SessionLocal() as db:
            for candidate_id in candidate_ids:
                candidate = db.get(HighlightCandidate, candidate_id)
                if candidate is None or candidate.project_id != project_id:
                    raise RenderError("A candidate changed or left the project during rendering.")
                if not candidate.approved or candidate.review_status != "approved" or not candidate.selected_for_render:
                    raise RenderError("A candidate is no longer approved and selected for rendering.")
                existing = db.scalars(select(Clip).where(
                    Clip.project_id == project_id,
                    Clip.candidate_id == candidate.id,
                    Clip.output_profile == profile,
                )).all()
                existing = next((clip for clip in existing if clip.file_path and Path(clip.file_path).is_file()), None)
                if existing is None:
                    filename = f"candidate_{candidate.id}_{profile}_{uuid.uuid4().hex}.mp4"
                    final_path = (exports_dir / filename).resolve()
                    if final_path.parent != exports_dir.resolve():
                        raise RenderError("The export path was outside the project export directory.")
                    metadata = render_candidate(source_path, candidate, profile, final_path)
                    clip = Clip(
                        project_id=project_id,
                        candidate_id=candidate.id,
                        file_path=str(final_path),
                        duration_seconds=metadata["duration_seconds"],
                        output_profile=profile,
                        file_size_bytes=metadata["file_size_bytes"],
                        width=metadata["width"],
                        height=metadata["height"],
                        media_metadata_json=metadata["media_metadata_json"],
                    )
                    db.add(clip)
                completed += 1
                job = db.get(ProcessingJob, job_id)
                job.progress_percent = int(completed * 100 / len(candidate_ids))
                db.commit()
        _set_job(job_id, status="completed", stage="completed", progress=100)
    except Exception as exc:
        logger.exception("Render job %s failed", job_id)
        message = str(exc) if isinstance(exc, RenderError) else "Rendering failed. Check local FFmpeg logs and retry."
        with database.SessionLocal() as db:
            job = db.get(ProcessingJob, job_id)
            if job:
                job.status = "failed"
                job.stage = "failed"
                job.error_message = message
                job.completed_at = _now()
                job.active_project_id = None
            db.commit()


def get_render_job(project_id: str, job_id: str | None = None) -> ProcessingJob | None:
    with database.SessionLocal() as db:
        statement = select(ProcessingJob).where(
            ProcessingJob.project_id == project_id,
            ProcessingJob.job_type == "render",
        )
        if job_id:
            statement = statement.where(ProcessingJob.id == job_id)
        job = db.scalar(statement.order_by(ProcessingJob.created_at.desc()))
        if job:
            db.expunge(job)
        return job


def list_exports(project_id: str) -> list[Clip]:
    with database.SessionLocal() as db:
        return db.scalars(select(Clip).where(Clip.project_id == project_id).order_by(Clip.created_at.desc())).all()
