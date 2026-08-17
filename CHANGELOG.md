# Changelog

## v0.3.0 (2026-08-17)

- **Seamless Codex discovery.** Installation now always creates or updates the
  project's `AGENTS.md`, even when that project already has only a
  `CLAUDE.md`. Previously that shape silently skipped the Codex entry point.
- The installed `## Webkit` pointer now explicitly routes launch, open,
  preview, show, and browser-test requests through `webkit/SETUP.md`, including
  its claimed color, stamping server, and canonical tab-reuse helper. It also
  tells agents to reuse an active session rather than claim twice.
- Installation verifies the Codex entry point before reporting success.

**Migration:** existing projects should add the documented `## Webkit` section
from the installer to a root `AGENTS.md` (create it if missing). No config or
runtime migration is required.

## v0.2.0 (2026-07-25)

First round of changes driven by real use on a live site.

- **One key, no button.** Showing/hiding the overlay is a single keypress
  (default `C`, `hotkeys.toggle` in config). The bottom-left toggle button is
  gone — nothing of the kit sits on the page at rest — and the old aliases
  (backquote, double-Esc, Ctrl/Cmd+.) are removed so there is exactly one thing
  to learn. Ctrl/Cmd+. was never reaching the page on macOS Chrome anyway.
  With a review round waiting, the key walks into the review.
- **`V` starts and stops dictation** (`hotkeys.dictate`), so a drawn rectangle
  goes straight to speech without reaching for the mic button. It still types a
  literal "v" once the note has text, and Cmd/Ctrl+V stays paste.
- **BEFORE|AFTER no longer reloads the page.** The snapshot is fetched once and
  the reviewed section is swapped in place, with the page's stylesheets
  repointed at their snapshot copies so CSS-only changes show a real difference.
  The compared element keeps its viewport position across toggles. Falls back to
  the old full navigation (with a toast) when no container matches both versions.
- **One ABC control during review.** The page's own variant switcher is parked
  for the point under review, leaving the action-bar chip — now a real button
  showing the current letter, the letter run and a cycle affordance.
- **Point numbers restart at #1** once a batch is finished and nothing is
  queued, instead of climbing forever. Numbering still continues within a batch.
- Keyboard shortcuts are matched by `KeyboardEvent.code`, so they work on
  non-US layouts (Hebrew, Arabic, …).
- Hardening: `--end-of-options` and a SHA format check before `git show`; the
  server binds `127.0.0.1` only; `site_root` is honoured as the document root;
  RTL host pages no longer mirror the overlay; touch/coarse pointers supported.

## v0.1.0 (2026-07-25) — initial release

- Multi-agent color system: atomic `mkdir` locks with owner records, stale-lock
  grace, emoji-stamped tab titles; five colors, config-driven palette.
- Preview server (`webkit/server/preview-server.py`): stdlib-only, no-store on
  every asset, claim verification at boot, overlay injection, `/__wk/*` API
  (state polling, feedback + verdict intake, git-served BEFORE mode).
- In-browser feedback overlay: hover-reveal toggle, rectangle annotations with
  numbered pins, live speech-to-text dictation, per-point ABC variant requests,
  review bar with BEFORE|AFTER toggle and accept/delete/redo verdicts.
- Agent protocol as plain markdown: `webkit/SETUP.md` (per-session startup),
  `webkit/LOOP.md` (the feedback-round contract with full JSON schemas),
  `webkit/UPDATE-KIT.md` (maintainer push/pull flow).
- abc skill (lettered A/B/C… variants with a live section-scoped switcher and
  lossless finalize) plus thin `webkit-setup` / `webkit-loop` skill wrappers
  for Claude Code; Codex runs the markdown directly.
- Install procedure in `AGENTS.md` (config inference, skill safety, pointer
  sections, gitignore) and a self-contained demo site in `examples/demo-site/`
  with a live A/B experiment for end-to-end testing.
