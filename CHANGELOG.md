# Changelog

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
