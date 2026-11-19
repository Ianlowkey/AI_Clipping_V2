# AIClipping Desktop

AIClipping Desktop is a local-first Windows application for turning long-form video into vertical short-form clips.

This repository is currently in **Phase 1: project foundation**. The desktop shell and local FastAPI backend are being established first; video processing is not yet implemented.

## Prerequisites

- Windows 10/11, 64-bit
- Python 3.12
- Node.js 20 or newer
- Rust 1.100 or newer
- Visual Studio 2022 C++ Build Tools
- FFmpeg (required for later video-processing phases)

## Development

Create the Python environment and install backend dependencies:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ./backend[dev]
```

Install frontend dependencies:

```powershell
npm install
npm run tauri dev
```

The backend can be run independently:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8765
```
