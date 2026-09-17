# Webkit Control Center agent mode

This file applies only when the environment contains `WK_CONTROL_CENTER=1`.
The user is working through the local AWESOME WEBKIT browser interface instead
of an agent app or terminal.

The Control Center already owns:

- the selected color, project color lock, and linked private same-user global
  TCP port reservation;
- this color's isolated Git worktree and branch;
- the preview-server process and open website tab;
- watching `feedback.json` and `verdicts.json`;
- invoking or resuming this CLI conversation when work arrives;
- integrating the branch into the managed checkout or discarding it when the user clicks the
  corresponding button;
- closing the preview window it opened after a successful merge, seed
  finalization, or discard.

The Control Center UI itself binds only to `127.0.0.1`. Preview LAN mode, when
explicitly configured, follows the `bind_host` and `allowed_hosts` contract in
`webkit/SETUP.md`. The Host allowlist prevents DNS rebinding; it does not
authenticate LAN clients. The same-account local process and coding agent are
trusted actors, as described in the root README.

The Control Center requires Git 2.30 or newer. Its preview server adapts
supported HTML CSP meta tags only in generated preview responses so its overlay
can run without editing project source. It returns HTTP 422 with JSON only when
such a tag cannot be transformed safely. An upstream HTTP response header can
still block the overlay and must be configured separately. Preview acceptance
is not a production CSP validation or a security audit.

## One invocation, one finite transition

When the controller invokes you because browser feedback changed, read
`webkit/LOOP.md`, read the configured `feedback_dir`, and inspect
`<feedback_dir>/<slug>/`.

Use `python3 webkit/scripts/read-feedback.py` on the live feedback or verdict
file, then read every actionable id separately with `--point <point-id>` before
editing or claiming instructions are missing. Bulk JSON output may omit saved
text. Empty marked space is a valid layout target; follow LOOP's source and
neighbor lookup before asking the user to draw or type again.

- `feedback.json` without `review.json`: process the batch through LOOP step 6,
  including local transcription of any `voiceNote`, one commit per point, and
  an atomic `review.json`, then **exit**.
- `verdicts.json`: process the verdicts and the round transition through LOOP
  step 9, including any `redoVoiceNote`, then **exit**. If a redo produces
  another review, publish it and exit.
- In this mode, `transition-round.py` publishes an atomic transition request
  for the trusted controller. The controller owns the localhost call to the
  preview server. When the helper reports `queued: true`, exit normally. Do
  not retry the localhost endpoint yourself or move protocol files by hand.
- Do **not** run `wait-for-file.sh`. The controller watches and will resume you
  for the next state transition.
- Do **not** claim/release a color or port, start/stop a preview server, create/remove a
  worktree, merge to any target branch, or discard the branch.
- Do not run `webkit/scripts/open-preview.sh` in this mode. The Control Center
  owns the existing preview tab, and its overlay discovers new review rounds by
  polling. Never open an embedded agent browser or a second preview tab.
- Never stage, force-add, commit, move, or delete `.webkit/` or the configured
  feedback root. Both are private runtime directories that must stay
  Git-ignored, untracked, and unstaged. The controller excludes them from its
  own staging and rejects integration if any provider-created session commit
  touched a private runtime root, even when a later commit deleted the path.

For a direct message from the Control Center chat, do the requested repository
work normally. Commit any file changes before exiting so the worktree remains
mergeable; use a concise message beginning `wk(<slug>): chat:`. If the message
only asks a question, make no commit.

One chat message accepts at most 20 attachments and 20 MiB combined. Retained
attachment storage is capped at 100 MiB, 2,000 regular files, and 512
directories per session, including the prospective message. Storage is
rescanned on each message, and symbolic links or special files are rejected.

For a new-project seed generation or seed finalization job, follow the explicit
onboarding prompt supplied by the controller. Seeds stay in the controller's
isolated worktree until the user chooses a direction. Do not merge, push, or
remove the worktree; the controller performs that lifecycle after the final
ready marker.

The ordinary `SETUP.md` + `LOOP.md` workflow remains canonical when
`WK_CONTROL_CENTER` is absent. Nothing in this file changes agent-app usage.

## Integration and retained state

For an existing repository, the controller first integrates a finished color
branch into its managed checkout. It then validates and fast-forwards the
detected local target branch and that branch's checked-out worktree, if any. A
dirty target, an in-progress Git operation, divergent history, or a target that
changes during validation stops finalization. The managed result and color
session worktree remain available so the user can resolve the source target and
retry without rebuilding the work.

That verified local target integration completes before any optional GitHub
push. GitHub creation, push, and remote-ref verification report a structured
failure without deleting or rolling back the local project, commit, managed
result, or integrated target. The user can retry the push from that local
state. Never claim that publication succeeded unless the controller reports
the verified result.

Control Center state is capped at 8 MiB and retains at most 50 merged or
discarded sessions. Per-session event logs are capped at 4 MiB and retain about
the newest 3 MiB when compacted. Feedback history and voice-note ceilings, plus
their stopped-server recovery procedure, are documented in `webkit/LOOP.md`.
