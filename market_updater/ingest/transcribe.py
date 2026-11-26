"""Podcast and earnings-call audio transcription with Groq Whisper (free tier).

Audio is first re-encoded to 16 kHz mono 32 kbps MP3, which makes an hour of speech about
14 MB, under Groq's 25 MB per-request limit. Longer files are split with ffmpeg.
"""
from __future__ import annotations

import math
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..util import http, log

GROQ_AUDIO_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
MODEL = os.environ.get("GROQ_WHISPER_MODEL", "whisper-large-v3-turbo")
LIMIT_BYTES = 24 * 1024 * 1024
MAX_DOWNLOAD = 400 * 1024 * 1024


class TranscriptionError(RuntimeError):
    pass


def _run(cmd: list[str]) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise TranscriptionError(f"{cmd[0]} failed: {p.stderr[-400:]}")
    return p.stdout


def duration_seconds(path: Path) -> float:
    out = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)])
    return float(out.strip() or 0)


def _download(url: str, dest: Path) -> None:
    with http().stream("GET", url, timeout=300) as r:
        r.raise_for_status()
        size = 0
        with dest.open("wb") as fh:
            for chunk in r.iter_bytes(1024 * 1024):
                size += len(chunk)
                if size > MAX_DOWNLOAD:
                    raise TranscriptionError("audio file too large")
                fh.write(chunk)


def _split(path: Path, workdir: Path) -> list[Path]:
    size = path.stat().st_size
    if size <= LIMIT_BYTES:
        return [path]
    dur = duration_seconds(path)
    n = math.ceil(size / LIMIT_BYTES)
    step = dur / n
    parts = []
    for i in range(n):
        part = workdir / f"part{i:03d}.mp3"
        _run(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(i * step), "-t", str(step),
              "-i", str(path), "-c", "copy", str(part)])
        parts.append(part)
    return parts


SPEED = float(os.environ.get("MU_AUDIO_SPEED", "1.4"))   # Whisper copes well with sped-up speech


def transcribe_url(audio_url: str, api_key: str | None = None, speed: float = SPEED) -> tuple[str, float]:
    """Return (transcript text, audio seconds sent to Groq).

    Speech is sped up (1.4x by default) before upload: Groq bills and rate-limits by audio
    seconds, so this fits about 40% more listening into the free allowance."""
    api_key = api_key or os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise TranscriptionError("GROQ_API_KEY not set")
    if not shutil.which("ffmpeg"):
        raise TranscriptionError("ffmpeg is not installed")
    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "raw"
        _download(audio_url, raw)
        return transcribe_file(raw, api_key, speed)


def transcribe_file(raw: Path, api_key: str | None = None, speed: float = SPEED) -> tuple[str, float]:
    """Transcribe an audio file already on disk (used for YouTube audio)."""
    api_key = api_key or os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise TranscriptionError("GROQ_API_KEY not set")
    if not shutil.which("ffmpeg"):
        raise TranscriptionError("ffmpeg is not installed")
    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        small = tmpd / "speech.mp3"
        tempo = ["-filter:a", f"atempo={speed}"] if speed and abs(speed - 1) > 0.01 else []
        _run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw), *tempo, "-ac", "1", "-ar", "16000",
              "-b:a", "32k", str(small)])
        seconds = duration_seconds(small)
        texts = []
        for part in _split(small, tmpd):
            with part.open("rb") as fh:
                r = http().post(GROQ_AUDIO_URL, headers={"Authorization": f"Bearer {api_key}"},
                                data={"model": MODEL, "response_format": "text", "language": "en"},
                                files={"file": (part.name, fh, "audio/mpeg")}, timeout=600)
            if r.status_code == 429:
                raise TranscriptionError("Groq audio rate limit reached for today")
            r.raise_for_status()
            texts.append(r.text.strip())
        log.info("Transcribed %.0f min of audio", seconds / 60)
        return "\n".join(texts), seconds
