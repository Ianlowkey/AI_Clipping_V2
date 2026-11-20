import json
import logging
import subprocess
from fractions import Fraction
from pathlib import Path

from app.core.config import settings

logger = logging.getLogger(__name__)


class VideoPreparationError(Exception):
    pass


def prepare_video(path: Path) -> dict:
    ffprobe = settings.resolve_ffprobe()
    ffmpeg = settings.resolve_ffmpeg()
    if not ffprobe or not ffmpeg:
        raise VideoPreparationError("FFmpeg and FFprobe are required. Install them or configure AICLIPPING_FFMPEG_PATH and AICLIPPING_FFPROBE_PATH.")

    try:
        probe = subprocess.run(
            [ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=45, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise VideoPreparationError("FFprobe timed out while inspecting this video.") from exc
    except OSError as exc:
        logger.exception("Unable to start FFprobe")
        raise VideoPreparationError("FFprobe could not be started. Check its configured path.") from exc
    if probe.returncode != 0:
        logger.warning("FFprobe rejected uploaded video (exit %s)", probe.returncode)
        raise VideoPreparationError("FFprobe could not read this file. It may be corrupt or use an unsupported codec.")
    try:
        data = json.loads(probe.stdout)
    except json.JSONDecodeError as exc:
        raise VideoPreparationError("FFprobe returned an unreadable report for this file.") from exc

    streams = data.get("streams", [])
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    if video is None:
        raise VideoPreparationError("The selected file contains no video stream.")
    audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
    duration_value = data.get("format", {}).get("duration") or video.get("duration")
    try:
        duration = float(duration_value) if duration_value is not None else None
    except (TypeError, ValueError):
        duration = None
    frame_rate = None
    try:
        frame_rate = float(Fraction(video.get("avg_frame_rate", "0/0")))
        if frame_rate <= 0:
            frame_rate = None
    except (ValueError, ZeroDivisionError):
        pass

    try:
        decode = subprocess.run(
            [ffmpeg, "-v", "error", "-i", str(path), "-map", "0:v:0", "-frames:v", "1", "-f", "null", "-"],
            capture_output=True, text=True, timeout=60, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise VideoPreparationError("FFmpeg timed out while checking the first video frame.") from exc
    except OSError as exc:
        logger.exception("Unable to start FFmpeg")
        raise VideoPreparationError("FFmpeg could not be started. Check its configured path.") from exc
    if decode.returncode != 0:
        logger.warning("FFmpeg could not decode the first frame (exit %s)", decode.returncode)
        raise VideoPreparationError("FFmpeg could not decode this video. The file may be corrupt or use an unsupported codec.")

    return {
        "duration_seconds": duration,
        "width": video.get("width"),
        "height": video.get("height"),
        "frame_rate": frame_rate,
        "video_codec": video.get("codec_name"),
        "audio_available": bool(audio_streams),
        "audio_codecs": [stream.get("codec_name") for stream in audio_streams],
        "format_name": data.get("format", {}).get("format_name"),
        "readable": True,
        "preparation_message": "Video stream readable; no audio stream found." if not audio_streams else "Video and audio streams detected.",
    }
