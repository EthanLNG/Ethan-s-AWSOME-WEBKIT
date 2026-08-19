#!/bin/sh
# wait-for-file.sh - block until <path> exists AND parses as JSON.
#
# The agent side of the feedback loop: the browser overlay (via the preview
# server) drops feedback.json / verdicts.json into .webkit/feedback/<slug>/,
# and the agent parks on this script until the file lands. The JSON-parse
# check guards against catching a partial write mid-flight - the kit's own
# server writes atomically (tmp + rename), but a file that appears via any
# other route (hand-edit, scp, a different tool) could be seen half-written;
# an unparsable file is simply retried next tick, never treated as arrival.
#
# Usage:  wait-for-file.sh <path> [timeout_seconds=300] [interval_seconds=1]
#
#   file arrives (and parses)  -> prints the path, exit 0
#   timeout, path absent       -> exit 124 (same code as timeout(1), so
#                                 callers can re-run on 124 to keep waiting)
#   timeout, path present but   -> exit 65: the file is there but never parsed
#     never parses as JSON         as JSON (truncated / hand-edited / corrupt).
#                                 Distinct from 124 so the caller stops
#                                 re-waiting and surfaces it instead of hanging
#                                 forever on a file that will never come good.
#
# Pure POSIX sh (only `set -u` - a missed tick must retry, not abort the
# wait), python3 stdlib for the JSON check. No inotify/fswatch: a 1s poll is
# plenty for a human-paced loop and works identically everywhere.

set -u

path="${1:?usage: wait-for-file.sh <path> [timeout_seconds=300] [interval_seconds=1]}"
timeout="${2:-300}"
interval="${3:-1}"

case "$timeout" in
  ''|*[!0-9]*) echo "wait-for-file.sh: timeout_seconds must be a non-negative integer, got '$timeout'" >&2; exit 2 ;;
esac
# interval must be a *positive* integer: `sleep 0`, `sleep ""`, or `sleep abc`
# all fail instantly, and because we deliberately run without `set -e` the loop
# would then spin at full speed, forking a python3 probe + a failing sleep every
# iteration (measured ~500/s) until timeout. Reject 0 too - an interval of 0 is
# a busy-loop by definition.
case "$interval" in
  ''|*[!0-9]*|0) echo "wait-for-file.sh: interval_seconds must be a positive integer, got '$interval'" >&2; exit 2 ;;
esac

start="$(date +%s)"
while :; do
  probe="$(python3 -c '
import json, os, stat, sys
path = sys.argv[1]
limit = 4 * 1024 * 1024
try:
    before = os.lstat(path)
except FileNotFoundError:
    sys.exit(10)
except OSError as exc:
    print("path cannot be inspected safely: %s" % exc)
    sys.exit(66)
if not stat.S_ISREG(before.st_mode):
    print("path is not a regular file")
    sys.exit(66)
if before.st_size > limit:
    print("JSON protocol file exceeds 4 MB")
    sys.exit(66)
flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
try:
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        chunks = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after_read = os.fstat(descriptor)
        current = os.lstat(path)
    finally:
        os.close(descriptor)
except FileNotFoundError:
    sys.exit(10)
except OSError as exc:
    print("path cannot be opened safely: %s" % exc)
    sys.exit(66)
if not stat.S_ISREG(opened.st_mode):
    print("opened path is not a regular file")
    sys.exit(66)
def signature(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
if signature(opened) != signature(before):
    print("path changed while it was opened")
    sys.exit(66)
if signature(opened) != signature(after_read) or signature(opened) != signature(current):
    print("path changed while it was read")
    sys.exit(66)
data = b"".join(chunks)
if len(data) > limit:
    print("JSON protocol file exceeds 4 MB")
    sys.exit(66)
try:
    def reject_constant(value):
        raise ValueError("non-standard JSON constant: %s" % value)
    def reject_surrogates(value):
        if isinstance(value, str):
            if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
                raise ValueError("JSON contains an unpaired Unicode surrogate")
        elif isinstance(value, list):
            for item in value:
                reject_surrogates(item)
        elif isinstance(value, dict):
            for key, item in value.items():
                reject_surrogates(key)
                reject_surrogates(item)
    value = json.loads(data.decode("utf-8"), parse_constant=reject_constant)
    reject_surrogates(value)
    if not isinstance(value, dict):
        raise ValueError("top-level JSON value must be an object")
except (UnicodeDecodeError, ValueError) as exc:
    print(str(exc))
    sys.exit(65)
' "$path" 2>&1)"
  probe_status=$?
  case "$probe_status" in
    0)
      printf '%s\n' "$path"
      exit 0
      ;;
    10) : ;;
    65) : ;;
    *)
      echo "wait-for-file.sh: refusing unsafe protocol path $path (${probe:-unknown file type})" >&2
      exit 66
      ;;
  esac
  now="$(date +%s)"
  if [ "$((now - start))" -ge "$timeout" ]; then
    # Distinguish "never arrived" from "arrived but won't parse". If the path
    # exists we've been silently retrying an unparsable file - re-running (the
    # 124 convention) would hang forever, so report it and exit 65 instead.
    if [ "$probe_status" -eq 65 ]; then
      echo "wait-for-file.sh: timed out after ${timeout}s; $path is regular but does not parse as JSON (${probe})" >&2
      exit 65
    fi
    echo "wait-for-file.sh: timed out after ${timeout}s waiting for $path" >&2
    exit 124
  fi
  sleep "$interval"
done
