# Ethan's AWESOME WEBKIT - agent install instructions

You are an agent (Claude Code, Codex, or similar) and a user has asked you to
**set up Ethan's AWESOME WEBKIT from a local clone into their website project**.
This file is the canonical install procedure. Follow it in order; every step says
*why* so you can adapt sensibly when a project is unusual, but never skip the
guardrails marked **NEVER**.

Throughout, `<kit>` is the clone you were pointed at (typically
`~/Projects/Ethans-AWESOME-WEBKIT`) and `<project>` is the root of the user's
website project (the git repo you are working in).

## 0. Update, not install?

If `<project>/webkit/` **already exists**, this is an update, not a fresh
install. Do not blindly re-copy; the project's config and any local divergences
must survive. Follow the **pull flow** in `<project>/webkit/UPDATE-KIT.md`
instead of the steps below, then stop.

## 1. Preconditions

Check all of these before changing any file. If one fails, stop and tell the
user what is missing:

- **Git 2.30 or newer** is available, and `<project>` is a Git repository
  (`git --version` and `git rev-parse --show-toplevel` succeed).
  The kit's before/after toggle and verdict loop are built on commits; without
  git there is no product.
- **python3** is on PATH, version 3.7+ (`python3 -c 'import sys; assert sys.version_info >= (3,7)'`).
  The preview server and helpers are stdlib-only, so this is the only runtime
  requirement beyond bash.
- `<project>` is completely clean, including untracked files:
  `test -z "$(git -C "<project>" status --porcelain=v1 --untracked-files=all)"`.
  A fresh install must never absorb or commit unrelated work. If it is dirty,
  ask the user to commit, stash, or remove those changes before continuing.
- `<kit>` is the exact root of a clean Git checkout. Resolve it with
  `git -C "<kit>" rev-parse --show-toplevel`, require an empty
  `git -C "<kit>" status --porcelain=v1 --untracked-files=all`, and inspect
  `git -C "<kit>" remote get-url origin` plus
  `git -C "<kit>" rev-parse HEAD`. Stop unless the root, remote, and commit are
  the source the user selected. Never install from a dirty or untrusted clone.

## 2. Copy the payload

Copy only this documented payload into `<project>/webkit/`: `VERSION`,
`SETUP.md`, `LOOP.md`, `CONTROL-CENTER.md`, `UPDATE-KIT.md`,
`webkit.config.template.json`, `scripts/`, `server/`, `overlay/`, and `skills/`.
Use a Git archive of the verified source commit so untracked or ignored clone
data cannot enter the project. Keep every path quoted and preserve modes:

```sh
set -eu
umask 077

WK_KIT="/absolute/path/to/Ethans-AWESOME-WEBKIT"
WK_PROJECT="/absolute/path/to/website-project"
WK_INSTALL_TMP="$(mktemp -d "${TMPDIR:-/tmp}/awesome-webkit-install.XXXXXX")" || exit 1
trap 'rm -rf -- "$WK_INSTALL_TMP"' EXIT HUP INT TERM
: > "$WK_INSTALL_TMP/claude-skills-added" || exit 1

WK_KIT_SELECTED="$(cd "$WK_KIT" && pwd -P)" || exit 1
WK_PROJECT_SELECTED="$(cd "$WK_PROJECT" && pwd -P)" || exit 1
WK_KIT_ROOT="$(git -C "$WK_KIT" rev-parse --show-toplevel)" || exit 1
WK_PROJECT_ROOT="$(git -C "$WK_PROJECT" rev-parse --show-toplevel)" || exit 1
test "$WK_KIT_SELECTED" = "$WK_KIT_ROOT" || exit 1
test "$WK_PROJECT_SELECTED" = "$WK_PROJECT_ROOT" || exit 1
WK_KIT_STATUS="$(git -C "$WK_KIT" status --porcelain=v1 --untracked-files=all)" || exit 1
WK_PROJECT_STATUS="$(git -C "$WK_PROJECT" status --porcelain=v1 --untracked-files=all)" || exit 1
test -z "$WK_KIT_STATUS" || exit 1
test -z "$WK_PROJECT_STATUS" || exit 1
WK_SOURCE_COMMIT="$(git -C "$WK_KIT" rev-parse --verify 'HEAD^{commit}')" || exit 1

git -C "$WK_KIT" archive --format=tar \
  --output="$WK_INSTALL_TMP/webkit.tar" "$WK_SOURCE_COMMIT" -- webkit || exit 1
mkdir "$WK_INSTALL_TMP/source" || exit 1
tar -xf "$WK_INSTALL_TMP/webkit.tar" -C "$WK_INSTALL_TMP/source" || exit 1
test ! -e "$WK_INSTALL_TMP/source/webkit/webkit.config.json" || {
  printf '%s\n' "Stop: the source commit tracks project configuration." >&2
  exit 1
}
WK_PAYLOAD_LINK="$(find "$WK_INSTALL_TMP/source/webkit" -type l -print -quit)" || exit 1
test -z "$WK_PAYLOAD_LINK" || {
  printf '%s\n' "Stop: the source payload contains a symbolic link." >&2
  exit 1
}
mkdir "$WK_PROJECT/webkit" || exit 1
for WK_ITEM in VERSION SETUP.md LOOP.md CONTROL-CENTER.md UPDATE-KIT.md \
  webkit.config.template.json scripts server overlay skills; do
  test -e "$WK_INSTALL_TMP/source/webkit/$WK_ITEM" || exit 1
  cp -pR "$WK_INSTALL_TMP/source/webkit/$WK_ITEM" "$WK_PROJECT/webkit/" || exit 1
done
```

Do not copy `webkit.config.json`, `.webkit/`, `__pycache__/`, bytecode,
`.DS_Store`, logs, PID files, or any other runtime data. This is a
**vendored copy**, not a submodule. The project owns
its copy and can diverge; `UPDATE-KIT.md` (which travels with it) handles
syncing improvements back and forth later.

## 3. Generate `webkit/webkit.config.json`

Create `<project>/webkit/webkit.config.json` from
`webkit/webkit.config.template.json`. The template carries the shape and the
defaults; you fill in the per-project values. Inference rules, per key:

First derive the repository identity from Git's canonical common directory.
Linked worktrees of one repository report the same common directory, while two
unrelated repositories with the same folder name resolve to different paths.
Use the resulting values for both `project_name` and `lock_dir`:

```sh
set -eu

WK_PROJECT_SELECTED="$(cd "$WK_PROJECT" && pwd -P)" || exit 1
WK_PROJECT_ROOT="$(git -C "$WK_PROJECT" rev-parse --show-toplevel)" || exit 1
test "$WK_PROJECT_SELECTED" = "$WK_PROJECT_ROOT" || {
  printf '%s\n' "Stop: choose the project repository root, not a subdirectory." >&2
  exit 1
}
WK_GIT_COMMON_RAW="$(git -C "$WK_PROJECT" rev-parse --git-common-dir)" || exit 1
WK_GIT_COMMON_DIR="$(python3 -c 'from pathlib import Path; import sys; root = Path(sys.argv[1]); value = Path(sys.argv[2]); print((value if value.is_absolute() else root / value).resolve())' "$WK_PROJECT" "$WK_GIT_COMMON_RAW")" || exit 1
test -d "$WK_GIT_COMMON_DIR" || {
  printf '%s\n' "Stop: Git common directory is unavailable." >&2
  exit 1
}
WK_REPOSITORY_NAME="$(python3 -c 'from pathlib import Path; import sys; common = Path(sys.argv[1]); print(common.parent.name if common.name == ".git" else Path(sys.argv[2]).name)' "$WK_GIT_COMMON_DIR" "$WK_PROJECT")" || exit 1
WK_PROJECT_NAME="$(python3 -c 'import re, sys; value = re.sub(r"[^a-z0-9]+", "-", sys.argv[1].lower()).strip("-") or "website"; reserved = {"con", "prn", "aux", "nul"} | {"com{}".format(i) for i in range(1, 10)} | {"lpt{}".format(i) for i in range(1, 10)}; print("website-" + value if value in reserved else value)' "$WK_REPOSITORY_NAME")" || exit 1
WK_REPOSITORY_SUFFIX="$(python3 -c 'import hashlib, sys; print(hashlib.sha256(sys.argv[1].encode("utf-8")).hexdigest()[:10])' "$WK_GIT_COMMON_DIR")" || exit 1
WK_LOCK_DIR="/tmp/${WK_PROJECT_NAME}-${WK_REPOSITORY_SUFFIX}-agent-colors"
printf 'Project name: %s\nLock directory: %s\n' "$WK_PROJECT_NAME" "$WK_LOCK_DIR"
```

- **`project_name`**: the canonical repository name, slugified: lowercase,
  every run of non-alphanumerics collapsed to a single `-` (e.g.
  `My Cool Site!` → `my-cool-site`). Use `WK_PROJECT_NAME` from the repository
  identity block above, including its Windows reserved-name protection.
- **`site_root`**: the preview server's **document root**: the directory it
  serves (it `os.chdir`s there at boot). `.` unless the served site clearly
  lives in a subdirectory (e.g. everything under `public/` or `site/`); then
  that subdirectory. This is the *serve* dir only; the feedback inbox is a
  separate concern anchored at the git root (see `feedback_dir` below), so a
  subdir-site project still keeps its inbox at the repo root, not under
  `site_root`.
- **`default_page`**: discover the site's entry `index.html` relative to
  `site_root`: prefer one at the root; otherwise the most rootward
  `index.html`. If several are equally plausible, **ask the user**; a wrong
  default page means every session opens on the wrong screen.
- **`lock_dir`**: use `WK_LOCK_DIR` from the repository identity block above.
  The canonical Git common-directory hash keeps every worktree of one
  repository in the same machine-global color namespace and separates
  unrelated repositories that happen to have the same name.
- **`grace_seconds`**: keep the template default (`180`) unless the user asks.
- **`palette`**: keep the template's five entries (blue/red/green/orange/purple,
  ports 5311–5315) **unless a port is already taken** on this machine. Check
  each port with a bind test:
  `python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 5311))'`
  (failure = taken) or `lsof -nP -iTCP:5311 -sTCP:LISTEN`. If any of the five
  is taken, shift the whole palette to the next free contiguous block of five
  (e.g. 5321 through 5325). Keeping the block contiguous makes the mapping
  predictable for humans scanning `lsof` output. This install-time probe is a
  convenience, not the runtime mutex. Each session also takes an atomic entry
  in the private same-user global port registry, linked by a random token to
  its project color lock, before the preview process may bind.
- **`browser`**: template default: `{"mode": "auto", "app_name": "Google Chrome"}`.
  `app_name` is the external desktop browser the user will actually see; it is
  never an agent-hosted in-app browser, preview pane, or webview.
  `auto` means: AppleScript tab management when on macOS with Chrome available,
  print-the-URL fallback everywhere else. Only change it if the user says so.
- **`feedback_dir`**: template default `.webkit/feedback`. Resolved relative
  to the **git root** (not `site_root`), because BEFORE mode reads round history
  out of git and so the inbox must sit inside the repo: the server writes to
  `<git-root>/<feedback_dir>/<slug>/`. Gitignored in step 6.

Do **not** edit `webkit.config.template.json` itself; it is upstream material;
project truth lives only in the generated `webkit.config.json`.

This identity rule applies to fresh configuration only. Reuse an existing
`webkit.config.json` unchanged during installs and updates, even after the
repository is moved, because every active worktree must continue using one
namespace. To migrate an older `/tmp/<project_name>-agent-colors` value, first
stop every preview for that repository, update the config once on a dedicated
branch, and make every worktree consume that change before starting another
session. The old namespace may be left in place after all old sessions stop;
it no longer coordinates the migrated project.

## 4. Install skills (Claude Code projects only)

If the project is driven by **Claude Code**, copy each skill directory from
`webkit/skills/` (`abc/`, `webkit-setup/`, `webkit-loop/`) into
`<project>/.claude/skills/`.

Run this block only for a Claude Code project. It first checks all three
destinations, then records only the directories this step created in the
private install manifest from step 2:

```sh
set -eu

test -f "$WK_INSTALL_TMP/claude-skills-added" || exit 1
test ! -s "$WK_INSTALL_TMP/claude-skills-added" || exit 1
test ! -L "$WK_PROJECT/.claude" || exit 1
test ! -L "$WK_PROJECT/.claude/skills" || exit 1
for WK_SKILL in abc webkit-setup webkit-loop; do
  test -d "$WK_INSTALL_TMP/source/webkit/skills/$WK_SKILL" || exit 1
  test ! -L "$WK_INSTALL_TMP/source/webkit/skills/$WK_SKILL" || exit 1
  if test -e "$WK_PROJECT/.claude/skills/$WK_SKILL" || \
    test -L "$WK_PROJECT/.claude/skills/$WK_SKILL"; then
    printf '%s\n' "Stop: .claude/skills/$WK_SKILL already exists." >&2
    exit 1
  fi
done
mkdir -p "$WK_PROJECT/.claude/skills" || exit 1
for WK_SKILL in abc webkit-setup webkit-loop; do
  cp -pR "$WK_INSTALL_TMP/source/webkit/skills/$WK_SKILL" \
    "$WK_PROJECT/.claude/skills/$WK_SKILL" || exit 1
done
WK_SKILL_MANIFEST_TMP="$WK_INSTALL_TMP/claude-skills-added.tmp"
printf '%s\n' \
  .claude/skills/abc \
  .claude/skills/webkit-setup \
  .claude/skills/webkit-loop > "$WK_SKILL_MANIFEST_TMP" || exit 1
mv "$WK_SKILL_MANIFEST_TMP" "$WK_INSTALL_TMP/claude-skills-added" || exit 1
```

**NEVER overwrite an existing same-named skill.** If `.claude/skills/abc/` (or
any other same-named skill) already exists, leave it untouched and **ask the
user** how to proceed; an existing skill may carry local adaptations the
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
if the line is already there). `.webkit/` holds per-session feedback inboxes:
machine-local runtime state that must never be committed. Also append
`__pycache__/` (and, on macOS, `.DS_Store`): running the vendored server
regenerates `webkit/server/__pycache__/` inside the project, and that compiled
bytecode should never be committed either. Skip any line already present.

## 7. Commit the install snapshot

Before any preview or feedback round, inspect the complete install diff. Stage
only the files created or deliberately edited by steps 2 through 6, then make a
dedicated snapshot commit:

```sh
set -eu

test -f "$WK_INSTALL_TMP/claude-skills-added" || exit 1
WK_PRESTAGED="$(git -C "$WK_PROJECT" diff --cached --name-only)" || exit 1
test -z "$WK_PRESTAGED" || {
  printf '%s\n' "Stop: the project already has staged content." >&2
  exit 1
}
git -C "$WK_PROJECT" status --short || exit 1
git -C "$WK_PROJECT" add -- \
  webkit/VERSION \
  webkit/SETUP.md \
  webkit/LOOP.md \
  webkit/CONTROL-CENTER.md \
  webkit/UPDATE-KIT.md \
  webkit/webkit.config.template.json \
  webkit/webkit.config.json \
  webkit/scripts \
  webkit/server \
  webkit/overlay \
  webkit/skills \
  AGENTS.md \
  .gitignore || exit 1
if test -f "$WK_PROJECT/CLAUDE.md"; then
  git -C "$WK_PROJECT" add -- CLAUDE.md || exit 1
fi
while IFS= read -r WK_SKILL_PATH; do
  case "$WK_SKILL_PATH" in
    .claude/skills/abc|.claude/skills/webkit-setup|.claude/skills/webkit-loop) ;;
    *) printf '%s\n' "Stop: invalid install-manifest path." >&2; exit 1 ;;
  esac
  test -d "$WK_PROJECT/$WK_SKILL_PATH" || exit 1
  test ! -L "$WK_PROJECT/$WK_SKILL_PATH" || exit 1
  git -C "$WK_PROJECT" add -- "$WK_SKILL_PATH" || exit 1
done < "$WK_INSTALL_TMP/claude-skills-added"
git -C "$WK_PROJECT" diff --cached --check || exit 1
git -C "$WK_PROJECT" diff --cached --stat || exit 1
git -C "$WK_PROJECT" diff --cached || exit 1
git -C "$WK_PROJECT" commit -m "Install AWESOME WEBKIT" || exit 1
WK_FINAL_STATUS="$(git -C "$WK_PROJECT" status --porcelain=v1 --untracked-files=all)" || exit 1
test -z "$WK_FINAL_STATUS" || exit 1
```

Read the staged diff before committing. If any unrelated path appears, unstage
it and stop for user direction. If Git identity is missing, the commit fails,
or the final worktree is not clean, stop and ask the user. LOOP must begin from
this clean install commit so its before reference contains no unrelated work.

## 8. Start the first session and report

Before starting, verify that `<project>/AGENTS.md` exists and contains the
`## Webkit` section. This is the Codex-support invariant; do not report a
successful install without it.

Confirm `git -C "$WK_PROJECT" status --porcelain=v1 --untracked-files=all`
is empty. Now run `webkit/SETUP.md` top to bottom. Finish by reporting to the
user, concretely:

- the **color** you claimed (emoji + slug),
- the **port** your server is on,
- the **preview URL** you opened (or printed, on non-Chrome/macOS setups),

and one sentence telling them the loop is live: press C, hold Alt/Option while
dragging a rectangle, describe the change, and hit send.
