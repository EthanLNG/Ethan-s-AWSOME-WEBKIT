# Webkit Control Center agent mode

This file applies only when the environment contains `WK_CONTROL_CENTER=1`.
The user is working through the local AWESOME WEBKIT browser interface instead
of an agent app or terminal.

The Control Center already owns:

- the selected color and its machine-global lock;
- this color's isolated Git worktree and branch;
- the preview-server process and open website tab;
- watching `feedback.json` and `verdicts.json`;
- invoking or resuming this CLI conversation when work arrives;
- merging the branch to `main` or discarding it when the user clicks the
  corresponding button.

## One invocation, one finite transition

When the controller invokes you because browser feedback changed, read
`webkit/LOOP.md` and inspect `.webkit/feedback/<slug>/`.

- `feedback.json` without `review.json`: process the batch through LOOP step 6,
  including one commit per point and an atomic `review.json`, then **exit**.
- `verdicts.json`: process the verdicts and the round transition through LOOP
  step 9, then **exit**. If a redo produces another review, publish it and exit.
- Do **not** run `wait-for-file.sh`. The controller watches and will resume you
  for the next state transition.
- Do **not** claim/release a color, start/stop a preview server, create/remove a
  worktree, merge to `main`, or discard the branch.
- Reopen review URLs only through `webkit/scripts/open-preview.sh`, as LOOP.md
  requires. Never open an embedded agent browser.

For a direct message from the Control Center chat, do the requested repository
work normally. Commit any file changes before exiting so the worktree remains
mergeable; use a concise message beginning `wk(<slug>): chat —`. If the message
only asks a question, make no commit.

The ordinary `SETUP.md` + `LOOP.md` workflow remains canonical when
`WK_CONTROL_CENTER` is absent. Nothing in this file changes agent-app usage.
