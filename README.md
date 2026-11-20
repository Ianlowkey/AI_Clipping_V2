# AIClipping Desktop

AIClipping Desktop is a local-first Windows application for turning long-form video into vertical short-form clips.

The application interface and local video import/preparation are implemented. AI transcription, highlight detection, clipping, rendering, and metadata generation are not implemented.

## Local Browser Development

You can run the React/Vite UI in a browser without launching Tauri. This mode uses the local FastAPI service at `http://127.0.0.1:8765`; the frontend is served at `http://127.0.0.1:1420`.

### Prerequisites

- Windows 10/11, 64-bit
- Python 3.12 (the app environment is `.venv`; Python 3.15 is not changed or used)
- Node.js with npm
- FFmpeg and FFprobe available on `PATH` (or configure `AICLIPPING_FFMPEG_PATH` and `AICLIPPING_FFPROBE_PATH`)

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

### Import and prepare a local video

Create a project, choose a local video file, then select **Create project**. The browser sends the file to the local FastAPI service, which validates and copies it under the project's `source` directory, then uses FFprobe and FFmpeg to check stream metadata and decode the first video frame. The dashboard displays the preparation report. Video URL entries are saved as notes only; they are not downloaded.

By default, project files and SQLite data live under `%LOCALAPPDATA%\AIClippingData` (or `~\AIClippingData` when `LOCALAPPDATA` is unavailable). Set `AICLIPPING_DATA_DIR` to use another root and `AICLIPPING_MAX_UPLOAD_SIZE_BYTES` to change the 20 GiB upload limit. If an existing database is found under the previous `%LOCALAPPDATA%\AIClipping` location, startup copies it to the new root without deleting the original.

Manual smoke check: create a project, upload a readable MP4, confirm duration/resolution/codecs/audio in the report and verify the file under `AIClippingData\projects\<id>\source`. Then try a text file renamed to `.mp4` and confirm it is rejected with a preparation error and no imported source.

### Local transcription (Faster-Whisper)

Backend dependencies, including Faster-Whisper, are installed into the Python 3.12 project environment by the first-time setup commands above. On first transcription, Faster-Whisper downloads its selected model weights; inference then runs locally. No audio is sent to a cloud transcription service.

Defaults are `small`, `cuda`, `float16`, and automatic language detection. Configure before starting the backend with these environment variables:

```powershell
$env:AICLIPPING_WHISPER_MODEL_SIZE = "small"
$env:AICLIPPING_WHISPER_DEVICE = "cuda"
$env:AICLIPPING_WHISPER_COMPUTE_TYPE = "float16"
# Leave language unset for automatic detection, or set an ISO language code such as "en".
# CPU fallback is opt-in; if enabled and CUDA fails, the job reports CPU/int8 as its actual device.
$env:AICLIPPING_WHISPER_CPU_FALLBACK = "true"
```

Verify that CTranslate2 detects the GPU from the same environment:

```powershell
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv
.\.venv\Scripts\python.exe -c "import ctranslate2; print(ctranslate2.get_cuda_device_count())"
```

A positive CTranslate2 device count confirms its CUDA runtime can see a device. If CUDA initialization still fails, check the Faster-Whisper/CTranslate2 CUDA 12 and cuDNN 9 runtime requirements; set `AICLIPPING_WHISPER_DEVICE=cpu` and `AICLIPPING_WHISPER_COMPUTE_TYPE=int8` for an explicit CPU run. CPU fallback is disabled by default.

For a real run, start Local Browser Development Mode, create a project, import and prepare a video with audio, open that project, then select **Process Video**. The project view displays honest processing stages, the actual model/device/language, the timestamped transcript, and TXT/SRT/JSON downloads. Existing successful transcript files are kept unless **Regenerate transcription** is explicitly selected.

### Stop

Press **Ctrl+C** in each service window, or close both windows opened by the startup script. The browser UI does not launch Tauri.

## Desktop Development

Tauri development remains available with `npm run tauri dev`. Desktop development additionally requires Rust 1.100 or newer and Visual Studio 2022 C++ Build Tools. FFmpeg and FFprobe are used by the current local video import and transcription pipeline.
