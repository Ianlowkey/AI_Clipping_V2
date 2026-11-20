from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    video_url: str | None = Field(default=None, max_length=2048)

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Project name is required")
        return value


class SourceVideoRead(BaseModel):
    id: str
    source_type: str
    source_uri: str
    title: str | None
    local_path: str | None
    duration_seconds: float | None
    created_at: datetime
    metadata_json: str | None

    model_config = {"from_attributes": True}


class ProjectRead(BaseModel):
    id: str
    name: str
    description: str | None
    status: str
    created_at: datetime
    source_videos: list[SourceVideoRead]

    model_config = {"from_attributes": True}


class ClipRead(BaseModel):
    id: str
    project_id: str
    file_path: str | None
    thumbnail_path: str | None
    duration_seconds: float | None
    created_at: datetime

    model_config = {"from_attributes": True}


class JobRead(BaseModel):
    id: str
    project_id: str
    status: str
    progress_percent: int
    error_message: str | None
    created_at: datetime
    updated_at: datetime
    project_name: str
    job_type: str = "generic"
    stage: str | None = None
    model_name: str | None = None
    device: str | None = None
    compute_type: str | None = None
    language: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class TranscriptionStart(BaseModel):
    regenerate: bool = False


class TranscriptionJobRead(BaseModel):
    id: str
    project_id: str
    status: str
    stage: str | None
    error_message: str | None
    model_name: str | None
    device: str | None
    compute_type: str | None
    language: str | None
    started_at: datetime | None
    completed_at: datetime | None
    updated_at: datetime


class TranscriptRead(BaseModel):
    project_id: str
    source_filename: str | None
    language: str | None
    language_probability: float | None
    model_name: str | None
    device: str | None
    duration_seconds: float | None
    text_path: str | None
    srt_path: str | None
    words_path: str | None
    segment_count: int
    word_count: int


