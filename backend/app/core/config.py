import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def default_data_dir() -> Path:
    root = os.environ.get("LOCALAPPDATA")
    if root:
        return Path(root) / "AIClipping"
    return Path.home() / ".aiclipping"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AICLIPPING_", extra="ignore")

    data_dir: Path = default_data_dir()
    database_path: Path = data_dir / "database" / "aiclipping.db"
    api_host: str = "127.0.0.1"
    api_port: int = 8765


settings = Settings()
