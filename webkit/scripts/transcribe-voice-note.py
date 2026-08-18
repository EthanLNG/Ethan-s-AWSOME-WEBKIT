#!/usr/bin/env python3
"""Transcribe a Webkit voice note with an installed, local Whisper engine."""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def transcribe(path, language):
    whisper = shutil.which("whisper")
    if whisper:
        with tempfile.TemporaryDirectory(prefix="webkit-whisper-") as output_dir:
            args = [whisper, str(path), "--output_format", "txt", "--output_dir", output_dir]
            if language:
                args += ["--language", language]
            result = subprocess.run(args, text=True, capture_output=True)
            if result.returncode:
                raise RuntimeError((result.stderr or result.stdout).strip())
            transcript = Path(output_dir) / (path.stem + ".txt")
            if transcript.is_file():
                return transcript.read_text(encoding="utf-8").strip()
    whisper_cpp = shutil.which("whisper-cli")
    model = os.environ.get("WHISPER_MODEL")
    if whisper_cpp and model:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("whisper-cli voice notes require ffmpeg to decode browser audio")
        with tempfile.TemporaryDirectory(prefix="webkit-whisper-cpp-") as output_dir:
            wav = Path(output_dir) / "voice-note.wav"
            converted = subprocess.run(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
                 "-ar", "16000", "-ac", "1", str(wav)],
                text=True, capture_output=True,
            )
            if converted.returncode:
                raise RuntimeError((converted.stderr or converted.stdout).strip())
            args = [whisper_cpp, "-m", model, "-f", str(wav), "-nt"]
            if language:
                args += ["-l", language]
            result = subprocess.run(args, text=True, capture_output=True)
            if result.returncode:
                raise RuntimeError((result.stderr or result.stdout).strip())
            return result.stdout.strip()
    raise RuntimeError(
        "No local transcription engine is configured. Install the Python 'whisper' "
        "command, or install whisper-cli and set WHISPER_MODEL."
    )


def main():
    parser = argparse.ArgumentParser(description="Transcribe an AWESOME WEBKIT voice note locally")
    parser.add_argument("audio")
    parser.add_argument("--language", choices=("en", "he"))
    args = parser.parse_args()
    path = Path(args.audio).expanduser().resolve()
    if not path.is_file():
        parser.error("audio file not found: {}".format(path))
    try:
        text = transcribe(path, args.language)
        if not text:
            raise RuntimeError("Whisper returned an empty transcript")
        print(text)
    except Exception as exc:
        print("transcription failed: {}".format(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
