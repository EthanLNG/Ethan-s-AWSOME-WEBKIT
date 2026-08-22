# Changelog

## v0.8.11 (2026-08-22): Flexible redo variants

- Made the number of variants requested from Redo adjustable from 2 to 10,
  with the saved selection restored when a verdict is edited.
- Kept the review control pill on one line until its content genuinely reaches
  the 20px viewport margin, then retained the compact narrow-screen layout.

**Migration:** finish or discard any active project session, click the project's
Webkit update button, and start a fresh color session. No configuration changes
are required.

## v0.8.10 (2026-08-22): Reliable agent and review lifecycle

- Initialized every colored agent with hidden, read-only Webkit context as soon
  as the session starts, including visual-system and full-scene coherence
  guidance, while keeping protocol and merge instructions out of user chat.
- Collapsed thinking by default with an animated live indicator and replaced
  the merge protocol wall with a short user-side progress message.
- Kept the review pill on one line until it reaches a 20px screen margin,
  simplified the variant control, and made the current feedback rectangle stay
  visually above overlapping points.
- Added canonical variant generation to Redo with end-to-end protocol
  validation and agent instructions.
- Made tab activity follow authoritative agent state and added external-browser
  cleanup when a finished session releases its preview.

**Migration:** finish or discard any active project session, click the project's
Webkit update button, and start a fresh color session. No configuration changes
are required.

## v0.8.9 (2026-08-22): Deliberate review feedback

- Made Done save new review-phase feedback locally without sending it, leaving
  the Add or Send control pill as the only way to involve the agent.
- Kept locally saved points editable and deletable until the user explicitly
  sends them.
- Opened ready reviews automatically after refreshing the completed website,
  removing the redundant Start review confirmation toast.

**Migration:** finish or discard any active project session, click the project's
Webkit update button, and start a fresh color session. No configuration changes
are required.

## v0.8.8 (2026-08-22): Reliable pending feedback

- Made saved pending points directly editable with an atomic revision check, so
  stale agent work cannot overwrite a correction or be accepted as current.
- Anchored each rectangle independently to a close-fitting page element and
  ignored oversized backgrounds that caused marks to drift after reloads.
- Added colored Working and Review ready tab-title animations, including a
  throttled background watch that detects completed agent work in hidden tabs.
- Kept active review controls safely waiting while newly added or edited
  feedback is being processed.

**Migration:** finish or discard any active project session, click the project's
Webkit update button, and start a fresh color session. No configuration changes
are required.

## v0.8.7 (2026-08-22): Visible active-review additions

- Kept feedback points and rectangles visible when they are added while an
  earlier review is still open, including the brief interval before the next
  server-state poll.
- Added a pending-count chip to the review controls so saved additions remain
  accounted for without being mistaken for points that are already reviewable.
- Deduplicated server, optimistic, and locally queued copies of the same point
  and cleared stale hidden send-button content after successful submission.

**Migration:** finish or discard any active project session, click the project's
Webkit update button, and start a fresh color session. No configuration changes
are required.

## v0.8.6 (2026-08-22): Reliable direct preview startup

- Added a tagged `claim-color.sh --session` result that distinguishes a reused
  live server from a fresh color claim without relying on an ambiguous second
  discovery call.
- Updated the direct setup flow to launch fresh claims and reuse verified live
  servers from the same atomic decision.

**Migration:** finish or discard any active project session, click the project's
Webkit update button, and start a fresh color session. No configuration changes
are required.

## v0.8.5 (2026-08-22): Stable feedback editing

- Kept the four edit-point footer actions inside the bounded feedback card by
  giving the row an explicit responsive grid and compact button padding.
- Confirmed that website key handlers are isolated by the v0.8.4 Shadow DOM
  event boundary. Existing sessions must update their vendored Webkit before
  they can receive that behavior.

**Migration:** finish or discard any active project session, click the project's
Webkit update button, and start a fresh color session. No configuration changes
are required.

## v0.8.4 (2026-08-22): Reliable live feedback

- Made sandboxed round-transition queue signals tolerate the known macOS
  `fsync` permission boundary while preserving fatal handling for real I/O
  failures. The Control Center now consumes queued transitions immediately
  after the agent exits and refuses to report a false active state when the
  feedback protocol did not advance.
- Added a per-project Webkit update action for already registered sites and
  blocked new color sessions from starting on an outdated vendored kit.
- Collapsed completed agent activity into a View thinking control with elapsed
  time while keeping the final response visible.
- Refreshed the Control Center-owned preview tab after every successful website
  chat turn so project changes cannot remain hidden behind a stale page.
- Restored submitted feedback points and rectangles from the active server
  batch after reload, alongside the existing durable unsent-draft storage.
- Stopped keyboard, composition, pointer, and input events from leaking out of
  Webkit controls into host-page interaction handlers.
- Installed the macOS desktop launcher as a native app bundle with an AWESOME
  WEBKIT icon matching the Control Center brand mark.

**Migration:** restart the Control Center and update existing projects when
prompted. No configuration changes are required.

## v0.8.3 (2026-08-21): Automatic preview cleanup

- Closed each Control Center-owned preview tab automatically after its session
  reaches a successful merge, seed finalization, or discard.
- Added an explicit close mode to the external preview launcher while keeping
  manual cleanup instructions visible on platforms without tab automation.

**Migration:** restart the Control Center and update existing projects when
prompted. No configuration changes are required.

## v0.8.2 (2026-08-21): Reliable agent completion

- Routed background-agent round transitions through an atomic request file so
  the trusted Control Center performs the localhost preview call. Sandboxed
  agents can now finish feedback, redo, and completion transitions without
  broad network access or a permanently stuck processing overlay.
- Preserved rejected transition requests for diagnosis, bounded and validated
  request and response data, retained private transition capabilities, and
  kept preview-server archive validation and idempotent receipts authoritative.

**Migration:** finish or discard active Control Center sessions, update the
vendored Webkit through the Control Center when prompted, and restart the
Control Center. No configuration changes are required.

## v0.8.1 (2026-08-19): Publish hardening

- Protected preview mutations with per-session credentials, strict JSON media
  types, origin checks, bounded request bodies, and safer host validation.
- Confined static and git-backed preview reads to their intended roots, made
  round transitions atomic, and hardened symlink, partial-write, and stale-state
  handling across the feedback protocol.
- Added machine-wide preview-port reservations and same-owner session reuse so
  separate projects, worktrees, and Control Center sessions cannot silently
  collide or claim duplicate colors.
- Hardened Control Center startup, shutdown, event logging, process ownership,
  provider prompts, target-branch merges, and recovery after interrupted work.
- Required Git 2.30 or newer and made the Control Center distinguish an
  outdated Git installation from a missing one.
- Made existing-project finalization explicitly fast-forward the validated
  local target branch or worktree from its managed checkout. Dirty, diverged,
  in-progress, or concurrently changed targets stop safely while managed and
  session work remain available for recovery.
- Let existing repositories open in an isolated managed checkout even when the
  local target has tracked or untracked changes. The source stays untouched and
  integration waits until its checkout is clean.
- Made old Webkit versions actionable in the Add Project dialog. The Control
  Center can now replace the vendored payload in its isolated checkout,
  preserve `webkit.config.json`, validate and commit the update, and then add
  the project without disturbing dirty source-checkout changes.
- Made divergent GitHub push failures persistent and actionable. The Control
  Center can launch a neutral issue agent in its own worktree, keep the alert
  visible across refreshes, and use the existing chat to ask for human
  decisions before safely integrating and retrying the push.
- Styled the persistent issue action as a circular Control Center control and
  made issue agents start with high reasoning by default.
- Safely migrate current-user-owned color registries and lock records from the
  older shared permission model to private `0700` and `0600` modes.
- Rejected risky reference uploads and high-confidence secrets before new-site
  commits or GitHub creation, with clearer privacy boundaries for references,
  browser dictation, voice notes, and the optional API proxy.
- Fixed overlay modal recovery, queued-feedback races, accessible keyboard and
  focus behavior, reduced-motion handling, and retry-safe voice-note cleanup.
- Tightened the Control Center phone header at 480px and below and raised muted
  text contrast to meet WCAG AA against every Control Center surface.
- Added preview-only CSP meta adaptation for the injected overlay. Unsafe or
  malformed policies return HTTP 422 before token-bearing HTML is served;
  HTTP-header CSP remains the responsibility of its upstream server or proxy.
- Added tracked-only installation and update guardrails, non-overwriting desktop
  shortcuts, scoped Git staging, private runtime files, cross-platform launchers,
  and stricter release checks.
- Made `.webkit/` and custom feedback roots fail-closed private runtime paths:
  they must be dedicated, non-root, Git-ignored, untracked, and unstaged. The
  Control Center excludes them from staging and rejects session history that
  ever committed private runtime data.
- Made existing-project installation transactional with per-path rollback,
  unborn-repository refusal, and preservation of concurrent user changes. New
  projects are locally committed and registered before any GitHub request.
- Added verified GitHub create and push results. Network or verification
  failures preserve the local project, commit, merged work, and retry path.
- Restricted preview `bind_host` to `127.0.0.1`, `localhost`, or `0.0.0.0`.
  LAN mode requires exact hostname or IPv4 Host values and now documents that
  Host validation prevents DNS rebinding but does not authenticate clients.
- Added fail-closed storage ceilings: 20 MiB of chat attachments per message;
  100 MiB, 2,000 files, and 512 directories per session; 8 MiB of Control
  Center state; 50 retained terminal sessions; 4 MiB event logs retaining
  about 3 MiB; and feedback history capped at 200 rounds, 128 MiB, and 4,096
  scanned entries without automatic receipt pruning.
- Bounded voice storage at 25 MiB per request and, per color, 100 MiB, 1,000
  persistent uploads, and 1,024 scanned entries, with prospective write slots,
  fail-closed unknown entries, and a one-hour orphan grace period. Bounded live
  and Git-backed BEFORE transformation at 8 MiB of source HTML, 16 MiB of
  transformed HTML, and 32 MiB of transformed BEFORE CSS, with a 15-second
  accepted-client I/O timeout.
- Expanded the automated suite across Linux, native Windows, and macOS, with
  supported-runtime coverage on Python 3.7 and Python 3.14 plus Bash 3.2 checks.

**Migration:** finish or discard active Control Center sessions and stop every
direct preview before refreshing the complete vendored `webkit/` payload and
matching Claude Code skills through `webkit/UPDATE-KIT.md`. Preserve the
project's `webkit/webkit.config.json`. `feedback_dir` must now be a dedicated
repository-relative directory other than `.`, and both it and `.webkit/` must
be Git-ignored, untracked, and unstaged. For a custom feedback root, add and
verify the new ignore rule before changing config; move complete color inboxes
only while the preview is stopped, keep the old root ignored until its runtime
data is archived, and never migrate during a live round. Existing history over
the new ceiling must be backed up and reduced with the stopped-server procedure
in `webkit/LOOP.md`, preserving every current-batch receipt. A LAN preview must
use `"bind_host": "0.0.0.0"` plus a non-empty exact-hostname or IPv4
`allowed_hosts` list; specific-interface and IPv6 bind values are no longer
accepted. Restart after the update to acquire fresh reservation and session
credentials. No other configuration key migration is required.

## v0.8.0 (2026-08-18): Seeded project onboarding

- Added optional new-project brand briefs and reference-file uploads.
- Added configurable multi-direction seed generation (10 seeds by default) in
  an isolated agent worktree.
- Added in-app seed previews, multi-selection, and instructions for combining
  parts from several directions into one production website.
- Added automatic seed finalization, merge, cleanup, and GitHub sync.
- Added an automatic one-click GitHub push action whenever local main is ahead.
- Refined the operational overlay into a monochrome system, with draggable
  feedback cards and editable multi-rectangle feedback points.

## v0.7.0 (2026-08-18): Custom preview shortcuts

- The Control Center now lets each user record any non-modifier key for opening
  the feedback tool and starting or stopping voice input.
- All three website controls, open/close, voice, and Alt/Option, stay visible
  in the Control Center top bar and are explained together in Settings.
- Shortcut preferences persist locally, flow into new project configs, and
  restart active previews with environment overrides.
- Removed the redundant bottom-left **Add or create** button. Project creation
  remains available from the clear top-bar **+ Project** action; the website
  overlay remains keyboard-only with no corner launcher.

## v0.6.1 (2026-08-18): Click-first website interaction

- Feedback mode now leaves the website directly clickable by default. Hold
  Alt/Option while dragging to draw a feedback rectangle.
- Added a Control Center setting for the legacy draw-first behavior, where
  Alt/Option temporarily passes clicks through to the website.
- The interaction preference is injected into every active preview, persisted
  locally, and defaults safely for existing state and older project configs.

## v0.6.0 (2026-08-18): Point-scoped review and private agent voice notes

- Before/after now defaults to the current feedback target. A compact arrow
  button beside the toggle switches to whole-website comparison and back.
- Added Control Center Settings with browser speech-to-text as the default and
  an optional Agent voice-note mode.
- Voice-note mode records and stores the original audio per feedback point;
  background agents transcribe it with the bundled local-Whisper bridge. The UI
  verifies that a local engine is available and never silently uploads audio.
- Added voice-note upload validation, size limits, schema documentation, and
  English/Hebrew language hints for local transcription.

## v0.5.1 (2026-08-18): Bilingual dictation and typing-safe hotkeys

- `C` and `V` now remain ordinary characters while any text field is focused,
  including an empty feedback note and the redo input. Their overlay and
  dictation shortcuts still work everywhere outside editable fields.
- Added a persistent English/עברית selector beside every microphone. It sets
  `SpeechRecognition.lang` to `en-US` or `he-IL` before recognition starts and
  restarts an active recognizer when the language changes.
- Updated the overlay hints and configuration documentation to describe the
  editable-field shortcut guard.

## v0.5.0 (2026-08-18): Local Control Center

- Added a token-protected localhost Control Center with first-run
  Codex/Claude Code selection, existing-project onboarding, and starter-site
  creation.
- Added five clickable color sessions backed by isolated Git worktrees,
  resumable local CLI conversations, live chat output, and automatic browser
  feedback/verdict wakeups.
- Added controller-owned **Merge to main** and confirmed **Discard work**
  actions so background agents never operate directly on `main`.
- Added a cross-platform launcher and desktop-shortcut installer; local Git is
  required, while GitHub publication remains optional.
- Added `webkit/CONTROL-CENTER.md` as the finite, event-driven companion to the
  existing `SETUP.md`/`LOOP.md` workflow. Existing Codex and Claude app usage
  remains backward compatible.

## v0.4.1 (2026-08-18)

- **Codex feedback waiting is now explicit and durable.** Codex agents keep the
  active turn open and poll the yielded waiter session in bounded chunks, so a
  submitted feedback batch is processed immediately instead of being noticed
  by an orphaned terminal process after the task has already returned.
- Setup now warns that opening the preview is not a terminal state for Codex.

**Migration:** refresh the vendored Webkit files. No config or runtime migration
is required.

## v0.4.0 (2026-08-18)

- **External-browser routing is explicit.** `webkit/scripts/open-preview.sh`
  is now documented as the sole browser-selection path. The configured
  `browser.app_name` is the user's real desktop browser (Google Chrome by
  default), never an agent-hosted in-app browser, preview pane, or webview.
- The installed `AGENTS.md` and `CLAUDE.md` pointer now blocks generic browser
  tools, the Codex/ChatGPT in-app browser, Claude preview panes, IDE webviews,
  and other embedded surfaces unless the user explicitly overrides the rule.
- The Claude `webkit-setup` skill now triggers for ordinary launch, open,
  preview, show, and browser-test requests, not only explicit Webkit setup
  phrasing.

**Migration:** refresh the vendored Webkit files, re-copy the updated
`webkit-setup` skill into `.claude/skills/`, and update existing `AGENTS.md`
and `CLAUDE.md` Webkit sections with the v0.4 pointer wording. No config or
runtime migration is required.

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
  gone, so nothing of the kit sits on the page at rest. The old aliases
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
  for the point under review, leaving the action-bar chip, now a real button
  showing the current letter, the letter run and a cycle affordance.
- **Point numbers restart at #1** once a batch is finished and nothing is
  queued, instead of climbing forever. Numbering still continues within a batch.
- Keyboard shortcuts are matched by `KeyboardEvent.code`, so they work on
  non-US layouts (Hebrew, Arabic, …).
- Hardening: `--end-of-options` and a SHA format check before `git show`; the
  server binds `127.0.0.1` only; `site_root` is honoured as the document root;
  RTL host pages no longer mirror the overlay; touch/coarse pointers supported.

## v0.1.0 (2026-07-25): initial release

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
