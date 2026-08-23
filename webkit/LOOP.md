# webkit/LOOP.md: the feedback-round protocol

**Re-read this file at the top of every round.** Long sessions push early
instructions out of your working context; the loop only works if every round is
run against the letter of this contract, not your memory of it.

This is the protocol between you (the agent), the preview server, and the
in-browser overlay. The user draws rectangles on the live site and describes
changes; the server writes them to your inbox; you apply them and publish a
manifest; the user reviews with a before/after toggle and sends verdicts; you
process verdicts and either run another round or close the batch.

## Ground rules

- **Use Git 2.30 or newer.** The review snapshots and guarded history operations
  are not supported on older Git releases.
- **Never write `feedback.json`, `verdicts.json`, or `history/` yourself.**
  They are server-owned. The server writes browser submissions and performs
  every archive transition as one locked transaction. If you fabricate or
  move those files, phase detection turns into fiction and a concurrent POST
  can be lost.
- **Write only the initial live `review.json`.** For a continuing round, build
  the next review as a separate JSON file and pass it to
  `webkit/scripts/transition-round.py`. The server installs it atomically with
  the archive and matching `feedback.json` round update.
- **Keep the working tree committed at round boundaries.** The before/after
  toggle serves files out of `git show`; uncommitted work is invisible to it
  and gets mixed into the *next* round's diff, corrupting the story.
  BEFORE swaps **in place** (no page reload): the overlay lifts the reviewed
  point's container out of the snapshot document and repoints the page's
  stylesheets at their `/__wk/before/` equivalents, so a point whose change
  lives entirely in CSS still shows a real difference. It falls back to a full
  navigation (with a toast saying why) when the container can't be matched in
  both documents. This is one more reason the point's primary element should sit
  inside a stable `section`/`article`/`[id]`.
- **One git commit per point.** The verdict loop reverts and redoes at
  point granularity; a commit spanning two points makes `delete` on one of
  them impossible to do cleanly.
- **Treat preview acceptance as visual and workflow evidence only.** The server
  may adapt an HTML CSP meta tag in its generated preview response so the
  overlay can run, without changing project source. It cannot rewrite a CSP
  supplied by an upstream HTTP response header. The loop does not validate
  production headers, authentication, backend behavior, third-party scripts,
  or other security controls, so it is not a security audit.

## Paths and identity

Your inbox is `<feedback_dir>/<slug>/` **relative to the git root**. With the
default config, it is `.webkit/feedback/<slug>/` at the repo root. `<slug>` is
your claimed color's slug from the palette in `webkit/webkit.config.json` (e.g.
`🔵` → `blue`). Below, `<slug>` always means that. The inbox is anchored at the
git root (not `site_root`) because BEFORE mode reads round history out of git;
even when the served site is a subdirectory, the inbox stays at the repo root.
At session start, keep the config value and build one pinned inbox path:

```sh
set -eu

feedback_dir="$(webkit/scripts/config-get.sh get feedback_dir)" || exit 1
case "$feedback_dir" in ''|'.'|-*|/*|*$'\n'*|*$'\r'*) exit 1 ;; esac
case "/$feedback_dir/" in */../*|*/./*) exit 1 ;; esac
test -n "${slug:-}" || exit 1
inbox="$feedback_dir/$slug"
```

Do not change `feedback_dir` during a live session. Run every `"$inbox/..."`
command below from the Git root. The server creates the directory at boot. Your
preview URLs are `http://localhost:<port><page>` where `<port>` is
your color's port and `<page>` is a root-absolute path like `/index.html`.

The feedback root is private runtime state. It must be a dedicated non-root
directory, untracked and unstaged, and covered by Git ignore rules. The same is
always true of `.webkit/`. Never force-add either root. The Control Center
excludes both from staging and rejects a session or integration if the current
tree or any provider-created session commit contains private runtime data.

The inbox holds at most these live files, and their presence *is* the state
machine (the overlay polls it as `phase`):

| Files present | Phase | Meaning |
|---|---|---|
| none | `collecting` | user is drawing points |
| `feedback.json` | `awaiting_agent` | a batch is waiting for you |
| `feedback.json` + matching `feedback_update` marker | `awaiting_agent` | points arrived before the first review was published |
| `feedback.json` + matching `review.json` | `reviewing` | user is walking your work |
| all three matching files | `verdicts_sent` | verdicts or a feedback interruption are waiting |
| any other combination | `transitioning` | a transition is in progress or needs recovery; do not submit or apply a new batch |

That is why the archive choreography at the end of this file is strict: a file
existing at the wrong moment is a wrong phase.

## Step 1: WAIT for feedback

```sh
webkit/scripts/wait-for-file.sh "$inbox/feedback.json" 3600
```

Exit `0`: the file exists and parses as JSON; proceed to step 2.
Exit `124`: timeout, file absent; just run it again (loop here indefinitely
until the user ends the session).
Exit `65`: the file exists but never parsed as JSON (truncated / corrupt /
hand-edited). Do **not** re-wait; you would hang forever. Surface the stderr
message to the user (the batch didn't arrive intact; ask them to re-send or
fix/delete `"$inbox/feedback.json"`), then wait again.

- **Claude Code idiom:** run it as a background Bash task
  (`run_in_background`), so the harness re-invokes you when it exits. You
  don't burn your turn polling. On `124`, start it again in the background.
- **Codex idiom:** keep the current agent turn alive until the waiter exits.
  Start the command in the foreground. If the shell tool yields a live session
  id, wait on that **same session** for the full 60-minute idle window when the
  client supports it; do not repeatedly poll the agent once per minute. The
  file watcher itself still exits immediately when feedback arrives. Do not
  send a final response while it is still running. A detached waiter can notice
  `feedback.json`, but its exit does not wake a Codex task after the task has
  already returned a final response. Exit `0` → read the batch immediately;
  exit `124` → start a fresh waiter and remain in this step. The user can still
  send a message while the turn is waiting; handle it normally without losing
  the waiter state.

The user may add or edit pending rectangles while a review is open. The server
folds them into the standing `feedback.json` and writes a typed `verdicts.json`
interruption with `kind:"feedback_update"`. This wakes the same waiter
immediately; handle it as described in step 8 instead of treating it as user
verdicts.

## Step 2: READ the batch

Read `"$inbox/feedback.json"`. Full schema:

```jsonc
{
  "version": 1,
  "kind": "feedback",
  "batchId": "b-20260725-1412-k3f9",   // b-<yyyymmdd>-<hhmm>-<rand4>; stable for the whole batch
  "round": 1,                           // 1-based; the server bumps it with each continuing transition
  "color": "blue",                      // your color's SLUG (matches the inbox dir name);
                                        // the overlay sends the slug, not the emoji. The
                                        // emoji appears only as `emoji` in /__wk/state.
  "sessionId": "…",                     // overlay session; opaque, echo nowhere, ignore
  "createdAt": "2026-07-25T14:12:03Z",
  "updatedAt": "2026-07-25T14:15:41Z",  // server bumps on merges while unclaimed
  "pages": ["/index.html"],             // every page that has points, root-absolute
  "points": [
    {
      "id": "opaque-id",     // OPAQUE unique string, assigned by the overlay. Never parse
                            // or invent ids; quote them verbatim everywhere.
      "number": 1,          // user-visible ordering; apply points in this order
      "revision": 2,        // absent means 1; server increments after a pending edit
      "page": "/index.html",
      "createdAt": "2026-07-25T14:12:03Z",
      "rect": { "x": 120, "y": 2260, "w": 300, "h": 84 },  // DOCUMENT coords, CSS px
      "rects": [             // every marked area for this point; rect is the last/primary one
        { "x": 120, "y": 2260, "w": 300, "h": 84 }
      ],
      "rectContexts": [      // one context array per rect, used for stable re-anchoring
        [{ "selector": "#hero .cta-row > a:nth-of-type(1)", "tag": "a",
           "text": "Get the kit", "box": { "x": 122, "y": 2262, "w": 180, "h": 52 },
           "role": "primary" }]
      ],
      "viewport": { "w": 1440, "h": 900, "dpr": 2 },
      "scroll": { "x": 0, "y": 1980 },
      "context": [          // ≤ 12 entries; [0] is the primary element under the rect
        {
          "selector": "#hero .cta-row > a:nth-of-type(1)",  // uniqueness-verified at capture
          "tag": "a",
          "text": "Get the kit",       // trimmed to ≤ 120 chars
          "box": { "x": 122, "y": 2262, "w": 180, "h": 52 },
          "role": "primary"            // "primary" | "intersecting"
        }
      ],
      "uiState": {          // null on the normal page; captured for top-layer UI
        "surfaces": [       // review reopens the same dialog/popover automatically
          { "kind": "dialog", "selector": "#settingsDialog" }
        ]
      },
      "abcState": null,     // snapshot of window.__abc at capture; null when no experiments
                            // live, else a map keyed by scope id, e.g.
                            //   { "cta": { "current": "B", "letters": "AB" } }
                            // Informational: it tells you what the user was LOOKING AT.
      "text": "make this button bigger and bolder",
      "voiceNote": null,    // or {"path":"<feedback_dir>/blue/voice-notes/voice-id.webm",
                            //     "mimeType":"audio/webm", "bytes":12345,
                            //     "durationMs":4200, "language":"en",
                            //     "transcription":"openai"}; text may be empty;
                            //     transcription is omitted for local Whisper
      "abcRequest": null,   // see below
      "status": "new"       // the overlay ALWAYS writes "new". "redo" only exists if
                            // YOU set it when re-queuing a point into a later round
                            // (step 9); the server never rewrites points, so a fresh
                            // batch is always all-"new".
    }
  ]
}
```

`abcRequest` being non-null means the user wants lettered variants for this point
instead of a single change:

- `{"mode": "model", "count": 4}`: **you** invent `count` genuinely different
  takes (vary the mechanism, such as scale, placement, style, or motion, not
  micro-jitters of one idea).
- `{"mode": "user", "prompts": {"A": "solid blue", "B": "outline only"}}`:
  build **exactly one variant per prompt letter**, each doing what its prompt
  says, nothing invented beyond it.

Use `context[]` to find the target: try `context[0].selector` first; if the
DOM shifted since capture, fall back to the rect + the other context entries.
`rectSurfaces`, when present, is overlay-only metadata parallel to `rects`: one
state fingerprint, anchor, and capture scroll per rectangle. Preserve it when
copying point objects, but ignore it when implementing the requested change.
The user's `text` is the instruction; the geometry is only there to tell you
*where*. A non-null `uiState` means the point was drawn inside a dialog or
popover. Edit the selected elements normally; the overlay reopens those
surfaces automatically during review so the user sees the same UI state.
If `voiceNote` is non-null, transcribe it before interpreting the request. Use
the engine selected by the saved note:

```sh
# When voiceNote.transcription is "openai":
python3 webkit/scripts/transcribe-voice-note.py <voiceNote.path> --language <voiceNote.language> --engine openai

# Otherwise:
python3 webkit/scripts/transcribe-voice-note.py <voiceNote.path> --language <voiceNote.language> --engine local
```

Treat that output as the point's instruction (combined with any typed `text`).
Do not skip, guess, or claim to have heard the recording if the helper reports
that the selected transcription engine is unavailable; report the concrete
setup error instead. OpenAI transcription requires `OPENAI_API_KEY`. The helper
never stores that key or includes it in its output.
After a successful transcription, leave the source audio file in place. The
preview server owns recording cleanup. It retains a note while any live
feedback, review, or verdict file references it, then removes it only after the
relevant round and its transition receipt have been durably archived.
A voice-note request is limited to 25 MiB. Each color's voice-note directory is
limited to 100 MiB, 1,000 persistent upload files, and 1,024 total scanned
entries. Upload admission reserves two slots for its temporary file and final
no-clobber link. Unknown entries, symbolic links, and special files fail closed.
Unreferenced recordings become eligible for cleanup after a one-hour orphan
grace period. A limit error is not permission to delete a recording that a live
protocol file still references.

## Step 3: BEFORE REF

Record the clean pre-round state so the overlay's BEFORE toggle has something
to serve. The installer and the Control Center create a dedicated setup commit,
so there must be no pending source changes here:

```sh
set -eu

wk_round_status="$(git status --porcelain --untracked-files=all)" || exit 1
test -z "$wk_round_status" || {
  echo "Refusing to start a round with unrelated worktree changes." >&2
  exit 1
}
beforeRef="$(git rev-parse --verify 'HEAD^{commit}')" || exit 1
test -n "$beforeRef" || exit 1
```

If the clean-tree check fails, stop and show the user the paths from
`git status --short`. Do not stage, stash, discard, or commit them as part of a
WebKit round. Resume only after the user has preserved or resolved that work.

On round 1 this is the pre-batch state. On later rounds (redos), **keep the
batch's original `beforeRef`**. The user is always comparing against how the
site looked before the batch started, not against the rejected attempt.

## Step 4: APPLY each point

Work through `points[]` in `number` order. Per point:

1. Make the change the point's `text` asks for. (On a redo round, a point you
   re-queued carries `status:"redo"`, one *you* set in step 9, never the
   overlay. The instruction to follow is that point's `redoText` from last
   round's `verdicts.json`, not its original `text`.)
2. Stage only the files changed for this point, inspect the staged diff, and
   commit it. Never use `git add -A` here. Confirm the worktree is clean after
   the commit before starting the next point. Use **one commit per point** with
   this message format:

   ```
   wk(<slug>): r<round> p<id>: <short summary>
   ```

   e.g. `wk(blue): r1 pp-mrzhxkfa-9f2q: enlarge hero CTA, weight 700`. `p<id>`
   is the letter `p` immediately followed by the point's `id` verbatim, and
   since real ids are `p-<base36-timestamp>-<rand4>` (assigned by the overlay,
   never the batch id), the token genuinely begins `pp-`. That doubled `p` is
   correct, not a typo; never truncate or re-derive the id.
3. `abcRequest` points: **use the abc skill** (`webkit/skills/abc/SKILL.md`,
   the canonical lettered-variants procedure: scope element, `ABC:` marker
   fences, the switcher widget). Model mode → invent `count` takes; user mode →
   one per prompt letter, letters = the prompt keys. Record the experiment's
   `{scopeId, letters}`. You need them for the manifest and for finalize.
   Still one commit for the whole point (the experiment is one unit of work).
4. A point you cannot or should not do (contradicts another point, needs an
   asset you don't have, is a question rather than a change) → make no change,
   mark it `skipped` in the manifest with a `note` that answers/explains.

## Step 5: MANIFEST (`review.json`)

Write `"$inbox/review.json"`. Full schema:

```jsonc
{
  "version": 1,
  "kind": "review",
  "batchId": "b-20260725-1412-k3f9",   // MUST equal the feedback batchId
  "round": 1,                           // this round's number
  "beforeRef": "<full SHA from step 3>",
  "createdAt": "2026-07-25T14:21:00Z",
  "points": [
    {
      "id": "opaque-id",                // the point id, verbatim
      "feedbackRevision": 2,             // copy point.revision, or 1 when absent
      "handled": "done",                // "done" | "abc" | "skipped"
      "note": "CTA now 1.25rem / 700",  // one line, shown to the user in the review bar
      "commit": "<full SHA of this point's commit>",   // omit/null for skipped
      "abc": { "scopeId": "cta", "letters": "ABCD" }   // ONLY when handled = "abc"
    }
  ]
}
```

For the first review only, **write it atomically**. Create an exclusive private
temporary file in the same directory, write the complete document, and move it
over the final name. The subshell's exit trap removes the temporary file after
any write or move failure:

```sh
(
  set -eu
  umask 077
  test ! -e "$inbox/review.json" && test ! -L "$inbox/review.json" || {
    printf '%s\n' "Refusing to replace an existing live review." >&2
    exit 1
  }
  wk_review_tmp="$(mktemp "$inbox/.review.json.XXXXXX")" || exit 1
  trap 'test -z "$wk_review_tmp" || rm -f -- "$wk_review_tmp"' EXIT
  trap 'exit 1' HUP INT TERM
  printf '%s' "$json" > "$wk_review_tmp" || exit 1
  mv -- "$wk_review_tmp" "$inbox/review.json" || exit 1
  wk_review_tmp=""
)
```

The overlay polls every 2 s; a half-written JSON is a parse error at exactly
the wrong moment. `mktemp` creates the file without following a predictable
leftover path, and the `mv` stays on one filesystem. The poll sees either no
review or the complete new file, never a torn one.

## Step 6: SHOW

Reopen the preview jumped into review mode, on the first pending point's page:

```sh
webkit/scripts/open-preview.sh "http://localhost:<port><first-page>?wk-review=<batchId>"
```

`<first-page>` = the `page` of the lowest-`number` point in this round. If the
page path already carries a query string, append with `&` instead of `?`. The
overlay sees `wk-review`, enters review mode, and jumps the user to point 1.

## Step 7: WAIT for verdicts

```sh
webkit/scripts/wait-for-file.sh "$inbox/verdicts.json" 3600
```

Same idioms and exit codes as step 1 (background + re-invoke on Claude Code;
foreground + re-run on `124` on Codex). On `65` (verdicts.json present but
unparsable), do not re-wait. Surface the message and ask the user to re-send or
fix/delete the file. While waiting, do nothing to the working tree.

## Step 8: PROCESS verdicts

Read `"$inbox/verdicts.json"`. Full schema:

```jsonc
{
  "version": 1,
  "kind": "verdicts",
  "batchId": "b-20260725-1412-k3f9",
  "round": 1,
  "sentAt": "2026-07-25T14:38:12Z",
  "verdicts": [
    {
      "pointId": "opaque-id",
      "verdict": "accept",        // "accept" | "delete" | "redo"
      "chosenLetter": "C",        // present iff accepting an abc point; the winner
      "redoText": "try a warmer palette", // may be empty with a voice note
      "redoVoiceNote": null,       // same shape and local-transcription rule as point.voiceNote
      "redoAbcRequest": {"mode": "model", "count": 4} // optional fresh variants
    }
  ]
}
```

**First verify it's for the round you just showed:** the `batchId` **and**
`round` in `verdicts.json` must equal the `batchId` and `round` of the
`review.json` you wrote in step 5/9. A mismatch means the file is stale (a
prior round's verdicts that failed to archive, or a race). Do **not** apply
it and do not move protocol files by hand. Report the protocol conflict and
wait for explicit recovery direction rather than reverting or redoing against
the wrong round.

If `kind` is `feedback_update`, the user added points or edited pending points
in this same batch. Do not finalize verdicts. `addedPointIds` is only a wake-up
hint and may be stale after retries. Re-read live `feedback.json`, current
`review.json`, and every archived review for the batch. A point is represented
only when a review entry has the same `id` and `feedbackRevision`; an absent
revision means 1. Apply every unrepresented revision, one commit per point. If
an older revision already has a commit, treat the latest text and rectangles as
a correction on top of that work, then record the current revision. Build a
complete round + 1 review in a separate file, preserving the original
`beforeRef`, then request the `feedback-update` transition shown in step 9. If
the server replies `409` with `missingPoints`, that list is authoritative:
apply every returned revision, rebuild the next review, and retry. When there
are no unreviewed revisions, the server archives only the interruption and
keeps the current review and round.

Per verdict:

- **accept**: keep the work. If the point was `handled: "abc"`, **finalize**
  the experiment on `chosenLetter` per the abc skill §6 (fold the winner in,
  delete every other letter's fenced code and the widget, verify zero leftover
  markers), and commit the finalize
  (`wk(<slug>): r<round> p<id>: finalize <scopeId>=<letter>`).
- **delete**: revert that point's commit(s):
  `git revert --no-edit <commit>` (all of the point's commits across rounds,
  newest first). If the point was abc, that *is* the abandon, but if a plain
  revert won't cleanly remove the experiment (later commits touched the same
  lines), do an explicit abandon per abc skill §6 instead, as its own commit.
- **redo**: locally transcribe `redoVoiceNote` when present, then leave its
  source audio in place for the preview server's post-transition cleanup.
  Re-edit per that transcript plus `redoText` (a correction on top of your
  attempt; do not revert first unless the redo text says to start over), make
  new commit(s), and use the same message format with the **next** round number.
  When `redoAbcRequest` is present, use the abc skill to turn this redo into a
  fresh canonical variant experiment using that request. Safely remove any
  earlier experiment for the point first, then record the new `handled: "abc"`
  metadata in the next review.

## Step 9: ROUND TRANSITION

The preview server owns archive choreography. Never copy, move, remove, or
rename live protocol files by hand. The transition helper reads a private
capability published only while the server is bound, then asks the server to
validate and atomically archive the exact live round. A successful transition
writes a receipt under
`$inbox/history/<batchId>-r<round>/`; retry the same helper
command after a lost response and the verified receipt makes it idempotent.
The same receipt records initial and redo voice-note paths. After the archive
is durable, the server removes only recordings no longer referenced by live
protocol files. An idempotent transition retry also retries interrupted
recording cleanup.

When `WK_CONTROL_CENTER=1`, the same helper atomically queues the validated
request instead. The trusted Control Center forwards it to the preview server
after the background agent exits. A `queued: true` result is success for that
finite agent invocation. The controller continues to own the archive call and
reports any server rejection in the session.

Prepare a continuing review as a separate JSON file, not as live
`review.json`. It must use the same `batchId` and original `beforeRef`, with
`round` set to the current round + 1.

**If `kind` was `feedback_update`:**

```sh
webkit/scripts/transition-round.py <slug> feedback-update <batchId> <round> \
  --next-review <next-review.json>
```

The server calculates unreviewed point revisions from the full live batch plus
all review history. A `409` response includes `missingPoints`; handle all of
them, copy each current revision into `feedbackRevision`, rebuild the file, and
retry. On success, inspect `nextRound`: it stays on the current round if nothing
was missing, or advances after the server archives the old review and installs
the supplied one.

**If any point got `redo`** (the batch continues):

1. Build the next review file with only the redone points and fresh `handled`,
   `note`, and `commit` values.
2. Run:

   ```sh
   webkit/scripts/transition-round.py <slug> redo <batchId> <round> \
     --next-review <next-review.json>
   ```

3. Reopen the preview as in step 6 on the first redone point's page.
4. Loop to step 7.

**If every point was accepted or deleted** (the batch is complete):

1. Run:

   ```sh
   webkit/scripts/transition-round.py <slug> complete <batchId> <round>
   ```

   The server archives all three live files and returns to `collecting`.
   Points queued during review stay saved in the overlay until the user sends
   them as a fresh batch.
2. Confirm the working tree is clean (commit any strays; there should not be
   any if you followed step 4).
3. Loop to step 1 and **re-read this file first**.

## History cap recovery

Each color inbox preserves at most 200 archived round directories, 128 MiB of
archive data, and 4,096 entries per bounded history scan. These are fail-closed
ceilings, not an automatic retention policy. When the next archive would cross
a ceiling, the server keeps all existing receipts and live protocol files and
refuses the transition.

Do not clear or move history while the preview server or a transition helper is
running. Recover in this order:

1. Record the current live `batchId` and round from `feedback.json`,
   `review.json`, and `verdicts.json`. If the live files disagree, preserve
   everything and stop for explicit recovery direction.
2. Stop the exact claimed preview process with the guarded shutdown in
   `webkit/SETUP.md`, confirm that it exited, and confirm that no
   `transition-round.py` process for this inbox remains.
3. Back up history outside both the repository and `feedback_dir`. If there is
   no live batch, you may move the complete `history/` directory as one unit.
   If a batch is live, keep every `<batchId>-r*` directory for that batch in
   place and archive only directories belonging to completed batches. Those
   retained receipts are required for idempotency and missing-point checks.
4. Confirm the remaining history is below all three ceilings and contains only
   real round directories with regular archive files. Keep both the backup and
   remaining history untracked and Git-ignored.
5. Restart the preview and retry only the transition that matches the current
   live files. Do not replay commands for an older batch. Restarting rotates the
   server-specific transition capability, which prevents a stopped helper from
   racing the maintenance window.

Never solve a history limit with `git add`, by deleting only selected receipt
files, or by removing current-batch history. Receipts and archived manifests
are one protocol unit.
