# Ethan's AWESOME WEBKIT

**Web design iteration for the agent era.** You look at your live site, draw a rectangle
around the thing that bugs you, and say (literally — there's a mic button) what you want
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
- **Feedback overlay** — a hover-reveal button on the live page opens a drawing layer:
  rectangle annotations, per-point notes, and **live speech-to-text** so you can dictate
  feedback instead of typing it.
- **Before/after toggle from git** — the review bar swaps the whole page between the
  pre-round commit (served straight out of `git show`) and the current state, scroll
  position preserved.
- **Accept / delete / redo verdict loop** — verdict each point in the browser; the agent
  keeps, reverts, or re-does the matching commit and comes back for another round.
- **ABC lettered variants** — any feedback point can request N variants (described by you,
  or invented by the model); a live bottom-left switcher cycles them on the page, and
  accepting a letter folds it in and deletes the rest, losslessly.
- **Harness-agnostic** — works with **Claude Code and Codex**. The protocol lives in plain
  markdown (`webkit/SETUP.md`, `webkit/LOOP.md`) that any agent can follow; Claude Code
  additionally gets thin skill wrappers.

## Quickstart

```sh
git clone https://github.com/EthanLNG/Ethan-s-AWSOME-WEBKIT.git ~/Projects/Ethans-AWESOME-WEBKIT
```

Then open your website project in your agent and tell it:

> Set up Ethan's AWESOME WEBKIT from ~/Projects/Ethans-AWESOME-WEBKIT in this project.

That's the whole install. The agent reads [AGENTS.md](AGENTS.md), vendors the kit into
your project, infers a config, claims a color, starts the preview server, and hands you
a URL. From then on, every session starts with `webkit/SETUP.md` and runs the feedback
loop in `webkit/LOOP.md`.

Want to try it without a project? `examples/demo-site/` is a self-contained page (with a
live A/B experiment already on it) built to exercise every part of the loop.

## Requirements

- **git** — the before/after toggle and the verdict loop are built on commits.
- **Python 3.7+** — the preview server and helpers are stdlib-only; no pip installs, no
  Node at runtime.
- **A Chromium-based browser** — the overlay's mic uses Chrome's live speech recognition
  (`webkitSpeechRecognition`). Everything except dictation works in other browsers.
- **macOS + Google Chrome** for automatic tab management (stale tabs closed, fresh tab
  opened and focused per design). On other platforms/browsers the kit degrades
  gracefully: it prints the URL and you open it yourself.

## Note on visibility

This repository is currently **private** while the kit stabilizes; the quickstart clone
works for the owner and collaborators. It's built to be shared — the owner can flip it
public in the repo settings whenever ready.

## License

[Proprietary, all rights reserved](LICENSE).
