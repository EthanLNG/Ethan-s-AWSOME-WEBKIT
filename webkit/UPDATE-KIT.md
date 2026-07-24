# webkit/UPDATE-KIT.md — maintainer flow

This file travels with every vendored copy of the kit, so any agent working in
any project can (a) push a genuinely reusable improvement made here **upstream**
to the kit repo, and (b) pull upstream improvements **down** into this project.
The kit is vendored, not submoduled — divergence is allowed and expected; this
flow is how the good parts of that divergence get shared.

## The canonical clone

Upstream lives at `~/Projects/Ethans-AWESOME-WEBKIT` (remote:
`https://github.com/EthanLNG/Ethan-s-AWSOME-WEBKIT.git`). If that directory is
missing:

```sh
gh repo clone EthanLNG/Ethan-s-AWSOME-WEBKIT ~/Projects/Ethans-AWESOME-WEBKIT
```

All pushes and pulls go through this clone — never edit the kit "upstream" from
inside a project copy directly.

## Push flow (project improvement → upstream)

1. **Freshen upstream first**: `cd ~/Projects/Ethans-AWESOME-WEBKIT && git pull`.
   You must classify against upstream's *current* state, or you'll re-push
   things that already landed (or clobber newer work).
2. **Diff** the two payloads, excluding the files that are project truth:

   ```sh
   diff -ru --exclude=webkit.config.json --exclude=__pycache__ --exclude=.DS_Store \
     ~/Projects/Ethans-AWESOME-WEBKIT/webkit <project>/webkit
   ```

   `__pycache__` and `.DS_Store` are runtime junk — running the server writes
   `server/__pycache__/*.pyc` into both payloads, and without these excludes the
   diff fills with `.pyc` hunks you'd have to reason about. (There's no `.webkit`
   exclude here: `.webkit` lives at the project root, not inside `webkit/`, so it
   never appears in this diff anyway.)

3. **Classify every hunk** with one question: **"would a stranger's project
   want this exact line?"**
   - *Yes* → it's a feature/fix — push it. Examples: a retry loop in
     `claim-color.sh`; a new `/__wk/` route in the server; an overlay bugfix;
     clearer wording in `LOOP.md`.
   - *No* → it's site-specific — leave it in the project. Examples: a site's
     name or path (`Landing-page/Var3/…`); a changed port number or palette
     emoji; copy about a specific product; anything that only makes sense
     because of *this* project's layout.
4. **Entangled hunks** — a real improvement hardwired with a site value (e.g. a
   new fallback that bakes in this project's port): don't push it as-is and
   don't drop it. **Extract the mechanism → push it; route the value through
   config** — add a key to `webkit.config.template.json` with a sane default,
   make the code read it, and set the project's value in the project's
   `webkit.config.json`.
5. **Bump `webkit/VERSION`** (semver): a **fix** → patch (`0.1.0` → `0.1.1`); a
   **new capability** → minor (`0.1.0` → `0.2.0`).
6. **Add a `CHANGELOG.md` entry** at the top: version, date, what changed, and
   any migration a puller must do (new config key, renamed file, …).
7. **Commit and push**: one commit, message `v<X.Y.Z>: <summary>`, push `main`.
8. **Back-sync the VERSION** into the project's `webkit/VERSION` so the
   project copy knows it is current (otherwise the next pull flow would
   "update" it with its own changes).

**Never push**: config values (`webkit.config.json` or project-specific values
inside any file), site content, or secrets/tokens of any kind — upstream is a
kit for strangers, and a leaked value is forever in history.
**Never force-push.** Upstream history is shared truth for every vendored copy;
if you pushed something wrong, push a revert.

## Pull flow (upstream → project)

1. Freshen the canonical clone: `git pull`.
2. **Compare VERSIONs**: upstream `webkit/VERSION` vs the project's. Same or
   older upstream → nothing to do, stop.
3. **Copy newer files** from upstream `webkit/` over the project's `webkit/`
   (skip `__pycache__/` and `.DS_Store` — copying compiled `.pyc` bytecode
   between machines is pointless and just dirties the project),
   **except**:
   - `webkit.config.json` — project truth, never overwritten (the template may
     update; the generated config only changes when a CHANGELOG migration says
     to add/rename a key — do that by hand).
   - **locally-diverged files** — any file the project intentionally changed
     (they show up in the step-2 style diff). Merge those by hand, keeping the
     local intent and the upstream improvement both alive.
4. **Re-copy the skills** into `.claude/skills/` (Claude Code projects) —
   honoring any project-local exception (a project that keeps its own version
   of a skill, e.g. its own `abc`, keeps it; note the exception stands).
5. **Read the CHANGELOG** entries between the two versions and perform any
   migrations they call out before declaring the update done.
