# AIClipping Desktop

AIClipping Desktop is a local-first Windows application for turning long-form video into vertical short-form clips.

The application interface, local video import/preparation, Faster-Whisper transcription, and transcript-based Gemini highlight suggestions are implemented. Video clipping, rendering, and metadata generation are not implemented.

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

The Windows Local Browser Development Mode launcher defaults the backend process to CPU `int8` and SQLAlchemy pure-Python mode. These settings address the current Windows Code Integrity policy without weakening or bypassing it. The launcher sets each value only when it is not already present in the launching PowerShell process; it restores the parent process environment after starting the backend, and does not pass these settings to the frontend. To change a setting, set the corresponding value before running `start-aiclipping.ps1`.

The backend defaults outside that launcher are `small`, `cuda`, `float16`, and automatic language detection. Configure those directly before starting the backend:

```powershell
$env:AICLIPPING_WHISPER_MODEL_SIZE = "small"
$env:AICLIPPING_WHISPER_DEVICE = "cpu"
$env:AICLIPPING_WHISPER_COMPUTE_TYPE = "int8"
$env:DISABLE_SQLALCHEMY_CEXT_RUNTIME = "1"
# Leave language unset for automatic detection, or set an ISO language code such as "en".
# CPU fallback is opt-in; if enabled and CUDA fails, the job reports CPU/int8 as its actual device.
$env:AICLIPPING_WHISPER_CPU_FALLBACK = "true"
```

Verify that CTranslate2 detects the GPU from the same environment:

```powershell
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv
.\.venv\Scripts\python.exe -c "import ctranslate2; print(ctranslate2.get_cuda_device_count())"
```

A positive CTranslate2 device count confirms only device discovery; it does not confirm successful model initialization or transcription. This machine's Windows Code Integrity policy blocks native extensions, so do not attempt to bypass the policy to enable CUDA. The supported local configuration is CPU `int8` with SQLAlchemy's pure-Python runtime mode.

For a real run, start Local Browser Development Mode, create a project, import and prepare a video with audio, open that project, then select **Process Video**. The project view displays the source filename, duration, resolution, job state, actual model/device/compute type/language, timestamped transcript, and individual TXT/SRT/JSON downloads. Select **Open Transcript Folder** to display the local folder path and **Copy Path**; browser mode cannot open arbitrary local folders directly. Existing successful transcript files are kept unless **Regenerate transcription** is explicitly selected. The viewer only reports a transcript as completed when all three transcript files exist and can be read.

### Gemini highlight suggestions

Set `AICLIPPING_GEMINI_API_KEY` in PowerShell before launching `start-aiclipping.ps1`, then restart the backend. The key stays in the backend process environment; it is not stored in SQLite, returned by the API, logged, or sent to the frontend. The Settings page reports only whether a key is configured. `AICLIPPING_GEMINI_MODEL` selects the model; the default is `gemini-3.6-flash` (listed as a stable Flash model in Google's model catalog, checked 2026-11-22). The backend uses the official Google Gen AI Python SDK and requests structured JSON candidates. Each analysis is a paid/quota-counted API request under the user's existing Gemini API access. Only transcript text with word timestamps is sent; original video bytes are never sent. Candidate timestamps are aligned to existing word boundaries and must pass the 20–60 second duration rule. Scores are model estimates, not performance guarantees. Review/edit candidates in the project view; no video is rendered in this phase.

Example for the current PowerShell session (replace the placeholder locally; do not paste the key into chat or commit it):

```powershell
$env:AICLIPPING_GEMINI_API_KEY = "your-key"
$env:AICLIPPING_GEMINI_MODEL = "gemini-3.6-flash"
.\start-aiclipping.ps1
```

Clear the key from that shell when finished with `Remove-Item Env:AICLIPPING_GEMINI_API_KEY`. Configure the key only in the environment from which the backend is started.

### Candidate review and source preview

Open a project with highlight candidates to preview each range against its locally stored source video. The local API streams the recorded project source with HTTP byte-range support for seeking; the browser does not send the file to a remote service. Candidate start/end edits are validated against the video duration and snapped to the nearest word timestamps. The saved excerpt is rebuilt from `words.json`, and the interface reports when a timestamp was adjusted. Editing an already approved candidate keeps it approved. Approved candidates can be selected for a future render queue; selection is saved locally, and rejecting a candidate clears its selection. Browser playback depends on the source format being supported by the browser; the original file is not modified. Clip rendering is not implemented yet.

### Stop

Press **Ctrl+C** in each service window, or close both windows opened by the startup script. The browser UI does not launch Tauri.

## Desktop Development

Tauri development remains available with `npm run tauri dev`. Desktop development additionally requires Rust 1.100 or newer and Visual Studio 2022 C++ Build Tools. FFmpeg and FFprobe are used by the current local video import and transcription pipeline.
