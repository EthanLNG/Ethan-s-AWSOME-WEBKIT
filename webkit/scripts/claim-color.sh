#!/bin/bash
# claim-color.sh — atomically claim ONE agent color for this session.
#
# Fixes the multi-agent race: two agents that start within a few seconds both
# scan the browser tab strip, see the same colors free, and pick the same one —
# because a claim was not *visible* until the tab got labeled, and both agents
# fell into that gap.
#
# The registry is an atomic lock dir (machine-global, shared by every
# worktree/session), not just the tab titles. `mkdir` is atomic: even in a
# dead heat exactly one agent can create a given color's lock; the loser's
# mkdir fails and it moves to the next color. On macOS, open browser tab
# titles are still honored as "reserved" (backward-compat with any agent on a
# manual flow, plus a belt-and-suspenders signal).
#
# The lock protocol is byte-compatible with the legacy RealClick scripts —
# a claim is `mkdir <lock_dir>/<slug>.lock`, ownership is one line (`pwd -P`)
# in `<lock>/owner` — so this script interoperates with locks claimed by the
# older scripts when webkit.config.json points lock_dir at the same registry.
#
# Palette (claim order), lock_dir and grace_seconds come from
# webkit.config.json via config-get.sh, so the same script serves any project.
#
# Usage:
#   color=$(webkit/scripts/claim-color.sh)    # prints the emoji, e.g. 🟢
#   webkit/scripts/claim-color.sh --full      # prints "emoji slug port"
#                                             # (port = this color's preview port)
# Release (session end): webkit/scripts/release-color.sh "$color"
#
# Env overrides:
#   WK_CONFIG         alternate config file (resolved by config-get.sh)
#   WK_COLOR_LOCKDIR  lock registry dir (else config lock_dir)
#   WK_COLOR_OWNER    owner string recorded in the lock (else `pwd -P`)
#   WK_COLOR_TABS     pre-computed tab-title snapshot (testing / other harness)
#
# Tab scan & stale-lock reclaim — deliberately conservative, because stealing a
# live agent's color is the exact two-agents-on-one-color collision this
# registry exists to prevent:
#   * A "tab signal" exists ONLY when a scan SUCCEEDS against a RUNNING browser
#     (mode applescript), or WK_COLOR_TABS is set. A FAILED scan (Automation
#     permission denied, browser.app_name wrong / not installed) or a browser
#     that isn't running is NOT a tab signal — it says nothing about which
#     colors live agents hold, so it must not be read as an empty snapshot.
#   * Your OWN aged lock (owner == this worktree) is always reclaimable, even
#     with no tab signal: one worktree is one session, so a crash-looping
#     worktree can never permanently exhaust the palette. This is reclaim, not
#     steal.
#   * Someone ELSE's lock is reclaimed only with a real tab signal AND no
#     matching tab AND older than grace_seconds AND its owner worktree no longer
#     exists on disk. A live tabless session (print mode) or one whose preview
#     tab the user merely closed keeps its color.
#   * lock mtime is set at claim (and, ideally, refreshed by the running preview
#     server as a heartbeat — see preview-server.py) so a live session never
#     ages past grace.
# WITHOUT a tab signal, others' locks are honored regardless of age — clean a
# genuinely dead session's lock with `WK_COLOR_FORCE=1 release-color.sh`.
#
# Prints the claimed emoji on stdout, exit 0. If the whole palette is taken,
# prints nothing to stdout, "NONE" to stderr, exit 1. A lock-registry problem
# (relative lock_dir, or a registry this user cannot write) exits 2 — that is a
# config/permissions fault, NOT a full palette.

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

full=0
if [ "${1:-}" = "--full" ]; then
  full=1
  shift
fi
if [ "$#" -gt 0 ]; then
  echo "usage: claim-color.sh [--full]" >&2
  exit 1
fi

# Config (palette read must fail loudly; grace falls back to the classic 180s
# so a config that predates the key still works).
palette="$("$script_dir/config-get.sh" palette)"
lockdir="${WK_COLOR_LOCKDIR:-$("$script_dir/config-get.sh" get lock_dir)}"
grace="$("$script_dir/config-get.sh" get grace_seconds 2>/dev/null || echo 180)"
case "$grace" in ''|*[!0-9]*) grace=180 ;; esac

# lock_dir MUST be absolute. A relative path resolves against each worktree's
# own cwd, so two worktrees each get a PRIVATE registry, both claim the same
# color, and every owner check passes — silently voiding the cross-agent mutual
# exclusion (in print mode there is no tab-scan backstop at all). Refuse rather
# than absolutize: there is no single correct anchor, and rewriting it could
# diverge from what the interoperating legacy scripts compute for the same key.
case "$lockdir" in
  /*) : ;;
  *)
    echo "claim-color.sh: lock_dir must be an absolute path (got '$lockdir') — a relative path resolves per-worktree and voids cross-agent mutual exclusion; use e.g. /tmp/<project>-agent-colors in webkit.config.json" >&2
    exit 2
    ;;
esac

mkdir -p "$lockdir"
# Make the shared registry cross-user usable (world-writable + sticky, like
# /tmp) so a second Unix user on a shared box can claim too; the sticky bit
# still blocks cross-user stale-lock reclaim, consistent with never-steal. chmod
# is a harmless no-op when we don't own the dir, so verify writability
# explicitly: an EACCES registry must fail LOUDLY here (exit 2) rather than be
# misread later as "every mkdir lost the race" => false "palette full" (NONE).
chmod 1777 "$lockdir" 2>/dev/null || true
if [ ! -w "$lockdir" ] || [ ! -x "$lockdir" ]; then
  echo "claim-color.sh: lock registry $lockdir is not writable by $(id -un) — fix its permissions or point lock_dir / WK_COLOR_LOCKDIR at a writable registry (exit 2 = registry problem, not a full palette)" >&2
  exit 2
fi

# Resolve browser mode: explicit "applescript"/"print" honored; "auto" (or
# anything unrecognized) means applescript only on macOS with osascript.
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

# One snapshot of the tab strip. have_tabs records whether a real tab SIGNAL
# exists at all — it gates the stale-lock reclaim below, not just the "reserved"
# check. A FAILED scan or a not-running browser is NOT a signal (have_tabs=0):
# it says nothing about live agents, and reading it as an empty snapshot would
# let us steal an aged-but-live lock.
have_tabs=0
tabs=""
if [ -n "${WK_COLOR_TABS+x}" ]; then
  tabs="$WK_COLOR_TABS"
  have_tabs=1
elif [ "$mode" = "applescript" ]; then
  app_name="$("$script_dir/config-get.sh" get browser.app_name 2>/dev/null || echo "Google Chrome")"
  # Interpolate the app name into the script (escaped) — a dynamic
  # `tell application (item 1 of argv)` would fail to compile the
  # Chromium-only `tab` terminology.
  app_esc="${app_name//\\/\\\\}"; app_esc="${app_esc//\"/\\\"}"
  # Guard the scan with `is running` so a QUIT browser returns a sentinel
  # (mapped to have_tabs=0) instead of an authoritative empty snapshot — and so
  # we never RELAUNCH the browser with zero tabs just to scan it. A non-zero
  # osascript exit (Automation denied, app_name wrong / not installed) also
  # drops to have_tabs=0. Only a clean scan of a running browser is a signal.
  if tabs="$(osascript -e "if application \"$app_esc\" is running then
  tell application \"$app_esc\" to get title of every tab of every window
else
  return \"__WK_NOT_RUNNING__\"
end if" 2>/dev/null)"; then
    case "$tabs" in
      *__WK_NOT_RUNNING__*) tabs=""; have_tabs=0 ;;
      *)                    have_tabs=1 ;;
    esac
  else
    tabs=""; have_tabs=0
  fi
fi

now="$(date +%s)"

while IFS=' ' read -r slug emoji port; do
  [ -n "$slug" ] || continue
  lock="$lockdir/$slug.lock"

  # 1. A live tab already shows this color -> reserved, skip.
  case "$tabs" in *"$emoji"*) continue ;; esac

  # 2. A lock exists: decide whether to honor or reclaim it (see the header for
  #    the full policy). Reclaim only when it is safe: our own aged lock, or an
  #    aged tab-less lock whose owner worktree is gone. Never on an empty/failed
  #    scan, never someone else's live-looking lock.
  if [ -d "$lock" ]; then
    # python3 os.stat: portable mtime (BSD `stat -f %m` vs GNU `stat -c %Y`).
    mtime="$(python3 -c 'import os,sys; print(int(os.stat(sys.argv[1]).st_mtime))' "$lock" 2>/dev/null || echo "$now")"
    owner="$(cat "$lock/owner" 2>/dev/null || true)"
    me="${WK_COLOR_OWNER:-$(pwd -P)}"

    reclaim=0
    if [ "$(( now - mtime ))" -le "$grace" ]; then
      :                                       # freshly held — an agent mid-claim
    elif [ -n "$owner" ] && [ "$owner" = "$me" ]; then
      reclaim=1                               # our OWN aged lock — retake, not steal
    elif [ "$have_tabs" -eq 1 ]; then
      # Aged + a real tab signal + no matching tab (step 1 already skipped a
      # match). Steal only if the owner worktree is GONE — a worktree still on
      # disk is likely a live session whose preview tab was merely closed.
      if [ -z "$owner" ] || [ ! -d "$owner" ]; then
        reclaim=1
      fi
    fi
    # else: no tab signal and not our own — honor regardless of age.

    [ "$reclaim" -eq 1 ] || continue

    # Reclaim ATOMICALLY: rename the stale lock aside before deleting it, so two
    # racing reapers can't both "succeed" and then clobber each other's fresh
    # lock (rm-then-mkdir is not atomic as a unit). rename(2) within the lockdir
    # is atomic — exactly one reaper wins the mv; the loser gets ENOENT and
    # moves on, its color already being taken by the winner.
    echo "claim-color.sh: reclaiming stale $emoji lock $lock (owner='${owner:-?}', age $(( now - mtime ))s > ${grace}s)" >&2
    if mv "$lock" "$lock.reaped.$$" 2>/dev/null; then
      rm -rf "$lock.reaped.$$" 2>/dev/null || true
    else
      continue                                # another reaper won the rename — move on
    fi
  fi

  # 3. Atomic claim. Win -> record owner & print. Lose the mkdir race -> next color.
  if mkdir "$lock" 2>/dev/null; then
    # Ownership record: the preview server refuses to stamp an emoji whose
    # lock this worktree doesn't own, and release-color.sh refuses to drop
    # someone else's lock. Born of a real two-agents-on-one-color collision:
    # an agent SKIPPED this script (assumed its color from a prior session's
    # notes) and stamped an emoji another agent held — a lock can only
    # protect claims that go through it, so the stamping point demands proof.
    printf '%s\n' "${WK_COLOR_OWNER:-$(pwd -P)}" > "$lock/owner" 2>/dev/null || true
    if [ "$full" -eq 1 ]; then
      printf '%s %s %s\n' "$emoji" "$slug" "$port"
    else
      printf '%s\n' "$emoji"
    fi
    exit 0
  elif [ ! -d "$lock" ]; then
    # mkdir failed but the lock still doesn't exist — this is NOT a lost race
    # (EEXIST), it's a real error (permissions etc.). Fail loudly rather than
    # skip the color and later report a false "palette full".
    echo "claim-color.sh: could not create $lock and it does not exist — not a lost race; check permissions on $lockdir (exit 2 = registry problem)" >&2
    exit 2
  fi
  # else: lost the mkdir race (lock now exists) — try the next color.
done <<< "$palette"

echo NONE >&2
exit 1
