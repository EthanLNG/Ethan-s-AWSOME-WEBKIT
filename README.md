<p align="center">
  <img src="control-center/static/brand-icon.svg" width="112" alt="AWESOME WEBKIT logo">
</p>

<h1 align="center">Ethan's AWESOME WEBKIT</h1>

<p align="center">
  <strong>Design at the speed of AI.</strong><br>
  The braindead-simple, stupidly-fast visual feedback loop for coding agents.
</p>

<p align="center">
  <code>POINT&nbsp;&nbsp;→&nbsp;&nbsp;DESCRIBE&nbsp;&nbsp;→&nbsp;&nbsp;BUILD&nbsp;&nbsp;→&nbsp;&nbsp;COMPARE&nbsp;&nbsp;→&nbsp;&nbsp;SHIP</code>
</p>

<p align="center">
  <a href="https://github.com/EthanLNG/Ethan-s-AWSOME-WEBKIT/actions/workflows/quality.yml"><img src="https://github.com/EthanLNG/Ethan-s-AWSOME-WEBKIT/actions/workflows/quality.yml/badge.svg" alt="Quality"></a>
  <img src="https://img.shields.io/badge/local--first-111111" alt="Local first">
  <img src="https://img.shields.io/badge/Git--backed-111111" alt="Git backed">
  <img src="https://img.shields.io/badge/token--efficient-111111" alt="Token efficient">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-proprietary-b00020" alt="Proprietary license"></a>
</p>

**Generate an irresponsible number of designs, stupidly fast. Never get lost.**
Run multiple agents in parallel, generate an irresponsible number of
variations, and explore entire design universes at the same time without
getting lost in the chaos. The interface remains so braindead simple that the
only thing you can lose is respect for slower workflows. AWESOME WEBKIT
automatically organizes every agent, branch, preview, feedback point, version,
and Git commit. It is stupidly optimized for mental clarity, so getting
overwhelmed would require genuine effort.

- **Stupidly fast iteration:** visual feedback, agent work, and live review stay
  in the same loop, so you can move from idea to verified change in moments.
- **Token-efficient by design:** precise points, rectangles, and page context
  tell the agent where to work without repeatedly describing the whole screen.
- **Automatic Git and GitHub safety:** isolated worktrees, point-level commits,
  guarded merges, and verified pushes keep every iteration traceable and
  recoverable.
- **Braindead simple:** point, describe, and review. The Webkit handles branches,
  preview ports, agent sessions, version control, and cleanup behind the scenes.

## What is included

- **Concurrent sessions:** atomic color locks and global preview-port
  reservations keep parallel worktrees and projects from claiming the same
  runtime identity.
- **Fresh previews:** the local server uses `Cache-Control: no-store` and stamps
  each browser tab with its session color.
- **Non-blocking feedback:** press **C**, then hold **Alt/Option** while dragging
  a rectangle. Normal clicks continue to reach the website. Press **V** for
  dictation. C and V are defaults and can be changed in the Control Center.
- **Git-backed review:** before/after comparison can target one feedback point
  or the whole page. Accept, delete, and redo actions remain point-specific.
- **Lettered experiments:** A/B/C variants stay scoped to one section and are
  removed cleanly when a winner is accepted.
- **Two workflows:** use the plain Markdown protocol from any coding-agent app,
  or run the local Control Center for persistent color sessions and chat.

## Install into a website project

Clone the kit once:

```sh
set -eu
git clone https://github.com/EthanLNG/Ethan-s-AWSOME-WEBKIT.git ~/Projects/Ethans-AWESOME-WEBKIT
```

Then open your website project in Codex or Claude Code and say:

> Set up Ethan's AWESOME WEBKIT from ~/Projects/Ethans-AWESOME-WEBKIT in this project.

The agent follows the [installation procedure](AGENTS.md), vendors the WebKit
payload, creates project configuration, commits the isolated installation, and
starts the [session setup](webkit/SETUP.md) and
[feedback loop](webkit/LOOP.md). Preview requests are routed to the configured
external desktop browser instead of an embedded agent browser or IDE webview.

## Run the local Control Center

The Control Center uses a Codex or Claude Code CLI that is already authenticated
on your computer. WebKit does not read or store the provider credential.

```sh
set -eu
cd ~/Projects/Ethans-AWESOME-WEBKIT
./launch-control-center.sh
```

The repository also includes `AWESOME WEBKIT.command` for direct macOS launch
and `AWESOME WEBKIT.cmd` for Windows. To install a branded desktop launcher
from the clone on macOS or Linux:

```sh
set -eu
cd ~/Projects/Ethans-AWESOME-WEBKIT
python3 control-center/install-shortcut.py
```

From a native Windows Command Prompt, use the Python launcher instead:

```bat
cd /d "%USERPROFILE%\Projects\Ethans-AWESOME-WEBKIT"
py -3 control-center\install-shortcut.py
```

On first launch, select Codex, Claude Code, or both. You can add an existing Git
repository or create a starter website. New-project onboarding can collect a
brand brief and reference files, then ask the agent for several distinct design
directions. You can preview the directions, choose one or more, and describe how
to combine them.

Reference files are copied into `project-context/` and committed to the new
project. WebKit blocks common credential files and high-confidence secret
patterns, but you should still review every selected reference before
continuing. The local project is created, committed, and registered before any
GitHub request. When GitHub CLI is authenticated, the Control Center then tries
to create a private repository and verifies the push. A create, push, or
verification failure is reported without deleting the local project or commit.

Each normal color session runs in an isolated worktree. For an existing
repository, **Merge changes** first integrates the color branch into the
Control Center's managed checkout, then fast-forwards the detected local target
branch and its checked-out worktree, if any. The target must still be clean,
free of an in-progress Git operation, and an ancestor of the managed branch.
If it is dirty, diverged, or changes during validation, finalization stops and
preserves both the managed result and session worktree for recovery. **Discard
work** permanently removes the unmerged color branch and worktree. Successful
merge, seed finalization, and discard operations also close the session preview
window that the Control Center opened.

Existing GitHub remotes are detected automatically, and successful merges are
pushed when GitHub is connected. The verified local target fast-forward
completes before the optional push. A GitHub push or verification failure never
rolls back that local integration; the merged work remains available and can
be pushed again later.

## Security and privacy boundaries

- Control Center and default preview servers bind to `127.0.0.1`. Their local
  mutation endpoints require session credentials and reject cross-origin browser
  requests. After launcher bootstrap, the Control Center keeps its bearer in
  origin-scoped session storage and sends API credentials only through a custom
  header, so unrelated services on other localhost ports do not receive it.
  Control Center state, agent event logs, and session metadata are kept under
  `~/.awesome-webkit/` with private filesystem permissions.
- Preview `bind_host` accepts only `127.0.0.1`, `localhost`, or `0.0.0.0`.
  `0.0.0.0` is deliberate LAN mode and requires a non-empty `allowed_hosts`
  list of exact hostnames or IPv4 addresses. That Host-header allowlist is a
  DNS-rebinding defense, not authentication and not a source-IP access list.
  Anyone who can reach the port and use an allowed Host value must be trusted.
  Use a host firewall and a trusted private LAN or authenticated VPN. Never
  expose the preview port through public Wi-Fi, router port forwarding, a
  public tunnel, or the public internet.
- AWESOME WEBKIT assumes that processes running as the same operating-system
  account are trusted. A same-account process, including the selected coding
  agent, can read project and runtime files, invoke local endpoints, and run
  Git commands. The tokens, path checks, and schemas defend protocol boundaries
  and accidents; they are not a sandbox against a hostile local process.
- The preview mutation token is injected into the served page so the overlay can
  submit feedback. Any script running on that same preview origin can read it.
  Preview only code you trust, especially when a page loads third-party scripts.
- Strict Content Security Policy values declared in HTML `meta` tags are
  adapted only in the generated preview response so the overlay can run; source
  files are not changed. A CSP meta tag that cannot be transformed safely gets
  an HTTP 422 JSON response before token-bearing HTML is served. CSP supplied by
  an upstream HTTP response header cannot be rewritten by this static HTML
  transform and must separately permit the overlay. A successful local review
  is a visual and workflow check, not a production CSP validation or a security
  audit.
- Browser speech-to-text uses the browser's Web Speech implementation. Chrome may
  send audio to its speech service under the browser vendor's privacy terms. The
  resulting transcript becomes feedback and is sent to the selected coding-agent
  provider when the point is processed.
- Agent voice-note mode stores the recording locally and requires local Whisper.
  The server retains referenced audio long enough for safe retries, then removes
  unreferenced notes after a durable round transition. The resulting transcript
  still becomes input to the selected coding-agent provider.
- `api_proxy_origin` is a privileged developer bridge and is disabled by default.
  It requires the explicit `WK_ENABLE_API_PROXY=1` process opt-in. Do not enable
  it for untrusted project configuration or pages with untrusted scripts.
- Discarding a session is intentionally destructive. The UI requires explicit
  confirmation before the unmerged worktree and branch are deleted.
- `feedback_dir` must be a dedicated repository-relative folder, never the
  repository root. Both `.webkit/` and any custom feedback root must remain
  untracked, unstaged, and covered by Git ignore rules. The Control Center
  refuses unsafe sessions and integrations and excludes those roots from its
  own staging. Never force-add their feedback, voice, attachment, capability,
  or transition files.

## Runtime limits and retention

Safety limits fail closed. They do not silently publish partial data:

- One Control Center chat message accepts at most 20 attachments and 20 MiB
  combined. Retained attachments are capped at 100 MiB, 2,000 regular files,
  and 512 directories per session. Storage is rescanned for every message;
  symbolic links and special files are rejected.
- Control Center state is capped at 8 MiB and retains at most 50 terminal
  sessions. Each event log is capped at 4 MiB and compacted to approximately
  the newest 3 MiB when necessary.
- Control Center secret scans inspect at most 10,000 commits, 20,000 changed
  file records, 256 MiB of aggregate scanned blob prefixes, and 2 MiB per blob.
  Staged commits and session integrations are refused when a scan ceiling is
  exceeded, rather than treating a partial scan as clean.
- Each color inbox preserves at most 200 archived feedback rounds, 128 MiB of
  archive data, and 4,096 scanned history entries. The server does not prune
  receipts automatically. It preserves existing history and refuses another
  archive when a ceiling is reached. Follow the stopped-server recovery in
  [`webkit/LOOP.md`](webkit/LOOP.md#history-cap-recovery).
- A voice-note request is capped at 25 MiB. Each color's voice-note directory
  is capped at 100 MiB, 1,000 persistent upload files, and 1,024 total scanned
  entries. Admission reserves two directory slots for its no-clobber write;
  unknown entries, links, and special files fail closed. Unreferenced
  recordings are eligible for cleanup after a one-hour orphan grace period.
- Live and Git-backed BEFORE HTML transformation accepts at most 8 MiB of
  source HTML. The transformed response is capped at 16 MiB for HTML, while
  transformed BEFORE CSS is capped at 32 MiB.
- Accepted client sockets use a 15-second I/O timeout so abandoned connections
  cannot hold shutdown indefinitely.

## Requirements

- **Git 2.30 or newer** for snapshots, point commits, isolated worktrees, and
  guarded merge handling.
- **Python 3.7 or newer.** Runtime code uses only the standard library.
- **Codex CLI or Claude Code CLI** installed and authenticated for Control Center
  agent sessions.
- **Bash** for the direct agent-app workflow on macOS and Linux. Windows users
  can run that flow in WSL. The Control Center and `.cmd` launcher run natively.
- **A Chromium-based browser** for the full experience. The feedback and review
  UI works in other modern browsers, but browser dictation support varies.
- **Local Whisper and ffmpeg**, optional, only for Agent voice-note mode.
- **GitHub CLI**, optional, for automatic private repository creation and sync.

On macOS with Google Chrome, the direct launcher can close stale matching tabs
and focus a fresh preview automatically. Other direct-flow configurations print
the exact URL for the user. The Control Center opens the platform's configured
browser.

## Demo and maintenance

[`examples/demo-site/index.html`](examples/demo-site/index.html) is a
self-contained visual fixture with a live lettered experiment. It is useful for
inspecting the ABC switcher without changing a real project.

Vendored projects use [the update and contribution guide](webkit/UPDATE-KIT.md)
to pull a newer payload or prepare a reusable fix on a topic branch. Remote
writes always require explicit user approval.

## License

**Proprietary. All rights reserved.** The source is public for inspection, but
no permission is granted to use, copy, modify, distribute, deploy, host, sell,
or create derivative works from it. GitHub's required platform rights to view
and fork public repositories still apply. See the full [license](LICENSE).
