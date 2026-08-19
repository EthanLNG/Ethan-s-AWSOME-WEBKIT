#!/bin/sh
set -eu
kit_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
python_bin=$(command -v python3 2>/dev/null || true)
if [ -z "$python_bin" ]; then
  echo "AWESOME WEBKIT requires Python 3.7 or newer. Install Python 3 and try again." >&2
  exit 1
fi
if ! "$python_bin" -c 'import sys; raise SystemExit(sys.version_info[0] != 3 or sys.version_info[1] < 7)' >/dev/null 2>&1; then
  echo "AWESOME WEBKIT requires Python 3.7 or newer. Install or select a supported Python 3 runtime." >&2
  exit 1
fi
exec "$python_bin" "$kit_dir/control-center/launch.py" "$@"
