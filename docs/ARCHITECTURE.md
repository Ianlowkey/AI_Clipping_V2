# Architecture

AIClipping Desktop uses a Tauri 2 desktop shell, a React/TypeScript frontend, and a local FastAPI backend. SQLite stores metadata and application state. Large media files remain on the filesystem.

The backend is bound to `127.0.0.1` and will be launched as an application-owned local service. The frontend communicates only with documented backend endpoints.

## Phase 1 components

- React and Vite desktop shell
- Tauri 2 Windows application shell
- FastAPI localhost API
- SQLAlchemy SQLite foundation
- Initial database tables for projects, jobs, transcripts, clips, metadata, settings, and logs
- Development documentation
