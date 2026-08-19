#!/bin/bash
# open-preview.sh - (re)open a preview URL in the user's configured external
# desktop browser, without piling up tabs. Agent-hosted in-app browsers,
# preview panes, and webviews are deliberately not launch targets.
#
# Why: several agents iterate on different designs at once, each serving on
# its own port. If every reload opened a NEW tab the strip would silt up in
# minutes - so the AppleScript path first closes any existing tab whose URL
# contains the same host:port (each agent/design has its own port, so only
# THIS preview's stale tab is closed; other agents' tabs on other ports are
# left alone), then opens a fresh tab and brings the browser forward.
#
# Dispatch on browser.mode from webkit.config.json (via config-get.sh):
#   "applescript" - drive browser.app_name via osascript. The app must speak
#                   the Chromium AppleScript dictionary (Google Chrome, Brave,
#                   Edge, Vivaldi…) - its name is interpolated into the script
#                   because a dynamic `tell application (item 1 of argv)`
#                   cannot compile the Chromium-only `tab` terminology.
#   "print"       - just print the URL for the user to open. Always exits 0:
#                   this branch must never fail an agent loop that calls it
#                   unconditionally after every change.
#   "auto"        - applescript on macOS with osascript available, else print.
#
# Usage: webkit/scripts/open-preview.sh <url>
#   e.g. webkit/scripts/open-preview.sh "http://localhost:5311/index.html?jump=hero"
#
# Env overrides:
#   WK_CONFIG   alternate config file (resolved by config-get.sh)

set -euo pipefail

url="${1:?usage: open-preview.sh <url>}"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

# Normalize and validate the URL before either launch path uses it. The
# normalized origin is also the exact stale-tab key, which prevents port 5311
# from matching port 53110 or a foreign page whose path contains that text.
url_info="$(python3 -c '
import sys
from urllib.parse import urlsplit, urlunsplit

raw = sys.argv[1]
if any(ord(ch) < 33 or ord(ch) == 127 for ch in raw):
    raise SystemExit("open-preview.sh: URL must not contain whitespace or control characters")
if "://" not in raw:
    raw = "http://" + raw
try:
    parsed = urlsplit(raw)
    port = parsed.port
except ValueError as exc:
    raise SystemExit("open-preview.sh: invalid URL: %s" % exc)
if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
    raise SystemExit("open-preview.sh: URL must use http or https and include a host")
if parsed.username is not None or parsed.password is not None:
    raise SystemExit("open-preview.sh: URL must not contain credentials")
scheme = parsed.scheme.lower()
if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
    port = None
host = parsed.hostname.lower()
if ":" not in host:
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        raise SystemExit("open-preview.sh: URL contains an invalid hostname")
else:
    host = "[" + host + "]"
netloc = host + ((":" + str(port)) if port is not None else "")
normalized = urlunsplit((scheme, netloc, parsed.path, parsed.query, parsed.fragment))
print(normalized + "\t" + scheme + "://" + netloc)
' "$url")" || exit $?
IFS=$'\t' read -r url match_origin <<< "$url_info"

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

app_name="$("$script_dir/config-get.sh" get browser.app_name)"

# The heredoc is unquoted so $app_esc interpolates (escaped for AppleScript's
# double-quoted string). The URL and match key still travel via argv - never
# interpolated - so no page URL can break out of the script.
app_esc="$(python3 -c '
import sys, unicodedata
name = sys.argv[1]
if not name or any(unicodedata.category(ch) == "Cc" or ch in "\u2028\u2029" for ch in name):
    raise SystemExit("open-preview.sh: browser.app_name must be nonempty and contain no control or newline characters")
print(name.replace("\\", "\\\\").replace("\"", "\\\""))
' "$app_name")"

osascript - "$url" "$match_origin" <<APPLESCRIPT
on run argv
  set theURL to item 1 of argv
  set matchOrigin to item 2 of argv
  tell application "$app_esc"
    -- Close any existing tab pointing at the same origin (back-to-front,
    -- across all windows, so indices stay valid as tabs close).
    try
      repeat with w in windows
        set t to count of tabs of w
        repeat with i from t to 1 by -1
          try
            set tabURL to URL of tab i of w
            if tabURL is matchOrigin or tabURL starts with (matchOrigin & "/") or tabURL starts with (matchOrigin & "?") or tabURL starts with (matchOrigin & "#") then
              close tab i of w
            end if
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
