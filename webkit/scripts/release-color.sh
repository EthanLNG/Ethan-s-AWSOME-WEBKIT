#!/bin/bash
# release-color.sh — free an agent color when a session ends ("merge to main").
#
# Removes the atomic lock created by claim-color.sh so the next agent can
# reclaim the color. Closing your preview tab + stopping your preview server
# is still done separately; this just drops the lock.
#
# Ownership guard: the lock records the claiming worktree in <lock>/owner;
# releasing a color someone else holds is refused so one agent's cleanup can't
# free another agent's color mid-session. Override (human cleanup of a dead
# session's lock) with WK_COLOR_FORCE=1. Same owner-file format as the legacy
# RealClick scripts, so this releases legacy-claimed locks too when lock_dir
# points at the shared registry.
#
# Palette and lock_dir come from webkit.config.json via config-get.sh.
#
# Usage:  webkit/scripts/release-color.sh <emoji>    # e.g. release-color.sh 🟢
#
# Env overrides:
#   WK_CONFIG         alternate config file (resolved by config-get.sh)
#   WK_COLOR_LOCKDIR  lock registry dir (else config lock_dir)
#   WK_COLOR_OWNER    identity to compare against the lock owner (else `pwd -P`)
#   WK_COLOR_FORCE=1  release even if the lock belongs to someone else

set -euo pipefail

emoji="${1:?usage: release-color.sh <emoji>}"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

# Unknown emoji / broken config -> config-get.sh already printed why; exit 1.
slug_port="$("$script_dir/config-get.sh" color "$emoji")"
slug="${slug_port%% *}"

lockdir="${WK_COLOR_LOCKDIR:-$("$script_dir/config-get.sh" get lock_dir)}"
lock="$lockdir/$slug.lock"

[ -d "$lock" ] || exit 0                    # nothing to release

owner="$(cat "$lock/owner" 2>/dev/null || true)"
me="${WK_COLOR_OWNER:-$(pwd -P)}"
if [ -n "$owner" ] && [ "$owner" != "$me" ] && [ -z "${WK_COLOR_FORCE:-}" ]; then
  echo "release-color.sh: $emoji is claimed by '$owner', not this worktree ('$me') — not releasing. Set WK_COLOR_FORCE=1 to override." >&2
  exit 1
fi

rm -rf "$lock"
