#!/usr/bin/env python3
"""Ask the local preview server to perform one feedback-round transition."""

import argparse
import http.client
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path


_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_BATCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_MAX_RESPONSE = 2 * 1024 * 1024
_MAX_NEXT_REVIEW = 2 * 1024 * 1024
_MAX_CONFIG = 1024 * 1024


def fail(message):
    print("transition-round.py: " + message, file=sys.stderr)
    return 2


def strict_json_loads(data):
    def reject_constant(value):
        raise ValueError("non-standard JSON constant: {}".format(value))

    value = json.loads(data, parse_constant=reject_constant)

    def reject_surrogates(item):
        if isinstance(item, str):
            if any(0xD800 <= ord(char) <= 0xDFFF for char in item):
                raise ValueError("JSON contains an unpaired Unicode surrogate")
        elif isinstance(item, list):
            for nested in item:
                reject_surrogates(nested)
        elif isinstance(item, dict):
            for key, nested in item.items():
                reject_surrogates(key)
                reject_surrogates(nested)

    reject_surrogates(value)
    return value


def read_bounded_regular_text(path, maximum, label):
    path = Path(path)
    try:
        before = os.lstat(str(path))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError("{} is unavailable: {}".format(label, exc))
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("{} must be a regular file, not a link or special path".format(label))
    if before.st_size > maximum:
        raise ValueError("{} exceeds {} bytes".format(label, maximum))
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = None
    try:
        descriptor = os.open(str(path), flags)
        opened = os.fstat(descriptor)
        chunks = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after_read = os.fstat(descriptor)
        current = os.lstat(str(path))
    except OSError as exc:
        raise ValueError("{} cannot be read safely: {}".format(label, exc))
    finally:
        if descriptor is not None:
            os.close(descriptor)
    def signature(value):
        return (
            value.st_dev, value.st_ino, value.st_mode, value.st_size,
            getattr(value, "st_mtime_ns", int(value.st_mtime * 1000000000)),
        )
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(current.st_mode)
        or signature(opened) != signature(before)
        or signature(opened) != signature(after_read)
        or signature(opened) != signature(current)
    ):
        raise ValueError("{} changed while it was read".format(label))
    data = b"".join(chunks)
    if len(data) > maximum:
        raise ValueError("{} exceeds {} bytes".format(label, maximum))
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("{} is not UTF-8: {}".format(label, exc))


def load_config(script_dir):
    configured = os.environ.get("WK_CONFIG")
    candidates = (
        [Path(configured)]
        if configured
        else [script_dir.parent / "webkit.config.json", script_dir.parent / "webkit.config.template.json"]
    )
    for candidate in candidates:
        try:
            source = read_bounded_regular_text(candidate, _MAX_CONFIG, "Webkit config")
            if source is None:
                continue
            value = strict_json_loads(source)
        except ValueError as exc:
            raise ValueError("{} is not valid JSON: {}".format(candidate, exc))
        if not isinstance(value, dict):
            raise ValueError("{} must contain a JSON object".format(candidate))
        return value
    raise ValueError("no Webkit config file was found")


def git_root():
    try:
        result = subprocess.run(
            ["git", "-C", os.getcwd(), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError("the current directory is not inside a git repository: {}".format(exc))
    return Path(result.stdout.strip()).resolve()


def palette_entry(config, slug):
    palette = config.get("palette")
    if not isinstance(palette, list) or not palette:
        raise ValueError("config palette must be a non-empty list")
    matches = []
    ports = set()
    for entry in palette:
        if not isinstance(entry, dict):
            raise ValueError("every palette entry must be an object")
        entry_slug = entry.get("slug")
        port = entry.get("port")
        if not isinstance(entry_slug, str) or not _SLUG_RE.fullmatch(entry_slug):
            raise ValueError("palette contains an unsafe slug")
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise ValueError("palette ports must be integers from 1 to 65535")
        if port in ports:
            raise ValueError("palette ports must be unique")
        ports.add(port)
        if entry_slug == slug:
            matches.append(entry)
    if len(matches) != 1:
        raise ValueError("slug {!r} does not identify one palette entry".format(slug))
    return matches[0]


def feedback_inbox(config, root, slug):
    value = config.get("feedback_dir", ".webkit/feedback")
    if not isinstance(value, str) or not value or os.path.isabs(value):
        raise ValueError("feedback_dir must be a non-empty relative path")
    inbox = (root / value / slug).resolve()
    try:
        inbox.relative_to(root)
    except ValueError:
        raise ValueError("feedback_dir escapes the git repository")
    return inbox


def read_private_token(path):
    try:
        before = os.lstat(str(path))
    except OSError as exc:
        raise ValueError("transition token is unavailable; start the preview server first: {}".format(exc))
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("transition token is not a regular file")
    if before.st_size > 256:
        raise ValueError("transition token is unexpectedly large")
    if os.name == "posix" and before.st_mode & 0o077:
        raise ValueError("transition token permissions are not private")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(str(path), flags)
        try:
            opened = os.fstat(descriptor)
            data = os.read(descriptor, 257)
            after_read = os.fstat(descriptor)
            current = os.lstat(str(path))
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise ValueError("transition token cannot be read: {}".format(exc))
    def signature(value):
        return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)

    if not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(current.st_mode):
        raise ValueError("transition token must remain a regular file")
    if signature(opened) != signature(before):
        raise ValueError("transition token changed while it was opened")
    if signature(opened) != signature(after_read) or signature(opened) != signature(current):
        raise ValueError("transition token changed while it was read")
    if len(data) > 256:
        raise ValueError("transition token is unexpectedly large")
    try:
        token = data.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise ValueError("transition token is not UTF-8: {}".format(exc))
    if not _TOKEN_RE.fullmatch(token):
        raise ValueError("transition token is malformed")
    return token


def load_review(path):
    try:
        before = os.lstat(str(path))
    except OSError as exc:
        raise ValueError("next review is not readable JSON: {}".format(exc))
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("next review must be a regular file, not a symlink or special path")
    if before.st_size > _MAX_NEXT_REVIEW:
        raise ValueError("next review exceeds 2 MB")

    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(str(path), flags)
        try:
            opened = os.fstat(descriptor)
            chunks = []
            remaining = _MAX_NEXT_REVIEW + 1
            while remaining:
                chunk = os.read(descriptor, min(remaining, 65536))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            after_read = os.fstat(descriptor)
            current = os.lstat(str(path))
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise ValueError("next review cannot be opened safely: {}".format(exc))
    if not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(current.st_mode):
        raise ValueError("next review must remain a regular file")
    def signature(value):
        return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)

    if signature(opened) != signature(before):
        raise ValueError("next review changed while it was opened")
    if signature(opened) != signature(after_read) or signature(opened) != signature(current):
        raise ValueError("next review changed while it was read")
    data = b"".join(chunks)
    if len(data) > _MAX_NEXT_REVIEW:
        raise ValueError("next review exceeds 2 MB")
    try:
        value = strict_json_loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("next review is not readable JSON: {}".format(exc))
    if not isinstance(value, dict):
        raise ValueError("next review must contain a JSON object")
    return value


def request_transition(port, token, encoded):
    connection = None
    try:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        connection.request(
            "POST",
            "/__wk/transition",
            body=encoded,
            headers={
                "Content-Type": "application/json",
                "X-WK-Transition-Token": token,
            },
        )
        response = connection.getresponse()
        payload = response.read(_MAX_RESPONSE + 1)
        status = response.status
    except (OSError, http.client.HTTPException) as exc:
        raise ValueError("preview server request failed: {}".format(exc))
    finally:
        try:
            if connection is not None:
                connection.close()
        except (OSError, http.client.HTTPException):
            pass
    if len(payload) > _MAX_RESPONSE:
        raise ValueError("preview server response exceeded 2 MB")
    try:
        result = strict_json_loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ValueError("preview server returned a non-JSON response")
    if not isinstance(result, dict):
        raise ValueError("preview server returned an invalid JSON response")
    return status, result


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("slug")
    parser.add_argument("mode", choices=("feedback-update", "redo", "complete"))
    parser.add_argument("batch_id")
    parser.add_argument("round", type=int)
    parser.add_argument("--next-review", type=Path)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if not _SLUG_RE.fullmatch(args.slug):
        return fail("slug is not a safe token")
    if not _BATCH_RE.fullmatch(args.batch_id):
        return fail("batch id is not a safe token")
    if args.round < 1:
        return fail("round must be a positive integer")
    if args.mode == "redo" and args.next_review is None:
        return fail("redo requires --next-review")
    if args.mode == "complete" and args.next_review is not None:
        return fail("complete does not accept --next-review")

    try:
        script_dir = Path(__file__).resolve().parent
        config = load_config(script_dir)
        entry = palette_entry(config, args.slug)
        root = git_root()
        inbox = feedback_inbox(config, root, args.slug)
        token = read_private_token(inbox / "transition-token")
        next_review = load_review(args.next_review) if args.next_review else None
    except ValueError as exc:
        return fail(str(exc))

    body = {
        "version": 1,
        "mode": args.mode,
        "batchId": args.batch_id,
        "round": args.round,
    }
    if next_review is not None:
        body["nextReview"] = next_review
    encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
    try:
        status, result = request_transition(entry["port"], token, encoded)
    except ValueError as exc:
        return fail(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if status == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
