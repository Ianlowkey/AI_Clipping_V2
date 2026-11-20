from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.api_schemas import ClipRead, JobRead, ProjectCreate, ProjectRead
from app.database import session as database
from app.database.models import Clip, ProcessingJob, Project, SourceVideo


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
    if payload.video_url and payload.source_video_name:
        raise HTTPException(status_code=422, detail="Choose a video URL or a local file, not both")
    if payload.video_url and not payload.video_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=422, detail="Video URL must start with http:// or https://")

    project = Project(name=payload.name, description=payload.description or None)
    if payload.video_url:
        project.source_videos.append(
            SourceVideo(source_type="url", source_uri=payload.video_url, title=payload.video_url)
        )
    elif payload.source_video_name:
        # Browser file selection supplies a filename, not a readable filesystem path.
        # Store source metadata only; file copying/upload is intentionally not implemented.
        project.source_videos.append(
            SourceVideo(source_type="local_file", source_uri=payload.source_video_name, title=payload.source_video_name)
        )

    with get_session() as db:
        db.add(project)
        db.commit()
        db.refresh(project)
        db.refresh(project, attribute_names=["source_videos"])
        return project


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
