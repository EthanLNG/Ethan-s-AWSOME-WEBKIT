# webkit/LOOP.md — the feedback-round protocol

**Re-read this file at the top of every round.** Long sessions push early
instructions out of your working context; the loop only works if every round is
run against the letter of this contract, not your memory of it.

This is the protocol between you (the agent), the preview server, and the
in-browser overlay. The user draws rectangles on the live site and describes
changes; the server writes them to your inbox; you apply them and publish a
manifest; the user reviews with a before/after toggle and sends verdicts; you
process verdicts and either run another round or close the batch.

## Ground rules

- **Never write `feedback.json` or `verdicts.json` yourself.** They are
  server-owned — the server writes them from the overlay's POSTs. Your only
  writes in the inbox are `review.json` and the `history/` archive. If you
  fabricate the server's files, phase detection (which is driven purely by file
  presence) turns into fiction.
- **`review.json` is yours.** The server and overlay only read it.
- **Keep the working tree committed at round boundaries.** The before/after
  toggle serves files out of `git show` — uncommitted work is invisible to it
  and gets mixed into the *next* round's diff, corrupting the story.
  BEFORE swaps **in place** (no page reload): the overlay lifts the reviewed
  point's container out of the snapshot document and repoints the page's
  stylesheets at their `/__wk/before/` equivalents, so a point whose change
  lives entirely in CSS still shows a real difference. It falls back to a full
  navigation (with a toast saying why) when the container can't be matched in
  both documents — one more reason the point's primary element should sit
  inside a stable `section`/`article`/`[id]`.
- **One git commit per point.** The verdict loop reverts and redoes at
  point granularity; a commit spanning two points makes `delete` on one of
  them impossible to do cleanly.

## Paths and identity

Your inbox is `<feedback_dir>/<slug>/` **relative to the git root** — with the
default config, `.webkit/feedback/<slug>/` at the repo root — where `<slug>` is
your claimed color's slug from the palette in `webkit/webkit.config.json` (e.g.
`🔵` → `blue`). Below, `<slug>` always means that. The inbox is anchored at the
git root (not `site_root`) because BEFORE mode reads round history out of git;
even when the served site is a subdirectory, the inbox stays at the repo root.
Run the `.webkit/feedback/<slug>/…` commands below from the git root. The server
creates the directory at boot. Your preview URLs are `http://localhost:<port><page>` where `<port>` is
your color's port and `<page>` is a root-absolute path like `/index.html`.

The inbox holds at most these live files, and their presence *is* the state
machine (the overlay polls it as `phase`):

| Files present | Phase | Meaning |
|---|---|---|
| none | `collecting` | user is drawing points |
| `feedback.json` | `awaiting_agent` | a batch is waiting for you |
| + `review.json` (same batchId) | `reviewing` | user is walking your work |
| + `verdicts.json` | `verdicts_sent` | verdicts are waiting for you |

That is why the archive choreography at the end of this file is strict: a file
existing at the wrong moment is a wrong phase.

## Step 1 — WAIT for feedback

```sh
webkit/scripts/wait-for-file.sh .webkit/feedback/<slug>/feedback.json 540
```

Exit `0`: the file exists and parses as JSON — proceed to step 2.
Exit `124`: timeout, file absent — just run it again (loop here indefinitely
until the user ends the session).
Exit `65`: the file exists but never parsed as JSON (truncated / corrupt /
hand-edited). Do **not** re-wait — you'd hang forever. Surface the stderr
message to the user (the batch didn't arrive intact; ask them to re-send or
fix/delete `.webkit/feedback/<slug>/feedback.json`), then wait again.

- **Claude Code idiom:** run it as a background Bash task
  (`run_in_background`), so the harness re-invokes you when it exits — you
  don't burn your turn polling. On `124`, start it again in the background.
- **Codex idiom:** keep the current agent turn alive until the waiter exits.
  Start the command in the foreground. If the shell tool yields a live session
  id, wait/poll that **same session** in chunks of at most 60 seconds; do not
  send a final response while it is still running. A detached waiter can notice
  `feedback.json`, but its exit does not wake a Codex task after the task has
  already returned a final response. Exit `0` → read the batch immediately;
  exit `124` → start a fresh waiter and remain in this step. The user can still
  send a message while the turn is waiting; handle it normally without losing
  the waiter state.

## Step 2 — READ the batch

Read `.webkit/feedback/<slug>/feedback.json`. Full schema:

```jsonc
{
  "version": 1,
  "kind": "feedback",
  "batchId": "b-20260725-1412-k3f9",   // b-<yyyymmdd>-<hhmm>-<rand4>; stable for the whole batch
  "round": 1,                           // 1-based; bumps only via YOUR review.json on redo rounds
  "color": "blue",                      // your color's SLUG (matches the inbox dir name);
                                        // the overlay sends the slug, not the emoji. The
                                        // emoji appears only as `emoji` in /__wk/state.
  "sessionId": "…",                     // overlay session; opaque, echo nowhere, ignore
  "createdAt": "2026-07-25T14:12:03Z",
  "updatedAt": "2026-07-25T14:15:41Z",  // server bumps on merges while unclaimed
  "pages": ["/index.html"],             // every page that has points, root-absolute
  "points": [
    {
      "id": "…",            // OPAQUE unique string, assigned by the overlay. Never parse
                            // or invent ids — quote them verbatim everywhere.
      "number": 1,          // user-visible ordering; apply points in this order
      "page": "/index.html",
      "createdAt": "2026-07-25T14:12:03Z",
      "rect": { "x": 120, "y": 2260, "w": 300, "h": 84 },  // DOCUMENT coords, CSS px
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
      "abcState": null,     // snapshot of window.__abc at capture; null when no experiments
                            // live, else a map keyed by scope id, e.g.
                            //   { "cta": { "current": "B", "letters": "AB" } }
                            // Informational: it tells you what the user was LOOKING AT.
      "text": "make this button bigger and bolder",
      "abcRequest": null,   // see below
      "status": "new"       // the overlay ALWAYS writes "new". "redo" only exists if
                            // YOU set it when re-queuing a point into a later round
                            // (step 9); the server never rewrites points, so a fresh
                            // batch is always all-"new".
    }
  ]
}
```

`abcRequest` — non-null means the user wants lettered variants for this point
instead of a single change:

- `{"mode": "model", "count": 4}` — **you** invent `count` genuinely different
  takes (vary the mechanism — scale, placement, style, motion — not
  micro-jitters of one idea).
- `{"mode": "user", "prompts": {"A": "solid blue", "B": "outline only", …}}` —
  build **exactly one variant per prompt letter**, each doing what its prompt
  says, nothing invented beyond it.

Use `context[]` to find the target: try `context[0].selector` first; if the
DOM shifted since capture, fall back to the rect + the other context entries.
The user's `text` is the instruction; the geometry is only there to tell you
*where*.

## Step 3 — BEFORE REF

Snapshot the pre-round state so the overlay's BEFORE toggle has something to
serve:

```sh
git add -A && git commit -m "wk(<slug>): pre-round snapshot"   # only if tree is dirty
beforeRef=$(git rev-parse HEAD)
```

On round 1 this is the pre-batch state. On later rounds (redos), **keep the
batch's original `beforeRef`** — the user is always comparing against how the
site looked before the batch started, not against the rejected attempt.

## Step 4 — APPLY each point

Work through `points[]` in `number` order. Per point:

1. Make the change the point's `text` asks for. (On a redo round, a point you
   re-queued carries `status:"redo"` — one *you* set in step 9, never the
   overlay — and the instruction to follow is that point's `redoText` from last
   round's `verdicts.json`, not its original `text`.)
2. Commit it — **one commit per point**, message format:

   ```
   wk(<slug>): r<round> p<id> — <short summary>
   ```

   e.g. `wk(blue): r1 pp-mrzhxkfa-9f2q — enlarge hero CTA, weight 700`. `p<id>`
   is the letter `p` immediately followed by the point's `id` verbatim — and
   since real ids are `p-<base36-timestamp>-<rand4>` (assigned by the overlay,
   never the batch id), the token genuinely begins `pp-`. That doubled `p` is
   correct, not a typo; never truncate or re-derive the id.
3. `abcRequest` points: **use the abc skill** (`webkit/skills/abc/SKILL.md` —
   the canonical lettered-variants procedure: scope element, `ABC:` marker
   fences, the switcher widget). Model mode → invent `count` takes; user mode →
   one per prompt letter, letters = the prompt keys. Record the experiment's
   `{scopeId, letters}` — you need them for the manifest and for finalize.
   Still one commit for the whole point (the experiment is one unit of work).
4. A point you cannot or should not do (contradicts another point, needs an
   asset you don't have, is a question rather than a change) → make no change,
   mark it `skipped` in the manifest with a `note` that answers/explains.

## Step 5 — MANIFEST (`review.json`)

Write `.webkit/feedback/<slug>/review.json`. Full schema:

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
      "id": "…",                        // the point id, verbatim
      "handled": "done",                // "done" | "abc" | "skipped"
      "note": "CTA now 1.25rem / 700",  // one line, shown to the user in the review bar
      "commit": "<full SHA of this point's commit>",   // omit/null for skipped
      "abc": { "scopeId": "cta", "letters": "ABCD" }   // ONLY when handled = "abc"
    }
  ]
}
```

**Write it atomically**: write the full content to a temp file in the same
directory, then `mv` it over the final name —

```sh
printf '%s' "$json" > .webkit/feedback/<slug>/review.json.tmp \
  && mv .webkit/feedback/<slug>/review.json.tmp .webkit/feedback/<slug>/review.json
```

The overlay polls every 2 s; a half-written JSON is a parse error at exactly
the wrong moment. The `mv` (same filesystem) is atomic; the poll sees either
the old state or the complete new file, never a torn one.

## Step 6 — SHOW

Reopen the preview jumped into review mode, on the first pending point's page:

```sh
webkit/scripts/open-preview.sh "http://localhost:<port><first-page>?wk-review=<batchId>"
```

`<first-page>` = the `page` of the lowest-`number` point in this round. If the
page path already carries a query string, append with `&` instead of `?`. The
overlay sees `wk-review`, enters review mode, and jumps the user to point 1.

## Step 7 — WAIT for verdicts

```sh
webkit/scripts/wait-for-file.sh .webkit/feedback/<slug>/verdicts.json 540
```

Same idioms and exit codes as step 1 (background + re-invoke on Claude Code;
foreground + re-run on `124` on Codex). On `65` (verdicts.json present but
unparsable), don't re-wait — surface the message and ask the user to re-send or
fix/delete the file. While waiting, do nothing to the working tree.

## Step 8 — PROCESS verdicts

Read `.webkit/feedback/<slug>/verdicts.json`. Full schema:

```jsonc
{
  "version": 1,
  "kind": "verdicts",
  "batchId": "b-20260725-1412-k3f9",
  "round": 1,
  "sentAt": "2026-07-25T14:38:12Z",
  "verdicts": [
    {
      "pointId": "…",
      "verdict": "accept",        // "accept" | "delete" | "redo"
      "chosenLetter": "C",        // present iff accepting an abc point — the winner
      "redoText": "…"             // present iff verdict is "redo" — the new instruction
    }
  ]
}
```

**First verify it's for the round you just showed:** the `batchId` **and**
`round` in `verdicts.json` must equal the `batchId` and `round` of the
`review.json` you wrote in step 5/9. A mismatch means the file is stale (a
prior round's verdicts that failed to archive, or a race) — do **not** apply
it: archive/delete the stale file and go back to step 7's wait rather than
reverting or redoing against the wrong round.

Per verdict:

- **accept** — keep the work. If the point was `handled: "abc"`: **finalize**
  the experiment on `chosenLetter` per the abc skill §6 (fold the winner in,
  delete every other letter's fenced code and the widget, verify zero leftover
  markers), and commit the finalize
  (`wk(<slug>): r<round> p<id> — finalize <scopeId>=<letter>`).
- **delete** — revert that point's commit(s):
  `git revert --no-edit <commit>` (all of the point's commits across rounds,
  newest first). If the point was abc: that *is* the abandon — but if a plain
  revert won't cleanly remove the experiment (later commits touched the same
  lines), do an explicit abandon per abc skill §6 instead, as its own commit.
- **redo** — re-edit per `redoText` (a correction on top of your attempt — do
  not revert first unless the redo text says to start over), new commit(s),
  same message format with the **next** round number.

## Step 9 — ROUND TRANSITION

Archive so the file-presence state machine stays truthful. `history/` lives
inside the inbox: `.webkit/feedback/<slug>/history/<batchId>-r<round>/`.

**If any point got `redo`** (the batch continues):

1. Snapshot the finished round: **copy** `feedback.json` and **move**
   `review.json` and `verdicts.json` into `history/<batchId>-r<round>/`.
   (`feedback.json` is copied, not moved — the overlay still needs the live
   points' geometry and text for the next round; `verdicts.json` MUST leave the
   live dir or your next step-7 wait would return instantly on stale data.)
2. Write the **new** `review.json` — `round` + 1, same `batchId`, same
   `beforeRef`, `points` = only the redone points (fresh `handled`, `note`,
   `commit` for the redo work you did in step 8). **Write it LAST**, after the
   old files are archived, so the overlay never sees new-review + old-verdicts
   together, and atomically as in step 5.
3. Reopen the preview as in step 6 (first redone point's page).
4. Loop to step 7.

**If every point was accepted or deleted** (the batch is complete):

1. **Move** all three files — `feedback.json`, `review.json`, `verdicts.json` —
   into `history/<batchId>-r<round>/`. The live dir being empty flips phase
   back to `collecting`; the overlay then auto-POSTs any points the user queued
   during review as a fresh batch.
2. Confirm the working tree is clean (commit any strays — there shouldn't be
   any if you followed step 4).
3. Loop to step 1 — and **re-read this file first**.
