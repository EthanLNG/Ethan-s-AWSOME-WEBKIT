# webkit/SETUP.md: per-session startup

Run these steps at the start of **every** working session, in order. They are
harness-agnostic: any agent (Claude Code, Codex, and others) that can run shell
commands can follow them literally. All paths are relative to the project root;
run the commands from there.

This workflow requires Git 2.30 or newer. Stop before claiming a session if
`git --version` reports an older release.

Why a ritual at all: several agents may be working on this machine (even this
project) at once. The color you claim is your identity for the whole session.
It names your lock, your server port, your tab emoji, and your feedback inbox.
Every step below hangs off it.

## 1. Reuse or claim one session

```sh
set -eu

session_info="$(webkit/scripts/claim-color.sh --session)" || exit 1
case "$session_info" in *$'\n'*|*$'\r'*) exit 1 ;; esac
IFS=' ' read -r session_state color slug port server_pid server_instance unexpected \
  <<< "$session_info" || exit 1
test -n "$color" && test -n "$slug" && test -n "$port" || exit 1
test -z "${unexpected:-}" || exit 1
case "$session_state" in
  active)
    test -n "$server_pid" && test -n "$server_instance" || exit 1
    case "$server_pid" in ''|*[!0-9]*) exit 1 ;; esac
    test "$server_pid" -gt 0 || exit 1
    reused_server=1
    ;;
  claimed)
    test -z "${server_pid:-}" && test -z "${server_instance:-}" || exit 1
    reused_server=0
    ;;
  *) exit 1 ;;
esac
case "$slug" in
  [A-Za-z0-9]*) ;;
  *) exit 1 ;;
esac
case "$slug" in *[!A-Za-z0-9_-]*|'') exit 1 ;; esac
test "${#slug}" -le 64 || exit 1
case "$port" in ''|*[!0-9]*) exit 1 ;; esac
test "$port" -ge 1 && test "$port" -le 65535 || exit 1
feedback_dir="$(webkit/scripts/config-get.sh get feedback_dir)" || exit 1
case "$feedback_dir" in ''|'.'|-*|/*|*$'\n'*|*$'\r'*) exit 1 ;; esac
case "/$feedback_dir/" in */../*|*/./*) exit 1 ;; esac
inbox="$feedback_dir/$slug"
```

- The tagged command first looks for one live session owned by this exact Git
  worktree. It validates the color-lock owner, linked global port reservation,
  heartbeat, and private server instance identity. A verified match is reused,
  with its exact color and port. Otherwise the command atomically claims both a
  project color and its same-user global TCP port reservation.
- Keep `$color`, `$slug`, and `$port` for the whole session. Never infer them
  from memory, old notes, tab titles, or a mutable config file.
- Keep `$feedback_dir` and `$inbox` too. `feedback_dir` must be a dedicated
  repository-relative directory, never `.` or another path that contains
  project source. The preview server rejects an unsafe value.
- A fresh claim creates a linked random reservation token in the private color
  and port registries. The preview server refuses to serve without both records
  and heartbeats both only while it still owns them.
- If it prints nothing and exits 1 (`NONE` on stderr): the whole palette is in
  use. **Ask the user** what to do. Do not steal a lock or invent a
  sixth color.
- If it exits 2 because a same-owner claim is starting, inconsistent, or not
  safely identifiable, stop. Do not claim a second color. Inspect the existing
  process and use the guarded release command only after it is known dead.

## Private runtime preflight

Before starting a preview, verify that neither private runtime root is tracked
or staged and that both are ignored:

```sh
set -eu

test "$feedback_dir" != "."
tracked_runtime="$(git ls-files -- \
  ":(top,literal).webkit" \
  ":(top,literal)$feedback_dir")" || exit 1
test -z "$tracked_runtime" || exit 1
git check-ignore --quiet --no-index -- .webkit/.awesome-webkit-ignore-probe || exit 1
git check-ignore --quiet --no-index -- \
  "$feedback_dir/.awesome-webkit-ignore-probe" || exit 1
```

If any check fails, stop before launching. Add the exact dedicated runtime root
to Git ignore rules and remove any runtime path from the index without deleting
the local files. Commit only the configuration and ignore-rule correction.
Never use `git add -f` for `.webkit/`, `$feedback_dir`, an inbox, a voice note,
a chat attachment, or a transition receipt. The Control Center performs the
same preflight, excludes both roots from its staging, and rejects integration
if a provider-created commit contains private runtime data.

To migrate a custom `feedback_dir`, first finish or discard every affected
Control Center session, stop the direct preview, and confirm that no transition
helper is still running. Choose a new dedicated repository-relative directory,
add its ignore rule, and verify both checks above before changing the config.
While the server is stopped, move the complete color-inbox directories as a
unit if you need to preserve live rounds and receipts. Commit only
`webkit/webkit.config.json` and the ignore rule. Keep the old root ignored until
its archived runtime data has been moved or intentionally removed, then restart
and claim a fresh session. Never migrate a feedback root during an active round.

## 2. Start the preview server

If `reused_server=1`, do not launch another process. Keep `server_pid` and
`server_instance` for guarded shutdown. If `reused_server=0`, launch exactly
one persistent process, write its log outside the tracked project, and capture
the authoritative child PID:

```sh
set -eu

umask 077
wk_runtime_dir=$(mktemp -d "${TMPDIR:-/tmp}/awesome-webkit-${port}.XXXXXX") || exit 1
server_log="$wk_runtime_dir/preview.log"
nohup python3 webkit/server/preview-server.py "$color" \
  </dev/null >"$server_log" 2>&1 &
server_pid=$!

wk_stop_unverified_server() {
  kill "$server_pid" 2>/dev/null || true
  wait "$server_pid" 2>/dev/null || true
  sed -n '1,160p' "$server_log" >&2
  printf '%s\n' "Stop: the new server identity could not be verified; its claim remains held." >&2
  exit 1
}

wk_attempt=0
active_info=""
while [ "$wk_attempt" -lt 80 ]; do
  active_status=0
  active_info="$(webkit/scripts/claim-color.sh --active --full 2>/dev/null)" || active_status=$?
  case "$active_status" in
    0) break ;;
    1) ;;
    *) wk_stop_unverified_server ;;
  esac
  if ! kill -0 "$server_pid" 2>/dev/null; then
    sed -n '1,160p' "$server_log" >&2
    webkit/scripts/release-color.sh --slug "$slug" || exit 1
    exit 1
  fi
  wk_attempt=$((wk_attempt + 1))
  sleep 0.1
done
if [ -z "$active_info" ]; then
  kill "$server_pid" 2>/dev/null || true
  wait "$server_pid" 2>/dev/null || true
  sed -n '1,160p' "$server_log" >&2
  webkit/scripts/release-color.sh --slug "$slug" || exit 1
  exit 1
fi
IFS=' ' read -r active_color active_slug active_port active_pid server_instance unexpected \
  <<< "$active_info" || wk_stop_unverified_server
case "$active_info" in *$'\n'*|*$'\r'*) wk_stop_unverified_server ;; esac
test -n "$active_color" && test -n "$active_slug" && test -n "$active_port" && \
  test -n "$active_pid" && test -n "$server_instance" || wk_stop_unverified_server
test -z "${unexpected:-}" || wk_stop_unverified_server
case "$active_port:$active_pid" in *[!0-9:]*) wk_stop_unverified_server ;; esac
test "$active_color" = "$color" && test "$active_slug" = "$slug" && \
  test "$active_port" = "$port" || wk_stop_unverified_server
test "$active_pid" = "$server_pid" || wk_stop_unverified_server
```

- `--active --full` prints data only after checking the exact loopback server's
  private instance header against the linked reservation. A PID alone is never
  accepted as server identity.
- Keep `server_pid` and `server_instance` in the live task state. Do not write
  them into tracked project files. The private temporary directory contains
  only this session's log and can be removed after shutdown.
- The server verifies your claim at boot (the lock exists and its owner is the
  worktree's Git root, even when `site_root` serves a subdirectory). If it
  refuses, do not force it. Read the private log, release only your exact claim,
  and return to step 1.
- After binding succeeds, the server publishes a private, server-specific
  transition capability in the color inbox. The round-transition helper uses
  it automatically. Never copy that token into HTML, logs, chat, or a manual
  request, and never use the browser mutation token for archive transitions.
- It serves content only from the config's `site_root` and binds **127.0.0.1
  by default**. Reach the normal local preview as
  `http://localhost:<port>/...`, not by machine IP. `bind_host` accepts only
  `127.0.0.1`, `localhost`, or `0.0.0.0`. Deliberate LAN use requires
  `"bind_host": "0.0.0.0"` and a non-empty `allowed_hosts` list, for example
  `"bind_host": "0.0.0.0", "allowed_hosts": ["192.168.1.20", "devbox.local"]`.
  Each entry is an exact hostname or IPv4 address with no scheme, port, path,
  wildcard, or IPv6 literal. Loopback names and addresses remain accepted. The
  server adds the active palette port when checking the HTTP Host header.
- `allowed_hosts` is a DNS-rebinding defense, not authentication and not a
  source-IP allowlist. A client that can reach the port can send an allowed Host
  value, load the page, and obtain the same-origin preview mutation capability.
  Use LAN mode only for reachable clients you trust, behind a host firewall on
  a trusted private network or authenticated VPN. Do not use public Wi-Fi,
  router port forwarding, public tunnels, or internet exposure.
- The runtime assumes that processes under the same operating-system account,
  including the coding agent, are trusted. They can read the worktree and
  private runtime paths and invoke local endpoints. Tokens and path validation
  are protocol defenses, not isolation from a hostile same-account process.
- The preview never serves hidden path components, common credential or private
  key filenames, `webkit.config.json`, or Webkit runtime files. Directory
  listings are disabled. Place only browser-public assets under `site_root` and
  use an index file for every directory that should be navigable.
- For an HTML `<meta http-equiv="Content-Security-Policy">`, the server creates
  a fresh response nonce and Trusted Types policy name, then adapts the preview
  policy to authorize the injected overlay, its dynamic styles, same-origin
  Webkit requests, and before-snapshot frame. Project source is unchanged. A
  CSP meta tag that cannot be parsed and reconstructed safely returns HTTP 422
  with JSON only, before token-bearing HTML is served. CSP added by an upstream
  HTTP response header is outside this static HTML transform and must permit the
  overlay separately. Passing this local preview does not validate production
  CSP headers and is not a security audit.
- `api_proxy_origin` is a developer-only privileged bridge. It forwards the
  origin-scoped `X-WKCC-Token` session header held by the Control Center UI,
  never cookies, and is disabled unless the preview process is
  deliberately launched with the exact opt-in `WK_ENABLE_API_PROXY=1`. Never
  enable it for an untrusted project config. The origin must resolve only to
  loopback, upstream redirects are rejected, and request and response bodies
  are bounded.
- **ONE server per session.** Never run the legacy project server and the
  webkit server simultaneously with the same emoji. Two servers stamping one
  color makes the tab strip (and the lock's meaning) a lie.

## 3. Open the preview

```sh
set -eu

default_page="$(webkit/scripts/config-get.sh get default_page)" || exit 1
case "$default_page" in ''|/*|*$'\n'*|*$'\r'*) exit 1 ;; esac
case "/$default_page/" in */../*|*/./*) exit 1 ;; esac
webkit/scripts/open-preview.sh \
  "http://localhost:${port}/${default_page}" || exit 1
```

- **This script owns browser selection.** Run it as a shell command even when
  your agent harness offers a generic Browser tool, an in-app browser, a
  preview pane, or a webview. Do not open a second copy there. The configured
  `browser.app_name` is the user's external desktop browser; with the default
  config it is the actual Google Chrome application.
- `<port>` is your color's port, `<default_page>` from the config.
- On macOS + Chrome this closes any stale tab on the same `host:port` first,
  then opens and focuses a fresh one: one tab per design, always current.
  Elsewhere it prints the URL; relay it to the user so they open it themselves.
- Reuse this script (never a bare `open`/new tab) every time you reopen the
  preview during the session. That is what keeps the tab strip at one tab per
  agent.

## 4. Start the feedback watcher

The session is now live for the user; your job is to wait for their first
feedback batch. Go to `webkit/LOOP.md` and run it from **step 1** (the wait).
Re-read that file at the top of every round. It says so itself.

**Codex:** opening the preview is not the end of the turn. Keep the current
turn alive while the waiter runs and follow the Codex polling instructions in
`LOOP.md`. A waiter left in a detached/background terminal after a final reply
can detect the file, but it cannot wake a Codex task whose turn has already
ended.

## End of session

When the user ends the session (e.g. asks you to merge / says you're done):

1. Revalidate the exact server immediately before signaling it:

   ```sh
   set -eu

   shutdown_info="$(webkit/scripts/claim-color.sh --active --full)" || exit 1
   case "$shutdown_info" in *$'\n'*|*$'\r'*) exit 1 ;; esac
   IFS=' ' read -r shutdown_color shutdown_slug shutdown_port shutdown_pid shutdown_instance unexpected \
     <<< "$shutdown_info" || exit 1
   test -n "$shutdown_color" && test -n "$shutdown_slug" && \
     test -n "$shutdown_port" && test -n "$shutdown_pid" && \
     test -n "$shutdown_instance" || exit 1
   test -z "${unexpected:-}" || exit 1
   case "$shutdown_port:$shutdown_pid" in *[!0-9:]*) exit 1 ;; esac
   test "$shutdown_color" = "$color" && test "$shutdown_slug" = "$slug" && \
     test "$shutdown_port" = "$port" || exit 1
   test "$shutdown_pid" = "$server_pid" && \
     test "$shutdown_instance" = "$server_instance" || exit 1
   ```

   If any check fails, do not signal the PID and do not release the claim. Stop
   for user direction because the process identity changed.
2. Stop that validated PID and confirm it exits:

   ```sh
   set -eu

   kill "$server_pid"
   wk_attempt=0
   while kill -0 "$server_pid" 2>/dev/null && [ "$wk_attempt" -lt 80 ]; do
     wk_attempt=$((wk_attempt + 1))
     sleep 0.1
   done
   wait "$server_pid" 2>/dev/null || true
   if kill -0 "$server_pid" 2>/dev/null; then
     printf '%s\n' "Preview server did not stop; claim remains held." >&2
     exit 1
   fi
   ```

3. Release both linked records by the stored slug:
   `webkit/scripts/release-color.sh --slug "$slug"`. Release finds the original
   reservation by its random token even if the config's port changed. It never
   deletes a newer token or another owner.
4. Remove the private runtime directory if this invocation created one:
   `test -z "${wk_runtime_dir:-}" || { rm -f -- "$server_log" && rmdir "$wk_runtime_dir"; }`,
   or leave it for the user if the log is needed for diagnosis.
5. Close your preview tab (on macOS+Chrome; otherwise tell the user the URL is
   dead so they close it). Your emoji leaving the tab strip is the signal that
   the color is truly free for the next agent.
