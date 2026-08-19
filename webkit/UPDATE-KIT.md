# Updating and contributing AWESOME WEBKIT

This guide travels with every vendored copy. Use it to refresh a website from
an upstream release or to prepare a reusable project improvement for a pull
request. A vendored project may diverge, so never replace local behavior without
first classifying it.

## Non-negotiable safeguards

- Use Git 2.30 or newer. The preview, update, and publication safeguards rely
  on option-safe and immutable-ref behavior from that release line.
- The POSIX workflow below requires `python3`, `git`, `tar`, `diff`, `cmp`, and
  `rsync` on `PATH`. Its first block verifies all six commands before fetching
  or changing either checkout.
- Never overwrite or alter the project's `webkit/webkit.config.json` during a
  payload refresh, and never copy that project-specific file into the upstream
  kit. Leave its existing tracked or untracked state unchanged.
- Never copy project content, credentials, tokens, private URLs, runtime data,
  or customer material into the kit repository.
- Work from clean Git checkouts. Stop if either checkout has tracked or
  untracked changes that are not part of this operation.
- Use a topic branch in both the website project and the kit clone. Do not edit,
  commit to, or push `main` directly.
- Copy only files stored in Git. Preserve executable modes.
- Stage explicit paths, inspect the cached diff, and run the full quality suite.
- Fetching is read-only. Any push or pull-request creation requires the user's
  explicit approval.

## Define and verify the two repositories

Replace both example paths with absolute paths. Keep the quotes in every
command. Run the blocks in the same POSIX shell so the variables and fail-fast
setting remain active.

```sh
set -eu

for WK_TOOL in python3 git tar diff cmp rsync; do
  command -v "$WK_TOOL" >/dev/null 2>&1 || {
    printf '%s\n' "Stop: required command is unavailable: $WK_TOOL" >&2
    exit 1
  }
done

WK_UPSTREAM="/absolute/path/to/Ethans-AWESOME-WEBKIT"
WK_PROJECT="/absolute/path/to/website-project"
WK_SOURCE_REMOTE="origin"
WK_SOURCE_REF="${WK_SOURCE_REMOTE}/main"

WK_UPSTREAM_SELECTED="$(python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve())' "$WK_UPSTREAM")" || exit 1
WK_PROJECT_SELECTED="$(python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve())' "$WK_PROJECT")" || exit 1
WK_UPSTREAM_ROOT="$(git -C "$WK_UPSTREAM" rev-parse --show-toplevel)" || exit 1
WK_PROJECT_ROOT="$(git -C "$WK_PROJECT" rev-parse --show-toplevel)" || exit 1
WK_UPSTREAM_ROOT="$(python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve())' "$WK_UPSTREAM_ROOT")" || exit 1
WK_PROJECT_ROOT="$(python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve())' "$WK_PROJECT_ROOT")" || exit 1
test "$WK_UPSTREAM_SELECTED" = "$WK_UPSTREAM_ROOT" || {
  printf '%s\n' "Stop: choose the kit repository root, not a subdirectory." >&2
  exit 1
}
test "$WK_PROJECT_SELECTED" = "$WK_PROJECT_ROOT" || {
  printf '%s\n' "Stop: choose the project repository root, not a subdirectory." >&2
  exit 1
}
WK_UPSTREAM="$WK_UPSTREAM_ROOT"
WK_PROJECT="$WK_PROJECT_ROOT"

git -C "$WK_UPSTREAM" remote get-url "$WK_SOURCE_REMOTE"

WK_UPSTREAM_STATUS="$(git -C "$WK_UPSTREAM" status --porcelain=v1 --untracked-files=all)" || exit 1
test -z "$WK_UPSTREAM_STATUS" || {
  printf '%s\n' "Stop: the kit checkout is not clean." >&2
  exit 1
}
WK_PROJECT_STATUS="$(git -C "$WK_PROJECT" status --porcelain=v1 --untracked-files=all)" || exit 1
test -z "$WK_PROJECT_STATUS" || {
  printf '%s\n' "Stop: the project checkout is not clean." >&2
  exit 1
}

git -C "$WK_UPSTREAM" fetch --prune "$WK_SOURCE_REMOTE"
git -C "$WK_UPSTREAM" rev-parse --verify "${WK_SOURCE_REF}^{commit}"
```

Inspect the printed roots and remote URL. Stop if either root is not the intended
repository or the remote is not the source the user selected. `fetch` updates a
remote-tracking ref without changing the checked-out branch.

Define this containment check in the same shell. It rejects a destination if
the destination or any component below the repository root is a symbolic link,
if it is not a real directory, or if its resolved path leaves the repository:

```sh
wk_require_real_project_directory() {
  python3 - "$1" "$2" <<'PY'
import os
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
target = Path(sys.argv[2])
try:
    relative = target.relative_to(root)
except ValueError:
    raise SystemExit("destination is not lexically inside the project")

current = root
for component in relative.parts:
    current = current / component
    try:
        metadata = os.lstat(str(current))
    except OSError as error:
        raise SystemExit("destination component is unavailable: {}".format(error))
    if stat.S_ISLNK(metadata.st_mode):
        raise SystemExit("destination traverses a symbolic link: {}".format(current))

if not target.is_dir():
    raise SystemExit("destination is not a real directory: {}".format(target))
try:
    target.resolve().relative_to(root)
except ValueError:
    raise SystemExit("resolved destination leaves the project")
PY
}

wk_require_real_project_directory "$WK_PROJECT" "$WK_PROJECT/webkit" || {
  printf '%s\n' "Stop: live webkit destination failed containment checks." >&2
  exit 1
}
```

Run this check again immediately before every dry run or write below. A clean
Git status does not make a tracked symbolic-link destination safe.

## Refresh an existing vendored project

### 1. Build tracked snapshots

Use `git archive`, not a live directory copy. It includes only committed files
and retains Git executable modes. The second snapshot lets the updater detect a
customized installed Claude Code skill before replacing it.

```sh
if git -C "$WK_UPSTREAM" cat-file -e \
  "${WK_SOURCE_REF}:webkit/webkit.config.json" 2>/dev/null; then
  printf '%s\n' "Stop: the source ref tracks project configuration." >&2
  exit 1
fi

WK_SNAPSHOT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/awesome-webkit-update.XXXXXX")" || exit 1
test -d "$WK_SNAPSHOT_DIR" || exit 1
trap 'rm -rf -- "$WK_SNAPSHOT_DIR"' EXIT HUP INT TERM

WK_UPSTREAM_ARCHIVE="$WK_SNAPSHOT_DIR/upstream-webkit.tar"
WK_PROJECT_ARCHIVE="$WK_SNAPSHOT_DIR/project-webkit.tar"
WK_UPSTREAM_TREE="$WK_SNAPSHOT_DIR/upstream"
WK_PROJECT_TREE="$WK_SNAPSHOT_DIR/project-before"
mkdir "$WK_UPSTREAM_TREE" "$WK_PROJECT_TREE"

git -C "$WK_UPSTREAM" archive --format=tar \
  --output="$WK_UPSTREAM_ARCHIVE" "$WK_SOURCE_REF" webkit
git -C "$WK_PROJECT" archive --format=tar \
  --output="$WK_PROJECT_ARCHIVE" HEAD webkit
tar -xf "$WK_UPSTREAM_ARCHIVE" -C "$WK_UPSTREAM_TREE"
tar -xf "$WK_PROJECT_ARCHIVE" -C "$WK_PROJECT_TREE"

WK_VERSION="$(tr -d '\r\n' < "$WK_UPSTREAM_TREE/webkit/VERSION")"
python3 -c 'import re, sys; raise SystemExit(re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", sys.argv[1]) is None)' \
  "$WK_VERSION" || {
  printf '%s\n' "Stop: upstream VERSION is not numeric SemVer." >&2
  exit 1
}
printf 'Installed: '
tr -d '\r\n' < "$WK_PROJECT_TREE/webkit/VERSION"
printf '\nAvailable: %s\n' "$WK_VERSION"
```

Read the matching sections of the upstream `CHANGELOG.md`, including every
migration after the installed version.

### 2. Classify differences before copying

`diff` returns status 1 when it finds ordinary differences. A status above 1 is
an error and must stop the update.

```sh
WK_DIFF_STATUS=0
diff -ru \
  --exclude='webkit.config.json' \
  --exclude='__pycache__' \
  --exclude='.DS_Store' \
  "$WK_UPSTREAM_TREE/webkit" "$WK_PROJECT_TREE/webkit" || WK_DIFF_STATUS=$?
if test "$WK_DIFF_STATUS" -gt 1; then
  printf '%s\n' "Stop: the payload comparison failed." >&2
  exit "$WK_DIFF_STATUS"
fi
```

Identify every intentional project adaptation. If the upstream behavior and a
local adaptation overlap, plan a manual merge. Do not weaken project behavior
merely to make the directories identical.

For a Claude Code project, verify each installed skill against the old vendored
source. The list produced here includes only exact copies that are safe to
refresh automatically.

```sh
WK_REFRESH_SKILLS=""
for WK_SKILL in abc webkit-setup webkit-loop; do
  WK_INSTALLED_SKILL="$WK_PROJECT/.claude/skills/$WK_SKILL"
  if test -e "$WK_INSTALLED_SKILL" || test -L "$WK_INSTALLED_SKILL"; then
    wk_require_real_project_directory "$WK_PROJECT" "$WK_INSTALLED_SKILL" || {
      printf '%s\n' "Stop: installed skill destination failed containment checks: $WK_SKILL" >&2
      exit 1
    }
    if diff -qr "$WK_PROJECT_TREE/webkit/skills/$WK_SKILL" \
      "$WK_INSTALLED_SKILL"; then
      WK_REFRESH_SKILLS="$WK_REFRESH_SKILLS $WK_SKILL"
    else
      printf '%s\n' \
        "Keep and review customized skill: .claude/skills/$WK_SKILL" >&2
    fi
  fi
done
printf 'Safe automatic skill refreshes:%s\n' "$WK_REFRESH_SKILLS"
```

Ask the user whether to keep or manually merge every customized skill. Do not
add a customized name to `WK_REFRESH_SKILLS` or overwrite it automatically.

### 3. Copy on a project topic branch

Review the dry run first. `rsync -a` preserves modes. `--delete` removes files
that disappeared upstream, but only inside the explicit `webkit/` destination.
The exclusions protect project configuration and ignored runtime files.

```sh
git -C "$WK_PROJECT" switch -c "chore/awesome-webkit-$WK_VERSION"

if test -f "$WK_PROJECT/webkit/webkit.config.json"; then
  cp -p "$WK_PROJECT/webkit/webkit.config.json" \
    "$WK_SNAPSHOT_DIR/project-webkit.config.json"
fi

wk_require_real_project_directory "$WK_PROJECT" "$WK_PROJECT/webkit" || {
  printf '%s\n' "Stop: live webkit destination changed before the dry run." >&2
  exit 1
}
rsync -ani --delete \
  --exclude='webkit.config.json' \
  --exclude='__pycache__/' \
  --exclude='.DS_Store' \
  "$WK_UPSTREAM_TREE/webkit/" "$WK_PROJECT/webkit/"
```

Continue only after every listed addition, change, and deletion is understood.
Then run the same command without dry-run mode:

```sh
wk_require_real_project_directory "$WK_PROJECT" "$WK_PROJECT/webkit" || {
  printf '%s\n' "Stop: live webkit destination changed before the update." >&2
  exit 1
}
rsync -ai --delete \
  --exclude='webkit.config.json' \
  --exclude='__pycache__/' \
  --exclude='.DS_Store' \
  "$WK_UPSTREAM_TREE/webkit/" "$WK_PROJECT/webkit/"

if test -f "$WK_SNAPSHOT_DIR/project-webkit.config.json"; then
  cmp -s "$WK_SNAPSHOT_DIR/project-webkit.config.json" \
    "$WK_PROJECT/webkit/webkit.config.json" || {
      printf '%s\n' "Stop: webkit.config.json changed." >&2
      exit 1
    }
fi
```

Reapply each classified local adaptation by hand and perform every changelog
migration. Refresh only installed Claude Code skills that passed the exact-copy
check above:

```sh
for WK_SKILL in $WK_REFRESH_SKILLS; do
  WK_INSTALLED_SKILL="$WK_PROJECT/.claude/skills/$WK_SKILL"
  wk_require_real_project_directory "$WK_PROJECT" "$WK_INSTALLED_SKILL" || {
    printf '%s\n' "Stop: installed skill destination changed before refresh: $WK_SKILL" >&2
    exit 1
  }
  rsync -ai --delete \
    "$WK_UPSTREAM_TREE/webkit/skills/$WK_SKILL/" "$WK_INSTALLED_SKILL/"
done
```

### 4. Stage only the refreshed payload

The first command stages tracked modifications and removals under `webkit/`
while excluding project configuration. The manifest pipeline then stages only
paths tracked by the selected upstream ref, including newly added files.

```sh
git -C "$WK_PROJECT" add -u -- \
  ':(top)webkit' \
  ':(top,exclude)webkit/webkit.config.json'
WK_PAYLOAD_MANIFEST="$WK_SNAPSHOT_DIR/upstream-webkit-paths.zlist"
git -C "$WK_UPSTREAM" ls-tree -r --name-only -z \
  "$WK_SOURCE_REF" -- webkit > "$WK_PAYLOAD_MANIFEST"
git -C "$WK_PROJECT" add \
  --pathspec-from-file="$WK_PAYLOAD_MANIFEST" --pathspec-file-nul

for WK_SKILL in $WK_REFRESH_SKILLS; do
  if test -d "$WK_PROJECT/.claude/skills/$WK_SKILL"; then
    git -C "$WK_PROJECT" add -- ".claude/skills/$WK_SKILL"
  fi
done

git -C "$WK_PROJECT" status --short
git -C "$WK_PROJECT" diff --cached --name-status
git -C "$WK_PROJECT" diff --cached --check
git -C "$WK_PROJECT" diff --cached -- webkit .claude/skills
```

Confirm the cached file list contains only the reviewed WebKit payload and any
approved skill refresh. Confirm that project names, credentials, private URLs,
reference material, and `webkit/webkit.config.json` are absent.

### 5. Validate and commit locally

Run the project's normal quality commands, then complete one real feedback and
verdict round using `webkit/SETUP.md` and `webkit/LOOP.md`. At minimum, compile
the refreshed Python payload and recheck the staged patch:

```sh
python3 -m compileall -q -f "$WK_PROJECT/webkit"
git -C "$WK_PROJECT" diff --cached --check
git -C "$WK_PROJECT" status --short
```

Commit only after all checks and the browser round pass:

```sh
git -C "$WK_PROJECT" commit -m "Update AWESOME WEBKIT to v$WK_VERSION"
```

Do not push the project branch unless the user explicitly approves it.

## Prepare a reusable improvement for upstream

### 1. Snapshot and classify the project change

Repeat the repository verification and clean-worktree checks above. Archive the
project's committed payload so untracked runtime data can never enter the kit:

```sh
WK_COMPARE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/awesome-webkit-contribution.XXXXXX")" || exit 1
test -d "$WK_COMPARE_DIR" || exit 1
trap 'rm -rf -- "$WK_COMPARE_DIR"' EXIT HUP INT TERM

git -C "$WK_PROJECT" archive --format=tar \
  --output="$WK_COMPARE_DIR/project-webkit.tar" HEAD webkit
mkdir "$WK_COMPARE_DIR/project" "$WK_COMPARE_DIR/upstream"
tar -xf "$WK_COMPARE_DIR/project-webkit.tar" -C "$WK_COMPARE_DIR/project"
git -C "$WK_UPSTREAM" archive --format=tar \
  --output="$WK_COMPARE_DIR/upstream-webkit.tar" "$WK_SOURCE_REF" webkit
tar -xf "$WK_COMPARE_DIR/upstream-webkit.tar" -C "$WK_COMPARE_DIR/upstream"

WK_DIFF_STATUS=0
diff -ru \
  --exclude='webkit.config.json' \
  "$WK_COMPARE_DIR/upstream/webkit" "$WK_COMPARE_DIR/project/webkit" || WK_DIFF_STATUS=$?
if test "$WK_DIFF_STATUS" -gt 1; then
  printf '%s\n' "Stop: the payload comparison failed." >&2
  exit "$WK_DIFF_STATUS"
fi
```

Classify each hunk with one question: would an unrelated website want this exact
behavior? Reusable mechanisms may go upstream. Site names, copy, paths, ports,
brand values, references, and project-only behavior must stay in the project.
Move any reusable mechanism that depends on a project value behind a safe
configuration default.

### 2. Create an upstream topic branch and copy exact paths

Choose a descriptive branch name. Branch from the fetched source ref, never
from or onto local `main`:

```sh
WK_TOPIC="fix/descriptive-webkit-change"
git -C "$WK_UPSTREAM" switch -c "$WK_TOPIC" "$WK_SOURCE_REF"
```

Copy one reviewed path at a time from the tracked project snapshot. Preserve its
mode with `rsync -a`. Add or edit upstream regression tests directly in the kit
clone.

```sh
WK_REUSABLE_PATH="webkit/path/to/reusable-file"
rsync -a "$WK_COMPARE_DIR/project/$WK_REUSABLE_PATH" \
  "$WK_UPSTREAM/$WK_REUSABLE_PATH"

git -C "$WK_UPSTREAM" add -- \
  "$WK_REUSABLE_PATH" \
  "tests/path-to-related-regression-test.py"
```

For an intentional deletion, use `git rm -- "exact/path"`. Never use
`git add -A`, `git add .`, or a broad recursive copy from the project.

### 3. Inspect the staged contribution

```sh
git -C "$WK_UPSTREAM" status --short
git -C "$WK_UPSTREAM" diff --cached --name-status
git -C "$WK_UPSTREAM" diff --cached --check
git -C "$WK_UPSTREAM" diff --cached
```

Review every line for project data and secrets. Add a concise changelog entry
when behavior changes. A version bump is a release decision and requires the
maintainer's direction.

### 4. Run the exact repository quality suite

These commands match the complete local suites used by continuous integration.
The hosted matrix repeats them on Python 3.7 and Python 3.14 across Linux,
Windows, and macOS where applicable.

```sh
(
  set -e
  cd "$WK_UPSTREAM" || exit 1
  python3 -m compileall -q -f .
  python3 -m unittest discover -s control-center/tests -p 'test_*.py' -v
  python3 -m unittest discover -s tests -p 'test_*.py' -v
  python3 tests/test_release_quality.py
  git diff --check
  git diff --cached --check
)
```

All commands must pass. Also exercise the changed behavior in the configured
external browser when the contribution affects the preview or overlay.

### 5. Commit and request publication approval

```sh
git -C "$WK_UPSTREAM" commit -m "Fix: describe the reusable WebKit change"
```

Show the commit, test results, and final diff to the user. Only after explicit
approval, push the topic branch to the user's fork or another authorized remote
and open a pull request. Never push a topic commit directly to upstream `main`,
never force-push, and never assume maintainer access.
