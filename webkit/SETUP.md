# webkit/SETUP.md — per-session startup

Run these steps at the start of **every** working session, in order. They are
harness-agnostic: any agent (Claude Code, Codex, …) that can run shell commands
can follow them literally. All paths are relative to the project root; run the
commands from there.

Why a ritual at all: several agents may be working on this machine (even this
project) at once. The color you claim is your identity for the whole session —
it names your lock, your server port, your tab emoji, and your feedback inbox.
Every step below hangs off it.

## 1. Claim your color

```sh
color=$(webkit/scripts/claim-color.sh)
```

- Prints your emoji (e.g. `🟢`) on success. Keep `$color` for the whole session.
- **NEVER assume a color from memory, a previous session, or notes in your
  worktree — claim it fresh, every session.** The lock only protects claims
  that go through it; assuming a color is exactly how two agents once ended up
  stamping the same emoji. The server will refuse to serve an emoji whose lock
  your worktree doesn't own, so a shortcut here just fails later, louder.
- If it prints nothing and exits 1 (`NONE` on stderr): the whole palette is in
  use — **ask the user** what to do. Do not steal a lock, do not invent a
  sixth color.

## 2. Start the preview server

```sh
python3 webkit/server/preview-server.py "$color"
```

- The port is looked up automatically from your color's palette entry in
  `webkit/webkit.config.json` (helper, if you need the values yourself:
  `webkit/scripts/config-get.sh color "$color"` prints your palette entry;
  `webkit/scripts/config-get.sh get default_page` prints the default page).
- Run it in the background the way your harness supports (Claude Code: a
  background Bash task; Codex: `nohup … &` or its background idiom) — it must
  keep serving while you do everything else.
- The server verifies your claim at boot (lock exists **and** its owner is the
  directory being served). If it refuses, don't force it — go back to step 1
  and claim properly.
- It serves the config's `site_root` (its document root) and binds **127.0.0.1
  only** — this is a local dev preview, so the working tree and feedback intake
  are never exposed to the LAN. Reach it as `http://localhost:<port>/…`, not by
  machine IP. (Deliberate LAN use needs an explicit `bind_host` in the config;
  the default forfeits both protections and SETUP does not recommend it.)
- **ONE server per session.** Never run the legacy project server and the
  webkit server simultaneously with the same emoji — two servers stamping one
  color makes the tab strip (and the lock's meaning) a lie.

## 3. Open the preview

```sh
webkit/scripts/open-preview.sh "http://localhost:<port>/<default_page>"
```

- **This script owns browser selection.** Run it as a shell command even when
  your agent harness offers a generic Browser tool, an in-app browser, a
  preview pane, or a webview. Do not open a second copy there. The configured
  `browser.app_name` is the user's external desktop browser; with the default
  config it is the actual Google Chrome application.
- `<port>` is your color's port, `<default_page>` from the config.
- On macOS + Chrome this closes any stale tab on the same `host:port` first,
  then opens and focuses a fresh one — one tab per design, always current.
  Elsewhere it prints the URL; relay it to the user so they open it themselves.
- Reuse this script (never a bare `open`/new tab) every time you reopen the
  preview during the session — that's what keeps the tab strip at one tab per
  agent.

## 4. Start the feedback watcher

The session is now live for the user; your job is to wait for their first
feedback batch. Go to `webkit/LOOP.md` and run it from **step 1** (the wait).
Re-read that file at the top of every round — it says so itself.

## End of session

When the user ends the session (e.g. asks you to merge / says you're done):

1. Stop your preview server.
2. Release your color: `webkit/scripts/release-color.sh "$color"` — it is
   owner-guarded, so it only ever drops *your* lock.
3. Close your preview tab (on macOS+Chrome; otherwise tell the user the URL is
   dead so they close it). Your emoji leaving the tab strip is the signal that
   the color is truly free for the next agent.
