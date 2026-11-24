# Development Status

## Current phase

Phase 2E - Local Clip Rendering

## Implemented

- React, TypeScript, Vite, and Tauri 2 foundation
- FastAPI backend and additive SQLite migrations
- Local project, source-video import, FFmpeg/FFprobe preparation
- Faster-Whisper CPU/int8 transcription with word timestamps
- Optional local speaker diarization with stable labels and unknown-speaker fallback
- TXT, SRT, and JSON transcript output
- General, comedy, and Podcasts / Conversations highlight profiles
- Gemini transcript-only analysis with explicit confirmation
- Timestamp alignment, candidate scoring, edit, approval, rejection, and selection workflows
- Local source-video preview and range seeking
- Local FFmpeg rendering for original, 1080x1920 vertical, and 1080x1080 square profiles
- Persistent render jobs and project-local MP4 exports
- Export playback and download endpoints
- Frontend production build

## Not implemented yet

- Word-timed caption rendering and styling
- Export management and platform metadata
- Installer packaging

## Verification

- Backend tests: 55 passed
- Frontend tests: 11 passed
- React/TypeScript production build: passed
- Tauri Rust shell check: blocked locally by Windows Application Control policy 4551 on generated build executables
