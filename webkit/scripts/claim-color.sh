#!/bin/bash
# claim-color.sh: atomically claim one color and its machine-local TCP port.
#
# Project color locks identify the worktree. A private same-user global port
# registry prevents separate projects or Control Center instances from both
# claiming the same configured port before either preview process binds it.
# Both records carry one random reservation token and are released together.
#
# Existing one-file legacy locks remain blocking and can still be released,
# but every new claim uses the linked color and port reservation protocol.
#
# Usage:
#   color=$(webkit/scripts/claim-color.sh)
#   webkit/scripts/claim-color.sh --full
#   webkit/scripts/claim-color.sh --slug blue
#   webkit/scripts/claim-color.sh --active --full
#
# A normal claim first discovers a verified live session owned by this exact
# worktree. If one exists, it is reused and no second color is claimed.
# --active performs only that discovery and prints:
#   emoji slug port pid instance
# The server identity comes from the private reservation and is checked against
# the response header on that exact loopback port before anything is printed.

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
registry="$script_dir/runtime_registry.py"

full=0
active_only=0
requested_slug=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --full)
      full=1
      shift
      ;;
    --active)
      active_only=1
      shift
      ;;
    --slug)
      [ "$#" -ge 2 ] || { echo "claim-color.sh: --slug needs a palette slug" >&2; exit 2; }
      requested_slug="$2"
      shift 2
      ;;
    *)
      echo "usage: claim-color.sh [--full] [--active] [--slug <palette-slug>]" >&2
      exit 2
      ;;
  esac
done

palette="$("$script_dir/config-get.sh" palette)"
lockdir="${WK_COLOR_LOCKDIR:-$("$script_dir/config-get.sh" get lock_dir)}"
grace="$("$script_dir/config-get.sh" get grace_seconds)"
me="${WK_COLOR_OWNER:-$(pwd -P)}"

palette_json="$(printf '%s\n' "$palette" | python3 -c '
import json, sys
items = []
for raw in sys.stdin:
    raw = raw.rstrip("\n")
    if not raw:
        continue
    parts = raw.split(" ")
    if len(parts) != 3:
        raise SystemExit("claim-color.sh: palette output was malformed")
    items.append({"slug": parts[0], "emoji": parts[1], "port": int(parts[2])})
print(json.dumps(items, ensure_ascii=False, separators=(",", ":")))
')"

active=""
if active="$(python3 "$registry" discover \
  --lock-dir "$lockdir" --owner "$me" --palette "$palette_json" --grace "$grace")"; then
  IFS="$(printf '\t')" read -r active_emoji active_slug active_port active_pid active_instance <<< "$active"
  if [ -n "$requested_slug" ] && [ "$requested_slug" != "$active_slug" ]; then
    echo "claim-color.sh: requested '$requested_slug', but this worktree already owns the verified active '$active_slug' session; reuse it or stop and release it first" >&2
    exit 2
  fi
  if [ "$active_only" -eq 1 ]; then
    if [ "$full" -eq 1 ]; then
      printf '%s %s %s %s %s\n' \
        "$active_emoji" "$active_slug" "$active_port" "$active_pid" "$active_instance"
    else
      printf '%s\n' "$active_emoji"
    fi
  elif [ "$full" -eq 1 ]; then
    printf '%s %s %s\n' "$active_emoji" "$active_slug" "$active_port"
  else
    printf '%s\n' "$active_emoji"
  fi
  if [ "$active_only" -eq 0 ]; then
    echo "claim-color.sh: reusing verified active $active_emoji session on port $active_port" >&2
  fi
  exit 0
else
  discover_status=$?
  if [ "$discover_status" -ne 1 ]; then
    echo "claim-color.sh: an existing same-owner claim could not be safely reused; stop or release it before claiming another color" >&2
    exit 2
  fi
fi

if [ "$active_only" -eq 1 ]; then
  exit 1
fi

# Browser tab titles remain a compatibility signal for older manual sessions.
# Failure to scan is not interpreted as an empty browser.
mode="$("$script_dir/config-get.sh" get browser.mode 2>/dev/null || echo auto)"
case "$mode" in
  applescript|print) : ;;
  *)
    if [ "$(uname -s)" = "Darwin" ] && command -v osascript >/dev/null 2>&1; then
      mode=applescript
    else
      mode=print
    fi
    ;;
esac

tabs=""
if [ -n "${WK_COLOR_TABS+x}" ]; then
  tabs="$WK_COLOR_TABS"
elif [ "$mode" = "applescript" ]; then
  app_name="$("$script_dir/config-get.sh" get browser.app_name)"
  app_esc="$(python3 -c '
import sys, unicodedata
name = sys.argv[1]
if not name or any(unicodedata.category(ch) == "Cc" or ch in "\u2028\u2029" for ch in name):
    raise SystemExit("claim-color.sh: browser.app_name must contain no control or newline characters")
print(name.replace("\\", "\\\\").replace("\"", "\\\""))
' "$app_name")"
  if ! tabs="$(osascript -e "if application \"$app_esc\" is running then
  tell application \"$app_esc\" to get title of every tab of every window
else
  return \"__WK_NOT_RUNNING__\"
end if" 2>/dev/null)"; then
    tabs=""
  fi
  case "$tabs" in *__WK_NOT_RUNNING__*) tabs="" ;; esac
fi

configured_slug=0
while IFS=' ' read -r slug emoji port; do
  [ -n "$slug" ] || continue
  if [ -n "$requested_slug" ] && [ "$slug" != "$requested_slug" ]; then
    continue
  fi
  configured_slug=1
  case "$tabs" in *"$emoji"*) continue ;; esac

  if python3 "$registry" claim-color \
    --lock-dir "$lockdir" --color "$slug" --port "$port" \
    --owner "$me" --grace "$grace" >/dev/null; then
    if [ "$full" -eq 1 ]; then
      printf '%s %s %s\n' "$emoji" "$slug" "$port"
    else
      printf '%s\n' "$emoji"
    fi
    exit 0
  else
    claim_status=$?
    if [ "$claim_status" -ne 3 ]; then
      echo "claim-color.sh: failed to create a safe linked color and port claim" >&2
      exit 2
    fi
  fi
done <<< "$palette"

if [ -n "$requested_slug" ] && [ "$configured_slug" -eq 0 ]; then
  echo "claim-color.sh: '$requested_slug' is not a configured palette slug" >&2
  exit 2
fi

echo NONE >&2
exit 1
