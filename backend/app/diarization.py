"""Optional, local-only speaker diarization with conservative word alignment."""

from __future__ import annotations

import importlib.util
import logging
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from app.core.config import settings

logger = logging.getLogger(__name__)
_pipeline_lock = threading.Lock()
_pipeline_path: str | None = None
_pipeline = None


def _configured_model_path() -> Path | None:
    value = settings.diarization_model_path
    if value is None:
        return None
    path = Path(value).expanduser().resolve()
    return path if path.is_dir() else None


def availability() -> dict[str, str | bool]:
    """Describe optional support without loading a model or exposing its path."""
    configured = settings.diarization_model_path is not None
    path = _configured_model_path()
    if not configured:
        return {"available": False, "status": "unavailable", "detail": "model_not_configured",
                "message": "No local diarization model is configured. Transcription remains available without speaker labels."}
    if path is None:
        return {"available": False, "status": "unavailable", "detail": "model_path_missing",
                "message": "The configured local diarization model directory is missing or unavailable. Transcription remains available without speaker labels."}
    config_names = ("config.yaml", "config.yml", "config.json")
    if not any((path / name).is_file() for name in config_names):
        return {"available": False, "status": "unavailable", "detail": "model_bundle_incomplete",
                "message": "The configured local diarization directory does not contain a recognizable pipeline configuration. Downloading or gated Hugging Face access is not performed."}
    try:
        importlib.util.find_spec("pyannote.audio")
    except (ImportError, ModuleNotFoundError, ValueError):
        return {"available": False, "status": "unavailable", "detail": "dependency_not_installed",
                "message": "Optional pyannote.audio support is not installed. Transcription remains available without speaker labels."}
    if importlib.util.find_spec("pyannote.audio") is None:
        return {"available": False, "status": "unavailable", "detail": "dependency_not_installed",
                "message": "Optional pyannote.audio support is not installed. Transcription remains available without speaker labels."}
    return {"available": True, "status": "ready", "detail": "ready",
            "message": "A local diarization model and optional dependency are configured."}


def _annotation_turns(output) -> list[tuple[float, float, str]]:
    annotation = getattr(output, "speaker_diarization", None)
    if annotation is None:
        annotation = getattr(output, "exclusive_speaker_diarization", None)
    if annotation is None and hasattr(output, "itertracks"):
        annotation = output
    if annotation is None or not hasattr(annotation, "itertracks"):
        return []
    turns = []
    for span, _track, label in annotation.itertracks(yield_label=True):
        start, end = float(span.start), float(span.end)
        if end > start and str(label):
            turns.append((start, end, str(label)))
    return sorted(turns, key=lambda turn: (turn[0], turn[1], turn[2]))


def assign_speakers(words: list[dict], segments: list[dict], turns: list[tuple[float, float, str]]) -> tuple[list[dict], list[dict], str]:
    """Map diarization turns to word times; leave weak or overlapped matches unknown."""
    if not words or not turns:
        return words, segments, "incomplete"

    first_seen: dict[str, str] = {}
    for _start, _end, raw_label in turns:
        first_seen.setdefault(raw_label, f"speaker_{len(first_seen) + 1}")

    def match(start: float, end: float) -> str | None:
        span = end - start
        if span <= 0:
            return None
        overlaps: dict[str, float] = {}
        for turn_start, turn_end, raw_label in turns:
            overlap = max(0.0, min(end, turn_end) - max(start, turn_start))
            if overlap:
                overlaps[raw_label] = overlaps.get(raw_label, 0.0) + overlap
        if not overlaps:
            return None
        best = sorted(overlaps.items(), key=lambda item: item[1], reverse=True)
        best_share = best[0][1] / span
        second_share = best[1][1] / span if len(best) > 1 else 0.0
        if best_share < 0.55 or second_share >= 0.2:
            return None
        return first_seen[best[0][0]]

    aligned_words = []
    for word in words:
        copied = dict(word)
        copied["speaker_id"] = match(float(word["start"]), float(word["end"]))
        aligned_words.append(copied)

    aligned_segments = []
    for segment in segments:
        copied = dict(segment)
        speaker_id = match(float(segment["start"]), float(segment["end"]))
        copied["speaker_id"] = speaker_id
        segment_words = [word for word in aligned_words if word["start"] < segment["end"] and word["end"] > segment["start"]]
        copied["words"] = [dict(word) for word in segment_words]
        aligned_segments.append(copied)

    speakers = [
        {"speaker_id": speaker_id, "label": f"Speaker {index}"}
        for index, speaker_id in enumerate(first_seen.values(), start=1)
    ]
    status = "complete" if any(word["speaker_id"] for word in aligned_words) else "incomplete"
    return aligned_words, aligned_segments, status


def _load_pipeline(model_path: Path):
    global _pipeline, _pipeline_path
    path_key = str(model_path)
    with _pipeline_lock:
        if _pipeline is not None and _pipeline_path == path_key:
            return _pipeline
        # The local bundle must contain all weights. The app never resolves a Hub identifier.
        previous_offline = os.environ.get("HF_HUB_OFFLINE")
        # pyannote.audio 4.x enables anonymous metrics by default; keep them disabled
        # for this process so local diarization does not contact its telemetry endpoint.
        os.environ["PYANNOTE_METRICS_ENABLED"] = "0"
        os.environ["HF_HUB_OFFLINE"] = "1"
        try:
            import torch
            from pyannote.audio import Pipeline

            pipeline = Pipeline.from_pretrained(path_key)
        finally:
            if previous_offline is None:
                os.environ.pop("HF_HUB_OFFLINE", None)
            else:
                os.environ["HF_HUB_OFFLINE"] = previous_offline
        pipeline.to(torch.device("cpu"))
        _pipeline, _pipeline_path = pipeline, path_key
        return pipeline


def _prepare_audio(source_path: Path) -> tuple[Path, Path]:
    ffmpeg = settings.resolve_ffmpeg()
    if not ffmpeg:
        raise FileNotFoundError("FFmpeg is required to prepare audio for local diarization")
    work_dir = Path(tempfile.mkdtemp(prefix="aiclipping-diarization-"))
    wav_path = work_dir / "diarization.wav"
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(source_path),
             "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
             "-c:a", "pcm_s16le", "-y", str(wav_path)],
            capture_output=True,
            text=True,
            timeout=24 * 60 * 60,
            check=False,
        )
        if result.returncode != 0 or not wav_path.is_file() or wav_path.stat().st_size < 45:
            raise RuntimeError("FFmpeg could not prepare a valid mono 16 kHz audio file for diarization")
        return wav_path, work_dir
    except Exception:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise


def diarize_file(source_path: Path, words: list[dict], segments: list[dict], source_offset_seconds: float = 0.0) -> dict:
    """Run only against a configured on-disk model; never download or call a service."""
    state = availability()
    if not state["available"]:
        return {"speakers": [], "speaker_turns": [], "words": words, "segments": segments,
                "status": "unavailable", "detail": state["detail"], "message": state["message"]}
    work_dir: Path | None = None
    try:
        model_path = _configured_model_path()
        if model_path is None:
            raise FileNotFoundError("Configured local model directory is unavailable")
        pipeline = _load_pipeline(model_path)
        audio_path, work_dir = _prepare_audio(source_path)
        previous = os.environ.get("HF_HUB_OFFLINE")
        os.environ["HF_HUB_OFFLINE"] = "1"
        try:
            output = pipeline(str(audio_path))
        finally:
            if previous is None:
                os.environ.pop("HF_HUB_OFFLINE", None)
            else:
                os.environ["HF_HUB_OFFLINE"] = previous
        turns = [
            (start + source_offset_seconds, end + source_offset_seconds, raw_label)
            for start, end, raw_label in _annotation_turns(output)
        ]
        aligned_words, aligned_segments, status = assign_speakers(words, segments, turns)
        first_seen: dict[str, str] = {}
        speaker_turns = []
        for start, end, raw_label in turns:
            speaker_id = first_seen.setdefault(raw_label, f"speaker_{len(first_seen) + 1}")
            speaker_turns.append({"start": start, "end": end, "speaker_id": speaker_id})
        labels = list(first_seen.values())
        speakers = [{"speaker_id": label, "label": f"Speaker {index}"} for index, label in enumerate(labels, start=1)]
        message = "Speaker turns were estimated locally; labels do not identify real people." if status == "complete" else "Diarization ran, but no word had a sufficiently clear speaker match."
        return {"speakers": speakers, "speaker_turns": speaker_turns, "words": aligned_words, "segments": aligned_segments,
                "status": status, "detail": "complete" if status == "complete" else "no_confident_word_alignment", "message": message}
    except Exception as exc:
        logger.warning("Optional local diarization failed (%s)", type(exc).__name__)
        return {"speakers": [], "speaker_turns": [], "words": words, "segments": segments,
                "status": "incomplete", "detail": "runtime_failed",
                "message": "Local speaker identification failed; transcript text and timestamps are still available."}
    finally:
        if work_dir is not None:
            shutil.rmtree(work_dir, ignore_errors=True)
