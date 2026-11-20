from contextlib import asynccontextmanager
import json
import logging
import os
import tempfile
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.api_schemas import ClipRead, JobRead, ProjectCreate, ProjectRead
from app.database import session as database
from app.database.models import Clip, ProcessingJob, Project, SourceVideo
from app.storage import ensure_project_directories, safe_video_filename
from app.video import VideoPreparationError, prepare_video

logger = logging.getLogger(__name__)
ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mpeg", ".mpg", ".wmv"}


@asynccontextmanager
async def lifespan(_: FastAPI):
    database.create_database()
    yield


app = FastAPI(
    title="AIClipping Local API",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "tauri://localhost",
        "http://tauri.localhost",
        "http://127.0.0.1:1420",
        "http://localhost:1420",
    ],
    allow_credentials=True,
    allow_methods=["GET", "HEAD", "POST"],
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
    if not file.filename or not file.filename.strip():
        raise HTTPException(status_code=422, detail="Choose a video file to upload.")
    extension = Path(file.filename.replace("\\", "/").split("/")[-1]).suffix.lower()
    if extension not in ALLOWED_VIDEO_EXTENSIONS:
        raise HTTPException(status_code=415, detail=f"Unsupported video extension. Allowed: {', '.join(sorted(ALLOWED_VIDEO_EXTENSIONS))}.")

    with get_session() as db:
        project = db.get(Project, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found. Create or select a project before uploading.")

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
                status=job.status,
                progress_percent=job.progress_percent,
                error_message=job.error_message,
                created_at=job.created_at,
                updated_at=job.updated_at,
            )
            for job in jobs
        ]
