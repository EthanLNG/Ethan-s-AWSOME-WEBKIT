# Ethan's AWESOME WEBKIT

**Web design iteration for the agent era.** You look at your live site, draw a rectangle
around the thing that bugs you, and say (literally — press V and talk) what you want
changed. Your coding agent — Claude Code or Codex — picks up the batch, applies every
point as its own git commit, and reopens the page jumped to point 1. You walk the points
with a before/after toggle and accept, delete, or redo each one. Want options instead of
one answer? Ask for A/B/C variants on any point and flip between them live before you
pick. And because real work happens in parallel, up to five agents can iterate on the
same machine at once — each locked to its own color, each visible at a glance in your
tab strip.

![demo](docs/demo.gif)

## Features

- **Multi-agent color system** — atomic per-color locks (`mkdir` under `/tmp`, race-proof
  even when two agents start in the same second) plus emoji-stamped browser tabs, so five
  parallel agents never collide and you always know which tab is whose.
- **No-store preview server** — every asset served with `Cache-Control: no-store`; what
  you see is always the file on disk, never a stale cache.
- **Feedback overlay** — press **C** on the live page to open a drawing layer: rectangle
  annotations, per-point notes, and **live speech-to-text** (press **V**) so you can
  dictate feedback instead of typing it. Both shortcuts switch off while a text field
  is focused, so `C` and `V` remain typable. Dictation has an explicit persistent
  English/עברית recognition selector. Nothing of the kit sits on the page at rest — no
  button, no badge — so what you look at is your design, not a tool around it.
- **Point-first before/after from git** — the review bar compares only the exact
  feedback target by default. Its adjacent arrow button switches to a full-site
  snapshot when you want to inspect every change together.
- **Accept / delete / redo verdict loop** — verdict each point in the browser; the agent
  keeps, reverts, or re-does the matching commit and comes back for another round.
- **ABC lettered variants** — any feedback point can request N variants (described by you,
  or invented by the model); a live bottom-left switcher cycles them on the page, and
  accepting a letter folds it in and deletes the rest, losslessly.
- **Harness-agnostic** — works with **Claude Code and Codex**. The protocol lives in plain
  markdown (`webkit/SETUP.md`, `webkit/LOOP.md`) that any agent can follow; Claude Code
  additionally gets thin skill wrappers. Installation always creates a Codex-readable
  `AGENTS.md` entry point, even in projects that previously had only `CLAUDE.md`, and
  preview requests are routed through WebKit instead of generic browser tooling. The
  configured external desktop browser (Google Chrome by default) is the only preview
  surface—never an agent in-app browser, Claude preview pane, or IDE webview.

## Quickstart

```sh
git clone https://github.com/EthanLNG/Ethan-s-AWSOME-WEBKIT.git ~/Projects/Ethans-AWESOME-WEBKIT
```

Then open your website project in your agent and tell it:

> Set up Ethan's AWESOME WEBKIT from ~/Projects/Ethans-AWESOME-WEBKIT in this project.

That's the whole install. The agent reads [AGENTS.md](AGENTS.md), vendors the kit into
your project, infers a config, installs the project-level `AGENTS.md` discovery pointer,
claims a color, starts the preview server, and hands you a URL. From then on, both Codex
and Claude route website launch/preview requests through `webkit/SETUP.md` and run the
feedback loop in `webkit/LOOP.md`. The canonical launcher opens the configured external
desktop browser, so a new agent conversation cannot silently divert the preview into an
embedded browser surface.

## Local Control Center

AWESOME WEBKIT also includes a small local browser app for people who want to
work without keeping Codex or Claude Code open. It uses the CLIs already
authenticated on your computer; no API keys are stored by Webkit.

```sh
./launch-control-center.sh
```

On first launch, choose Codex, Claude Code, or both, then add an existing
website or create a starter website. Every project chooses one provider. Pick
one of the five colors to start an isolated Git worktree, background agent, and
stamped preview. The chat stays collapsed until you want it; normal rectangle
feedback on the website wakes the matching CLI automatically.

The top-bar **Settings** button controls the microphone mode. Browser
speech-to-text remains the default. **Agent voice notes** instead stores the
original recording with the feedback point and has the background agent
transcribe it with a local Whisper installation; Settings reports whether that
private, on-device transcription engine is ready.

Each color session has two explicit finish actions:

- **Merge to main** merges the color branch and closes its worktree.
- **Discard work** permanently deletes the unmerged color branch and worktree.

Agents never work directly on `main`. Project onboarding may create an initial
Git commit or install the vendored kit before the first color session. GitHub
is optional—the UI reminds you to publish useful projects, but local Git is all
Webkit requires.

Install a clickable desktop shortcut:

```sh
python3 control-center/install-shortcut.py
```

The Control Center is dependency-free Python plus HTML/CSS/JavaScript, binds to
`127.0.0.1` only, protects mutating endpoints with a per-launch token, and
keeps its local state under `~/.awesome-webkit/`. On macOS the launcher opens
Google Chrome by default (`WKCC_BROWSER_APP` can override it); elsewhere it
uses the system browser. The original agent-app flow above remains fully
supported and unchanged.

Want to try it without a project? `examples/demo-site/` is a self-contained page (with a
live A/B experiment already on it) built to exercise every part of the loop.

## Requirements

- **git** — the before/after toggle and the verdict loop are built on commits.
- **Python 3.7+** — the preview server and helpers are stdlib-only; no pip installs, no
  Node at runtime.
- **A Chromium-based browser** — the default mic mode uses Chrome's live speech recognition
  (`webkitSpeechRecognition`) and explicitly requests English (`en-US`) or Hebrew
  (`he-IL`) from the user's selector. Everything except dictation works in other browsers.
- **Local Whisper (optional)** — required only for the Control Center's Agent
  voice-note setting. WebKit never silently uploads recordings to a third-party
  transcription API.
- **macOS + Google Chrome** for automatic tab management (stale tabs closed, fresh tab
  opened and focused per design). On other platforms/browsers the kit degrades
  gracefully: it prints the URL and you open it yourself.

## Note on visibility

This repository is currently **private** while the kit stabilizes; the quickstart clone
works for the owner and collaborators. It's built to be shared — the owner can flip it
public in the repo settings whenever ready.

## License

[Proprietary, all rights reserved](LICENSE).
