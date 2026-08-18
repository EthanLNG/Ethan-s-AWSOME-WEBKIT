#!/usr/bin/env python3
"""Color-stamping preview server + feedback-loop API for Ethan's AWESOME WEBKIT.

Drop-in replacement for `python3 -m http.server`, extended in two directions:

1. TAB IDENTITY (the battle-tested part). Serves the working tree as-is but
   rewrites the <title> of every HTML page it hands out so the browser tab
   leads with this agent's color emoji — source files are never touched, and
   the label survives every reload (unlike setting document.title from the
   outside). Each agent runs its own server on its own port; the color in the
   tab tells the user which design/agent a tab belongs to at a glance.

2. FEEDBACK LOOP (the webkit part). Every served HTML page gets the webkit
   overlay injected (a single <script> tag; the overlay itself lives in
   ../overlay/). The overlay talks back to this server over /__wk/* endpoints:
   it POSTs feedback batches into the agent's per-color inbox
   (<git-root>/<feedback_dir>/<slug>/feedback.json), polls /__wk/state for
   round transitions, POSTs verdicts, and — during review — loads the
   pre-round version of any page straight out of git via /__wk/before/<path>
   so the user can flip BEFORE|AFTER without the agent stashing anything.

Usage:  preview-server.py <color-emoji> [port] [root-dir]
  e.g.  python3 webkit/server/preview-server.py 🔵
        (port defaults to the palette entry's port for that emoji;
         root-dir — the DOCUMENT ROOT to serve — defaults to the config's
         site_root resolved relative to the project root, else the current
         working directory. site_root is what gets served; it is NOT where
         the feedback inbox lives — that stays anchored at the git root.)

Config: $WK_CONFIG if set, else ../webkit.config.json, else
../webkit.config.template.json (both relative to this file). The palette in
the config is the single source of truth for emojis/slugs/ports — nothing
color-related is hardcoded here.

Behaviors deliberately preserved from the server this grew out of:
  * Cache-Control: no-store on EVERY response (see end_headers), which fixed
    a real lost-iteration bug where edited CSS was served stale from disk
    cache even after a ?v= bump.
  * Claim enforcement before serving (see _verify_claim), added after two
    agents ended up stamping the same color.
  * ThreadingHTTPServer, so one tab holding a connection open (WebGL pages
    keep-alive) can't hang everyone else's requests.
"""
import json
import mimetypes
import os
import re
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

_HERE = os.path.dirname(os.path.abspath(__file__))

# --- config ------------------------------------------------------------------
# The kit is config-driven: palette (emoji/slug/port), lock_dir, feedback_dir
# all come from webkit.config.json so a project can rebrand the whole palette
# without touching code. The template is the last-resort fallback so the kit
# works out of the box straight from a fresh clone.


def _load_config():
    env = os.environ.get("WK_CONFIG")
    if env:
        if not os.path.isfile(env):
            sys.exit(
                "preview-server.py: WK_CONFIG points at {!r} which does not exist.".format(env)
            )
        candidates = [env]
    else:
        candidates = [
            os.path.join(_HERE, "..", "webkit.config.json"),
            os.path.join(_HERE, "..", "webkit.config.template.json"),
        ]
    for cand in candidates:
        if not os.path.isfile(cand):
            continue
        try:
            with open(cand, encoding="utf-8") as f:
                return json.load(f), os.path.abspath(cand)
        except ValueError as e:
            sys.exit("preview-server.py: {} is not valid JSON: {}".format(cand, e))
    sys.exit(
        "preview-server.py: no config found (looked for {}).".format(
            ", ".join(os.path.abspath(c) for c in candidates)
        )
    )


CONFIG, CONFIG_PATH = _load_config()
API_PROXY_ORIGIN = (CONFIG.get("api_proxy_origin") or "").rstrip("/")

PALETTE = CONFIG.get("palette") or []
if not PALETTE or not all(("emoji" in p and "slug" in p) for p in PALETTE):
    sys.exit(
        "preview-server.py: config {} has no usable palette "
        "(need a list of {{slug, emoji, port}} entries).".format(CONFIG_PATH)
    )
_SLUGS = {p["emoji"]: p["slug"] for p in PALETTE}
_PORTS = {p["emoji"]: p.get("port") for p in PALETTE}

# --- CLI ---------------------------------------------------------------------
COLOR = sys.argv[1] if len(sys.argv) > 1 else PALETTE[0]["emoji"]
if COLOR not in _SLUGS:
    sys.exit(
        "preview-server.py: {!r} is not a palette color ({}).".format(
            COLOR, " ".join(_SLUGS)
        )
    )
SLUG = _SLUGS[COLOR]
if len(sys.argv) > 2:
    try:
        PORT = int(sys.argv[2])
    except ValueError:
        sys.exit(
            "usage: preview-server.py <color-emoji> [port] [root-dir]  "
            "({!r} is not a port number)".format(sys.argv[2])
        )
else:
    PORT = _PORTS.get(COLOR)
    if not PORT:
        sys.exit(
            "preview-server.py: palette entry for {} has no port in {} — "
            "pass one explicitly.".format(COLOR, CONFIG_PATH)
        )
    PORT = int(PORT)
if len(sys.argv) > 3:
    ROOT = os.path.abspath(sys.argv[3])
else:
    # No root-dir on the CLI: fall back to the config's site_root — the
    # directory this server should serve (its document root). site_root is
    # relative to the project root, and the config always lives at
    # <project>/webkit/webkit.config.json, so the project root is two levels up
    # from CONFIG_PATH. This is the SERVE dir only; the feedback inbox is a
    # separate concern anchored at the git root (see FEEDBACK_DIR below).
    _site_root = CONFIG.get("site_root")
    if _site_root:
        _project_dir = os.path.dirname(os.path.dirname(CONFIG_PATH))
        ROOT = os.path.abspath(os.path.join(_project_dir, _site_root))
    else:
        ROOT = os.getcwd()

# --- claim enforcement -------------------------------------------------------
# Kept from the original after a real two-agents-on-one-color collision: an
# agent skipped claim-color.sh (assumed its color from a prior session's
# notes) and stamped an emoji another agent had properly locked. The lock
# registry can only protect claims that go through it, so the STAMPING point
# demands proof: serving refuses unless this color's lock exists and — when
# the lock records an owner — that owner is the worktree being served.
# Manual runs outside the agent flow can bypass with WK_COLOR_FORCE=1
# (or just claim first).


def _verify_claim():
    if os.environ.get("WK_COLOR_FORCE"):
        return
    lockdir = (
        os.environ.get("WK_COLOR_LOCKDIR")
        or CONFIG.get("lock_dir")
        or "/tmp/webkit-agent-colors"
    )
    lock = os.path.join(lockdir, SLUG + ".lock")
    if not os.path.isdir(lock):
        sys.exit(
            "preview-server.py: refusing to stamp {} — no claim lock at {}.\n"
            "Claim your color first (and keep it for the whole session):\n"
            "    color=$(webkit/scripts/claim-color.sh)\n"
            "Never assume a color from a previous session or from notes.".format(COLOR, lock)
        )
    owner_file = os.path.join(lock, "owner")
    owner = ""
    if os.path.isfile(owner_file):
        with open(owner_file) as f:
            owner = f.read().strip()
    if not owner:
        sys.exit(
            "preview-server.py: refusing to stamp {} — its lock at {} has no owner "
            "record, so this worktree can't prove the claim is its own (the lock may "
            "belong to another agent that claimed before ownership records existed).\n"
            "If the claim really is yours from this session, record it:\n"
            "    pwd -P > {}\n"
            "Otherwise claim a color properly: color=$(webkit/scripts/claim-color.sh)".format(
                COLOR, lock, owner_file
            )
        )
    if os.path.realpath(owner) != os.path.realpath(ROOT):
        sys.exit(
            "preview-server.py: refusing to stamp {} — its lock is owned by another "
            "agent's worktree:\n    {}\nThis server would serve:\n    {}\n"
            "Claim your own color with webkit/scripts/claim-color.sh.".format(
                COLOR, owner, os.path.realpath(ROOT)
            )
        )


_verify_claim()

# --- boot: git root, feedback inbox, overlay assets --------------------------
# The feedback loop needs git twice: the inbox lives at the repo root (so the
# agent and the server agree on one location no matter which subdirectory is
# being served), and BEFORE-mode serves files out of a pre-round commit via
# `git show`. Outside a git repo we fail soft: static serving + stamping
# still work, only the /__wk data endpoints answer 503.


def _git_root():
    try:
        p = subprocess.run(
            ["git", "-C", ROOT, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if p.returncode != 0:
        return None
    top = p.stdout.strip()
    return top or None


GIT_ROOT = _git_root()
FEEDBACK_DIR = None
if GIT_ROOT:
    FEEDBACK_DIR = os.path.join(
        GIT_ROOT, CONFIG.get("feedback_dir", ".webkit/feedback"), SLUG
    )
    os.makedirs(FEEDBACK_DIR, exist_ok=True)
OVERLAY_DIR = os.path.realpath(os.path.join(_HERE, "..", "overlay"))

# --- title stamping ----------------------------------------------------------
# Strip any already-present color prefix so reloads don't stack emojis. Built
# from the palette via re.escape ALTERNATION, not a character class: several
# emojis are multi-codepoint sequences and a class would shred them.
_LEAD = re.compile(
    r"^(?:(?:{})\s*)+".format("|".join(re.escape(p["emoji"]) for p in PALETTE))
)


def stamp(html):
    def repl(m):
        inner = _LEAD.sub("", m.group(1))
        return "<title>{} {}</title>".format(COLOR, inner)

    return re.sub(r"<title>(.*?)</title>", repl, html, count=1, flags=re.I | re.S)


# --- overlay injection -------------------------------------------------------
# One <script> tag before the LAST </body> (some pages embed literal
# "</body>" strings in inline templates — the last real one closes the
# document). The overlay reads its own data- attributes for color/mode, so
# the injected tag is the entire server→overlay handshake.
_BODY_CLOSE = re.compile(r"</body\s*>", re.I)
_HTML_CLOSE = re.compile(r"</html\s*>", re.I)

# Optional per-project hotkey overrides: {"hotkeys": {"toggle": "Backquote",
# "dictate": "KeyV"}}. Values are KeyboardEvent.code strings (layout-independent
# — this matters on a Hebrew site, where e.key differs per layout); the overlay
# carries the same two defaults, so an absent config block changes nothing.
# Anything that isn't a bare alphanumeric code is dropped rather than escaped:
# it could not be a valid code anyway, and dropping it keeps the injected tag
# un-quotable from config.
_HOTKEY_CODE = re.compile(r"^[A-Za-z0-9]+$")
_HOTKEYS = CONFIG.get("hotkeys", {})
if not isinstance(_HOTKEYS, dict):
    _HOTKEYS = {}
_HOTKEY_VALUES = {}
_HOTKEY_RESERVED = {
    "AltLeft", "AltRight", "ControlLeft", "ControlRight", "Escape",
    "MetaLeft", "MetaRight", "ShiftLeft", "ShiftRight",
}
for _name, _default in (("toggle", "KeyC"), ("dictate", "KeyV")):
    _value = os.environ.get("WK_HOTKEY_{}".format(_name.upper()), _HOTKEYS.get(_name, _default))
    if (
        not isinstance(_value, str)
        or not _HOTKEY_CODE.fullmatch(_value)
        or _value in _HOTKEY_RESERVED
    ):
        _value = _default
    _HOTKEY_VALUES[_name] = _value
if _HOTKEY_VALUES["dictate"] == _HOTKEY_VALUES["toggle"]:
    _HOTKEY_VALUES["dictate"] = "KeyC" if _HOTKEY_VALUES["toggle"] == "KeyV" else "KeyV"
_HOTKEY_ATTRS = "".join(
    ' data-wk-hotkey-{}="{}"'.format(name, _HOTKEY_VALUES[name])
    for name in ("toggle", "dictate")
)
_DICTATION = CONFIG.get("dictation", {})
if not isinstance(_DICTATION, dict):
    _DICTATION = {}
_DICTATION_MODE = os.environ.get("WK_DICTATION_MODE", _DICTATION.get("mode", "speech"))
if _DICTATION_MODE not in ("speech", "voice-note"):
    _DICTATION_MODE = "speech"
_INTERACTION = CONFIG.get("interaction", {})
if not isinstance(_INTERACTION, dict):
    _INTERACTION = {}
_INTERACTION_MODE = os.environ.get(
    "WK_INTERACTION_MODE", _INTERACTION.get("mode", "browse-default")
)
if _INTERACTION_MODE not in ("browse-default", "draw-default"):
    _INTERACTION_MODE = "browse-default"


def inject(html, mode):
    if "data-wk-color" in html:  # already carries an overlay tag — don't stack
        return html
    tag = (
        '<script src="/__wk/overlay.js" defer data-wk-color="{}" '
        'data-wk-emoji="{}" data-wk-mode="{}" data-wk-dictation-mode="{}" '
        'data-wk-interaction-mode="{}"{}></script>'.format(
            SLUG, COLOR, mode, _DICTATION_MODE, _INTERACTION_MODE, _HOTKEY_ATTRS
        )
    )
    matches = list(_BODY_CLOSE.finditer(html)) or list(_HTML_CLOSE.finditer(html))
    if matches:
        i = matches[-1].start()
        return html[:i] + tag + "\n" + html[i:]
    return html + "\n" + tag + "\n"


# --- feedback data files -----------------------------------------------------
# The agent⇄overlay contract is three JSON files in FEEDBACK_DIR:
#   feedback.json  (server-written)  the user's batch of points
#   review.json    (agent-written)   the agent's per-point manifest + beforeRef
#   verdicts.json  (server-written)  the user's accept/delete/redo calls
# "File exists" is the protocol's state signal, so every write must be atomic
# (tmp file + os.replace — a reader can never see a half-written file) and
# serialized (one lock — concurrent POSTs must not interleave read-merge-write).
_DATA_FILES = ("feedback.json", "review.json", "verdicts.json")
_WRITE_LOCK = threading.Lock()


def _data_path(name):
    return os.path.join(FEEDBACK_DIR, name)


def _load_data(name):
    """Parsed contents of a data file, or None if absent/unreadable/not an object."""
    try:
        with open(_data_path(name), encoding="utf-8") as f:
            obj = json.load(f)
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _mtime_ns(name):
    try:
        return os.stat(_data_path(name)).st_mtime_ns
    except OSError:
        return 0


def _rev():
    # Cheap change fingerprint: the three files' mtimes (0 = absent). The
    # overlay polls with its last-seen rev and gets a tiny reply when nothing
    # moved — no JSON parsing on the hot path.
    return "-".join(str(_mtime_ns(n)) for n in _DATA_FILES)


def _atomic_write(name, obj):
    # Caller holds _WRITE_LOCK. tmp file in the SAME directory so os.replace
    # is an atomic rename, never a cross-device copy.
    fd, tmp = tempfile.mkstemp(dir=FEEDBACK_DIR, prefix="." + name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, _data_path(name))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _phase(batch, review, verdicts):
    # Derived, never stored — file presence IS the state machine. A review is
    # only "live" if it matches the current batch (a stale review.json from a
    # batch the agent hasn't archived yet must not mask a fresh feedback.json).
    review_active = review is not None and (
        batch is None or review.get("batchId") == batch.get("batchId")
    )
    if review_active:
        return "verdicts_sent" if verdicts is not None else "reviewing"
    if batch is not None:
        return "awaiting_agent"
    return "collecting"


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


# Rewrite root-absolute references so every asset a BEFORE page pulls in is
# also served from the before snapshot (relative refs resolve under the
# /__wk/before/ prefix automatically). Without this the toggle silently shows
# CURRENT-tree assets whenever a path is unchanged but its CONTENT changed —
# corrupting the exact before/after fidelity the toggle exists to provide.
# We deliberately do NOT use a <base> tag: it has no effect on root-absolute
# ("/…") URLs, which is precisely the case we must fix.
#
# Four reference forms are covered: (1) src/href/poster attrs in either quote
# style, (2) srcset candidate lists (comma-split, each URL rewritten), (3)
# url()/@import inside inline <style> blocks and style="" attrs, and (4)
# root-absolute url()/@import inside external stylesheets (rewritten in the
# text/css branch of _wk_before). All exclude /__wk/ itself and
# protocol-relative //host URLs.
_ROOT_REF = re.compile(r'\b(src|href|poster)=(["\'])/(?!/|__wk/)')
_SRCSET = re.compile(r'\b(srcset)=(["\'])(.*?)\2', re.S)
_CSS_URL = re.compile(r'url\(\s*(["\']?)/(?!/|__wk/)')
_CSS_IMPORT = re.compile(r'(@import\s+)(["\'])/(?!/|__wk/)')


def _rewrite_before_srcset(m):
    attr, quote, val = m.group(1), m.group(2), m.group(3)
    out = []
    for cand in val.split(","):
        parts = cand.strip().split(None, 1)
        if not parts:
            continue
        url = parts[0]
        if url.startswith("/") and not url.startswith(("//", "/__wk/")):
            parts[0] = "/__wk/before" + url
        out.append(" ".join(parts))
    return "{}={}{}{}".format(attr, quote, ", ".join(out), quote)


def _rewrite_before_css(text):
    # Root-absolute url(...) and @import "..." → /__wk/before/... . Applied to
    # HTML bodies (inline <style> + style="") and to served .css text.
    text = _CSS_URL.sub(r"url(\1/__wk/before/", text)
    text = _CSS_IMPORT.sub(r"\1\2/__wk/before/", text)
    return text


def _rewrite_before_html(html):
    html = _ROOT_REF.sub(r"\1=\2/__wk/before/", html)
    html = _SRCSET.sub(_rewrite_before_srcset, html)
    return _rewrite_before_css(html)


# A beforeRef is always a full `git rev-parse HEAD` SHA (per LOOP.md). Validate
# it against this before it ever reaches `git show`: a value beginning with `-`
# would otherwise be parsed by git as an OPTION (e.g. --output=…), an argument-
# injection / arbitrary-file-write vector. See _wk_before.
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")

_MAX_BODY = 2 * 1024 * 1024
_MAX_VOICE_BODY = 25 * 1024 * 1024
_VOICE_ID = re.compile(r"^[A-Za-z0-9_-]{6,80}$")
_VOICE_TYPES = {
    "audio/webm": ".webm", "audio/ogg": ".ogg", "audio/mp4": ".m4a",
    "audio/mpeg": ".mp3", "audio/wav": ".wav", "audio/x-wav": ".wav",
}


class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        # Live dev preview: never let the browser cache ANY asset. The base
        # SimpleHTTPRequestHandler only sends Last-Modified for static files
        # (css/js/images), so an edited stylesheet could be served stale from
        # disk cache even after a ?v= bump — this stamps no-store on every
        # response (HTML, /__wk JSON, overlay assets, before-mode: all of it).
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    # --- tiny response helpers ---
    def _send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body, ctype, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # --- request entry points ---
    def do_GET(self):
        raw = self.path.split("?", 1)[0]
        if raw.startswith("/__wk/"):
            return self._wk(self._wk_get, raw)
        if API_PROXY_ORIGIN and raw.startswith("/api/"):
            return self._proxy_api("GET")
        path = self.translate_path(self.path)
        if os.path.isdir(path):
            if not raw.endswith("/"):
                return super().do_GET()  # base class issues the trailing-slash redirect
            for idx in ("index.html", "index.htm"):
                cand = os.path.join(path, idx)
                if os.path.isfile(cand):
                    path = cand
                    break
            else:
                return super().do_GET()  # directory listing
        if path.endswith((".html", ".htm")) and os.path.isfile(path):
            return self._serve_html(path, include_body=True)
        return super().do_GET()

    def do_HEAD(self):
        # HEAD must report the SAME Content-Length GET would return. Since GET
        # stamps the <title> and injects the overlay (growing the body), the
        # base class's do_HEAD (which reports the on-disk file size) would lie
        # to prefetchers/link-checkers that HEAD before GETting. Route HTML
        # through the same transform with the body suppressed; everything else
        # (non-HTML, /__wk/, directory listings) keeps base-class behavior.
        raw = self.path.split("?", 1)[0]
        if raw.startswith("/__wk/"):
            return super().do_HEAD()
        path = self.translate_path(self.path)
        if os.path.isdir(path):
            if not raw.endswith("/"):
                return super().do_HEAD()  # base class issues the redirect
            for idx in ("index.html", "index.htm"):
                cand = os.path.join(path, idx)
                if os.path.isfile(cand):
                    path = cand
                    break
            else:
                return super().do_HEAD()  # directory listing
        if path.endswith((".html", ".htm")) and os.path.isfile(path):
            return self._serve_html(path, include_body=False)
        return super().do_HEAD()

    def _serve_html(self, path, include_body):
        # Shared GET/HEAD body computation so both report identical headers
        # (Content-Length in particular) for the transformed HTML.
        with open(path, "rb") as f:
            body = f.read()
        try:
            html = stamp(body.decode("utf-8"))
            seed_preview = parse_qs(urlparse(self.path).query).get("wk_seed_preview", [""])[0] == "1"
            body = (html if seed_preview else inject(html, "after")).encode("utf-8")
        except UnicodeDecodeError:
            pass
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()  # no-store added by the override above
        if include_body:
            self.wfile.write(body)

    def do_POST(self):
        raw = self.path.split("?", 1)[0]
        if raw.startswith("/__wk/"):
            return self._wk(self._wk_post, raw)
        if API_PROXY_ORIGIN and raw.startswith("/api/"):
            return self._proxy_api("POST")
        return self._send_json(404, {"error": "POST is only supported under /__wk/"})

    def _proxy_api(self, method):
        """Forward Control Center API calls while the UI is WebKit-injected.

        The upstream server still owns all application behavior. The preview
        server only gives its static interface the normal WebKit overlay, then
        keeps same-origin browser requests working by relaying /api/* locally.
        """
        body = None
        if method == "POST":
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self._send_json(400, {"error": "bad Content-Length"})
            if length > _MAX_BODY:
                return self._send_json(413, {"error": "body exceeds 2 MB"})
            body = self.rfile.read(length) if length else b""
        headers = {"Accept": self.headers.get("Accept", "application/json")}
        content_type = self.headers.get("Content-Type")
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(
            API_PROXY_ORIGIN + self.path,
            data=body,
            headers=headers,
            method=method,
        )
        try:
            response = urllib.request.urlopen(request, timeout=30)
        except urllib.error.HTTPError as exc:
            response = exc
        except (OSError, urllib.error.URLError) as exc:
            return self._send_json(502, {"error": "Control Center API unavailable: {}".format(exc)})
        with response:
            payload = response.read()
            self.send_response(response.status)
            self.send_header(
                "Content-Type",
                response.headers.get("Content-Type", "application/octet-stream"),
            )
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    def _wk(self, method, raw):
        # /__wk/* is API surface: a bug in a handler must come back as JSON,
        # not a dropped connection with a traceback only in the terminal.
        try:
            return method(raw)
        except (BrokenPipeError, ConnectionResetError):
            raise
        except Exception as e:  # noqa: BLE001 — deliberate API-boundary catch
            try:
                return self._send_json(500, {"error": "internal error: {}".format(e)})
            except OSError:
                pass

    # --- GET routes ---
    def _wk_get(self, raw):
        if raw in ("/__wk/overlay.js", "/__wk/overlay.css"):
            return self._wk_overlay_asset(os.path.basename(raw))
        if raw == "/__wk/state":
            return self._wk_state()
        if raw == "/__wk/before" or raw.startswith("/__wk/before/"):
            return self._wk_before(raw)
        return self._send_json(404, {"error": "unknown webkit endpoint: " + raw})

    def _wk_overlay_asset(self, name):
        try:
            with open(os.path.join(OVERLAY_DIR, name), "rb") as f:
                body = f.read()
        except OSError:
            return self._send_json(
                404, {"error": "overlay asset missing from kit: " + name}
            )
        ctype = (
            "text/javascript; charset=utf-8"
            if name.endswith(".js")
            else "text/css; charset=utf-8"
        )
        return self._send_bytes(body, ctype)

    def _wk_state(self):
        if FEEDBACK_DIR is None:
            return self._send_json(503, {"error": "not a git repository — feedback loop disabled"})
        known = parse_qs(urlparse(self.path).query).get("known", [""])[0]
        rev = _rev()
        if known == rev:
            return self._send_json(200, {"changed": False, "rev": rev})
        batch = _load_data("feedback.json")
        review = _load_data("review.json")
        verdicts = _load_data("verdicts.json")
        return self._send_json(
            200,
            {
                "changed": True,
                "rev": rev,
                "color": SLUG,
                "emoji": COLOR,
                "phase": _phase(batch, review, verdicts),
                "batch": batch,
                "review": review,
                "verdicts": verdicts,
            },
        )

    def _wk_before(self, raw):
        if GIT_ROOT is None:
            return self._send_json(503, {"error": "not a git repository — before mode disabled"})
        sub = raw[len("/__wk/before"):] or "/"
        # Resolve exactly like normal serving so BEFORE|AFTER swap 1:1.
        fs = self.translate_path(sub)
        if os.path.isdir(fs):
            if not raw.endswith("/"):
                # Mirror the base class's trailing-slash redirect so relative
                # asset URLs inside the page resolve under the right prefix.
                query = self.path.partition("?")[2]
                self.send_response(301)
                self.send_header("Location", raw + "/" + ("?" + query if query else ""))
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            for idx in ("index.html", "index.htm"):
                cand = os.path.join(fs, idx)
                if os.path.isfile(cand):
                    fs = cand
                    break
            else:
                return self._send_json(404, {"error": "no index page in directory"})
        rel = os.path.relpath(os.path.realpath(fs), os.path.realpath(GIT_ROOT))
        if rel.startswith(".."):
            return self._send_json(403, {"error": "path escapes the git repository"})
        # beforeRef is read fresh on EVERY request: the agent rewrites
        # review.json at round transitions and the toggle must follow it.
        review = _load_data("review.json") if FEEDBACK_DIR else None
        before_ref = (review or {}).get("beforeRef")
        if not before_ref:
            return self._send_json(409, {"error": "no review round active"})
        # beforeRef flows into `git show` as a rev — it MUST be a bare SHA. An
        # unvalidated value beginning with `-` would be parsed by git as an
        # option (git argument injection → arbitrary file write). Reject
        # anything that isn't a hex SHA, AND pass --end-of-options as a second
        # guard so even a hypothetical leading-`-` ref can't be read as a flag.
        if not isinstance(before_ref, str) or not _SHA_RE.match(before_ref):
            return self._send_json(409, {"error": "review.json beforeRef is not a valid commit SHA"})
        try:
            p = subprocess.run(
                ["git", "-C", GIT_ROOT, "show", "--end-of-options",
                 "{}:{}".format(before_ref, rel.replace(os.sep, "/"))],
                capture_output=True,
            )
        except OSError:
            p = None
        if p is None or p.returncode != 0:
            return self._send_json(404, {"error": "not in before snapshot"})
        body = p.stdout
        if fs.endswith((".html", ".htm")):
            try:
                html = body.decode("utf-8")
            except UnicodeDecodeError:
                return self._send_bytes(body, "text/html; charset=utf-8")
            html = stamp(html)
            html = _rewrite_before_html(html)
            html = inject(html, "before")
            return self._send_bytes(html.encode("utf-8"), "text/html; charset=utf-8")
        ctype = mimetypes.guess_type(fs)[0] or "application/octet-stream"
        # External stylesheets are served verbatim from git — but any
        # root-absolute url()/@import inside them would escape to the current
        # tree, so run the same CSS rewrite the HTML path uses (the href that
        # loaded this CSS was already rewritten to come through /__wk/before/).
        if ctype == "text/css" or fs.endswith(".css"):
            try:
                text = body.decode("utf-8")
            except UnicodeDecodeError:
                return self._send_bytes(body, "text/css; charset=utf-8")
            text = _rewrite_before_css(text)
            return self._send_bytes(text.encode("utf-8"), "text/css; charset=utf-8")
        return self._send_bytes(body, ctype)

    # --- POST routes ---
    def _wk_post(self, raw):
        if raw == "/__wk/feedback":
            return self._wk_feedback()
        if raw == "/__wk/voice-note":
            return self._wk_voice_note()
        if raw == "/__wk/voice-note/delete":
            return self._wk_voice_note_delete()
        if raw == "/__wk/verdicts":
            return self._wk_verdicts()
        return self._send_json(404, {"error": "unknown webkit endpoint: " + raw})

    def _wk_voice_note(self):
        if FEEDBACK_DIR is None:
            return self._send_json(503, {"error": "not a git repository — feedback loop disabled"})
        note_id = parse_qs(urlparse(self.path).query).get("id", [""])[0]
        if not _VOICE_ID.match(note_id):
            return self._send_json(400, {"error": "invalid voice-note id"})
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        extension = _VOICE_TYPES.get(content_type)
        if not extension:
            return self._send_json(415, {"error": "unsupported voice-note audio type"})
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._send_json(411, {"error": "Content-Length required"})
        if length <= 0:
            return self._send_json(400, {"error": "voice note is empty"})
        if length > _MAX_VOICE_BODY:
            return self._send_json(413, {"error": "voice note exceeds 25 MB"})
        body = self.rfile.read(length)
        if len(body) != length:
            return self._send_json(400, {"error": "voice note upload was incomplete"})
        directory = os.path.join(FEEDBACK_DIR, "voice-notes")
        os.makedirs(directory, exist_ok=True)
        filename = note_id + extension
        path = os.path.join(directory, filename)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix="." + filename + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(body)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        relative = os.path.relpath(path, GIT_ROOT).replace(os.sep, "/")
        return self._send_json(201, {
            "ok": True,
            "voiceNote": {"path": relative, "mimeType": content_type, "bytes": length},
        })

    def _wk_voice_note_delete(self):
        incoming = self._read_json_body()
        if incoming is None:
            return
        relative = incoming.get("path")
        if not isinstance(relative, str):
            return self._send_json(400, {"error": "voice-note path is required"})
        directory = os.path.realpath(os.path.join(FEEDBACK_DIR, "voice-notes"))
        candidate = os.path.realpath(os.path.join(GIT_ROOT, relative))
        try:
            inside = os.path.commonpath((directory, candidate)) == directory
        except ValueError:
            inside = False
        if not inside or not _VOICE_ID.match(os.path.splitext(os.path.basename(candidate))[0]):
            return self._send_json(400, {"error": "invalid voice-note path"})
        try:
            os.unlink(candidate)
        except FileNotFoundError:
            pass
        return self._send_json(200, {"ok": True})

    def _read_json_body(self):
        """Read+parse the POST body. Returns the object, or None after having
        already sent the error response (503/411/400/413)."""
        if FEEDBACK_DIR is None:
            self._send_json(503, {"error": "not a git repository — feedback loop disabled"})
            return None
        length = self.headers.get("Content-Length")
        if length is None:
            self._send_json(411, {"error": "Content-Length required"})
            return None
        try:
            length = int(length)
        except ValueError:
            self._send_json(400, {"error": "bad Content-Length"})
            return None
        if length > _MAX_BODY:
            self._send_json(413, {"error": "body exceeds 2 MB"})
            return None
        try:
            obj = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"error": "body is not valid JSON"})
            return None
        if not isinstance(obj, dict):
            self._send_json(400, {"error": "body must be a JSON object"})
            return None
        return obj

    def _wk_feedback(self):
        incoming = self._read_json_body()
        if incoming is None:
            return
        if incoming.get("version") != 1:
            return self._send_json(400, {"error": "expected a version-1 feedback batch"})
        points = incoming.get("points")
        if not isinstance(points, list) or not points:
            return self._send_json(400, {"error": "batch has no points"})
        with _WRITE_LOCK:
            existing = _load_data("feedback.json")
            review = _load_data("review.json")
            claimed = (
                existing is not None
                and review is not None
                and review.get("batchId") == existing.get("batchId")
            )
            if existing is not None and not claimed:
                # The user drew more points before the agent picked the batch
                # up — fold them into the standing batch (same batchId, so the
                # agent still sees exactly one unit of work), numbering
                # continued so pins stay stable.
                old_points = list(existing.get("points") or [])
                nums = [
                    p.get("number") for p in old_points
                    if isinstance(p.get("number"), int)
                ]
                base = max(nums) if nums else len(old_points)
                for i, pt in enumerate(points):
                    pt = dict(pt)
                    pt["number"] = base + 1 + i
                    old_points.append(pt)
                existing["points"] = old_points
                pages = list(existing.get("pages") or [])
                for pg in incoming.get("pages") or []:
                    if pg not in pages:
                        pages.append(pg)
                existing["pages"] = pages
                # batchId/createdAt stay the original's; only updatedAt moves.
                existing["updatedAt"] = (
                    incoming.get("updatedAt") or incoming.get("createdAt") or _now_iso()
                )
                _atomic_write("feedback.json", existing)
                batch_id = existing.get("batchId")
            elif claimed:
                # Additions belong to the standing batch even during review.
                # Wake the agent's verdict waiter with a typed interruption so
                # it can extend the same batch instead of making the user wait
                # for an artificial second batch.
                old_points = list(existing.get("points") or [])
                known_ids = {p.get("id") for p in old_points}
                nums = [p.get("number") for p in old_points if isinstance(p.get("number"), int)]
                base = max(nums) if nums else len(old_points)
                added_ids = []
                for pt in points:
                    if pt.get("id") in known_ids:
                        continue
                    pt = dict(pt)
                    base += 1
                    pt["number"] = base
                    old_points.append(pt)
                    known_ids.add(pt.get("id"))
                    added_ids.append(pt.get("id"))
                existing["points"] = old_points
                pages = list(existing.get("pages") or [])
                for pg in incoming.get("pages") or []:
                    if pg not in pages:
                        pages.append(pg)
                existing["pages"] = pages
                existing["updatedAt"] = incoming.get("updatedAt") or _now_iso()
                _atomic_write("feedback.json", existing)
                _atomic_write("verdicts.json", {
                    "version": 1,
                    "kind": "feedback_update",
                    "batchId": existing.get("batchId"),
                    "round": review.get("round", 1),
                    "sentAt": _now_iso(),
                    "addedPointIds": added_ids,
                })
                batch_id = existing.get("batchId")
            else:
                # Fresh inbox (existing is None after archive), or an idempotent
                # retry of the same batchId that IS under review.
                _atomic_write("feedback.json", incoming)
                batch_id = incoming.get("batchId")
        return self._send_json(200, {"ok": True, "batchId": batch_id})

    def _wk_verdicts(self):
        incoming = self._read_json_body()
        if incoming is None:
            return
        with _WRITE_LOCK:
            review = _load_data("review.json")
            if review is None:
                return self._send_json(409, {"error": "no review round active"})
            if incoming.get("batchId") != review.get("batchId"):
                return self._send_json(
                    409,
                    {
                        "error": "batchId mismatch: the active review round is {}".format(
                            review.get("batchId")
                        )
                    },
                )
            # Same batch can span multiple rounds (the agent re-queues redone
            # points into review.json with a bumped `round`). A tab still
            # holding an earlier round's review state would otherwise post
            # verdicts whose pointIds don't match the current round — accepting
            # already-settled points and missing the redone ones. Reject on
            # round skew; the overlay re-polls and re-enters the live round.
            if incoming.get("round") != review.get("round"):
                return self._send_json(
                    409,
                    {
                        "error": "round mismatch: the active review round is {}".format(
                            review.get("round")
                        )
                    },
                )
            _atomic_write("verdicts.json", incoming)
        return self._send_json(200, {"ok": True})


if __name__ == "__main__":
    os.chdir(ROOT)
    # flush=True: agents run this in the background with stdout redirected to
    # a log and then read the log to confirm boot — without the flush the
    # block-buffered messages only appear at exit.
    print(
        "serving {} on :{} with tab color {} ({})".format(ROOT, PORT, COLOR, SLUG),
        flush=True,
    )
    if FEEDBACK_DIR:
        print("feedback inbox: {}".format(FEEDBACK_DIR), flush=True)
    else:
        print(
            "WARNING: {} is not inside a git repository — static serving only "
            "(feedback + before-mode endpoints will answer 503).".format(ROOT),
            flush=True,
        )
    # Bind loopback ONLY. The browser, overlay, and agent are all on this
    # machine, so nothing needs remote access — and binding all interfaces
    # would expose the entire served working tree AND the /__wk/feedback
    # channel (whose `text` fields the agent applies and commits) to anyone on
    # the LAN, i.e. unauthenticated remote prompt-injection into an agent with
    # write access. bind_host is configurable for deliberate LAN use, but SETUP
    # documents that 0.0.0.0 forfeits both protections.
    HOST = CONFIG.get("bind_host", "127.0.0.1")
    # ThreadingHTTPServer: a browser tab that holds a connection open (WebGL
    # pages keep-alive) must not block other requests (e.g. headless QA) — the
    # single-threaded HTTPServer would hang the whole server on one stuck client.
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
