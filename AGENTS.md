# Ethan's AWESOME WEBKIT — agent install instructions

You are an agent (Claude Code, Codex, or similar) and a user has asked you to
**set up Ethan's AWESOME WEBKIT from a local clone into their website project**.
This file is the canonical install procedure. Follow it in order; every step says
*why* so you can adapt sensibly when a project is unusual — but never skip the
guardrails marked **NEVER**.

Throughout, `<kit>` is the clone you were pointed at (typically
`~/Projects/Ethans-AWESOME-WEBKIT`) and `<project>` is the root of the user's
website project (the git repo you are working in).

## 0. Update, not install?

If `<project>/webkit/` **already exists**, this is an update, not a fresh
install. Do not blindly re-copy — the project's config and any local divergences
must survive. Follow the **pull flow** in `<project>/webkit/UPDATE-KIT.md`
instead of the steps below, then stop.

## 1. Preconditions

Check both; if either fails, stop and tell the user what's missing:

- `<project>` is a **git repository** (`git rev-parse --show-toplevel` succeeds).
  The kit's before/after toggle and verdict loop are built on commits — without
  git there is no product.
- **python3** is on PATH, version 3.7+ (`python3 -c 'import sys; assert sys.version_info >= (3,7)'`).
  The preview server and helpers are stdlib-only, so this is the only runtime
  requirement beyond bash.

## 2. Copy the payload

Copy `<kit>/webkit/` into `<project>/webkit/` — the whole directory: `VERSION`,
`SETUP.md`, `LOOP.md`, `CONTROL-CENTER.md`, `UPDATE-KIT.md`, `webkit.config.template.json`,
`scripts/`, `server/`, `overlay/`, `skills/`. Preserve the executable bits on
`scripts/*.sh`, but **skip runtime junk** — a `server/__pycache__/` may sit in
the kit clone from a prior run, and vendoring a stray `.pyc` into someone's
project is noise. Use e.g.
`rsync -a --exclude='__pycache__' --exclude='.DS_Store' <kit>/webkit/ <project>/webkit/`
(or `cp -R` then `find <project>/webkit -name __pycache__ -type d -exec rm -rf {} +`).
This is a **vendored copy**, not a submodule — the project owns
its copy and can diverge; `UPDATE-KIT.md` (which travels with it) handles
syncing improvements back and forth later.

## 3. Generate `webkit/webkit.config.json`

Create `<project>/webkit/webkit.config.json` from
`webkit/webkit.config.template.json`. The template carries the shape and the
defaults; you fill in the per-project values. Inference rules, per key:

- **`project_name`** — the project directory's name, slugified: lowercase,
  every run of non-alphanumerics collapsed to a single `-` (e.g.
  `My Cool Site!` → `my-cool-site`). It namespaces the machine-global lock dir,
  so it must be filesystem-safe and reasonably unique on this machine.
- **`site_root`** — the preview server's **document root**: the directory it
  serves (it `os.chdir`s there at boot). `.` unless the served site clearly
  lives in a subdirectory (e.g. everything under `public/` or `site/`); then
  that subdirectory. This is the *serve* dir only — the feedback inbox is a
  separate concern anchored at the git root (see `feedback_dir` below), so a
  subdir-site project still keeps its inbox at the repo root, not under
  `site_root`.
- **`default_page`** — discover the site's entry `index.html` relative to
  `site_root`: prefer one at the root; otherwise the most rootward
  `index.html`. If several are equally plausible, **ask the user** — a wrong
  default page means every session opens on the wrong screen.
- **`lock_dir`** — `/tmp/<project_name>-agent-colors`. Machine-global on
  purpose: agents in *different worktrees of the same project* must contend for
  the same five colors, and `/tmp` is the rendezvous they all can see.
- **`grace_seconds`** — keep the template default (`180`) unless the user asks.
- **`palette`** — keep the template's five entries (blue/red/green/orange/purple,
  ports 5311–5315) **unless a port is already taken** on this machine. Check
  each port with a bind test:
  `python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 5311))'`
  (failure = taken) or `lsof -nP -iTCP:5311 -sTCP:LISTEN`. If any of the five
  is taken, shift the whole palette to the next free contiguous block of five
  (e.g. 5321–5325) — keeping the block contiguous keeps slug→port predictable
  for humans scanning `lsof` output.
- **`browser`** — template default: `{"mode": "auto", "app_name": "Google Chrome"}`.
  `app_name` is the external desktop browser the user will actually see; it is
  never an agent-hosted in-app browser, preview pane, or webview.
  `auto` means: AppleScript tab management when on macOS with Chrome available,
  print-the-URL fallback everywhere else. Only change it if the user says so.
- **`feedback_dir`** — template default `.webkit/feedback`. Resolved relative
  to the **git root** (not `site_root`), because BEFORE mode reads round history
  out of git and so the inbox must sit inside the repo: the server writes to
  `<git-root>/<feedback_dir>/<slug>/`. Gitignored in step 6.

Do **not** edit `webkit.config.template.json` itself — it is upstream material;
project truth lives only in the generated `webkit.config.json`.

## 4. Install skills (Claude Code projects only)

If the project is driven by **Claude Code**, copy each skill directory from
`webkit/skills/` (`abc/`, `webkit-setup/`, `webkit-loop/`) into
`<project>/.claude/skills/`.

**NEVER overwrite an existing same-named skill.** If `.claude/skills/abc/` (or
any other same-named skill) already exists, leave it untouched and **ask the
user** how to proceed — an existing skill may carry local adaptations the
project depends on, and clobbering it is unrecoverable from the user's point of
view.

**Codex projects skip this step entirely.** The markdown files
(`webkit/SETUP.md`, `webkit/LOOP.md`) are canonical and self-sufficient; the
skills are only thin convenience wrappers for Claude Code's skill system.

## 5. Install the project's Codex/Claude entry points

**Always ensure `<project>/AGENTS.md` exists**, creating it if needed, and append
the pointer section below. Codex auto-reads `AGENTS.md`; a project that only has
`CLAUDE.md` is not Codex-configured. This is mandatory even when the user is
currently installing through Claude Code, because the project must remain
harness-portable.

If `<project>/CLAUDE.md` exists, append the same section there too. Do not create
`CLAUDE.md` solely for the kit. Skip a file that already contains a `## Webkit`
section (idempotency).

Use this section:

```markdown
## Webkit

This project uses Ethan's AWESOME WEBKIT for design iteration. At the start of
every website-working session, and whenever the user asks to launch, open,
preview, show, or test the website in a browser, read and follow
`webkit/SETUP.md` exactly. Use its claimed color, stamping preview server, and
`webkit/scripts/open-preview.sh`; never substitute a generic HTTP server, a
manually opened browser tab, the Codex/ChatGPT in-app browser, a Claude preview
pane, an IDE webview, or another embedded browser. The configured external
desktop browser is the only preview surface unless the user explicitly
overrides it. If the WebKit session is already active, reuse it instead of
claiming again. Read `webkit/LOOP.md` for browser feedback rounds.
```

The pointer is deliberately explicit about launch/preview requests: those are
the common prompts where an agent may otherwise reach for its generic browser
tooling before discovering the project workflow. The operational details remain
canonical in `webkit/`, where the pull flow keeps them current.
The same section goes into `CLAUDE.md` when that file exists, so Claude Code and
Codex receive the identical external-browser rule.

Then append this separate, idempotent controller section if it is missing:

```markdown
## Webkit Control Center

When `WK_CONTROL_CENTER=1`, the local Control Center already owns the color,
worktree, preview process, browser tab, and waiting. Read
`webkit/CONTROL-CENTER.md`, process one finite feedback or chat transition, and
exit; never merge or discard the controller-owned branch yourself.
```

Keeping this under its own heading lets the Control Center upgrade projects
that already have an older `## Webkit` section without rewriting user-owned
instructions.

## 6. Gitignore the runtime data

Append `.webkit/` to `<project>/.gitignore` (create the file if missing; skip
if the line is already there). `.webkit/` holds per-session feedback inboxes —
machine-local runtime state that must never be committed. Also append
`__pycache__/` (and, on macOS, `.DS_Store`): running the vendored server
regenerates `webkit/server/__pycache__/` inside the project, and that compiled
bytecode should never be committed either. Skip any line already present.

## 7. Start the first session and report

Before starting, verify that `<project>/AGENTS.md` exists and contains the
`## Webkit` section. This is the Codex-support invariant; do not report a
successful install without it.

Now run `webkit/SETUP.md` top to bottom (claim a color, start the server, open
the preview, start the watcher). Finish by reporting to the user, concretely:

- the **color** you claimed (emoji + slug),
- the **port** your server is on,
- the **preview URL** you opened (or printed, on non-Chrome/macOS setups),

and one sentence telling them the loop is live: press C, hold Option while
dragging a rectangle, describe the change, and hit send.
