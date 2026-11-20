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


