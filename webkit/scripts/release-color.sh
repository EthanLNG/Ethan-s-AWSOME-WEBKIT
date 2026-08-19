#!/bin/bash
# release-color.sh: release one linked color and TCP port reservation.
#
# The owner guard and random reservation token prevent stale cleanup from
# deleting a newer claim or another worktree's reservation. WK_COLOR_FORCE=1
# is the explicit human recovery path for a known dead legacy or linked claim.

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
registry="$script_dir/runtime_registry.py"
if [ "$#" -eq 2 ] && [ "$1" = "--slug" ]; then
  slug="$2"
  case "$slug" in
    ''|[!A-Za-z0-9]*|*[!A-Za-z0-9_-]*)
      echo "release-color.sh: --slug needs a safe palette slug" >&2
      exit 2
      ;;
  esac
  port="$("$script_dir/config-get.sh" palette | awk -v wanted="$slug" '$1 == wanted { print $3; found=1 } END { if (!found) print 1 }')"
  emoji="$slug"
elif [ "$#" -eq 1 ]; then
  emoji="$1"
  slug_port="$("$script_dir/config-get.sh" color "$emoji")"
  slug="${slug_port%% *}"
  port="${slug_port##* }"
else
  echo "usage: release-color.sh <emoji> | release-color.sh --slug <palette-slug>" >&2
  exit 2
fi
lockdir="${WK_COLOR_LOCKDIR:-$("$script_dir/config-get.sh" get lock_dir)}"
me="${WK_COLOR_OWNER:-$(pwd -P)}"

status=0
if [ "${WK_COLOR_FORCE:-0}" = "1" ]; then
  if python3 "$registry" release-color \
    --lock-dir "$lockdir" --color "$slug" --port "$port" --owner "$me" \
    --force; then
    :
  else
    status=$?
  fi
else
  if python3 "$registry" release-color \
    --lock-dir "$lockdir" --color "$slug" --port "$port" --owner "$me"; then
    :
  else
    status=$?
  fi
fi

if [ "$status" -eq 0 ]; then
  exit 0
else
  if [ "$status" -eq 3 ]; then
    echo "release-color.sh: $emoji is claimed by another owner; not releasing. Set WK_COLOR_FORCE=1 only for a verified dead session." >&2
    exit 1
  fi
  echo "release-color.sh: refused unsafe or inconsistent claim cleanup" >&2
  exit 2
fi
