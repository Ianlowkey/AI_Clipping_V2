import json
import sys
import wave
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace

import pytest

from app import transcription


SAMPLE_RATE = 16000


def write_wav(path: Path, frames: int):
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(SAMPLE_RATE)
        audio.writeframes(b"\0\0" * frames)


def install_fake_ffmpeg(monkeypatch, durations, *, invalid=None, ffmpeg_error=None, ffprobe_error=None):
    monkeypatch.setattr(type(transcription.settings), "resolve_ffmpeg", lambda _self: "fake-ffmpeg")
    monkeypatch.setattr(type(transcription.settings), "resolve_ffprobe", lambda _self: "fake-ffprobe")
    durations_by_path = {}

    def run(args, **kwargs):
        if args[0] == "fake-ffmpeg":
            if ffmpeg_error:
                return CompletedProcess(args, 9, "", ffmpeg_error)
            pattern = Path(args[-1])
            for index, duration in enumerate(durations):
                path = pattern.parent / f"audio_{index:05}.wav"
                if invalid == index:
                    path.write_bytes(b"not a complete wav" * 8)
                else:
                    frames = round(duration * SAMPLE_RATE)
                    write_wav(path, frames)
                    durations_by_path[path] = frames / SAMPLE_RATE
            return CompletedProcess(args, 0, "", "")
        if ffprobe_error:
            return CompletedProcess(args, 7, "", ffprobe_error)
        path = Path(args[-1])
        duration = durations_by_path[path]
        report = {"format": {"duration": str(duration)}, "streams": [{"codec_name": "pcm_s16le", "sample_rate": str(SAMPLE_RATE), "channels": 1}]}
        return CompletedProcess(args, 0, json.dumps(report), "")

    monkeypatch.setattr(transcription.subprocess, "run", run)
    return durations_by_path


def test_extracts_valid_pcm_wav_chunks_and_uses_sample_counts_for_offsets(tmp_path, monkeypatch):
    install_fake_ffmpeg(monkeypatch, [2.0, 0.75, 0.25])
    monkeypatch.setattr(transcription.settings, "whisper_audio_chunk_seconds", 2)
    chunks = transcription._extract_audio_chunks(tmp_path / "source.mp4", tmp_path, source_offset_seconds=1.5)

    assert [chunk.duration_seconds for chunk in chunks] == [2.0, 0.75, 0.25]
    assert [chunk.source_offset_seconds for chunk in chunks] == [1.5, 3.5, 4.25]
    assert all(chunk.path.suffix == ".wav" and chunk.path.stat().st_size > 44 for chunk in chunks)


def test_audio_that_starts_before_video_is_trimmed_to_video_zero(tmp_path, monkeypatch):
    install_fake_ffmpeg(monkeypatch, [1.0])
    monkeypatch.setattr(transcription.settings, "whisper_audio_chunk_seconds", 2)
    original_run = transcription.subprocess.run
    commands = []

    def capture(args, **kwargs):
        if args[0] == "fake-ffmpeg":
            commands.append(args)
        return original_run(args, **kwargs)

    monkeypatch.setattr(transcription.subprocess, "run", capture)
    chunks = transcription._extract_audio_chunks(tmp_path / "source.mp4", tmp_path, source_offset_seconds=-0.25)

    assert chunks[0].source_offset_seconds == 0.0
    assert commands[0][commands[0].index("-af") + 1] == "atrim=start=0.250000000,asetpts=PTS-STARTPTS"


def test_rejects_empty_chunk(tmp_path, monkeypatch):
    install_fake_ffmpeg(monkeypatch, [0.0])
    with pytest.raises(transcription.TranscriptionError, match="chunk 1 is empty"):
        transcription._extract_audio_chunks(tmp_path / "source.mp4", tmp_path)


def test_rejects_invalid_incomplete_wav(tmp_path, monkeypatch):
    install_fake_ffmpeg(monkeypatch, [1.0], invalid=0)
    with pytest.raises(transcription.TranscriptionError, match="incomplete or has invalid duration metadata"):
        transcription._extract_audio_chunks(tmp_path / "source.mp4", tmp_path)


def test_reports_ffmpeg_failure_and_diagnostic_exit_code(tmp_path, monkeypatch, caplog):
    install_fake_ffmpeg(monkeypatch, [], ffmpeg_error="decoder failed")
    with pytest.raises(transcription.TranscriptionError, match="could not extract audio"):
        transcription._extract_audio_chunks(tmp_path / "source.mp4", tmp_path)
    assert "exit=9" in caplog.text
    assert "decoder failed" in caplog.text


def test_reports_ffprobe_failure_with_chunk_details(tmp_path, monkeypatch, caplog):
    install_fake_ffmpeg(monkeypatch, [1.0], ffprobe_error="invalid WAV header")
    with pytest.raises(transcription.TranscriptionError, match="could not validate extracted audio chunk 1"):
        transcription._extract_audio_chunks(tmp_path / "source.mp4", tmp_path)
    assert "index=1" in caplog.text
    assert "requested_chunk_duration" in caplog.text
    assert "file_size=" in caplog.text
    assert "ffprobe_exit=7" in caplog.text
    assert "invalid WAV header" in caplog.text


def test_chunk_relative_timestamps_align_to_video_timeline_and_temp_audio_is_cleaned(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"mock video")
    chunks = [
        transcription.AudioChunk(tmp_path / "audio_00000.wav", 2.0, 1.25),
        transcription.AudioChunk(tmp_path / "audio_00001.wav", 0.5, 3.25),
    ]
    monkeypatch.setattr(transcription, "prepare_video", lambda _: {"audio_available": True, "duration_seconds": 3.75})
    monkeypatch.setattr(transcription, "_source_audio_offset", lambda *_: 1.25)
    monkeypatch.setattr(transcription, "_extract_audio_chunks", lambda *_args, **_kwargs: chunks)

    class Model:
        def transcribe(self, _path, **_kwargs):
            word = SimpleNamespace(start=0.1, end=0.2, word=" Hi", probability=0.9)
            segment = SimpleNamespace(start=0.1, end=0.2, text="Hi", words=[word])
            return iter([segment]), SimpleNamespace(language="en", language_probability=0.9, duration=0.2)

    monkeypatch.setattr(transcription, "load_model", lambda *_: Model())
    monkeypatch.setattr(transcription.settings, "whisper_device", "cpu")
    monkeypatch.setattr(transcription.settings, "whisper_compute_type", "int8")

    result = transcription.transcribe_video(source, "project", "source.mp4", tmp_path / "project" / "transcript", lambda *_: None)

    assert [segment["start"] for segment in result["segments"]] == pytest.approx([1.35, 3.35])
    assert [word["start"] for word in result["words"]] == pytest.approx([1.35, 3.35])
    assert not list((tmp_path / "project" / "temp").glob("transcription-audio-*"))


def test_source_audio_offset_uses_audio_start_relative_to_video(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    report = {"format": {"start_time": "0.0"}, "streams": [
        {"codec_type": "video", "start_time": "0.500000"},
        {"codec_type": "audio", "start_time": "1.250000"},
    ]}
    monkeypatch.setattr(type(transcription.settings), "resolve_ffprobe", lambda _self: "fake-ffprobe")
    monkeypatch.setattr(transcription.subprocess, "run", lambda *_args, **_kwargs: CompletedProcess([], 0, json.dumps(report), ""))
    assert transcription._source_audio_offset(source, "fake-ffprobe") == pytest.approx(0.75)


def test_pyav_19_compatibility_drops_only_removed_metadata_keyword(monkeypatch):
    received = {}

    def open_file(file, *args, **kwargs):
        received.update(file=file, args=args, kwargs=kwargs)
        return "opened"

    fake_av = SimpleNamespace(__version__="19.0.2", open=open_file)
    monkeypatch.setitem(sys.modules, "av", fake_av)

    transcription._configure_pyav_open_compatibility()
    result = fake_av.open("audio.wav", mode="r", metadata_errors="ignore", timeout=3)

    assert result == "opened"
    assert received == {"file": "audio.wav", "args": (), "kwargs": {"mode": "r", "timeout": 3}}
