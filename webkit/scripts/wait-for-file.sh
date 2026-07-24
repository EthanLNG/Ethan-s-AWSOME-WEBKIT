#!/bin/sh
# wait-for-file.sh — block until <path> exists AND parses as JSON.
#
# The agent side of the feedback loop: the browser overlay (via the preview
# server) drops feedback.json / verdicts.json into .webkit/feedback/<slug>/,
# and the agent parks on this script until the file lands. The JSON-parse
# check guards against catching a partial write mid-flight — the kit's own
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
# Pure POSIX sh (only `set -u` — a missed tick must retry, not abort the
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
# iteration (measured ~500/s) until timeout. Reject 0 too — an interval of 0 is
# a busy-loop by definition.
case "$interval" in
  ''|*[!0-9]*|0) echo "wait-for-file.sh: interval_seconds must be a positive integer, got '$interval'" >&2; exit 2 ;;
esac

start="$(date +%s)"
while :; do
  if [ -e "$path" ] && python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$path" 2>/dev/null; then
    printf '%s\n' "$path"
    exit 0
  fi
  now="$(date +%s)"
  if [ "$((now - start))" -ge "$timeout" ]; then
    # Distinguish "never arrived" from "arrived but won't parse". If the path
    # exists we've been silently retrying an unparsable file — re-running (the
    # 124 convention) would hang forever, so report it and exit 65 instead.
    if [ -e "$path" ]; then
      err="$(python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$path" 2>&1 >/dev/null | tail -n1)"
      echo "wait-for-file.sh: timed out after ${timeout}s — $path exists but does not parse as JSON; fix or delete it (${err})" >&2
      exit 65
    fi
    echo "wait-for-file.sh: timed out after ${timeout}s waiting for $path" >&2
    exit 124
  fi
  sleep "$interval"
done
