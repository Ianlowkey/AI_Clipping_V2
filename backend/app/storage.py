import re
from pathlib import Path
from uuid import UUID

from app.core.config import settings

PROJECT_DIRECTORIES = (
    "source", "transcript", "candidates", "clips", "metadata", "exports", "downloads", "logs", "temp",
)


def project_directory(project_id: str) -> Path:
    try:
        safe_id = str(UUID(project_id))
    except ValueError as exc:
        raise ValueError("Project identifier is invalid") from exc
    root = (settings.data_dir / "projects").resolve()
    path = (root / safe_id).resolve()
    if path.parent != root:
        raise ValueError("Project path is invalid")
    return path


def ensure_project_directories(project_id: str) -> Path:
    directory = project_directory(project_id)
    directory.mkdir(parents=True, exist_ok=True)
    for child in PROJECT_DIRECTORIES:
        (directory / child).mkdir(exist_ok=True)
    return directory


def safe_video_filename(original_name: str) -> str:
    leaf = original_name.replace("\\", "/").split("/")[-1]
    suffix = Path(leaf).suffix.lower()
    stem = Path(leaf).stem
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")[:80] or "video"
    return f"{stem}{suffix}"
