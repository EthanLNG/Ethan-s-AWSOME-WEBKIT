#!/usr/bin/env python3
"""Transcribe a Webkit voice note locally or with OpenAI."""

import argparse
import json
import mimetypes
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path


MAX_AUDIO_BYTES = 25 * 1024 * 1024
MAX_OPENAI_RESPONSE_BYTES = 1024 * 1024
OPENAI_TRANSCRIPTION_URL = "https://api.openai.com/v1/audio/transcriptions"
OPENAI_TRANSCRIPTION_MODEL = "gpt-4o-transcribe"


def command_timeout_seconds():
    raw = os.environ.get("WK_TRANSCRIPTION_TIMEOUT", "300")
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        raise RuntimeError("WK_TRANSCRIPTION_TIMEOUT must be a positive number")
    if not 0 < seconds < float("inf"):
        raise RuntimeError("WK_TRANSCRIPTION_TIMEOUT must be a positive finite number")
    return seconds


def run_local_command(args, label):
    timeout = command_timeout_seconds()
    try:
        return subprocess.run(
            args,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            "{} timed out after {:g} seconds".format(label, timeout)
        )


def transcribe(path, language):
    ffmpeg = shutil.which("ffmpeg")
    whisper = shutil.which("whisper")
    if whisper:
        if not ffmpeg:
            raise RuntimeError("Python Whisper voice notes require ffmpeg to decode browser audio")
        with tempfile.TemporaryDirectory(prefix="webkit-whisper-") as output_dir:
            args = [whisper, str(path), "--output_format", "txt", "--output_dir", output_dir]
            if language:
                args += ["--language", language]
            result = run_local_command(args, "Python Whisper transcription")
            if result.returncode:
                raise RuntimeError((result.stderr or result.stdout).strip())
            transcript = Path(output_dir) / (path.stem + ".txt")
            if transcript.is_file():
                return transcript.read_text(encoding="utf-8").strip()
    whisper_cpp = shutil.which("whisper-cli")
    model = os.environ.get("WHISPER_MODEL")
    if whisper_cpp and model and Path(model).is_file():
        if not ffmpeg:
            raise RuntimeError("whisper-cli voice notes require ffmpeg to decode browser audio")
        with tempfile.TemporaryDirectory(prefix="webkit-whisper-cpp-") as output_dir:
            wav = Path(output_dir) / "voice-note.wav"
            converted = run_local_command(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
                 "-ar", "16000", "-ac", "1", str(wav)],
                "ffmpeg voice-note conversion",
            )
            if converted.returncode:
                raise RuntimeError((converted.stderr or converted.stdout).strip())
            args = [whisper_cpp, "-m", model, "-f", str(wav), "-nt"]
            if language:
                args += ["-l", language]
            result = run_local_command(args, "whisper-cli transcription")
            if result.returncode:
                raise RuntimeError((result.stderr or result.stdout).strip())
            return result.stdout.strip()
    raise RuntimeError(
        "No local transcription engine is configured. Install the Python 'whisper' "
        "command, or install whisper-cli and set WHISPER_MODEL to an existing model file."
    )


def _multipart_field(boundary, name, value):
    return (
        "--{}\r\nContent-Disposition: form-data; name=\"{}\"\r\n\r\n{}\r\n"
        .format(boundary, name, value)
    ).encode("utf-8")


def transcribe_openai(path, language):
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is required for OpenAI cloud transcription"
        )
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise RuntimeError("voice note could not be read: {}".format(exc))
    if not 0 < size <= MAX_AUDIO_BYTES:
        raise RuntimeError("voice note must be between 1 byte and 25 MB")
    try:
        audio = path.read_bytes()
    except OSError as exc:
        raise RuntimeError("voice note could not be read: {}".format(exc))
    if len(audio) != size:
        raise RuntimeError("voice note changed while it was read")

    boundary = "webkit-{}".format(secrets.token_hex(16))
    mime_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    filename = path.name.replace('"', "") or "voice-note.webm"
    body = bytearray()
    body.extend(_multipart_field(boundary, "model", OPENAI_TRANSCRIPTION_MODEL))
    if language:
        body.extend(_multipart_field(boundary, "language", language))
    body.extend((
        "--{}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{}\"\r\n"
        "Content-Type: {}\r\n\r\n"
        .format(boundary, filename, mime_type)
    ).encode("utf-8"))
    body.extend(audio)
    body.extend("\r\n--{}--\r\n".format(boundary).encode("ascii"))
    request = urllib.request.Request(
        OPENAI_TRANSCRIPTION_URL,
        data=bytes(body),
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "multipart/form-data; boundary=" + boundary,
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request, timeout=command_timeout_seconds()
        ) as response:
            payload = response.read(MAX_OPENAI_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        try:
            payload = exc.read(MAX_OPENAI_RESPONSE_BYTES + 1)
            detail = json.loads(payload.decode("utf-8")).get("error", {}).get("message")
        except (AttributeError, UnicodeDecodeError, ValueError):
            detail = None
        raise RuntimeError(
            "OpenAI transcription request failed with HTTP {}{}".format(
                exc.code, ": " + str(detail)[:500] if detail else ""
            )
        )
    except (OSError, urllib.error.URLError) as exc:
        raise RuntimeError("OpenAI transcription request failed: {}".format(exc))
    if len(payload) > MAX_OPENAI_RESPONSE_BYTES:
        raise RuntimeError("OpenAI transcription response exceeded 1 MB")
    try:
        result = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise RuntimeError("OpenAI transcription returned invalid JSON")
    text = result.get("text") if isinstance(result, dict) else None
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError("OpenAI transcription returned an empty transcript")
    return text.strip()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Transcribe an AWESOME WEBKIT voice note")
    parser.add_argument("audio")
    parser.add_argument("--language", choices=("en", "he"))
    parser.add_argument("--engine", choices=("local", "openai"), default="local")
    args = parser.parse_args(argv)
    path = Path(args.audio).expanduser().resolve()
    if not path.is_file():
        parser.error("audio file not found: {}".format(path))
    try:
        text = (
            transcribe_openai(path, args.language)
            if args.engine == "openai"
            else transcribe(path, args.language)
        )
        if not text:
            raise RuntimeError("Transcription returned an empty transcript")
        print(text)
    except Exception as exc:
        print("transcription failed: {}".format(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
