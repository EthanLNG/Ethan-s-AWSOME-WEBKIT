#!/bin/bash
# open-preview.sh — (re)open a preview URL in the user's configured external
# desktop browser, without piling up tabs. Agent-hosted in-app browsers,
# preview panes, and webviews are deliberately not launch targets.
#
# Why: several agents iterate on different designs at once, each serving on
# its own port. If every reload opened a NEW tab the strip would silt up in
# minutes — so the AppleScript path first closes any existing tab whose URL
# contains the same host:port (each agent/design has its own port, so only
# THIS preview's stale tab is closed; other agents' tabs on other ports are
# left alone), then opens a fresh tab and brings the browser forward.
#
# Dispatch on browser.mode from webkit.config.json (via config-get.sh):
#   "applescript" — drive browser.app_name via osascript. The app must speak
#                   the Chromium AppleScript dictionary (Google Chrome, Brave,
#                   Edge, Vivaldi…) — its name is interpolated into the script
#                   because a dynamic `tell application (item 1 of argv)`
#                   cannot compile the Chromium-only `tab` terminology.
#   "print"       — just print the URL for the user to open. Always exits 0:
#                   this branch must never fail an agent loop that calls it
#                   unconditionally after every change.
#   "auto"        — applescript on macOS with osascript available, else print.
#
# Usage: webkit/scripts/open-preview.sh <url>
#   e.g. webkit/scripts/open-preview.sh "http://localhost:5311/index.html?jump=hero"
#
# Env overrides:
#   WK_CONFIG   alternate config file (resolved by config-get.sh)

set -euo pipefail

url="${1:?usage: open-preview.sh <url>}"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

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

if [ "$mode" = "print" ]; then
  echo "Open in your browser: $url"
  exit 0
fi

app_name="$("$script_dir/config-get.sh" get browser.app_name 2>/dev/null || echo "Google Chrome")"

# Normalize scheme-less input (users type "localhost:5311/..."): default to
# http:// so both the extraction below and the Chrome open get a real scheme.
case "$url" in
  *://*) : ;;
  *) url="http://$url" ;;
esac
# Extract host:port (the part after the scheme, up to the first slash) —
# the stale-tab match key. Scheme match is case-insensitive so "HTTP://…"
# doesn't fall through and leave matchKey as the whole URL (which would only
# close an exact-path tab, defeating the one-tab-per-port purpose).
hostport="$(printf '%s' "$url" | sed -E 's#^[A-Za-z][A-Za-z0-9+.-]*://([^/]+).*#\1#')"

# The heredoc is unquoted so $app_esc interpolates (escaped for AppleScript's
# double-quoted string). The URL and match key still travel via argv — never
# interpolated — so no page URL can break out of the script.
app_esc="${app_name//\\/\\\\}"; app_esc="${app_esc//\"/\\\"}"

osascript - "$url" "$hostport" <<APPLESCRIPT
on run argv
  set theURL to item 1 of argv
  set matchKey to item 2 of argv
  tell application "$app_esc"
    -- Close any existing tab pointing at the same host:port (back-to-front,
    -- across all windows, so indices stay valid as tabs close).
    try
      repeat with w in windows
        set t to count of tabs of w
        repeat with i from t to 1 by -1
          try
            if (URL of tab i of w) contains matchKey then close tab i of w
          end try
        end repeat
      end repeat
    end try
    -- Open a fresh tab (new window if none) and bring the browser forward.
    if (count of windows) is 0 then
      make new window
      set URL of active tab of front window to theURL
    else
      tell front window to make new tab with properties {URL:theURL}
    end if
    activate
  end tell
end run
APPLESCRIPT
