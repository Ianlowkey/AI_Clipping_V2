import gc
import json
import logging
import math
import os
import shutil
import subprocess
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from app.core.config import settings
from app.video import VideoPreparationError, prepare_video

logger = logging.getLogger(__name__)
_model_lock = threading.Lock()
_cached_model = None
_cached_key: tuple[str, str, str] | None = None


class TranscriptionError(Exception):
    pass


def load_model(model_size: str, device: str, compute_type: str):
    global _cached_model, _cached_key
    key = (model_size, device, compute_type)
    with _model_lock:
        if _cached_key == key and _cached_model is not None:
            return _cached_model
        _cached_model = None
        _cached_key = None
        gc.collect()
        try:
            from faster_whisper import WhisperModel
            _cached_model = WhisperModel(model_size, device=device, compute_type=compute_type)
            _cached_key = key
            return _cached_model
        except Exception as exc:
            raise TranscriptionError(_model_error(exc, device)) from exc


def _model_error(error: Exception, device: str) -> str:
    detail = str(error).lower()
    if "out of memory" in detail:
        return "The transcription model ran out of memory. Use a smaller model or explicitly configure CPU int8."
    if device == "cuda" and any(token in detail for token in ("cuda", "cudnn", "cublas", "gpu", "device")):
        if settings.whisper_cpu_fallback:
            return "CUDA initialization failed. The configured CPU fallback could not load; verify the model and CPU dependencies."
        return "CUDA could not initialize Faster-Whisper. Install compatible CUDA 12 and cuDNN 9 runtime libraries, or explicitly configure CPU with AICLIPPING_WHISPER_DEVICE=cpu and AICLIPPING_WHISPER_COMPUTE_TYPE=int8."
    if any(token in detail for token in ("connection", "download", "huggingface", "http", "offline", "repository", "snapshot")):
        return "The transcription model could not be downloaded. Check internet access for the first model download, then retry."
    return "The transcription model could not be loaded. Check the model name, device, compute type, and local dependencies."


def _normalise(segments) -> tuple[list[dict], list[dict]]:
    normal_segments: list[dict] = []
    all_words: list[dict] = []
    for segment in segments:
        try:
            start, end = float(segment.start), float(segment.end)
        except (AttributeError, TypeError, ValueError):
            continue
        text = str(getattr(segment, "text", "") or "").strip()
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start or not text:
            continue
        words = []
        for word in (getattr(segment, "words", None) or []):
            try:
                word_start, word_end = float(word.start), float(word.end)
                word_text = str(word.word)
            except (AttributeError, TypeError, ValueError):
                continue
            if not math.isfinite(word_start) or not math.isfinite(word_end) or word_start < 0 or word_end <= word_start or not word_text:
                continue
            item = {"start": word_start, "end": word_end, "text": word_text}
            probability = getattr(word, "probability", None)
            if probability is not None:
                item["probability"] = float(probability)
            words.append(item)
            all_words.append(item)
        normal_segments.append({"start": start, "end": end, "text": text, "words": words})
    if normal_segments and not all_words:
        raise TranscriptionError("Speech was detected, but the model returned no valid word timestamps. Retry with another model or compute type.")
    return normal_segments, all_words


def _srt_time(seconds: float) -> str:
    milliseconds = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds_part, millis = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{seconds_part:02},{millis:03}"


def _write_outputs(transcript_dir: Path, payload: dict) -> dict[str, str]:
    transcript_dir.mkdir(parents=True, exist_ok=True)
    stage_dir = Path(tempfile.mkdtemp(prefix=".transcription-", dir=transcript_dir))
    try:
        text_path = stage_dir / "transcript.txt"
        srt_path = stage_dir / "transcript.srt"
        words_path = stage_dir / "words.json"
        text_path.write_text("\n".join(segment["text"] for segment in payload["segments"]), encoding="utf-8")
        cues = []
        for index, segment in enumerate(payload["segments"], start=1):
            end = max(segment["end"], segment["start"] + 0.001)
            cues.append(f"{index}\n{_srt_time(segment['start'])} --> {_srt_time(end)}\n{segment['text']}\n")
        srt_path.write_text("\n".join(cues), encoding="utf-8")
        words_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        targets = {
            "text_path": transcript_dir / "transcript.txt",
            "srt_path": transcript_dir / "transcript.srt",
            "words_path": transcript_dir / "words.json",
        }
        for key, target in targets.items():
            os.replace(stage_dir / target.name, target)
        return {key: str(path) for key, path in targets.items()}
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)


def _extract_audio_chunks(source_path: Path, work_dir: Path) -> list[tuple[Path, float]]:
    ffmpeg, ffprobe = settings.resolve_ffmpeg(), settings.resolve_ffprobe()
    if not ffmpeg or not ffprobe:
        raise TranscriptionError("FFmpeg and FFprobe are required to prepare transcription audio.")
    pattern = work_dir / "audio_%05d.flac"
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(source_path), "-map", "0:a:0", "-vn",
             "-f", "segment", "-segment_time", str(settings.whisper_audio_chunk_seconds), "-reset_timestamps", "1",
             "-c:a", "flac", str(pattern)],
            capture_output=True, text=True, timeout=24 * 60 * 60, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.exception("FFmpeg audio preparation failed")
        raise TranscriptionError("FFmpeg could not prepare the audio. Check FFmpeg and available disk space.") from exc
    if result.returncode:
        logger.warning("FFmpeg audio extraction failed (exit %s)", result.returncode)
        raise TranscriptionError("FFmpeg could not extract audio from this video. The audio may be corrupt or unsupported.")
    paths = sorted(work_dir.glob("audio_*.flac"))
    if not paths:
        raise TranscriptionError("No audio could be extracted from this video.")
    chunks = []
    for path in paths:
        try:
            probe = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                capture_output=True, text=True, timeout=30, check=False,
            )
            duration = float(probe.stdout.strip())
            if probe.returncode != 0 or not math.isfinite(duration) or duration <= 0:
                raise ValueError("invalid chunk duration")
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise TranscriptionError("FFprobe could not measure an extracted audio chunk, so transcript timestamps cannot be aligned safely.") from exc
        chunks.append((path, duration))
    return chunks


def transcribe_video(
    source_path: Path,
    project_id: str,
    source_filename: str,
    transcript_dir: Path,
    stage_callback: Callable[[str, str, str | None], None],
    model_size: str | None = None,
    language: str | None = None,
) -> dict:
    if not source_path.is_file():
        raise TranscriptionError("The imported source video is missing. Re-import the video before transcribing.")
    stage_callback("preparing_audio", "Checking source audio", None)
    try:
        media = prepare_video(source_path)
    except VideoPreparationError as exc:
        raise TranscriptionError(f"The source video is no longer readable: {exc}") from exc
    if not media.get("audio_available"):
        raise TranscriptionError("This video has no audio stream to transcribe.")

    temp_root = transcript_dir.parent / "temp"
    temp_root.mkdir(parents=True, exist_ok=True)
    audio_work_dir = Path(tempfile.mkdtemp(prefix="transcription-audio-", dir=temp_root))
    try:
        chunks = _extract_audio_chunks(source_path, audio_work_dir)
        return _transcribe_chunks(
            chunks, project_id, source_filename, transcript_dir, stage_callback,
            model_size=model_size, language=language,
            duration=media.get("duration_seconds"),
        )
    finally:
        shutil.rmtree(audio_work_dir, ignore_errors=True)


def _transcribe_chunks(
    chunks: list[tuple[Path, float]],
    project_id: str,
    source_filename: str,
    transcript_dir: Path,
    stage_callback: Callable[[str, str, str | None], None],
    model_size: str | None,
    language: str | None,
    duration: float | None,
) -> dict:

    chosen_model = model_size or settings.whisper_model_size
    chosen_language = language or settings.whisper_language
    selected_device = settings.whisper_device
    compute_type = settings.whisper_compute_type
    stage_callback("loading_model", f"Loading {chosen_model} model for {selected_device}", selected_device)
    try:
        model = load_model(chosen_model, selected_device, compute_type)
    except TranscriptionError as exc:
        cuda_issue = "cuda could not initialize" in str(exc).lower() or "out of memory" in str(exc).lower()
        if selected_device != "cuda" or not settings.whisper_cpu_fallback or not cuda_issue:
            raise
        logger.warning("CUDA unavailable; configured CPU fallback enabled")
        selected_device, compute_type = "cpu", "int8"
        stage_callback("loading_model", f"CUDA unavailable; loading configured CPU fallback ({chosen_model}, int8)", selected_device)
        model = load_model(chosen_model, selected_device, compute_type)

    def run_chunks(active_model, active_device: str, active_language: str | None):
        output_segments: list[dict] = []
        output_words: list[dict] = []
        detected_info = None
        offset = 0.0
        for index, (chunk_path, chunk_duration) in enumerate(chunks, start=1):
            stage_callback("transcribing", f"Transcribing audio part {index} of {len(chunks)} with {chosen_model} on {active_device}", active_device)
            iterator, info = active_model.transcribe(
                str(chunk_path), language=active_language, word_timestamps=True, vad_filter=True,
            )
            normal_segments, normal_words = _normalise(list(iterator))
            for segment in normal_segments:
                segment["start"] += offset
                segment["end"] += offset
                for word in segment["words"]:
                    word["start"] += offset
                    word["end"] += offset
                output_segments.append(segment)
            output_words.extend(word for segment in normal_segments for word in segment["words"])
            detected_info = info
            if active_language is None:
                active_language = getattr(info, "language", None)
            offset += chunk_duration
        return output_segments, output_words, detected_info

    try:
        segments, words, info = run_chunks(model, selected_device, chosen_language)
    except Exception as exc:
        detail = str(exc).lower()
        if selected_device == "cuda" and settings.whisper_cpu_fallback and any(token in detail for token in ("cuda", "cudnn", "cublas", "out of memory", "gpu", "device")):
            logger.warning("CUDA transcription failed; configured CPU fallback enabled")
            selected_device, compute_type = "cpu", "int8"
            stage_callback("loading_model", f"CUDA failed; retrying with configured CPU fallback ({chosen_model}, int8)", selected_device)
            model = load_model(chosen_model, selected_device, compute_type)
            try:
                segments, words, info = run_chunks(model, selected_device, chosen_language)
            except Exception as retry_error:
                raise TranscriptionError(_model_error(retry_error, selected_device)) from retry_error
        else:
            if isinstance(exc, TranscriptionError):
                raise
            raise TranscriptionError(_model_error(exc, selected_device)) from exc

    detected_language = getattr(info, "language", None)
    language_probability = getattr(info, "language_probability", None)
    duration = duration or getattr(info, "duration", None)
    timestamp = datetime.now(timezone.utc).isoformat()
    payload = {
        "project_id": project_id,
        "source_filename": source_filename,
        "language": detected_language,
        "language_probability": language_probability,
        "model": chosen_model,
        "device": selected_device,
        "compute_type": compute_type,
        "processed_at": timestamp,
        "duration_seconds": duration,
        "segments": segments,
        "words": words,
    }
    stage_callback("saving_transcript", "Saving transcript files", selected_device)
    paths = _write_outputs(transcript_dir, payload)
    return {**payload, **paths}
