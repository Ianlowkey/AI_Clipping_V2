from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.database.session import create_database


@asynccontextmanager
async def lifespan(_: FastAPI):
    create_database()
    yield


app = FastAPI(
    title="AIClipping Local API",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["tauri://localhost", "http://tauri.localhost", "http://127.0.0.1:1420"],
    allow_credentials=True,
    allow_methods=["GET", "HEAD"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "database_ready": settings.database_path.exists(),
        "mode": "local",
    }
