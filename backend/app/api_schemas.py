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


class GeminiSettingsRead(BaseModel):
    configured: bool
    model: str
    key_environment_variable: str = "AICLIPPING_GEMINI_API_KEY"


class HighlightJobRead(BaseModel):
    id: str
    project_id: str
    status: str
    stage: str | None
    error_message: str | None
    model_name: str | None
    started_at: datetime | None
    completed_at: datetime | None
    updated_at: datetime


class CandidateRead(BaseModel):
    id: str
    project_id: str
    transcript_id: str | None
    start_seconds: float
    end_seconds: float
    duration_seconds: float | None
    excerpt: str | None
    title: str | None
    hook: str | None
    category: str | None
    selection_explanation: str | None
    score: float
    hook_strength: int | None
    engagement_potential: int | None
    standalone_clarity: int | None
    emotional_value: int | None
    review_status: str
    selected_for_render: bool
    validation_warnings: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class CandidateEdit(BaseModel):
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    title: str = Field(min_length=1, max_length=300)
    hook: str = Field(min_length=1, max_length=1000)

    @field_validator("title", "hook")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Title and hook must not be blank")
        return value


class CandidateReview(BaseModel):
    status: str

    @field_validator("status")
    @classmethod
    def valid_status(cls, value: str) -> str:
        normalized = value.lower()
        if normalized not in {"approved", "rejected"}:
            raise ValueError("Review status must be approved or rejected")
        return normalized


class CandidateSelection(BaseModel):
    selected: bool


