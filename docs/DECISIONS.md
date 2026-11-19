# Architectural Decisions

- Use Tauri 2 for the Windows desktop shell.
- Use React and TypeScript for the UI.
- Use Python 3.12 for the processing backend.
- Use FastAPI, Pydantic, SQLAlchemy, SQLite, and Uvicorn.
- Bind the backend only to `127.0.0.1`.
- Keep media files outside SQLite.
- Implement capabilities incrementally by the approved development phases.
