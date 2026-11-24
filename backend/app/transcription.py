import gc
import json
import logging
import math
import os
import shutil
import subprocess
import tempfile
import threading
import wave
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from app.core.config import settings
from app import diarization
from app.video import VideoPreparationError, prepare_video

logger = logging.getLogger(__name__)
_model_lock = threading.Lock()
_cached_model = None
_cached_key: tuple[str, str, str] | None = None


class TranscriptionError(Exception):
    pass


@dataclass(frozen=True)
class AudioChunk:
    path: Path
    duration_seconds: float
    source_offset_seconds: float


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
            _configure_pyav_open_compatibility()
            from faster_whisper import WhisperModel
            _cached_model = WhisperModel(model_size, device=device, compute_type=compute_type)
            _cached_key = key
            return _cached_model
        except Exception as exc:
            raise TranscriptionError(_model_error(exc, device)) from exc


def _configure_pyav_open_compatibility() -> None:
    """Bridge Faster-Whisper 1.2.1's removed PyAV 19 keyword without security changes."""
    try:
        import av
    except ImportError:
        return
    try:
        pyav_major = int(av.__version__.split(".", 1)[0])
    except (AttributeError, ValueError):
        return
    if pyav_major < 19 or getattr(av.open, "_aiclipping_pyav19_compat", False):
        return

    original_open = av.open

    def compatible_open(file, *args, **kwargs):
        kwargs.pop("metadata_errors", None)
        return original_open(file, *args, **kwargs)

    compatible_open._aiclipping_pyav19_compat = True
    av.open = compatible_open


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
        text_lines = []
        for segment in payload["segments"]:
            text_lines.append(_format_transcript_segment(segment, payload))
        text_path.write_text("\n".join(text_lines), encoding="utf-8")
        cues = []
        for index, segment in enumerate(payload["segments"], start=1):
            end = max(segment["end"], segment["start"] + 0.001)
            text = _format_transcript_segment(segment, payload)
            cues.append(f"{index}\n{_srt_time(segment['start'])} --> {_srt_time(end)}\n{text}\n")
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


def _format_transcript_segment(segment: dict, payload: dict) -> str:
    speakers = payload.get("speakers") or []
    labels = {speaker.get("speaker_id"): speaker.get("label") for speaker in speakers}
    diarization_ran = payload.get("diarization_status") in {"complete", "incomplete"}
    words = segment.get("words") or []
    if words and (diarization_ran or any(word.get("speaker_id") for word in words)):
        groups: list[tuple[str, str]] = []
        for word in words:
            speaker_id = word.get("speaker_id")
            label = labels.get(speaker_id) if speaker_id else None
            label = label or "Unknown speaker"
            if groups and groups[-1][0] == label:
                groups[-1] = (label, groups[-1][1] + str(word.get("text", "")))
            else:
                groups.append((label, str(word.get("text", ""))))
        return " | ".join(f"{label}: {text.strip()}" for label, text in groups)
    speaker_id = segment.get("speaker_id")
    label = labels.get(speaker_id) if speaker_id else None
    if label:
        return f"{label}: {segment['text']}"
    return f"Unknown speaker: {segment['text']}" if diarization_ran else segment["text"]


def _diagnostic_text(text: str, *paths: Path) -> str:
    for path in paths:
        text = text.replace(str(path), "<local-file>")
    return text.strip()[:1200]


def _source_audio_offset(source_path: Path, ffprobe: str | None) -> float:
    if not ffprobe:
        raise TranscriptionError("FFprobe is required to inspect source audio timestamps.")
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=start_time:stream=codec_type,start_time", "-of", "json", str(source_path)],
            capture_output=True, text=True, timeout=45, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("FFprobe source timeline inspection failed (%s)", type(exc).__name__)
        raise TranscriptionError("FFprobe could not inspect the video's audio start time. Check FFprobe and retry.") from exc
    if result.returncode != 0:
        logger.warning("Source timeline probe failed (exit=%s): %s", result.returncode, _diagnostic_text(result.stderr, source_path))
        raise TranscriptionError("FFprobe could not inspect the video's audio start time. Check the local backend logs.")
    try:
        report = json.loads(result.stdout)
        streams = report.get("streams", [])
        audio = next(stream for stream in streams if stream.get("codec_type") == "audio")
        video = next(stream for stream in streams if stream.get("codec_type") == "video")
        raw_container_start = report.get("format", {}).get("start_time")
        container_start = float(raw_container_start) if raw_container_start not in (None, "N/A", "") else 0.0
        raw_video_start = video.get("start_time")
        video_start = float(raw_video_start) if raw_video_start not in (None, "N/A", "") else container_start
        raw_audio_start = audio.get("start_time")
        audio_start = float(raw_audio_start) if raw_audio_start not in (None, "N/A", "") else video_start
        offset = audio_start - video_start
    except (KeyError, StopIteration, TypeError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("Source timeline probe returned unusable timestamps (exit=%s)", result.returncode)
        raise TranscriptionError("FFprobe did not return usable audio and video start timestamps; safe transcript alignment is unavailable.") from exc
    if not math.isfinite(offset):
        raise TranscriptionError("FFprobe returned a non-finite audio start timestamp; safe transcript alignment is unavailable.")
    logger.info("Source audio timeline inspected: video-relative audio start offset=%.6fs", offset)
    return offset


def _extract_audio_chunks(source_path: Path, work_dir: Path, source_offset_seconds: float = 0.0) -> list[AudioChunk]:
    ffmpeg, ffprobe = settings.resolve_ffmpeg(), settings.resolve_ffprobe()
    if not ffmpeg or not ffprobe:
        raise TranscriptionError("FFmpeg and FFprobe are required to prepare transcription audio.")
    requested_duration = settings.whisper_audio_chunk_seconds
    pattern = work_dir / "audio_%05d.wav"
    effective_offset = source_offset_seconds
    trim_before_video = max(0.0, -source_offset_seconds)
    extraction_args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(source_path), "-map", "0:a:0", "-vn"]
    if trim_before_video:
        extraction_args.extend(["-af", f"atrim=start={trim_before_video:.9f},asetpts=PTS-STARTPTS"])
        effective_offset = 0.0
    extraction_args.extend([
        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-f", "segment", "-segment_time", str(requested_duration),
        "-reset_timestamps", "1", str(pattern),
    ])
    try:
        result = subprocess.run(
            extraction_args,
            capture_output=True, text=True, timeout=24 * 60 * 60, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("FFmpeg audio preparation failed (%s)", type(exc).__name__)
        raise TranscriptionError("FFmpeg could not prepare the audio. Check FFmpeg and available disk space.") from exc
    if result.returncode:
        logger.warning("FFmpeg audio extraction failed (exit=%s, requested_chunk_duration=%ss): %s", result.returncode, requested_duration, _diagnostic_text(result.stderr, source_path, work_dir))
        raise TranscriptionError("FFmpeg could not extract audio from this video. The audio may be corrupt or unsupported.")
    paths = sorted(work_dir.glob("audio_*.wav"))
    if not paths:
        logger.warning("FFmpeg exited successfully but created no audio chunks (exit=0, requested_chunk_duration=%ss)", requested_duration)
        raise TranscriptionError("FFmpeg completed without creating audio chunks. Check the source audio stream and backend logs.")
    chunks = []
    offset = effective_offset
    for index, path in enumerate(paths, start=1):
        probe = None
        file_size = path.stat().st_size
        logger.info("Audio chunk extracted: index=%s source_offset=%.6fs requested_chunk_duration=%ss file_size=%s ffmpeg_exit=0", index, offset, requested_duration, file_size)
        if file_size < 45:
            logger.warning("Audio chunk is empty or too short: index=%s source_offset=%.6fs requested_chunk_duration=%ss file_size=%s", index, offset, requested_duration, file_size)
            raise TranscriptionError(f"Extracted audio chunk {index} is empty. Check the source audio and local backend logs.")
        try:
            with wave.open(str(path), "rb") as audio:
                frame_count = audio.getnframes()
                sample_rate = audio.getframerate()
                channels = audio.getnchannels()
                sample_width = audio.getsampwidth()
                compression = audio.getcomptype()
                if compression != "NONE" or sample_rate != 16000 or channels != 1 or sample_width != 2 or frame_count <= 0:
                    raise ValueError("unexpected PCM WAV format or empty sample data")
                expected_payload_bytes = frame_count * channels * sample_width
                if file_size < expected_payload_bytes + 44 or not audio.readframes(1):
                    raise ValueError("WAV sample data is incomplete")
                duration = frame_count / sample_rate
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError("invalid sample duration")
            probe = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "format=duration:stream=codec_name,sample_rate,channels", "-of", "json", str(path)],
                capture_output=True, text=True, timeout=30, check=False,
            )
            if probe.returncode != 0:
                logger.warning("FFprobe rejected audio chunk: index=%s source_offset=%.6fs requested_chunk_duration=%ss file_size=%s ffprobe_exit=%s stdout=%s stderr=%s", index, offset, requested_duration, file_size, probe.returncode, _diagnostic_text(probe.stdout, path, work_dir), _diagnostic_text(probe.stderr, path, work_dir))
                raise TranscriptionError(f"FFprobe could not validate extracted audio chunk {index} (exit {probe.returncode}). Check the local backend logs.")
            report = json.loads(probe.stdout)
            streams = report.get("streams", [])
            stream = next((item for item in streams if item.get("codec_type") == "audio"), streams[0] if streams else {})
            probed_duration = float(report.get("format", {}).get("duration", "nan"))
            if stream.get("codec_name") != "pcm_s16le" or int(stream.get("sample_rate", 0)) != sample_rate or int(stream.get("channels", 0)) != channels:
                raise ValueError("FFprobe reported an unexpected WAV audio stream")
            if not math.isfinite(probed_duration) or abs(probed_duration - duration) > (1 / sample_rate) + 0.001:
                raise ValueError("FFprobe duration did not match the WAV sample count")
        except TranscriptionError:
            raise
        except (OSError, EOFError, wave.Error, TypeError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
            logger.warning("Audio chunk validation failed: index=%s source_offset=%.6fs requested_chunk_duration=%ss file_size=%s ffprobe_exit=%s stdout=%s stderr=%s reason=%s", index, offset, requested_duration, file_size, probe.returncode if probe else "not-run", _diagnostic_text(probe.stdout, path, work_dir) if probe else "", _diagnostic_text(probe.stderr, path, work_dir) if probe else "", exc)
            raise TranscriptionError(f"Extracted audio chunk {index} is incomplete or has invalid duration metadata. Check the local backend logs.") from exc
        chunks.append(AudioChunk(path, duration, offset))
        offset += duration
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
        source_offset = _source_audio_offset(source_path, settings.resolve_ffprobe())
        chunks = _extract_audio_chunks(source_path, audio_work_dir, source_offset)
        return _transcribe_chunks(
            chunks, project_id, source_filename, transcript_dir, stage_callback,
            model_size=model_size, language=language,
            duration=media.get("duration_seconds"), source_path=source_path,
            source_offset_seconds=source_offset,
        )
    finally:
        shutil.rmtree(audio_work_dir, ignore_errors=True)


def _transcribe_chunks(
    chunks: list[AudioChunk],
    project_id: str,
    source_filename: str,
    transcript_dir: Path,
    stage_callback: Callable[[str, str, str | None], None],
    model_size: str | None,
    language: str | None,
    duration: float | None,
    source_path: Path,
    source_offset_seconds: float = 0.0,
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
        for index, chunk in enumerate(chunks, start=1):
            stage_callback("transcribing", f"Transcribing audio part {index} of {len(chunks)} with {chosen_model} on {active_device}", active_device)
            iterator, info = active_model.transcribe(
                str(chunk.path), language=active_language, word_timestamps=True, vad_filter=True,
            )
            normal_segments, normal_words = _normalise(list(iterator))
            for segment in normal_segments:
                segment["start"] += chunk.source_offset_seconds
                segment["end"] += chunk.source_offset_seconds
                for word in segment["words"]:
                    word["start"] += chunk.source_offset_seconds
                    word["end"] += chunk.source_offset_seconds
                output_segments.append(segment)
            output_words.extend(word for segment in normal_segments for word in segment["words"])
            detected_info = info
            if active_language is None:
                active_language = getattr(info, "language", None)
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
    diarization_state = diarization.availability()
    if diarization_state["available"]:
        stage_callback("diarizing", "Estimating speaker turns locally", selected_device)
        diarized = diarization.diarize_file(source_path, words, segments, source_offset_seconds)
        words, segments = diarized["words"], diarized["segments"]
    else:
        diarized = {"speakers": [], "speaker_turns": [], "status": "unavailable",
                    "detail": diarization_state.get("detail", "unavailable"), "message": diarization_state["message"]}
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
        "speakers": diarized["speakers"],
        "speaker_turns": diarized["speaker_turns"],
        "diarization_status": diarized["status"],
        "diarization_detail": diarized.get("detail"),
        "diarization_message": diarized["message"],
        "segments": segments,
        "words": words,
    }
    stage_callback("saving_transcript", "Saving transcript files", selected_device)
    paths = _write_outputs(transcript_dir, payload)
    return {**payload, **paths}
