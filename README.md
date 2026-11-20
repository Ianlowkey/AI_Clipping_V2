# AIClipping Desktop

AIClipping Desktop is a local-first Windows application for turning long-form video into vertical short-form clips.

This repository is currently in **Phase 1: project foundation**. The desktop shell and local FastAPI backend are being established first; video processing is not yet implemented.

## Local Browser Development

You can run the React/Vite UI in a browser without launching Tauri. This mode uses the local FastAPI service at `http://127.0.0.1:8765`; the frontend is served at `http://127.0.0.1:1420`.

### Prerequisites

- Windows 10/11, 64-bit
- Python 3.12 (the app environment is `.venv`; Python 3.15 is not changed or used)
- Node.js with npm

### First-time setup

From the repository root in PowerShell:

```powershell
py -3.12 -m venv .venv
npm ci
Push-Location backend
..\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Pop-Location
```

### Start

From the repository root:

```powershell
.\start-aiclipping.ps1
```

The script verifies that `.venv` uses Python 3.12, then opens separate service processes for:

- FastAPI: `http://127.0.0.1:8765` (health: `/api/health`, API docs: `/docs`)
- Vite/React: `http://127.0.0.1:1420`

Open `http://127.0.0.1:1420` in your browser. The dashboard reports whether the local API is connected. To start either service manually instead, use two PowerShell windows from the repository root:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8765 --reload
```

```powershell
npm run dev -- --host 127.0.0.1
```

### Stop

Press **Ctrl+C** in each service window, or close both windows opened by the startup script. The browser UI does not launch Tauri.

## Desktop Development

Tauri development remains available with `npm run tauri dev`. Desktop development additionally requires Rust 1.100 or newer and Visual Studio 2022 C++ Build Tools. FFmpeg is needed only for later video-processing phases.
