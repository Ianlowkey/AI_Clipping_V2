import os
import shutil
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def default_data_dir() -> Path:
    root = os.environ.get("LOCALAPPDATA")
    if root:
        return Path(root) / "AIClippingData"
    return Path.home() / "AIClippingData"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AICLIPPING_", extra="ignore")

    data_dir: Path = Field(default_factory=default_data_dir)
    database_path: Path | None = None
    max_upload_size_bytes: int = Field(default=20 * 1024 * 1024 * 1024, gt=0)
    ffmpeg_path: str | None = None
    ffprobe_path: str | None = None
    api_host: str = "127.0.0.1"
    api_port: int = 8765

    @model_validator(mode="after")
    def set_default_database_path(self):
        if self.database_path is None:
            self.database_path = self.data_dir / "database" / "aiclipping.db"
        return self

    def resolve_ffmpeg(self) -> str | None:
        return self.ffmpeg_path or shutil.which("ffmpeg")

    def resolve_ffprobe(self) -> str | None:
        return self.ffprobe_path or shutil.which("ffprobe")


settings = Settings()
