#!/usr/bin/env python3
"""Color-stamping preview server + feedback-loop API for Ethan's AWESOME WEBKIT.

Drop-in replacement for `python3 -m http.server`, extended in two directions:

1. TAB IDENTITY (the battle-tested part). Serves the working tree as-is but
   rewrites the <title> of every HTML page it hands out so the browser tab
   leads with this agent's color emoji - source files are never touched, and
   the label survives every reload (unlike setting document.title from the
   outside). Each agent runs its own server on its own port; the color in the
   tab tells the user which design/agent a tab belongs to at a glance.

2. FEEDBACK LOOP (the webkit part). Every served HTML page gets the webkit
   overlay injected (a single <script> tag; the overlay itself lives in
   ../overlay/). The overlay talks back to this server over /__wk/* endpoints:
   it POSTs feedback batches into the agent's per-color inbox
   (<git-root>/<feedback_dir>/<slug>/feedback.json), polls /__wk/state for
   round transitions, POSTs verdicts, and - during review - loads the
   pre-round version of any page straight out of git via /__wk/before/<path>
   so the user can flip BEFORE|AFTER without the agent stashing anything.

Usage:  preview-server.py <color-emoji> [port] [root-dir]
  e.g.  python3 webkit/server/preview-server.py 🔵
        (port defaults to the palette entry's port for that emoji;
         root-dir - the DOCUMENT ROOT to serve - defaults to the config's
         site_root resolved relative to the project root, else the current
         working directory. site_root is what gets served; it is NOT where
         the feedback inbox lives - that stays anchored at the git root.)

Config: $WK_CONFIG if set, else ../webkit.config.json, else
../webkit.config.template.json (both relative to this file). The palette in
the config is the single source of truth for emojis/slugs/ports - nothing
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
import ipaddress
import hashlib
import hmac
import html as html_lib
import json
import math
import mimetypes
import os
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import parse_qs, unquote, urlparse

_HERE = os.path.dirname(os.path.abspath(__file__))
_MAX_PROTOCOL_FILE = 2 * 1024 * 1024
_MAX_CONFIG_FILE = 1024 * 1024
_MAX_HISTORY_ROUNDS = 200
_MAX_HISTORY_BYTES = 128 * 1024 * 1024
_MAX_HISTORY_SCAN_ENTRIES = 4096
_RUNTIME_SCRIPTS = os.path.realpath(os.path.join(_HERE, "..", "scripts"))
if _RUNTIME_SCRIPTS not in sys.path:
    sys.path.insert(0, _RUNTIME_SCRIPTS)
try:
    from runtime_registry import (  # noqa: E402
        RegistryError as _RuntimeRegistryError,
        inspect_reservation as _inspect_port_reservation,
        read_color_lock as _read_color_lock,
        register_instance as _register_preview_instance,
        touch_reservation as _touch_port_reservation,
    )
except (ImportError, OSError) as exc:
    sys.exit("preview-server.py: runtime port registry support is unavailable: {}".format(exc))


def _reject_json_constant(value):
    raise ValueError("non-finite JSON number: {}".format(value))


def _reject_json_surrogates(value):
    if isinstance(value, str):
        if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise ValueError("JSON contains an unpaired Unicode surrogate")
    elif isinstance(value, list):
        for item in value:
            _reject_json_surrogates(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            _reject_json_surrogates(key)
            _reject_json_surrogates(item)
    return value

# --- config ------------------------------------------------------------------
# The kit is config-driven: palette (emoji/slug/port), lock_dir, feedback_dir
# all come from webkit.config.json so a project can rebrand the whole palette
# without touching code. The template is the last-resort fallback so the kit
# works out of the box straight from a fresh clone.


def _read_config_text(path):
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError("cannot inspect config: {}".format(exc))
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("config must be a regular file, not a link or special path")
    if before.st_size > _MAX_CONFIG_FILE:
        raise ValueError("config exceeds 1 MB")
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = None
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        chunks = []
        remaining = _MAX_CONFIG_FILE + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after_read = os.fstat(descriptor)
        current = os.lstat(path)
    except OSError as exc:
        raise ValueError("config could not be read safely: {}".format(exc))
    finally:
        if descriptor is not None:
            os.close(descriptor)
    def signature(value):
        return (
            value.st_dev, value.st_ino, value.st_mode, value.st_size,
            getattr(value, "st_mtime_ns", int(value.st_mtime * 1000000000)),
        )
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(current.st_mode)
        or signature(opened) != signature(before)
        or signature(opened) != signature(after_read)
        or signature(opened) != signature(current)
    ):
        raise ValueError("config changed while it was read")
    data = b"".join(chunks)
    if len(data) > _MAX_CONFIG_FILE:
        raise ValueError("config exceeds 1 MB")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("config is not UTF-8: {}".format(exc))


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
        try:
            source = _read_config_text(cand)
            if source is None:
                continue
            config = json.loads(source, parse_constant=_reject_json_constant)
            return _reject_json_surrogates(config), os.path.abspath(cand)
        except ValueError as e:
            sys.exit("preview-server.py: {} is not valid JSON: {}".format(cand, e))
    sys.exit(
        "preview-server.py: no config found (looked for {}).".format(
            ", ".join(os.path.abspath(c) for c in candidates)
        )
    )


CONFIG, CONFIG_PATH = _load_config()
if not isinstance(CONFIG, dict):
    sys.exit("preview-server.py: config {} must contain a JSON object.".format(CONFIG_PATH))
GRACE_SECONDS = CONFIG.get("grace_seconds", 180)
if (
    isinstance(GRACE_SECONDS, bool)
    or not isinstance(GRACE_SECONDS, int)
    or not 30 <= GRACE_SECONDS <= 86400
):
    sys.exit(
        "preview-server.py: grace_seconds must be an integer from 30 through 86400."
    )
_api_proxy_value = CONFIG.get("api_proxy_origin", "")
if not isinstance(_api_proxy_value, str):
    sys.exit("preview-server.py: api_proxy_origin must be a string.")
API_PROXY_ORIGIN = _api_proxy_value.rstrip("/")
API_PROXY_HOST_HEADER = ""
if API_PROXY_ORIGIN:
    if os.environ.get("WK_ENABLE_API_PROXY") != "1":
        sys.exit(
            "preview-server.py: api_proxy_origin requires the exact process-level "
            "opt-in WK_ENABLE_API_PROXY=1 because it forwards a privileged header."
        )
    try:
        _proxy_url = urlparse(API_PROXY_ORIGIN)
        _proxy_port = _proxy_url.port
    except ValueError as exc:
        sys.exit("preview-server.py: api_proxy_origin is invalid: {}.".format(exc))
    if (
        _proxy_url.scheme != "http"
        or _proxy_url.hostname not in ("127.0.0.1", "localhost", "::1")
        or _proxy_port is None
        or not 1 <= _proxy_port <= 65535
        or _proxy_url.username is not None
        or _proxy_url.password is not None
        or _proxy_url.path not in ("", "/")
        or _proxy_url.params
        or _proxy_url.query
        or _proxy_url.fragment
    ):
        sys.exit(
            "preview-server.py: api_proxy_origin must be a bare loopback http origin "
            "with an explicit port."
        )
    if _proxy_url.hostname == "localhost":
        try:
            _proxy_addresses = {
                item[4][0].split("%", 1)[0]
                for item in socket.getaddrinfo(
                    "localhost", _proxy_port, type=socket.SOCK_STREAM
                )
            }
        except OSError as exc:
            sys.exit(
                "preview-server.py: localhost API proxy resolution failed: {}.".format(exc)
            )
        try:
            _proxy_all_loopback = bool(_proxy_addresses) and all(
                ipaddress.ip_address(address).is_loopback
                for address in _proxy_addresses
            )
        except ValueError:
            _proxy_all_loopback = False
        if not _proxy_all_loopback:
            sys.exit(
                "preview-server.py: localhost API proxy must resolve only to loopback addresses."
            )
        _proxy_literal = (
            "127.0.0.1" if "127.0.0.1" in _proxy_addresses
            else sorted(_proxy_addresses)[0]
        )
        _proxy_literal_host = (
            "[{}]".format(_proxy_literal) if ":" in _proxy_literal else _proxy_literal
        )
        API_PROXY_HOST_HEADER = "localhost:{}".format(_proxy_port)
        API_PROXY_ORIGIN = "http://{}:{}".format(_proxy_literal_host, _proxy_port)
BIND_HOST = CONFIG.get("bind_host", "127.0.0.1")
if not isinstance(BIND_HOST, str) or not BIND_HOST:
    sys.exit("preview-server.py: bind_host must be a non-empty string.")
if BIND_HOST not in ("127.0.0.1", "localhost", "0.0.0.0"):
    sys.exit(
        "preview-server.py: bind_host must be 127.0.0.1, localhost, or "
        "0.0.0.0; IPv6 and specific non-loopback binds are not supported."
    )
_LOOPBACK_BIND = BIND_HOST in ("127.0.0.1", "localhost")
_LOOPBACK_ALLOWED_HOSTS = {"127.0.0.1", "localhost"}


def _normalize_allowed_host(value):
    if not isinstance(value, str) or value != value.strip() or not value:
        return None
    value = value.lower()
    if value.startswith("[") or value.endswith("]"):
        return None
    if ":" in value or not re.fullmatch(
        r"[a-z0-9](?:[a-z0-9._-]{0,251}[a-z0-9])?", value
    ):
        return None
    return value


if _LOOPBACK_BIND:
    ALLOWED_HOSTS = set(_LOOPBACK_ALLOWED_HOSTS)
else:
    _configured_allowed_hosts = CONFIG.get("allowed_hosts")
    if not isinstance(_configured_allowed_hosts, list) or not _configured_allowed_hosts:
        sys.exit(
            "preview-server.py: non-loopback bind_host requires a non-empty allowed_hosts list."
        )
    ALLOWED_HOSTS = {
        _normalize_allowed_host(value) for value in _configured_allowed_hosts
    }
    if None in ALLOWED_HOSTS or not ALLOWED_HOSTS:
        sys.exit(
            "preview-server.py: allowed_hosts entries must be hostnames or IPv4 "
            "addresses without a scheme, port, path, or wildcard; IPv6 is not supported."
        )
    ALLOWED_HOSTS.update(_LOOPBACK_ALLOWED_HOSTS)
_mutation_token = os.environ.get("WK_MUTATION_TOKEN", "")
MUTATION_TOKEN = (
    _mutation_token
    if re.fullmatch(r"[A-Za-z0-9_-]{16,128}", _mutation_token)
    else secrets.token_urlsafe(32)
)
# Archive transitions are agent-only. The browser token above is present in
# injected HTML, so it must never authorize history or live-state moves.
TRANSITION_TOKEN = secrets.token_urlsafe(32)
_instance_token = os.environ.get("WK_PREVIEW_INSTANCE_TOKEN", "")
INSTANCE_TOKEN = (
    _instance_token
    if re.fullmatch(r"[A-Za-z0-9_-]{16,128}", _instance_token)
    else secrets.token_urlsafe(32)
)
_BEFORE_CAPABILITY_KEY = secrets.token_bytes(32)

_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_BATCH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_POINT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_BIDI_FORMATTING_CLASSES = {"LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI"}


def _valid_color_label(value):
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 32
        and all(
            not char.isspace()
            and unicodedata.category(char) != "Cc"
            and unicodedata.bidirectional(char) not in _BIDI_FORMATTING_CLASSES
            and char not in "<>&\"'`"
            for char in value
        )
    )


PALETTE = CONFIG.get("palette") or []
if not PALETTE or not all(
    isinstance(p, dict)
    and isinstance(p.get("emoji"), str)
    and _valid_color_label(p.get("emoji"))
    and isinstance(p.get("slug"), str)
    and _SLUG_RE.fullmatch(p.get("slug"))
    and isinstance(p.get("port"), int)
    and not isinstance(p.get("port"), bool)
    and 1 <= p.get("port") <= 65535
    for p in PALETTE
):
    sys.exit(
        "preview-server.py: config {} has no usable palette "
        "(need safe slug, short display label, and integer ports from 1 to 65535).".format(CONFIG_PATH)
    )
if len({p["emoji"] for p in PALETTE}) != len(PALETTE) or len(
    {p["slug"] for p in PALETTE}
) != len(PALETTE):
    sys.exit("preview-server.py: palette emoji and slug values must be unique.")
if len({p["port"] for p in PALETTE}) != len(PALETTE):
    sys.exit("preview-server.py: palette port values must be unique.")
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
    if not 1 <= PORT <= 65535:
        sys.exit("preview-server.py: port must be between 1 and 65535.")
else:
    PORT = _PORTS.get(COLOR)
    if not PORT:
        sys.exit(
            "preview-server.py: palette entry for {} has no port in {} - "
            "pass one explicitly.".format(COLOR, CONFIG_PATH)
        )
    PORT = int(PORT)
if len(sys.argv) > 3:
    ROOT = os.path.abspath(sys.argv[3])
else:
    # No root-dir on the CLI: fall back to the config's site_root - the
    # directory this server should serve (its document root). site_root is
    # relative to the project root, and the config always lives at
    # <project>/webkit/webkit.config.json, so the project root is two levels up
    # from CONFIG_PATH. This is the SERVE dir only; the feedback inbox is a
    # separate concern anchored at the git root (see FEEDBACK_DIR below).
    _site_root = CONFIG.get("site_root")
    if _site_root is not None:
        if (
            not isinstance(_site_root, str)
            or not _site_root
            or os.path.isabs(_site_root)
        ):
            sys.exit(
                "preview-server.py: site_root must be a non-empty relative path."
            )
        _project_dir = os.path.realpath(os.path.dirname(os.path.dirname(CONFIG_PATH)))
        ROOT = os.path.realpath(os.path.join(_project_dir, _site_root))
        try:
            _inside_project = os.path.commonpath((_project_dir, ROOT)) == _project_dir
        except ValueError:
            _inside_project = False
        if not _inside_project:
            sys.exit("preview-server.py: site_root must stay inside the project directory.")
        if not os.path.isdir(ROOT):
            sys.exit("preview-server.py: configured site_root does not exist or is not a directory.")
    else:
        ROOT = os.getcwd()

ROOT = os.path.realpath(ROOT)
if not os.path.isdir(ROOT):
    sys.exit("preview-server.py: root directory does not exist or is not a directory.")


def _path_is_within(base, candidate):
    base = os.path.realpath(base)
    candidate = os.path.realpath(candidate)
    try:
        return os.path.commonpath((base, candidate)) == base
    except ValueError:
        return False


def _path_is_lexically_within(base, candidate):
    base = os.path.abspath(base)
    candidate = os.path.abspath(candidate)
    try:
        return os.path.commonpath((base, candidate)) == base
    except ValueError:
        return False


_PRIVATE_STATIC_NAMES = {
    "credentials",
    "credentials.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "service-account.json",
    "secrets.json",
    "secrets.yaml",
    "secrets.yml",
    "webkit.config.json",
}
_PRIVATE_STATIC_SUFFIXES = (
    ".jks", ".key", ".keystore", ".kdbx", ".p12", ".pem", ".pfx"
)


def _private_static_component(component):
    lowered = component.casefold()
    return (
        not component
        or component.startswith(".")
        or lowered in _PRIVATE_STATIC_NAMES
        or lowered.endswith(_PRIVATE_STATIC_SUFFIXES)
        or any(ord(char) < 32 or ord(char) == 127 for char in component)
    )


def _static_path_allowed(request_path, translated_path):
    """Reject private URL components and private resolved symlink targets."""
    decoded = urlparse(request_path).path
    for _ in range(3):
        replacement = unquote(decoded, errors="replace")
        if replacement == decoded:
            break
        decoded = replacement
    if "\\" in decoded or "\x00" in decoded:
        return False
    requested = [part for part in decoded.split("/") if part]
    if any(part in (".", "..") or _private_static_component(part) for part in requested):
        return False
    resolved = os.path.realpath(translated_path)
    if not _path_is_within(ROOT, resolved):
        return False
    relative = os.path.relpath(resolved, ROOT)
    if relative == os.curdir:
        return True
    return not any(
        part in (".", "..") or _private_static_component(part)
        for part in relative.split(os.sep)
    )

# --- claim enforcement -------------------------------------------------------
# Kept from the original after a real two-agents-on-one-color collision: an
# agent skipped claim-color.sh (assumed its color from a prior session's
# notes) and stamped an emoji another agent had properly locked. The lock
# registry can only protect claims that go through it, so the STAMPING point
# demands proof: serving refuses unless this color's lock exists and - when
# the lock records an owner - that owner is the worktree being served.
# Manual runs outside the agent flow can bypass with WK_COLOR_FORCE=1
# (or just claim first).


def _verify_claim():
    if os.environ.get("WK_COLOR_FORCE") == "1":
        return None
    lockdir = (
        os.environ.get("WK_COLOR_LOCKDIR")
        or CONFIG.get("lock_dir")
        or "/tmp/webkit-agent-colors"
    )
    if (
        not isinstance(lockdir, str)
        or not lockdir
        or not os.path.isabs(lockdir)
        or os.path.realpath(lockdir) == os.path.realpath(os.path.sep)
        or os.path.islink(lockdir)
    ):
        sys.exit(
            "preview-server.py: lock_dir must be an absolute, non-symlink path "
            "that does not resolve to the filesystem root."
        )
    lock = os.path.join(lockdir, SLUG + ".lock")
    try:
        lock_stat = os.lstat(lock)
    except OSError:
        lock_stat = None
    if lock_stat is None or not stat.S_ISDIR(lock_stat.st_mode):
        sys.exit(
            "preview-server.py: refusing to stamp {} - no claim lock at {}.\n"
            "Claim your color first (and keep it for the whole session):\n"
            "    color=$(webkit/scripts/claim-color.sh)\n"
            "Never assume a color from a previous session or from notes.".format(COLOR, lock)
        )
    try:
        owner, reservation_token, _lock_stat = _read_color_lock(lock)
    except _RuntimeRegistryError as exc:
        sys.exit(
            "preview-server.py: refusing unsafe {} claim at {}: {}".format(
                COLOR, lock, exc
            )
        )
    claimed_root = os.path.realpath(GIT_ROOT or ROOT)
    if os.path.realpath(owner) != claimed_root:
        sys.exit(
            "preview-server.py: refusing to stamp {} - its lock is owned by another "
            "agent's worktree:\n    {}\nThis server's worktree is:\n    {}\n"
            "Claim your own color with webkit/scripts/claim-color.sh.".format(
                COLOR, owner, claimed_root
            )
        )
    if not reservation_token:
        sys.exit(
            "preview-server.py: refusing legacy {} claim without a linked TCP port "
            "reservation. Release it, then claim the color again.".format(COLOR)
        )
    try:
        reservation_status, _reservation = _inspect_port_reservation(
            PORT, owner, os.path.abspath(lock), SLUG, reservation_token,
            GRACE_SECONDS,
        )
        if reservation_status == "active":
            sys.exit(
                "preview-server.py: a verified preview server is already active for "
                "{} on port {}. Reuse that session.".format(COLOR, PORT)
            )
        if reservation_status in ("missing", "foreign", "occupied"):
            sys.exit(
                "preview-server.py: refusing {} because its TCP port reservation is {}."
                .format(COLOR, reservation_status)
            )
        if not _touch_port_reservation(
            PORT, owner, os.path.abspath(lock), SLUG, reservation_token
        ):
            raise _RuntimeRegistryError("reservation ownership changed")
    except _RuntimeRegistryError as exc:
        sys.exit(
            "preview-server.py: refusing {} because its TCP port reservation is "
            "unsafe: {}".format(COLOR, exc)
        )
    return lock, claimed_root, reservation_token


CLAIM = None


def _touch_owned_claim(claim=None):
    """Refresh this process's claim only while its lock and owner stay intact."""
    claim = CLAIM if claim is None else claim
    if claim is None:
        return False
    lock, expected_owner = claim[:2]
    reservation_token = claim[2] if len(claim) > 2 else None
    owner_file = os.path.join(lock, "owner")
    try:
        before = os.lstat(lock)
        owner_stat = os.lstat(owner_file)
        if not stat.S_ISDIR(before.st_mode) or not stat.S_ISREG(owner_stat.st_mode):
            return False
        with open(owner_file, encoding="utf-8") as handle:
            opened = os.fstat(handle.fileno())
            if (opened.st_dev, opened.st_ino) != (owner_stat.st_dev, owner_stat.st_ino):
                return False
            owner = handle.read(4097).strip()
        after = os.lstat(lock)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            return False
        if os.path.realpath(owner) != expected_owner:
            return False
        if reservation_token is not None and not _touch_port_reservation(
            PORT, owner, os.path.abspath(lock), SLUG, reservation_token,
            INSTANCE_TOKEN,
        ):
            return False
        os.utime(lock, None, follow_symlinks=False)
    except (OSError, TypeError, ValueError):
        return False
    return True


def _claim_heartbeat(stop_event, interval=None, claim=None):
    """Keep a verified claim young until stopped or ownership changes."""
    if claim is None:
        claim = CLAIM
    if claim is None:
        return
    if interval is None:
        interval = max(0.25, min(30.0, float(GRACE_SECONDS) / 3.0))
    while not stop_event.is_set():
        if not _touch_owned_claim(claim):
            return
        if stop_event.wait(interval):
            return

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
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        return None
    if p.returncode != 0:
        return None
    top = p.stdout.strip()
    return top or None


def _git_common_dir():
    try:
        result = subprocess.run(
            ["git", "-C", ROOT, "rev-parse", "--git-common-dir"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    if not value:
        return None
    if not os.path.isabs(value):
        value = os.path.join(ROOT, value)
    return os.path.realpath(value)


GIT_ROOT = _git_root()
GIT_COMMON_DIR = _git_common_dir()
CLAIM = _verify_claim()
_project_name = CONFIG.get("project_name")
if _project_name is not None and (
    not isinstance(_project_name, str) or not _SLUG_RE.fullmatch(_project_name)
):
    sys.exit("preview-server.py: project_name must be a safe token up to 64 characters.")
_project_identity_source = GIT_COMMON_DIR or os.path.realpath(ROOT)
PROJECT_STORAGE_ID = "{}-{}".format(
    _project_name or "project",
    hashlib.sha256(_project_identity_source.encode("utf-8")).hexdigest()[:16],
)
FEEDBACK_DIR = None
TRANSITION_TOKEN_PATH = None
if GIT_ROOT:
    _feedback_value = CONFIG.get("feedback_dir", ".webkit/feedback")
    if (
        not isinstance(_feedback_value, str)
        or not _feedback_value
        or os.path.isabs(_feedback_value)
    ):
        sys.exit("preview-server.py: feedback_dir must be a non-empty relative path.")
    _git_root_real = os.path.realpath(GIT_ROOT)
    _feedback_root_lexical = os.path.abspath(
        os.path.join(_git_root_real, _feedback_value)
    )
    if _feedback_root_lexical == _git_root_real:
        sys.exit(
            "preview-server.py: feedback_dir must not be the repository root."
        )
    _feedback_relative = os.path.relpath(
        _feedback_root_lexical, _git_root_real
    ).replace(os.sep, "/")
    if _feedback_relative.split("/", 1)[0].lower() == ".git":
        sys.exit(
            "preview-server.py: feedback_dir must not use repository metadata."
        )
    _feedback_lexical = os.path.abspath(
        os.path.join(_feedback_root_lexical, SLUG)
    )
    FEEDBACK_DIR = os.path.realpath(_feedback_lexical)
    if not _path_is_lexically_within(
        _git_root_real, _feedback_root_lexical
    ) or not _path_is_lexically_within(_git_root_real, _feedback_lexical):
        sys.exit("preview-server.py: feedback_dir must stay inside the git repository.")
    if (
        os.path.realpath(_feedback_root_lexical) != _feedback_root_lexical
        or FEEDBACK_DIR != _feedback_lexical
    ):
        sys.exit("preview-server.py: feedback_dir must not traverse symbolic links.")
    try:
        tracked = subprocess.run(
            [
                "git", "-C", _git_root_real, "ls-files", "--error-unmatch",
                "--", _feedback_relative,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        ignored = subprocess.run(
            [
                "git", "-C", _git_root_real, "check-ignore", "-q",
                "--no-index", "--",
                os.path.relpath(_feedback_lexical, _git_root_real).replace(
                    os.sep, "/"
                ),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        sys.exit(
            "preview-server.py: could not verify feedback_dir Git safety: {}"
            .format(exc)
        )
    if tracked.returncode == 0:
        sys.exit(
            "preview-server.py: feedback_dir contains tracked files; move protocol "
            "state to an untracked directory."
        )
    if tracked.returncode != 1 or ignored.returncode not in (0, 1):
        sys.exit("preview-server.py: could not verify feedback_dir Git safety.")
    if ignored.returncode != 0:
        sys.exit(
            "preview-server.py: feedback_dir must be Git-ignored before preview "
            "protocol files can be written."
        )
    os.makedirs(FEEDBACK_DIR, exist_ok=True)
    TRANSITION_TOKEN_PATH = os.path.join(FEEDBACK_DIR, "transition-token")
OVERLAY_DIR = os.path.realpath(os.path.join(_HERE, "..", "overlay"))


def _publish_transition_token():
    if TRANSITION_TOKEN_PATH is None:
        return
    token_fd, token_tmp = tempfile.mkstemp(
        dir=FEEDBACK_DIR, prefix=".transition-token.", suffix=".tmp"
    )
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(token_fd, 0o600)
        with os.fdopen(token_fd, "w", encoding="utf-8") as token_file:
            token_file.write(TRANSITION_TOKEN + "\n")
        os.replace(token_tmp, TRANSITION_TOKEN_PATH)
        try:
            os.chmod(TRANSITION_TOKEN_PATH, 0o600)
        except OSError:
            if os.name == "posix":
                raise
    except BaseException:
        try:
            os.close(token_fd)
        except OSError:
            pass
        try:
            os.unlink(token_tmp)
        except OSError:
            pass
        raise


def _remove_transition_token():
    if TRANSITION_TOKEN_PATH is None:
        return
    try:
        token_stat = os.lstat(TRANSITION_TOKEN_PATH)
        if not stat.S_ISREG(token_stat.st_mode):
            return
        with open(TRANSITION_TOKEN_PATH, encoding="utf-8") as token_file:
            opened = os.fstat(token_file.fileno())
            if (opened.st_dev, opened.st_ino) != (token_stat.st_dev, token_stat.st_ino):
                return
            current = token_file.read(257).strip()
        if secrets.compare_digest(current, TRANSITION_TOKEN):
            os.unlink(TRANSITION_TOKEN_PATH)
    except FileNotFoundError:
        pass
    except OSError:
        pass

# --- title stamping ----------------------------------------------------------
# Strip any already-present color prefix so reloads don't stack emojis. Built
# from the palette via re.escape ALTERNATION, not a character class: several
# emojis are multi-codepoint sequences and a class would shred them.
_LEAD = re.compile(
    r"^(?:(?:{})\s*)+".format("|".join(re.escape(p["emoji"]) for p in PALETTE))
)


class _StampParser(HTMLParser):
    _INERT_CONTAINERS = {"template", "noscript"}
    _VOID_ELEMENTS = {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }

    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.source = source
        self.line_offsets = [0]
        self.line_offsets.extend(match.end() for match in re.finditer(r"\n", source))
        self.inert_stack = []
        self.foreign_stack = []
        self.in_head = False
        self.title_open = None
        self.title = None
        self.head_end = None
        self.html_end = None

    def _offset(self):
        line, column = self.getpos()
        return self.line_offsets[line - 1] + column

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in self._INERT_CONTAINERS:
            self.inert_stack.append(tag)
            return
        if self.inert_stack:
            return
        if self.foreign_stack:
            if tag in ("svg", "math"):
                self.foreign_stack.append(tag)
            return
        if tag in ("svg", "math"):
            self.foreign_stack.append(tag)
            return
        raw = self.get_starttag_text() or ""
        start = self._offset()
        if tag == "title" and self.in_head and self.title is None and self.title_open is None:
            self.title_open = (start, start + len(raw))
        elif tag == "head" and self.head_end is None:
            self.head_end = start + len(raw)
            self.in_head = True
        elif tag == "html" and self.html_end is None:
            self.html_end = start + len(raw)

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        self.handle_starttag(tag, attrs)
        if tag in self._VOID_ELEMENTS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self.inert_stack:
            if tag == self.inert_stack[-1]:
                self.inert_stack.pop()
            return
        if self.foreign_stack:
            if tag == self.foreign_stack[-1]:
                self.foreign_stack.pop()
            return
        if tag == "head":
            self.in_head = False
            return
        if tag != "title" or self.title_open is None or self.title is not None:
            return
        close_start = self._offset()
        close_end = self.source.find(">", close_start)
        if close_end < 0:
            return
        start, inner_start = self.title_open
        self.title = (start, close_end + 1, self.source[inner_start:close_start])
        self.title_open = None


def stamp(html):
    parser = _StampParser(html)
    try:
        parser.feed(html)
        parser.close()
    except (AssertionError, ValueError) as exc:
        raise _CSPTransformError("HTML title structure could not be parsed") from exc
    if parser.title_open is not None:
        raise _CSPTransformError("HTML title element is incomplete")
    color = html_lib.escape(COLOR)
    if parser.title is not None:
        start, end, original = parser.title
        inner = _LEAD.sub("", original)
        title = "<title>{}{}</title>".format(
            color, " " + inner if inner else ""
        )
        return html[:start] + title + html[end:]
    title = "<title>{} Preview</title>".format(html_lib.escape(COLOR))
    if parser.head_end is not None:
        return html[:parser.head_end] + title + html[parser.head_end:]
    if parser.html_end is not None:
        return html[:parser.html_end] + "<head>" + title + "</head>" + html[parser.html_end:]
    return "<head>" + title + "</head>" + html


# --- overlay injection -------------------------------------------------------
# One real <script> tag before the final parsed </body>. The overlay reads its
# own data attributes for color and mode, so this tag is the server-to-overlay
# handshake. HTMLParser keeps tag-looking strings in scripts and comments from
# being mistaken for document structure.
_CSP_DIRECTIVE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9-]*$")
_HTML_ATTR_NAME = re.compile(r"^[A-Za-z_:][A-Za-z0-9_.:-]*$")
_CSP_NONCE_VALUE = re.compile(r"^[A-Za-z0-9+/_-]{16,128}={0,2}$")
_CSP_NONCE_SOURCE = re.compile(r"^'nonce-[A-Za-z0-9+/_-]+={0,2}'$", re.I)
_CSP_HASH_SOURCE = re.compile(
    r"^'(?:sha256|sha384|sha512)-[A-Za-z0-9+/_-]+={0,2}'$", re.I
)
_TRUSTED_TYPES_POLICY_NAME = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_CSP_META_HINT = re.compile(
    r"\bhttp-equiv\s*=\s*(?:[\"']\s*)?content-security-policy\b", re.I
)


class _CSPTransformError(ValueError):
    """The preview cannot safely authorize its overlay in a CSP meta tag."""


class _OverlayDocumentParser(HTMLParser):
    _INERT_CONTAINERS = {"template", "noscript"}
    _EXECUTABLE_SCRIPT_TYPES = {
        "", "module", "text/javascript", "application/javascript",
        "text/ecmascript", "application/ecmascript",
        "text/javascript1.0", "text/javascript1.1", "text/javascript1.2",
        "text/javascript1.3", "text/javascript1.4", "text/javascript1.5",
        "text/jscript", "text/livescript",
    }
    _OVERLAY_MARKERS = {
        "data-wk-color", "data-wk-token", "data-wk-project", "data-wk-mode",
        "data-wk-nonce", "data-wk-trusted-types-policy",
    }

    def __init__(self, source, expected_overlay_attrs=None):
        super().__init__(convert_charrefs=True)
        self.source = source
        self.expected_overlay_attrs = dict(expected_overlay_attrs or {})
        self.overlay_found = False
        self.body_closes = []
        self.html_closes = []
        self.inert_stack = []
        self.foreign_stack = []
        self.line_offsets = [0]
        self.line_offsets.extend(match.end() for match in re.finditer(r"\n", source))

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in self._INERT_CONTAINERS:
            self.inert_stack.append(tag)
            return
        if self.inert_stack:
            return
        if self.foreign_stack:
            if tag in ("svg", "math"):
                self.foreign_stack.append(tag)
            return
        if tag in ("svg", "math"):
            self.foreign_stack.append(tag)
            return
        if tag != "script":
            return
        values = {}
        duplicates = set()
        for name, value in attrs:
            if name in values:
                duplicates.add(name)
            else:
                values[name] = value
        sources = [value for name, value in attrs if name == "src"]
        if (
            len(sources) != 1
            or not isinstance(sources[0], str)
            or duplicates.intersection(
                self._OVERLAY_MARKERS
                | set(self.expected_overlay_attrs)
                | {"src", "type", "nonce", "defer", "nomodule"}
            )
            or not self._OVERLAY_MARKERS.issubset(values)
            or any(not isinstance(values[name], str) for name in self._OVERLAY_MARKERS)
            or "defer" not in values
            or "nomodule" in values
            or not isinstance(values.get("nonce"), str)
            or values["nonce"] != values["data-wk-nonce"]
            or not _CSP_NONCE_VALUE.fullmatch(values["nonce"])
            or not _TRUSTED_TYPES_POLICY_NAME.fullmatch(
                values["data-wk-trusted-types-policy"]
            )
            or any(
                values.get(name) != expected
                for name, expected in self.expected_overlay_attrs.items()
            )
        ):
            return
        script_type = values.get("type")
        if script_type is not None and script_type.strip().lower() not in self._EXECUTABLE_SCRIPT_TYPES:
            return
        parsed = urlparse(sources[0])
        if not parsed.scheme and not parsed.netloc and parsed.path == "/__wk/overlay.js":
            self.overlay_found = True

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self.inert_stack:
            if tag == self.inert_stack[-1]:
                self.inert_stack.pop()
            return
        if self.foreign_stack:
            if tag == self.foreign_stack[-1]:
                self.foreign_stack.pop()
            return
        line, column = self.getpos()
        offset = self.line_offsets[line - 1] + column
        if tag == "body":
            self.body_closes.append(offset)
        elif tag == "html":
            self.html_closes.append(offset)


def _overlay_document_info(source, expected_overlay_attrs=None):
    parser = _OverlayDocumentParser(source, expected_overlay_attrs)
    try:
        parser.feed(source)
        incomplete = parser.rawdata
        unsafe_append = (
            parser.cdata_elem is not None
            or bool(parser.inert_stack)
            or bool(parser.foreign_stack)
            or bool(incomplete)
        )
        parser.close()
    except (AssertionError, ValueError):
        raise _CSPTransformError("HTML structure could not be parsed for overlay injection")
    closes = parser.body_closes or parser.html_closes
    if closes:
        return parser.overlay_found, closes[-1]
    if unsafe_append:
        raise _CSPTransformError("HTML has no safe overlay insertion point")
    return parser.overlay_found, len(source)


def _parse_csp_policy(policy):
    if any(ord(char) < 0x20 and char not in "\t\n\r" for char in policy):
        raise _CSPTransformError("CSP policy contains a control character")
    directives = []
    for raw_directive in policy.split(";"):
        fields = raw_directive.split()
        if not fields:
            continue
        if not _CSP_DIRECTIVE_NAME.fullmatch(fields[0]):
            raise _CSPTransformError("CSP policy contains an invalid directive name")
        if any(any(ord(char) < 0x21 or ord(char) == 0x7F for char in token) for token in fields[1:]):
            raise _CSPTransformError("CSP policy contains an invalid source token")
        directives.append([fields[0].lower(), fields[1:]])
    return directives


def _first_csp_directive(directives, name):
    return next((directive for directive in directives if directive[0] == name), None)


def _without_none(tokens):
    return [token for token in tokens if token.lower() != "'none'"]


def _append_source(tokens, source):
    lowered = {token.lower() for token in tokens}
    if source.lower() not in lowered:
        tokens.append(source)


def _has_nonce_or_hash(tokens):
    return any(
        _CSP_NONCE_SOURCE.fullmatch(token) or _CSP_HASH_SOURCE.fullmatch(token)
        for token in tokens
    )


def _authorize_element_source(directives, element_name, general_name, nonce, needs_self):
    directive = _first_csp_directive(directives, element_name)
    if directive is None:
        directive = _first_csp_directive(directives, general_name)
    if directive is None:
        fallback = _first_csp_directive(directives, "default-src")
        if fallback is None:
            return
        directive = [general_name, list(fallback[1])]
        directives.append(directive)

    tokens = directive[1]
    lowered = {token.lower() for token in tokens}
    active_unsafe_inline = "'unsafe-inline'" in lowered and not _has_nonce_or_hash(tokens)
    tokens[:] = _without_none(tokens)
    if active_unsafe_inline:
        # Adding any nonce would make browsers ignore the host's existing
        # unsafe-inline source and could break the page under preview.
        if needs_self:
            _append_source(tokens, "'self'")
        return
    _append_source(tokens, "'nonce-{}'".format(nonce))


def _authorize_same_origin_source(directives, name, fallback_names):
    directive = _first_csp_directive(directives, name)
    if directive is None:
        fallback = None
        for fallback_name in fallback_names:
            fallback = _first_csp_directive(directives, fallback_name)
            if fallback is not None:
                break
        if fallback is None:
            return
        directive = [name, list(fallback[1])]
        directives.append(directive)
    directive[1][:] = _without_none(directive[1])
    _append_source(directive[1], "'self'")


def _transform_csp_policy(policy, nonce, trusted_types_policy):
    directives = _parse_csp_policy(policy)
    _authorize_element_source(
        directives, "script-src-elem", "script-src", nonce, needs_self=True
    )
    _authorize_element_source(
        directives, "style-src-elem", "style-src", nonce, needs_self=False
    )

    style_attr = _first_csp_directive(directives, "style-src-attr")
    if style_attr is None:
        directives.append(["style-src-attr", ["'unsafe-inline'"]])
    else:
        # Dynamic overlay geometry uses element.style extensively. Hash and
        # nonce sources can suppress unsafe-inline, so this preview-only
        # directive must be unambiguous.
        style_attr[1][:] = ["'unsafe-inline'"]

    _authorize_same_origin_source(directives, "connect-src", ("default-src",))
    _authorize_same_origin_source(
        directives, "frame-src", ("child-src", "default-src")
    )

    trusted_types = _first_csp_directive(directives, "trusted-types")
    if trusted_types is not None:
        trusted_types[1][:] = _without_none(trusted_types[1])
        _append_source(trusted_types[1], trusted_types_policy)

    return "; ".join(
        " ".join([name] + tokens) if tokens else name
        for name, tokens in directives
    )


def _serialize_csp_meta(attrs, transformed_content, self_closing):
    rendered = []
    content_written = False
    for name, value in attrs:
        if not _HTML_ATTR_NAME.fullmatch(name):
            raise _CSPTransformError("CSP meta tag contains an invalid attribute name")
        if name == "content":
            if content_written:
                raise _CSPTransformError("CSP meta tag contains duplicate content attributes")
            value = transformed_content
            content_written = True
        if value is None:
            rendered.append(name)
        else:
            rendered.append('{}="{}"'.format(name, html_lib.escape(value, quote=True)))
    if not content_written:
        raise _CSPTransformError("CSP meta tag is missing its content attribute")
    ending = " />" if self_closing else ">"
    return "<meta {}{}".format(" ".join(rendered), ending)


class _CSPMetaParser(HTMLParser):
    def __init__(self, source, nonce, trusted_types_policy):
        super().__init__(convert_charrefs=True)
        self.source = source
        self.nonce = nonce
        self.trusted_types_policy = trusted_types_policy
        self.replacements = []
        self.line_offsets = [0]
        self.line_offsets.extend(match.end() for match in re.finditer(r"\n", source))

    def _handle_meta(self, tag, attrs, self_closing):
        if tag.lower() != "meta":
            return
        raw = self.get_starttag_text()
        if not raw:
            raise _CSPTransformError("CSP meta tag could not be reconstructed")
        http_equiv = [value for name, value in attrs if name == "http-equiv"]
        is_csp = any(
            isinstance(value, str) and value.strip().lower() == "content-security-policy"
            for value in http_equiv
        )
        if not is_csp:
            if _CSP_META_HINT.search(raw):
                raise _CSPTransformError("CSP meta tag is malformed")
            return
        if len(http_equiv) != 1:
            raise _CSPTransformError("CSP meta tag contains duplicate http-equiv attributes")
        contents = [value for name, value in attrs if name == "content"]
        if len(contents) != 1 or contents[0] is None:
            raise _CSPTransformError("CSP meta tag must contain exactly one content value")
        transformed = _transform_csp_policy(
            contents[0], self.nonce, self.trusted_types_policy
        )
        line, column = self.getpos()
        start = self.line_offsets[line - 1] + column
        replacement = _serialize_csp_meta(attrs, transformed, self_closing)
        self.replacements.append((start, start + len(raw), replacement))

    def handle_starttag(self, tag, attrs):
        self._handle_meta(tag, attrs, False)

    def handle_startendtag(self, tag, attrs):
        self._handle_meta(tag, attrs, True)


def _transform_csp_meta_tags(html, nonce, trusted_types_policy):
    if not _CSP_NONCE_VALUE.fullmatch(nonce):
        raise _CSPTransformError("CSP nonce is invalid")
    if not _TRUSTED_TYPES_POLICY_NAME.fullmatch(trusted_types_policy):
        raise _CSPTransformError("Trusted Types policy name is invalid")
    parser = _CSPMetaParser(html, nonce, trusted_types_policy)
    try:
        parser.feed(html)
        incomplete = parser.rawdata
        if (
            parser.cdata_elem is None
            and not incomplete.lstrip().startswith("<!--")
            and _CSP_META_HINT.search(incomplete)
        ):
            raise _CSPTransformError("CSP meta tag is incomplete")
        parser.close()
    except _CSPTransformError:
        raise
    except (AssertionError, ValueError) as exc:
        raise _CSPTransformError("CSP meta tag could not be parsed") from exc
    for start, end, replacement in reversed(parser.replacements):
        html = html[:start] + replacement + html[end:]
    return html

# Optional per-project hotkey overrides: {"hotkeys": {"toggle": "Backquote",
# "dictate": "KeyV"}}. Values are KeyboardEvent.code strings (layout-independent
# - this matters on a Hebrew site, where e.key differs per layout); the overlay
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


def inject(html, mode, before_prefix="", nonce=None, trusted_types_policy=None):
    expected_overlay_attrs = {
        "data-wk-color": str(SLUG),
        "data-wk-token": str(MUTATION_TOKEN),
        "data-wk-project": str(PROJECT_STORAGE_ID),
        "data-wk-emoji": str(COLOR),
        "data-wk-mode": str(mode),
        "data-wk-dictation-mode": str(_DICTATION_MODE),
        "data-wk-interaction-mode": str(_INTERACTION_MODE),
        "data-wk-before-prefix": str(before_prefix),
        "data-wk-hotkey-toggle": str(_HOTKEY_VALUES["toggle"]),
        "data-wk-hotkey-dictate": str(_HOTKEY_VALUES["dictate"]),
    }
    overlay_found, _insertion = _overlay_document_info(
        html, expected_overlay_attrs
    )
    if overlay_found:
        return html
    nonce = nonce or secrets.token_urlsafe(24)
    trusted_types_policy = trusted_types_policy or "wk-overlay-{}".format(
        secrets.token_hex(16)
    )
    html = _transform_csp_meta_tags(html, nonce, trusted_types_policy)
    attr = lambda value: html_lib.escape(str(value), quote=True)
    tag = (
        '<script src="/__wk/overlay.js" defer nonce="{}" data-wk-nonce="{}" '
        'data-wk-trusted-types-policy="{}" data-wk-color="{}" data-wk-token="{}" '
        'data-wk-project="{}" data-wk-emoji="{}" data-wk-mode="{}" data-wk-dictation-mode="{}" '
        'data-wk-interaction-mode="{}" data-wk-before-prefix="{}"{}></script>'.format(
            attr(nonce), attr(nonce), attr(trusted_types_policy),
            attr(SLUG), attr(MUTATION_TOKEN), attr(PROJECT_STORAGE_ID), attr(COLOR),
            attr(mode), attr(_DICTATION_MODE), attr(_INTERACTION_MODE),
            attr(before_prefix), _HOTKEY_ATTRS
        )
    )
    _overlay_found, insertion = _overlay_document_info(
        html, expected_overlay_attrs
    )
    return html[:insertion] + tag + "\n" + html[insertion:]


# --- feedback data files -----------------------------------------------------
# The agent⇄overlay contract is three JSON files in FEEDBACK_DIR:
#   feedback.json  (server-written)  the user's batch of points
#   review.json    (agent-written)   the agent's per-point manifest + beforeRef
#   verdicts.json  (server-written)  the user's accept/delete/redo calls
# "File exists" is the protocol's state signal, so every write must be atomic
# (tmp file + os.replace - a reader can never see a half-written file) and
# serialized (one lock - concurrent POSTs must not interleave read-merge-write).
_DATA_FILES = ("feedback.json", "review.json", "verdicts.json")
_WRITE_LOCK = threading.Lock()


def _data_path(name):
    return os.path.join(FEEDBACK_DIR, name)


def _load_json_object_path(path, max_bytes=_MAX_PROTOCOL_FILE):
    """Read one bounded, regular JSON object without following a swapped inode."""
    try:
        path_stat = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(path_stat.st_mode) or path_stat.st_size > max_bytes:
        return None
    try:
        with open(path, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if (
                (opened.st_dev, opened.st_ino) != (path_stat.st_dev, path_stat.st_ino)
                or opened.st_size > max_bytes
            ):
                return None
            raw = handle.read(max_bytes + 1)
    except OSError:
        return None
    if len(raw) > max_bytes:
        return None
    try:
        obj = json.loads(raw.decode("utf-8"), parse_constant=_reject_json_constant)
        _reject_json_surrogates(obj)
    except (UnicodeDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _load_data(name):
    """Parsed contents of a data file, or None if absent/unreadable/not an object."""
    return _load_json_object_path(_data_path(name))


def _mtime_ns(name):
    path = _data_path(name)
    try:
        path_stat = os.lstat(path)
    except OSError:
        return 0
    return path_stat.st_mtime_ns if stat.S_ISREG(path_stat.st_mode) else 0


def _rev():
    # These files are small and bounded. Hashing their bytes prevents a same-mtime
    # atomic replacement from hiding a new review round from a polling overlay.
    fingerprints = []
    for name in _DATA_FILES:
        path = _data_path(name)
        try:
            path_stat = os.lstat(path)
        except OSError:
            fingerprints.append("0")
            continue
        if not stat.S_ISREG(path_stat.st_mode) or path_stat.st_size > _MAX_PROTOCOL_FILE:
            fingerprints.append("unsafe:{}".format(path_stat.st_size))
            continue
        try:
            with open(path, "rb") as handle:
                opened = os.fstat(handle.fileno())
                if (
                    (opened.st_dev, opened.st_ino)
                    != (path_stat.st_dev, path_stat.st_ino)
                    or opened.st_size > _MAX_PROTOCOL_FILE
                ):
                    fingerprints.append("changed")
                    continue
                raw = handle.read(_MAX_PROTOCOL_FILE + 1)
        except OSError:
            fingerprints.append("unreadable")
            continue
        if len(raw) > _MAX_PROTOCOL_FILE:
            fingerprints.append("unsafe:{}".format(len(raw)))
            continue
        fingerprints.append(
            "{}:{}:{}:{}:{}".format(
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                getattr(opened, "st_ctime_ns", int(opened.st_ctime * 1000000000)),
                hashlib.sha256(raw).hexdigest(),
            )
        )
    return "-".join(fingerprints)


def _best_effort_fsync_directory(directory):
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = None
    try:
        descriptor = os.open(directory, flags)
        os.fsync(descriptor)
    except OSError:
        # Directory fsync is unavailable on some supported filesystems and on
        # Windows. The temporary file itself is always synchronously durable.
        pass
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _atomic_write(name, obj):
    # Caller holds _WRITE_LOCK. tmp file in the SAME directory so os.replace
    # is an atomic rename, never a cross-device copy.
    fd, tmp = tempfile.mkstemp(dir=FEEDBACK_DIR, prefix="." + name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, _data_path(name))
        _best_effort_fsync_directory(FEEDBACK_DIR)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _atomic_write_path(path, obj, mode=0o600):
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix="." + os.path.basename(path) + ".", suffix=".tmp")
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(obj, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _best_effort_fsync_directory(directory)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _valid_round(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _before_capability(review):
    if not isinstance(review, dict):
        return None
    batch_id = review.get("batchId")
    round_number = review.get("round")
    before_ref = review.get("beforeRef")
    if (
        not isinstance(batch_id, str)
        or not _BATCH_ID_RE.fullmatch(batch_id)
        or not _valid_round(round_number)
        or not isinstance(before_ref, str)
        or not _SHA_RE.fullmatch(before_ref)
    ):
        return None
    identity = "{}\n{}\n{}".format(
        batch_id, round_number, before_ref
    ).encode("utf-8")
    return hmac.new(
        _BEFORE_CAPABILITY_KEY, identity, hashlib.sha256
    ).hexdigest()


def _before_prefix(review):
    capability = _before_capability(review)
    return "/__wk/before/" + capability if capability else ""


def _point_id_list(points):
    result = []
    if not isinstance(points, list):
        return result
    for point in points:
        if not isinstance(point, dict):
            continue
        point_id = point.get("id")
        if isinstance(point_id, str) and _POINT_ID_RE.fullmatch(point_id):
            result.append(point_id)
    return result


def _valid_point_objects(points):
    if not isinstance(points, list) or not points:
        return False
    point_ids = _point_id_list(points)
    return len(point_ids) == len(points) and len(point_ids) == len(set(point_ids))


_BATCH_FIELDS = {
    "version", "kind", "batchId", "round", "color", "sessionId",
    "createdAt", "updatedAt", "pages", "points",
}
_POINT_FIELDS = {
    "id", "number", "page", "createdAt", "rect", "rects", "viewport",
    "scroll", "anchor", "context", "uiState", "abcState", "text",
    "voiceNote", "abcRequest", "status",
}


def _bounded_number(value, minimum, maximum):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    if isinstance(value, float) and not math.isfinite(value):
        return False
    return minimum <= value <= maximum


def _bounded_string(value, maximum, allow_empty=True):
    return (
        isinstance(value, str)
        and (allow_empty or bool(value))
        and len(value) <= maximum
        and all(ord(char) >= 32 and ord(char) != 127 for char in value)
    )


def _bounded_human_text(value, maximum, allow_empty=True):
    return (
        isinstance(value, str)
        and (allow_empty or bool(value))
        and len(value) <= maximum
        and all(
            char in "\n\r\t" or unicodedata.category(char) != "Cc"
            for char in value
        )
    )


def _valid_logical_page(value):
    if not _bounded_string(value, 2048, allow_empty=False):
        return False
    parsed = urlparse(value)
    if (
        not value.startswith("/")
        or value.startswith("//")
        or parsed.scheme
        or parsed.netloc
        or parsed.params
        or parsed.query
        or parsed.fragment
        or "\\" in value
    ):
        return False
    decoded = parsed.path
    for _ in range(3):
        replacement = unquote(decoded, errors="replace")
        if replacement == decoded:
            break
        decoded = replacement
    parts = [part for part in decoded.split("/") if part]
    return not any(
        part in (".", "..") or _private_static_component(part)
        for part in parts
    )


def _valid_rect(value, allow_negative=True, allow_zero_size=False):
    if not isinstance(value, dict) or set(value) != {"x", "y", "w", "h"}:
        return False
    low = -1000000 if allow_negative else 0
    return (
        _bounded_number(value["x"], low, 10000000)
        and _bounded_number(value["y"], low, 10000000)
        and _bounded_number(value["w"], 0 if allow_zero_size else 1, 1000000)
        and _bounded_number(value["h"], 0 if allow_zero_size else 1, 1000000)
    )


def _valid_context(value):
    if not isinstance(value, list) or len(value) > 12:
        return False
    for item in value:
        if not isinstance(item, dict) or not set(item).issubset(
            {"selector", "tag", "text", "box", "role"}
        ):
            return False
        if not _bounded_string(item.get("selector"), 2048):
            return False
        if not _bounded_human_text(item.get("text", ""), 120):
            return False
        tag = item.get("tag")
        if not isinstance(tag, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9:-]{0,63}", tag):
            return False
        if item.get("role") not in ("primary", "intersecting"):
            return False
        if not _valid_rect(
            item.get("box"), allow_negative=True, allow_zero_size=True
        ):
            return False
    return True


def _valid_ui_state(value):
    if value is None:
        return True
    if not isinstance(value, dict) or set(value) != {"surfaces"}:
        return False
    surfaces = value["surfaces"]
    if not isinstance(surfaces, list) or len(surfaces) > 16:
        return False
    for surface in surfaces:
        if (
            not isinstance(surface, dict)
            or set(surface) != {"kind", "selector"}
            or surface.get("kind") not in ("dialog", "popover")
            or not _bounded_string(surface.get("selector"), 2048, allow_empty=False)
        ):
            return False
    return True


def _valid_abc_state(value):
    if value is None:
        return True
    if not isinstance(value, dict) or len(value) > 32:
        return False
    for scope, state_value in value.items():
        if not _bounded_string(scope, 128, allow_empty=False):
            return False
        if not isinstance(state_value, dict) or set(state_value) != {"current", "letters"}:
            return False
        letters = state_value.get("letters")
        current = state_value.get("current")
        if (
            not isinstance(letters, str)
            or not 1 <= len(letters) <= 10
            or len(set(letters)) != len(letters)
            or any(letter not in "ABCDEFGHIJ" for letter in letters)
            or not isinstance(current, str)
            or len(current) != 1
            or current not in letters
        ):
            return False
    return True


def _valid_abc_request(value):
    if value is None:
        return True
    if not isinstance(value, dict) or value.get("mode") not in ("model", "user"):
        return False
    if "brief" in value and not _bounded_human_text(value["brief"], 4000):
        return False
    if value["mode"] == "model":
        return (
            set(value).issubset({"mode", "count", "brief"})
            and isinstance(value.get("count"), int)
            and not isinstance(value.get("count"), bool)
            and 2 <= value["count"] <= 10
        )
    if not set(value).issubset({"mode", "prompts", "brief"}):
        return False
    prompts = value.get("prompts")
    if not isinstance(prompts, dict) or not 1 <= len(prompts) <= 10:
        return False
    return all(
        isinstance(letter, str)
        and len(letter) == 1
        and letter in "ABCDEFGHIJ"
        and _bounded_human_text(prompt, 4000, allow_empty=False)
        for letter, prompt in prompts.items()
    )


def _valid_voice_note_shape(value):
    if value is None:
        return True
    if not isinstance(value, dict) or not set(value).issubset(
        {"path", "mimeType", "bytes", "durationMs", "language"}
    ):
        return False
    if (
        not _bounded_string(value.get("path"), 4096, allow_empty=False)
        or value.get("mimeType") not in _VOICE_TYPES
        or not isinstance(value.get("bytes"), int)
        or isinstance(value.get("bytes"), bool)
        or not 1 <= value["bytes"] <= _MAX_VOICE_BODY
    ):
        return False
    if "durationMs" in value and not _bounded_number(value["durationMs"], 0, 3600000):
        return False
    if "language" in value and not (
        isinstance(value["language"], str)
        and re.fullmatch(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8}){0,3}", value["language"])
    ):
        return False
    return True


def _feedback_schema_error(batch):
    if not isinstance(batch, dict) or not set(batch).issubset(_BATCH_FIELDS):
        return "feedback batch contains unsupported fields"
    if batch.get("version") != 1 or batch.get("kind") != "feedback":
        return "feedback must be a version-1 feedback batch"
    if (
        not isinstance(batch.get("batchId"), str)
        or not _BATCH_ID_RE.fullmatch(batch["batchId"])
    ):
        return "feedback batchId is invalid"
    if not _valid_round(batch.get("round")):
        return "feedback round must be a positive integer"
    if batch.get("color") != SLUG:
        return "feedback color must match the active preview color"
    for name in ("sessionId", "createdAt", "updatedAt"):
        if name in batch and not _bounded_string(batch[name], 256):
            return "feedback {} is invalid".format(name)
    pages = batch.get("pages")
    if (
        not isinstance(pages, list)
        or not 1 <= len(pages) <= 128
        or len(pages) != len(set(pages))
        or any(not _valid_logical_page(page) for page in pages)
    ):
        return "feedback pages must be unique safe logical paths"
    points = batch.get("points")
    if not isinstance(points, list) or not 1 <= len(points) <= 500:
        return "feedback must contain between 1 and 500 points"
    if not _valid_point_objects(points):
        return "feedback point ids must be unique safe tokens up to 128 characters"
    numbers = []
    point_pages = []
    for point in points:
        if not set(point).issubset(_POINT_FIELDS):
            return "feedback point contains unsupported fields"
        number = point.get("number")
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            return "feedback point numbers must be positive integers"
        numbers.append(number)
        page = point.get("page")
        if not _valid_logical_page(page) or page not in pages:
            return "feedback point page must match a listed safe page"
        point_pages.append(page)
        if not _bounded_human_text(point.get("text"), 10000):
            return "feedback point text must be at most 10000 characters"
        if "createdAt" in point and not _bounded_string(point["createdAt"], 256):
            return "feedback point createdAt is invalid"
        if "rect" in point and not _valid_rect(point["rect"], allow_negative=True):
            return "feedback point rect is invalid"
        if "rects" in point:
            rects = point["rects"]
            if (
                not isinstance(rects, list)
                or not 1 <= len(rects) <= 32
                or any(not _valid_rect(rect, allow_negative=True) for rect in rects)
            ):
                return "feedback point rects are invalid"
        if "viewport" in point:
            viewport = point["viewport"]
            if (
                not isinstance(viewport, dict)
                or set(viewport) != {"w", "h", "dpr"}
                or not _bounded_number(viewport["w"], 1, 100000)
                or not _bounded_number(viewport["h"], 1, 100000)
                or not _bounded_number(viewport["dpr"], 0.1, 100)
            ):
                return "feedback point viewport is invalid"
        if "scroll" in point:
            scroll = point["scroll"]
            if (
                not isinstance(scroll, dict)
                or set(scroll) != {"x", "y"}
                or not _bounded_number(scroll["x"], -1000000, 10000000)
                or not _bounded_number(scroll["y"], -1000000, 10000000)
            ):
                return "feedback point scroll is invalid"
        if "anchor" in point and point["anchor"] not in ("doc", "viewport"):
            return "feedback point anchor is invalid"
        if "context" in point and not _valid_context(point["context"]):
            return "feedback point context is invalid"
        if "uiState" in point and not _valid_ui_state(point["uiState"]):
            return "feedback point uiState is invalid"
        if "abcState" in point and not _valid_abc_state(point["abcState"]):
            return "feedback point abcState is invalid"
        if "voiceNote" in point and not _valid_voice_note_shape(point["voiceNote"]):
            return "feedback point voiceNote shape is invalid"
        if "abcRequest" in point and not _valid_abc_request(point["abcRequest"]):
            return "feedback point abcRequest is invalid"
        if "status" in point and point["status"] != "new":
            return "feedback point status must be new"
    if len(numbers) != len(set(numbers)):
        return "feedback point numbers must be unique"
    if set(point_pages) != set(pages):
        return "feedback pages must exactly match point pages"
    return None


def _dedupe_points(existing_points, incoming_points):
    """Return copies of novel incoming points, preserving their first-seen order."""
    known = set(_point_id_list(existing_points))
    novel = []
    for point in incoming_points:
        point_id = point.get("id")
        if point_id in known:
            continue
        known.add(point_id)
        novel.append(dict(point))
    return novel


def _history_round_directory(batch_id, round_number, create=False):
    history = os.path.join(FEEDBACK_DIR, "history")
    if os.path.lexists(history):
        if os.path.islink(history) or not os.path.isdir(history):
            raise OSError("history path is not a safe directory")
    elif create:
        os.mkdir(history, 0o700)
    directory = os.path.join(history, "{}-r{}".format(batch_id, round_number))
    if os.path.lexists(directory):
        if os.path.islink(directory) or not os.path.isdir(directory):
            raise OSError("round history path is not a safe directory")
    elif create:
        os.mkdir(directory, 0o700)
    if not _path_is_within(FEEDBACK_DIR, directory):
        raise OSError("round history path escapes the feedback inbox")
    return directory


class _HistoryLimit(OSError):
    pass


def _bounded_history_entries(path, budget):
    entries = os.scandir(path)
    try:
        for entry in entries:
            budget[0] += 1
            if budget[0] > _MAX_HISTORY_SCAN_ENTRIES:
                raise _HistoryLimit(
                    "feedback history exceeds the {}-entry scan budget".format(
                        _MAX_HISTORY_SCAN_ENTRIES
                    )
                )
            yield entry
    finally:
        entries.close()


def _history_usage():
    history = os.path.join(FEEDBACK_DIR, "history")
    if not os.path.lexists(history):
        return 0, 0
    try:
        history_stat = os.lstat(history)
    except OSError as exc:
        raise _HistoryLimit("feedback history cannot be inspected: {}".format(exc))
    if not stat.S_ISDIR(history_stat.st_mode) or stat.S_ISLNK(history_stat.st_mode):
        raise _HistoryLimit("feedback history is not a safe directory")
    rounds = 0
    total = 0
    budget = [0]
    try:
        for round_entry in _bounded_history_entries(history, budget):
            if not round_entry.is_dir(follow_symlinks=False):
                raise _HistoryLimit(
                    "feedback history contains an unexpected non-directory entry"
                )
            rounds += 1
            if rounds > _MAX_HISTORY_ROUNDS:
                raise _HistoryLimit(
                    "feedback history exceeds the {}-round limit".format(
                        _MAX_HISTORY_ROUNDS
                    )
                )
            for entry in _bounded_history_entries(round_entry.path, budget):
                entry_stat = entry.stat(follow_symlinks=False)
                if not stat.S_ISREG(entry_stat.st_mode):
                    raise _HistoryLimit(
                        "feedback history contains an unsafe archive entry"
                    )
                total += max(0, entry_stat.st_size)
                if total > _MAX_HISTORY_BYTES:
                    raise _HistoryLimit(
                        "feedback history exceeds the {}-byte limit".format(
                            _MAX_HISTORY_BYTES
                        )
                    )
    except _HistoryLimit:
        raise
    except OSError as exc:
        raise _HistoryLimit("feedback history cannot be scanned: {}".format(exc))
    return rounds, total


def _history_archive_size(moves, copies, receipt):
    total = 0
    for source_name, _target_name in list(copies) + list(moves):
        source = _data_path(source_name)
        try:
            source_stat = os.lstat(source)
        except OSError as exc:
            raise OSError("live {} is missing or unsafe: {}".format(
                source_name, exc
            ))
        if not stat.S_ISREG(source_stat.st_mode):
            raise OSError("live {} is missing or unsafe".format(source_name))
        total += max(0, source_stat.st_size)
    if receipt is not None:
        total += len(
            (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode(
                "utf-8"
            )
        )
    return total


def _require_history_capacity(round_path, moves, copies, receipt):
    rounds, total = _history_usage()
    if not os.path.isdir(round_path) and rounds + 1 > _MAX_HISTORY_ROUNDS:
        raise _HistoryLimit(
            "feedback history reached the {}-round limit; existing receipts "
            "were preserved".format(_MAX_HISTORY_ROUNDS)
        )
    projected = total + _history_archive_size(moves, copies, receipt)
    if projected > _MAX_HISTORY_BYTES:
        raise _HistoryLimit(
            "feedback history would exceed the {}-byte limit; existing receipts "
            "were preserved".format(_MAX_HISTORY_BYTES)
        )


def _archived_review_point_ids(batch_id):
    result = set()
    history = os.path.join(FEEDBACK_DIR, "history")
    if not os.path.isdir(history) or os.path.islink(history):
        return result
    prefix = batch_id + "-r"
    budget = [0]
    try:
        entries = _bounded_history_entries(history, budget)
        for entry in entries:
            if not entry.name.startswith(prefix) or not entry.is_dir(follow_symlinks=False):
                continue
            review_path = os.path.join(entry.path, "review.json")
            if not _path_is_within(FEEDBACK_DIR, review_path) or os.path.islink(review_path):
                continue
            review = _load_json_object_path(review_path)
            if review is None or review.get("batchId") != batch_id:
                continue
            result.update(_point_id_list(review.get("points")))
    except _HistoryLimit:
        raise
    except OSError:
        return result
    return result


def _archive_transaction(
    batch_id, round_number, moves, copies=(), feedback=None, review=None,
    receipt_name="transition.json", receipt=None,
):
    """Archive live files and roll back every completed filesystem step on failure."""
    history_path = os.path.join(FEEDBACK_DIR, "history")
    history_existed = os.path.isdir(history_path)
    round_candidate = os.path.join(history_path, "{}-r{}".format(batch_id, round_number))
    round_existed = os.path.isdir(round_candidate)
    round_path = _history_round_directory(batch_id, round_number, create=False)
    _require_history_capacity(round_path, moves, copies, receipt)
    receipt_path = os.path.join(round_path, receipt_name)
    feedback_original = _load_data("feedback.json") if feedback is not None else None
    for source_name, target_name in list(copies) + list(moves):
        source = _data_path(source_name)
        target = os.path.join(round_path, target_name)
        if os.path.islink(source) or not os.path.isfile(source):
            raise OSError("live {} is missing or unsafe".format(source_name))
        if os.path.lexists(target):
            raise OSError("archive target already exists: {}".format(target_name))
    if receipt is not None and os.path.lexists(receipt_path):
        raise OSError("archive receipt already exists: {}".format(receipt_name))

    copied = []
    moved = []
    feedback_replaced = False
    review_replaced = False
    try:
        round_path = _history_round_directory(batch_id, round_number, create=True)
        for source_name, target_name in copies:
            source = _data_path(source_name)
            target = os.path.join(round_path, target_name)
            shutil.copy2(source, target)
            copied.append(target)
        for source_name, target_name in moves:
            source = _data_path(source_name)
            target = os.path.join(round_path, target_name)
            os.rename(source, target)
            moved.append((source, target))
        if feedback is not None:
            _atomic_write("feedback.json", feedback)
            feedback_replaced = True
        if review is not None:
            _atomic_write("review.json", review)
            review_replaced = True
        if receipt is not None:
            _atomic_write_path(receipt_path, receipt)
    except Exception as exc:
        rollback_errors = []
        if review_replaced:
            try:
                os.unlink(_data_path("review.json"))
            except FileNotFoundError:
                pass
            except Exception as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        if feedback_replaced and feedback_original is not None:
            try:
                _atomic_write("feedback.json", feedback_original)
            except OSError as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        for source, target in reversed(moved):
            try:
                if os.path.lexists(target) and not os.path.lexists(source):
                    os.rename(target, source)
            except OSError as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        for target in reversed(copied):
            try:
                os.unlink(target)
            except FileNotFoundError:
                pass
            except OSError as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        try:
            os.unlink(receipt_path)
        except FileNotFoundError:
            pass
        except OSError as rollback_exc:
            rollback_errors.append(str(rollback_exc))
        if not round_existed:
            try:
                os.rmdir(round_path)
            except OSError:
                pass
        if not history_existed:
            try:
                os.rmdir(history_path)
            except OSError:
                pass
        if rollback_errors:
            raise OSError(
                "archive failed: {}; rollback also failed: {}".format(
                    exc, "; ".join(rollback_errors)
                )
            )
        raise
    return [
        os.path.relpath(os.path.join(round_path, target_name), GIT_ROOT).replace(os.sep, "/")
        for _, target_name in list(copies) + list(moves)
    ] + ([os.path.relpath(receipt_path, GIT_ROOT).replace(os.sep, "/")] if receipt else [])


class _TransitionConflict(Exception):
    pass


def _verified_transition_receipt(batch_id, round_number, mode, allow_interim=False):
    try:
        directory = _history_round_directory(batch_id, round_number)
    except OSError as exc:
        raise _TransitionConflict(str(exc))
    if not os.path.isdir(directory):
        return None
    standard = os.path.join(directory, "transition.json")
    candidates = []
    if os.path.lexists(standard):
        candidates.append(standard)
    if allow_interim:
        budget = [0]
        try:
            for entry in _bounded_history_entries(directory, budget):
                if (
                    entry.name.startswith("transition-feedback-update-")
                    and entry.name.endswith(".json")
                    and entry.is_file(follow_symlinks=False)
                ):
                    candidates.append(entry.path)
        except (OSError, _HistoryLimit) as exc:
            raise _TransitionConflict(str(exc))
    for receipt_path in candidates:
        if os.path.islink(receipt_path):
            raise _TransitionConflict("archive receipt is unsafe")
        receipt = _load_json_object_path(receipt_path)
        if receipt is None:
            raise _TransitionConflict("archive receipt is unreadable or invalid")
        if (
            receipt.get("version") != 1
            or receipt.get("batchId") != batch_id
            or receipt.get("round") != round_number
            or receipt.get("mode") != mode
        ):
            if receipt_path == standard:
                raise _TransitionConflict(
                    "round was already archived by another transition"
                )
            continue
        files = receipt.get("files")
        if not isinstance(files, dict) or not files:
            raise _TransitionConflict("archive receipt has no file digests")
        for target_name, expected_digest in files.items():
            if (
                not isinstance(target_name, str)
                or target_name in (".", "..")
                or os.path.basename(target_name) != target_name
                or not isinstance(expected_digest, str)
            ):
                raise _TransitionConflict("archive receipt contains an invalid file entry")
            target = os.path.join(directory, target_name)
            if os.path.islink(target) or not os.path.isfile(target):
                raise _TransitionConflict("archived file is missing or unsafe: {}".format(target_name))
            if not secrets.compare_digest(_sha256_file(target), expected_digest):
                raise _TransitionConflict("archived file digest changed: {}".format(target_name))
        response = receipt.get("response")
        if not isinstance(response, dict):
            raise _TransitionConflict("archive receipt has no response")
        voice_paths = receipt.get("voiceNotePaths", [])
        if (
            not isinstance(voice_paths, list)
            or len(voice_paths) != len(set(voice_paths))
            or any(not isinstance(path, str) for path in voice_paths)
        ):
            raise _TransitionConflict("archive receipt has invalid voice-note paths")
        cleanup_pending = _cleanup_voice_notes(voice_paths)
        result = dict(response)
        result["idempotent"] = True
        if cleanup_pending:
            result.update({
                "error": "voice_cleanup_pending",
                "transitionDurable": True,
                "voiceCleanupPending": cleanup_pending,
            })
        return result
    return None


def _history_contains_batch(batch_id):
    history = os.path.join(FEEDBACK_DIR, "history")
    if not os.path.lexists(history):
        return False
    if not os.path.isdir(history) or not _path_is_within(FEEDBACK_DIR, history):
        return True
    prefix = batch_id + "-r"
    try:
        budget = [0]
        for entry in _bounded_history_entries(history, budget):
            suffix = entry.name[len(prefix):] if entry.name.startswith(prefix) else ""
            if suffix.isdigit() and entry.is_dir(follow_symlinks=False):
                return True
    except (OSError, _HistoryLimit):
        return True
    return False


def _voice_notes_directory(create=False):
    directory = os.path.join(FEEDBACK_DIR, "voice-notes")
    if os.path.lexists(directory):
        if os.path.islink(directory) or not os.path.isdir(directory):
            return None
    elif create:
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            if os.path.islink(directory) or not os.path.isdir(directory):
                return None
    resolved = os.path.realpath(directory)
    return resolved if _path_is_within(FEEDBACK_DIR, resolved) else None


def _voice_note_candidate(relative):
    if (
        not isinstance(relative, str)
        or not relative
        or os.path.isabs(relative)
        or "\\" in relative
    ):
        return None
    directory = _voice_notes_directory()
    if directory is None:
        return None
    candidate = os.path.abspath(os.path.join(GIT_ROOT, relative))
    stem, extension = os.path.splitext(os.path.basename(candidate))
    if (
        os.path.dirname(candidate) != directory
        or os.path.relpath(candidate, GIT_ROOT).replace(os.sep, "/") != relative
        or not _VOICE_ID.fullmatch(stem)
        or extension not in set(_VOICE_TYPES.values())
    ):
        return None
    return candidate


def _collect_voice_paths(*values):
    paths = set()

    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if (
                    key in ("voiceNote", "redoVoiceNote")
                    and isinstance(item, dict)
                    and isinstance(item.get("path"), str)
                ):
                    paths.add(item["path"])
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    for value in values:
        visit(value)
    return sorted(paths)


class _VoiceStorageError(RuntimeError):
    pass


def _voice_entry_kind(name):
    stem, extension = os.path.splitext(name)
    if (
        _VOICE_ID.fullmatch(stem) is not None
        and extension in set(_VOICE_TYPES.values())
    ):
        return "upload"
    if not (name.startswith(".") and name.endswith(".tmp")):
        return None
    temporary = name[1:-4]
    upload_name, separator, nonce = temporary.rpartition(".")
    upload_stem, upload_extension = os.path.splitext(upload_name)
    if (
        separator
        and 6 <= len(nonce) <= 64
        and all(character.isascii() and (character.isalnum() or character in "_-")
                for character in nonce)
        and _VOICE_ID.fullmatch(upload_stem) is not None
        and upload_extension in set(_VOICE_TYPES.values())
    ):
        return "temporary"
    return None


def _gc_voice_notes(now=None, grace_seconds=None, include_counts=False):
    """Reclaim old unreferenced uploads without following directory entries."""
    def result(total, uploads, entries):
        usage = (max(0, total), max(0, uploads), max(0, entries))
        return usage if include_counts else usage[0]

    if FEEDBACK_DIR is None:
        return result(0, 0, 0)
    raw_directory = os.path.join(FEEDBACK_DIR, "voice-notes")
    if not os.path.lexists(raw_directory):
        return result(0, 0, 0)
    directory = _voice_notes_directory()
    if directory is None:
        if os.path.lexists(os.path.join(FEEDBACK_DIR, "voice-notes")):
            raise _VoiceStorageError("voice-note directory is unsafe")
        return result(0, 0, 0)
    now = time.time() if now is None else float(now)
    grace = (
        _VOICE_ORPHAN_GRACE_SECONDS
        if grace_seconds is None
        else float(grace_seconds)
    )
    if grace < 0:
        raise _VoiceStorageError("voice-note cleanup grace is invalid")
    live = []
    for name in _DATA_FILES:
        value = _load_data(name)
        if value is None and os.path.lexists(_data_path(name)):
            raise _VoiceStorageError(
                "live protocol data is unsafe, so voice notes cannot be reclaimed"
            )
        live.append(value)
    referenced = set(_collect_voice_paths(*live))
    total = 0
    upload_count = 0
    entry_count = 0
    inspected = []
    try:
        entries = os.scandir(directory)
    except OSError as exc:
        raise _VoiceStorageError("voice-note directory is unreadable: {}".format(exc))
    with entries:
        for entry in entries:
            entry_count += 1
            if entry_count > _MAX_VOICE_SCAN_ENTRIES:
                raise _VoiceStorageError(
                    "voice-note directory exceeds the {}-entry scan limit".format(
                        _MAX_VOICE_SCAN_ENTRIES
                    )
                )
            try:
                before = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise _VoiceStorageError(
                    "voice-note entry is unreadable: {}".format(exc)
                )
            if not stat.S_ISREG(before.st_mode):
                raise _VoiceStorageError(
                    "voice-note directory contains a link or special file"
                )
            kind = _voice_entry_kind(entry.name)
            if kind is None:
                raise _VoiceStorageError(
                    "voice-note directory contains an unexpected entry"
                )
            total += max(0, before.st_size)
            if kind == "upload":
                upload_count += 1
            inspected.append((entry.path, before, kind))
    if upload_count > _MAX_VOICE_FILES:
        raise _VoiceStorageError(
            "voice-note directory exceeds the {}-file limit".format(
                _MAX_VOICE_FILES
            )
        )
    for entry_path, before, kind in inspected:
        is_upload = kind == "upload"
        relative = os.path.relpath(entry_path, GIT_ROOT).replace(os.sep, "/")
        if is_upload and relative in referenced:
            continue
        if max(0.0, now - before.st_mtime) < grace:
            continue
        try:
            current = os.lstat(entry_path)
        except FileNotFoundError:
            total -= max(0, before.st_size)
            entry_count -= 1
            if is_upload:
                upload_count -= 1
            continue
        except OSError as exc:
            raise _VoiceStorageError(
                "voice-note entry changed during cleanup: {}".format(exc)
            )
        before_signature = (
            before.st_dev, before.st_ino, before.st_mode, before.st_size,
            getattr(before, "st_mtime_ns", int(before.st_mtime * 1000000000)),
        )
        current_signature = (
            current.st_dev, current.st_ino, current.st_mode, current.st_size,
            getattr(current, "st_mtime_ns", int(current.st_mtime * 1000000000)),
        )
        if before_signature != current_signature or not stat.S_ISREG(current.st_mode):
            raise _VoiceStorageError("voice-note entry changed during cleanup")
        try:
            os.unlink(entry_path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise _VoiceStorageError(
                "old orphan voice note could not be removed: {}".format(exc)
            )
        total -= max(0, before.st_size)
        entry_count -= 1
        if is_upload:
            upload_count -= 1
    return result(total, upload_count, entry_count)


def _cleanup_voice_notes(paths):
    pending = []
    live = [_load_data(name) for name in _DATA_FILES]
    for relative in dict.fromkeys(paths):
        if any(_references_voice_path(value, relative) for value in live):
            continue
        candidate = _voice_note_candidate(relative)
        if candidate is None:
            pending.append(relative)
            continue
        try:
            candidate_stat = os.lstat(candidate)
            if stat.S_ISREG(candidate_stat.st_mode):
                os.unlink(candidate)
            else:
                pending.append(relative)
        except FileNotFoundError:
            pass
        except OSError:
            # The durable receipt keeps the path so an idempotent retry can
            # attempt cleanup again without rolling back the completed archive.
            pending.append(relative)
    return pending


def _valid_voice_note(note):
    """Accept only an existing upload stored directly in this inbox."""
    if note is None:
        return True
    if not _valid_voice_note_shape(note):
        return False
    candidate = _voice_note_candidate(note.get("path"))
    if candidate is None:
        return False
    stem, extension = os.path.splitext(os.path.basename(candidate))
    if (
        not _VOICE_ID.fullmatch(stem)
        or extension != _VOICE_TYPES.get(note.get("mimeType"))
    ):
        return False
    try:
        candidate_stat = os.lstat(candidate)
    except OSError:
        return False
    return (
        stat.S_ISREG(candidate_stat.st_mode)
        and candidate_stat.st_size == note.get("bytes")
    )


def _safe_file_equals(path, expected):
    try:
        path_stat = os.lstat(path)
    except OSError:
        return False
    if not stat.S_ISREG(path_stat.st_mode) or path_stat.st_size != len(expected):
        return False
    try:
        with open(path, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if (opened.st_dev, opened.st_ino) != (path_stat.st_dev, path_stat.st_ino):
                return False
            return handle.read(len(expected) + 1) == expected
    except OSError:
        return False


def _references_voice_path(value, relative):
    if isinstance(value, dict):
        if value.get("path") == relative:
            return True
        return any(_references_voice_path(item, relative) for item in value.values())
    if isinstance(value, list):
        return any(_references_voice_path(item, relative) for item in value)
    return False


def _review_schema_error(review, feedback):
    allowed_top = {
        "version", "kind", "batchId", "round", "beforeRef", "createdAt", "points"
    }
    allowed_point = {"id", "handled", "note", "commit", "abc"}
    if not isinstance(review, dict) or not set(review).issubset(allowed_top):
        return "review contains unsupported fields"
    if review.get("version") != 1 or review.get("kind") != "review":
        return "review kind or version is invalid"
    if (
        not isinstance(review.get("batchId"), str)
        or not _BATCH_ID_RE.fullmatch(review["batchId"])
        or not _valid_round(review.get("round"))
    ):
        return "review batch or round is invalid"
    before_ref = review.get("beforeRef")
    if not isinstance(before_ref, str) or not _SHA_RE.fullmatch(before_ref):
        return "review beforeRef is invalid"
    if "createdAt" in review and not _bounded_string(review["createdAt"], 256):
        return "review createdAt is invalid"
    points = review.get("points")
    if not _valid_point_objects(points):
        return "review points must have unique safe ids"
    feedback_ids = (
        set(_point_id_list(feedback.get("points")))
        if isinstance(feedback, dict) else set()
    )
    if not set(_point_id_list(points)).issubset(feedback_ids):
        return "review points must be a subset of feedback points"
    for point in points:
        if not set(point).issubset(allowed_point):
            return "review point contains unsupported fields"
        handled = point.get("handled")
        if handled not in ("done", "abc", "skipped"):
            return "review point handled value is invalid"
        if not _bounded_human_text(point.get("note"), 10000):
            return "review point note is invalid"
        commit = point.get("commit")
        if handled in ("done", "abc"):
            if not isinstance(commit, str) or not _SHA_RE.fullmatch(commit):
                return "done and ABC review points require a full commit SHA"
        elif commit is not None:
            return "skipped review point commit must be omitted or null"
        abc = point.get("abc")
        if handled == "abc":
            if not isinstance(abc, dict) or set(abc) != {"scopeId", "letters"}:
                return "ABC review metadata is invalid"
            letters = abc.get("letters")
            if (
                not _bounded_string(abc.get("scopeId"), 128, allow_empty=False)
                or not isinstance(letters, str)
                or not 2 <= len(letters) <= 10
                or len(set(letters)) != len(letters)
                or any(letter not in "ABCDEFGHIJ" for letter in letters)
            ):
                return "ABC review metadata is invalid"
        elif "abc" in point:
            return "ABC metadata is allowed only on an ABC review point"
    return None


def _feedback_update_marker_error(marker, batch_id, round_number):
    allowed = {"version", "kind", "batchId", "round", "sentAt", "addedPointIds"}
    if not isinstance(marker, dict) or not set(marker).issubset(allowed):
        return "feedback update marker contains unsupported fields"
    ids = marker.get("addedPointIds")
    if (
        marker.get("version") != 1
        or marker.get("kind") != "feedback_update"
        or marker.get("batchId") != batch_id
        or marker.get("round") != round_number
        or not isinstance(ids, list)
        or len(ids) != len(set(ids))
        or any(
            not isinstance(point_id, str) or not _POINT_ID_RE.fullmatch(point_id)
            for point_id in ids
        )
        or ("sentAt" in marker and not _bounded_string(marker["sentAt"], 256))
    ):
        return "feedback update marker is invalid"
    return None


def _persisted_verdict_error(payload, review):
    allowed_top = {"version", "kind", "batchId", "round", "sentAt", "verdicts"}
    allowed_item = {
        "pointId", "verdict", "chosenLetter", "redoText", "redoVoiceNote"
    }
    if not isinstance(payload, dict) or not set(payload).issubset(allowed_top):
        return "verdict payload contains unsupported fields"
    if payload.get("version") != 1 or payload.get("kind") != "verdicts":
        return "verdict payload kind or version is invalid"
    if (
        payload.get("batchId") != review.get("batchId")
        or payload.get("round") != review.get("round")
    ):
        return "verdict payload does not match the active review"
    if "sentAt" in payload and not _bounded_string(payload["sentAt"], 256):
        return "verdict sentAt is invalid"
    review_points = review.get("points")
    if not _valid_point_objects(review_points):
        return "active review points are invalid"
    review_by_id = {point["id"]: point for point in review_points}
    items = payload.get("verdicts")
    if not isinstance(items, list):
        return "verdict list is invalid"
    ids = []
    for item in items:
        if (
            not isinstance(item, dict)
            or not set(item).issubset(allowed_item)
            or not isinstance(item.get("pointId"), str)
            or not _POINT_ID_RE.fullmatch(item["pointId"])
            or item.get("verdict") not in ("accept", "delete", "redo")
        ):
            return "verdict item is invalid"
        ids.append(item["pointId"])
        verdict = item["verdict"]
        if "redoText" in item and not _bounded_human_text(
            item["redoText"], _MAX_REDO_TEXT
        ):
            return "verdict redoText is invalid"
        if verdict != "redo" and ("redoText" in item or "redoVoiceNote" in item):
            return "redo fields are allowed only on redo"
        if verdict != "accept" and "chosenLetter" in item:
            return "chosenLetter is allowed only on accept"
        if "redoVoiceNote" in item and not _valid_voice_note_shape(item["redoVoiceNote"]):
            return "redoVoiceNote shape is invalid"
    if len(ids) != len(set(ids)) or set(ids) != set(review_by_id):
        return "verdicts must cover exactly the active review points"
    for item in items:
        if item["verdict"] != "accept":
            continue
        point = review_by_id[item["pointId"]]
        if point.get("handled") == "abc":
            abc = point.get("abc")
            letters = abc.get("letters") if isinstance(abc, dict) else None
            chosen = item.get("chosenLetter")
            if (
                not isinstance(letters, str)
                or not 1 <= len(letters) <= 10
                or len(set(letters)) != len(letters)
                or not isinstance(chosen, str)
                or len(chosen) != 1
                or chosen not in letters
            ):
                return "ABC accept metadata is invalid"
        elif "chosenLetter" in item:
            return "non-ABC accepts cannot include chosenLetter"
    return None


def _phase(batch, review, verdicts):
    # Only the empty inbox is collecting. Other incomplete combinations are
    # transition states and must not invite the overlay to submit a new batch.
    if batch is None:
        return "collecting" if review is None and verdicts is None else "transitioning"
    if _feedback_schema_error(batch):
        return "transitioning"
    batch_id = batch.get("batchId")
    batch_round = batch.get("round", 1)
    if (
        not isinstance(batch_id, str)
        or not _BATCH_ID_RE.fullmatch(batch_id)
        or not _valid_round(batch_round)
    ):
        return "transitioning"
    if review is None:
        if verdicts is None:
            return "awaiting_agent"
        marker_matches = not _feedback_update_marker_error(
            verdicts, batch_id, batch_round
        )
        return "awaiting_agent" if marker_matches else "transitioning"
    if _review_schema_error(review, batch):
        return "transitioning"
    review_matches = (
        review.get("batchId") == batch_id
        and _valid_round(review.get("round"))
        and review.get("round") == batch_round
    )
    if not review_matches:
        return "transitioning"
    if verdicts is None:
        return "reviewing"
    if verdicts.get("kind") == "feedback_update":
        verdicts_match = not _feedback_update_marker_error(
            verdicts, batch_id, batch_round
        )
    else:
        verdicts_match = not _persisted_verdict_error(verdicts, review)
    return "verdicts_sent" if verdicts_match else "transitioning"


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


# Rewrite root-absolute references so every asset a BEFORE page pulls in is
# also served from the before snapshot (relative refs resolve under the
# /__wk/before/ prefix automatically). Without this the toggle silently shows
# CURRENT-tree assets whenever a path is unchanged but its CONTENT changed -
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
_HTML_ATTRIBUTE = re.compile(
    r'(?P<space>\s+)(?P<name>[^\s"\'<>/=]+)(?P<equals>\s*=\s*)'
    r'(?:(?P<quote>["\'])(?P<quoted_value>.*?)(?P=quote)'
    r'|(?P<unquoted_value>[^\s"\'=<>`]+))',
    re.S,
)


class _TransformTooLarge(ValueError):
    pass


def _root_reference(value):
    return value.startswith("/") and not value.startswith(("//", "/__wk/"))


def _srcset_url_spans(value):
    """Yield URL spans using the candidate boundaries from the srcset grammar."""
    length = len(value)
    position = 0
    whitespace = " \t\n\r\f"
    while position < length:
        while position < length and value[position] in whitespace + ",":
            position += 1
        if position >= length:
            return
        start = position
        while position < length and value[position] not in whitespace:
            position += 1
        end = position
        while end > start and value[end - 1] == ",":
            end -= 1
        if end > start:
            yield start, end
        if end != position:
            continue
        parentheses = 0
        while position < length:
            char = value[position]
            if char == "(":
                parentheses += 1
            elif char == ")" and parentheses:
                parentheses -= 1
            elif char == "," and not parentheses:
                position += 1
                break
            position += 1


def _rewrite_before_srcset_value(value, prefix):
    insertions = [
        start for start, end in _srcset_url_spans(value)
        if _root_reference(value[start:end])
    ]
    for start in reversed(insertions):
        value = value[:start] + prefix + value[start:]
    return value


def _ensure_transform_size(size, maximum):
    if maximum is not None and size > maximum:
        raise _TransformTooLarge("transformed response exceeds its output limit")


def _skip_css_string(text, position):
    quote = text[position]
    position += 1
    while position < len(text):
        if text[position] == "\\":
            position += 2
            continue
        if text[position] == quote:
            return position + 1
        position += 1
    return position


def _css_root_reference_offsets(text):
    offsets = []
    lower = text.lower()
    position = 0
    whitespace = " \t\n\r\f"
    identifier = "abcdefghijklmnopqrstuvwxyz0123456789-_"
    while position < len(text):
        if text.startswith("/*", position):
            end = text.find("*/", position + 2)
            position = len(text) if end < 0 else end + 2
            continue
        if text[position] in "\"'":
            position = _skip_css_string(text, position)
            continue
        if lower.startswith("url", position) and (
            position == 0 or lower[position - 1] not in identifier
        ):
            cursor = position + 3
            while cursor < len(text) and text[cursor] in whitespace:
                cursor += 1
            if cursor < len(text) and text[cursor] == "(":
                cursor += 1
                while cursor < len(text) and text[cursor] in whitespace:
                    cursor += 1
                if cursor < len(text) and text[cursor] in "\"'":
                    value_start = cursor + 1
                    if _root_reference(text[value_start:]):
                        offsets.append(value_start)
                    position = _skip_css_string(text, cursor)
                    while position < len(text) and text[position] in whitespace:
                        position += 1
                    if position < len(text) and text[position] == ")":
                        position += 1
                    continue
                value_start = cursor
                if _root_reference(text[value_start:]):
                    offsets.append(value_start)
                while cursor < len(text):
                    if text[cursor] == "\\":
                        cursor += 2
                        continue
                    if text[cursor] == ")":
                        cursor += 1
                        break
                    cursor += 1
                position = cursor
                continue
        if lower.startswith("@import", position) and (
            position == 0 or lower[position - 1] not in identifier
        ):
            cursor = position + len("@import")
            if cursor < len(text) and text[cursor] in whitespace:
                while cursor < len(text) and text[cursor] in whitespace:
                    cursor += 1
                if cursor < len(text) and text[cursor] in "\"'":
                    value_start = cursor + 1
                    if _root_reference(text[value_start:]):
                        offsets.append(value_start)
                    position = _skip_css_string(text, cursor)
                    continue
        position += 1
    return offsets


def _rewrite_before_css(text, prefix="/__wk/before", maximum=None):
    offsets = _css_root_reference_offsets(text)
    projected = len(text.encode("utf-8")) + len(prefix.encode("utf-8")) * len(offsets)
    _ensure_transform_size(projected, maximum)
    for offset in reversed(offsets):
        text = text[:offset] + prefix + text[offset:]
    _ensure_transform_size(len(text.encode("utf-8")), maximum)
    return text


def _rewrite_before_starttag(raw, prefix):
    def rewrite_value(name, value):
        lowered = name.lower()
        if lowered in ("src", "href", "poster") and _root_reference(value):
            return prefix + value
        if lowered == "srcset":
            return _rewrite_before_srcset_value(value, prefix)
        if lowered == "style":
            return _rewrite_before_css(value, prefix)
        return value

    def replace(match):
        name = match.group("name")
        quote = match.group("quote") or ""
        original = (
            match.group("quoted_value")
            if match.group("quoted_value") is not None
            else match.group("unquoted_value")
        )
        value = rewrite_value(name, original)
        return "{}{}{}{}{}{}".format(
            match.group("space"), name, match.group("equals"),
            quote, value, quote,
        )

    return _HTML_ATTRIBUTE.sub(replace, raw)


class _BeforeHTMLParser(HTMLParser):
    _INERT_CONTAINERS = {"template", "noscript"}

    def __init__(self, source, prefix, maximum):
        super().__init__(convert_charrefs=True)
        self.source = source
        self.prefix = prefix
        self.maximum = maximum
        self.projected = len(source.encode("utf-8"))
        self.replacements = []
        self.inert_stack = []
        self.in_style = False
        self.line_offsets = [0]
        self.line_offsets.extend(match.end() for match in re.finditer(r"\n", source))
        _ensure_transform_size(self.projected, maximum)

    def _offset(self):
        line, column = self.getpos()
        return self.line_offsets[line - 1] + column

    def _replace(self, start, end, replacement):
        original = self.source[start:end]
        if replacement == original:
            return
        self.projected += len(replacement.encode("utf-8")) - len(original.encode("utf-8"))
        _ensure_transform_size(self.projected, self.maximum)
        self.replacements.append((start, end, replacement))

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in self._INERT_CONTAINERS:
            self.inert_stack.append(tag)
            return
        if self.inert_stack:
            return
        raw = self.get_starttag_text() or ""
        start = self._offset()
        self._replace(start, start + len(raw), _rewrite_before_starttag(raw, self.prefix))
        if tag == "style":
            self.in_style = True

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() == "style":
            self.in_style = False

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self.inert_stack:
            if tag == self.inert_stack[-1]:
                self.inert_stack.pop()
            return
        if tag == "style":
            self.in_style = False

    def handle_data(self, data):
        if not self.in_style or self.inert_stack:
            return
        start = self._offset()
        self._replace(start, start + len(data), _rewrite_before_css(data, self.prefix))


def _rewrite_before_html(html, prefix="/__wk/before", maximum=None):
    parser = _BeforeHTMLParser(html, prefix, maximum)
    try:
        parser.feed(html)
        parser.close()
    except _TransformTooLarge:
        raise
    except (AssertionError, ValueError) as exc:
        raise _CSPTransformError("before snapshot HTML could not be parsed safely") from exc
    for start, end, replacement in reversed(parser.replacements):
        html = html[:start] + replacement + html[end:]
    _ensure_transform_size(len(html.encode("utf-8")), maximum)
    return html


# A beforeRef is always a full `git rev-parse HEAD` SHA (per LOOP.md). Validate
# it against this before it ever reaches `git show`: a value beginning with `-`
# would otherwise be parsed by git as an OPTION (e.g. --output=…), an argument-
# injection / arbitrary-file-write vector. See _wk_before.
_SHA_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")

_MAX_BODY = 2 * 1024 * 1024
_MAX_BEFORE_BODY = 30 * 1024 * 1024
_MAX_PROXY_BODY = 30 * 1024 * 1024
_MAX_PROXY_RESPONSE = 30 * 1024 * 1024
_MAX_VOICE_BODY = 25 * 1024 * 1024
_MAX_VOICE_DIRECTORY_BYTES = 100 * 1024 * 1024
_MAX_VOICE_FILES = 1000
_MAX_VOICE_SCAN_ENTRIES = 1024
_VOICE_ORPHAN_GRACE_SECONDS = 60 * 60
_MAX_TRANSFORM_HTML = 8 * 1024 * 1024
_MAX_TRANSFORMED_HTML = 16 * 1024 * 1024
_MAX_TRANSFORMED_BEFORE_HTML = 16 * 1024 * 1024
_MAX_TRANSFORMED_BEFORE_CSS = 32 * 1024 * 1024
_CLIENT_IO_TIMEOUT_SECONDS = 15
_MAX_ERROR_DIAGNOSTIC_BYTES = 2048
_MAX_REDO_TEXT = 10000
_VOICE_ID = re.compile(r"^[A-Za-z0-9_-]{6,80}$")
_VOICE_TYPES = {
    "audio/webm": ".webm", "audio/ogg": ".ogg", "audio/mp4": ".m4a",
    "audio/mpeg": ".mp3", "audio/wav": ".wav", "audio/x-wav": ".wav",
}


class _NoProxyRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_PROXY_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), _NoProxyRedirect()
)


def _proxy_urlopen(request, timeout):
    return _PROXY_OPENER.open(request, timeout=timeout)


def _exception_diagnostic(exc):
    try:
        detail = repr(str(exc))
    except Exception:
        detail = "<unprintable exception>"
    diagnostic = "{}: {}".format(type(exc).__name__, detail)
    encoded = diagnostic.encode("utf-8", errors="backslashreplace")
    if len(encoded) > _MAX_ERROR_DIAGNOSTIC_BYTES:
        encoded = encoded[:_MAX_ERROR_DIAGNOSTIC_BYTES - 3] + b"..."
    return encoded.decode("utf-8", errors="ignore")


def _bounded_log_text(value):
    encoded = str(value).encode("utf-8", errors="backslashreplace")
    if len(encoded) > _MAX_ERROR_DIAGNOSTIC_BYTES:
        encoded = encoded[:_MAX_ERROR_DIAGNOSTIC_BYTES - 3] + b"..."
    return encoded.decode("utf-8", errors="ignore")


class PreviewHTTPServer(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(_CLIENT_IO_TIMEOUT_SECONDS)
        return request, address


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("directory", ROOT)
        super().__init__(*args, **kwargs)

    def log_request(self, _code="-", _size="-"):
        return

    def log_error(self, fmt, *args):
        try:
            message = fmt % args
        except (TypeError, ValueError):
            message = fmt
        SimpleHTTPRequestHandler.log_message(
            self, "%s", _bounded_log_text(message)
        )

    def _host_allowed(self):
        host = self.headers.get("Host", "").strip().lower()
        port = int(self.server.server_port)
        expected = {"{}:{}".format(value, port) for value in ALLOWED_HOSTS}
        if port == 80:
            expected.update(ALLOWED_HOSTS)
        return host in expected

    def _guard_host(self):
        if self._host_allowed():
            return True
        self._send_json(421, {"error": "request Host is not allowed for this preview"})
        return False

    def _same_origin(self):
        origin = self.headers.get("Origin", "").strip().lower()
        host = self.headers.get("Host", "").strip().lower()
        return bool(origin and host and origin == "http://" + host)

    def _safe_translated_path(self, request_path):
        path = self.translate_path(request_path)
        return path if _path_is_within(ROOT, path) else None

    def end_headers(self):
        # Live dev preview: never let the browser cache ANY asset. The base
        # SimpleHTTPRequestHandler only sends Last-Modified for static files
        # (css/js/images), so an edited stylesheet could be served stale from
        # disk cache even after a ?v= bump - this stamps no-store on every
        # response (HTML, /__wk JSON, overlay assets, before-mode: all of it).
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if INSTANCE_TOKEN:
            self.send_header("X-WK-Preview-Instance", INSTANCE_TOKEN)
        super().end_headers()

    # --- tiny response helpers ---
    def _send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_bytes(self, body, ctype, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # --- request entry points ---
    def do_GET(self):
        if not self._guard_host():
            return
        raw = self.path.split("?", 1)[0]
        if raw.startswith("/__wk/"):
            return self._wk(self._wk_get, raw)
        if API_PROXY_ORIGIN and raw.startswith("/api/"):
            return self._proxy_api("GET")
        path = self._safe_translated_path(self.path)
        if path is None:
            return self._send_json(403, {"error": "path escapes the preview root"})
        if not _static_path_allowed(self.path, path):
            return self._send_json(404, {"error": "path is not available from this preview"})
        if FEEDBACK_DIR and _path_is_within(FEEDBACK_DIR, path):
            return self._send_json(404, {"error": "feedback runtime files are not served"})
        if os.path.isdir(path):
            index_path = None
            for idx in ("index.html", "index.htm"):
                cand = os.path.join(path, idx)
                if os.path.isfile(cand) and _static_path_allowed(self.path, cand):
                    index_path = cand
                    break
            if index_path is None:
                return self._send_json(404, {"error": "directory indexes are disabled"})
            if not raw.endswith("/"):
                return super().do_GET()  # base class issues the trailing-slash redirect
            path = index_path
        if not _path_is_within(ROOT, path):
            return self._send_json(403, {"error": "path escapes the preview root"})
        if FEEDBACK_DIR and _path_is_within(FEEDBACK_DIR, path):
            return self._send_json(404, {"error": "feedback runtime files are not served"})
        if path.endswith((".html", ".htm")) and os.path.isfile(path):
            return self._serve_html(path, include_body=True)
        return super().do_GET()

    def do_HEAD(self):
        if not self._guard_host():
            return
        # HEAD must report the SAME Content-Length GET would return. Since GET
        # stamps the <title> and injects the overlay (growing the body), the
        # base class's do_HEAD (which reports the on-disk file size) would lie
        # to prefetchers/link-checkers that HEAD before GETting. Route HTML
        # through the same transform with the body suppressed; everything else
        # (non-HTML, /__wk/, directory listings) keeps base-class behavior.
        raw = self.path.split("?", 1)[0]
        if raw.startswith("/__wk/"):
            return self._wk(self._wk_get, raw)
        path = self._safe_translated_path(self.path)
        if path is None:
            return self._send_json(403, {"error": "path escapes the preview root"})
        if not _static_path_allowed(self.path, path):
            return self._send_json(404, {"error": "path is not available from this preview"})
        if FEEDBACK_DIR and _path_is_within(FEEDBACK_DIR, path):
            return self._send_json(404, {"error": "feedback runtime files are not served"})
        if os.path.isdir(path):
            index_path = None
            for idx in ("index.html", "index.htm"):
                cand = os.path.join(path, idx)
                if os.path.isfile(cand) and _static_path_allowed(self.path, cand):
                    index_path = cand
                    break
            if index_path is None:
                return self._send_json(404, {"error": "directory indexes are disabled"})
            if not raw.endswith("/"):
                return super().do_HEAD()  # base class issues the redirect
            path = index_path
        if not _path_is_within(ROOT, path):
            return self._send_json(403, {"error": "path escapes the preview root"})
        if FEEDBACK_DIR and _path_is_within(FEEDBACK_DIR, path):
            return self._send_json(404, {"error": "feedback runtime files are not served"})
        if path.endswith((".html", ".htm")) and os.path.isfile(path):
            return self._serve_html(path, include_body=False)
        return super().do_HEAD()

    def _serve_html(self, path, include_body):
        # Shared GET/HEAD body computation so both report identical headers
        # (Content-Length in particular) for the transformed HTML.
        try:
            with open(path, "rb") as f:
                opened = os.fstat(f.fileno())
                if opened.st_size > _MAX_TRANSFORM_HTML:
                    return self._send_json(
                        413,
                        {"error": "HTML exceeds the 8 MB transformation limit"},
                    )
                body = f.read(_MAX_TRANSFORM_HTML + 1)
        except OSError:
            return self._send_json(404, {"error": "HTML file is unavailable"})
        if len(body) > _MAX_TRANSFORM_HTML:
            return self._send_json(
                413, {"error": "HTML exceeds the 8 MB transformation limit"}
            )
        try:
            html = stamp(body.decode("utf-8"))
            seed_preview = parse_qs(urlparse(self.path).query).get("wk_seed_preview", [""])[0] == "1"
            body = (html if seed_preview else inject(html, "after")).encode("utf-8")
        except UnicodeDecodeError:
            pass
        except _CSPTransformError:
            return self._send_json(
                422,
                {"error": "HTML CSP could not be adapted safely for the Webkit overlay"},
            )
        if len(body) > _MAX_TRANSFORMED_HTML:
            return self._send_json(
                413, {"error": "transformed HTML exceeds the 16 MB response limit"}
            )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()  # no-store added by the override above
        if include_body:
            self.wfile.write(body)

    def do_POST(self):
        if not self._guard_host():
            return
        raw = self.path.split("?", 1)[0]
        if raw.startswith("/__wk/"):
            if self.headers.get("Origin") is not None and not self._same_origin():
                return self._send_json(
                    403, {"error": "cross-origin Webkit mutation rejected"}
                )
            if raw == "/__wk/transition":
                supplied = self.headers.get("X-WK-Transition-Token", "")
                if not supplied or not secrets.compare_digest(supplied, TRANSITION_TOKEN):
                    return self._send_json(403, {"error": "invalid Webkit transition token"})
            else:
                supplied = self.headers.get("X-WK-Token", "")
                if not supplied or not secrets.compare_digest(supplied, MUTATION_TOKEN):
                    return self._send_json(403, {"error": "invalid Webkit mutation token"})
            return self._wk(self._wk_post, raw)
        if API_PROXY_ORIGIN and raw.startswith("/api/"):
            if not self._same_origin():
                return self._send_json(403, {"error": "cross-origin API proxy request rejected"})
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
            if length < 0:
                return self._send_json(400, {"error": "bad Content-Length"})
            if length > _MAX_PROXY_BODY:
                return self._send_json(413, {
                    "error": "Project references are too large to send. Keep the combined upload under 20 MB."
                })
            body = self.rfile.read(length) if length else b""
            if len(body) != length:
                return self._send_json(400, {"error": "proxy request body was incomplete"})
        headers = {"Accept": self.headers.get("Accept", "application/json")}
        if API_PROXY_HOST_HEADER:
            headers["Host"] = API_PROXY_HOST_HEADER
        content_type = self.headers.get("Content-Type")
        if content_type:
            headers["Content-Type"] = content_type
        control_token = self.headers.get("X-WKCC-Token", "")
        if re.fullmatch(r"[A-Za-z0-9_-]{16,128}", control_token):
            headers["X-WKCC-Token"] = control_token
        request = urllib.request.Request(
            API_PROXY_ORIGIN + self.path,
            data=body,
            headers=headers,
            method=method,
        )
        try:
            response = _proxy_urlopen(request, timeout=180)
        except urllib.error.HTTPError as exc:
            response = exc
        except (OSError, urllib.error.URLError) as exc:
            return self._send_json(502, {"error": "Control Center API unavailable: {}".format(exc)})
        with response:
            if 300 <= response.status < 400:
                return self._send_json(
                    502, {"error": "Control Center API redirects are not allowed"}
                )
            payload = response.read(_MAX_PROXY_RESPONSE + 1)
            if len(payload) > _MAX_PROXY_RESPONSE:
                return self._send_json(
                    502, {"error": "Control Center API response exceeded 30 MB"}
                )
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
        except Exception as exc:  # noqa: BLE001, deliberate API-boundary catch
            self.log_error(
                "WebKit endpoint failed: %s", _exception_diagnostic(exc)
            )
            try:
                return self._send_json(500, {"error": "internal server error"})
            except OSError:
                pass

    # --- GET routes ---
    def _wk_get(self, raw):
        if raw in ("/__wk/overlay.js", "/__wk/overlay.css"):
            return self._wk_overlay_asset(os.path.basename(raw))
        if raw in ("/__wk/handshake", "/__wk/state"):
            supplied = self.headers.get("X-WK-Token", "")
            if not supplied or not secrets.compare_digest(supplied, MUTATION_TOKEN):
                return self._send_json(403, {"error": "invalid Webkit mutation token"})
            if raw == "/__wk/handshake":
                return self._send_bytes(b"", "text/plain; charset=utf-8", code=204)
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
            return self._send_json(503, {"error": "not a git repository - feedback loop disabled"})
        known = parse_qs(urlparse(self.path).query).get("known", [""])[0]
        with _WRITE_LOCK:
            rev = _rev()
            review = _load_data("review.json")
            before_prefix = _before_prefix(review)
            if known == rev:
                response = {
                    "changed": False, "rev": rev,
                    "beforePrefix": before_prefix,
                }
            else:
                batch = _load_data("feedback.json")
                verdicts = _load_data("verdicts.json")
                response = {
                    "changed": True,
                    "rev": rev,
                    "color": SLUG,
                    "emoji": COLOR,
                    "phase": _phase(batch, review, verdicts),
                    "batch": batch,
                    "review": review,
                    "verdicts": verdicts,
                    "beforePrefix": before_prefix,
                }
        return self._send_json(
            200,
            response,
        )

    def _wk_before(self, raw):
        if GIT_ROOT is None:
            return self._send_json(503, {"error": "not a git repository - before mode disabled"})
        with _WRITE_LOCK:
            review = _load_data("review.json") if FEEDBACK_DIR else None
        expected_capability = _before_capability(review)
        tail = raw[len("/__wk/before/"):] if raw.startswith("/__wk/before/") else ""
        supplied_capability, separator, requested = tail.partition("/")
        if (
            not expected_capability
            or not supplied_capability
            or not secrets.compare_digest(supplied_capability, expected_capability)
        ):
            return self._send_json(403, {"error": "invalid before-snapshot capability"})
        sub = "/" + requested if separator else "/"
        # Resolve exactly like normal serving so BEFORE|AFTER swap 1:1.
        fs = self._safe_translated_path(sub)
        if fs is None:
            return self._send_json(403, {"error": "path escapes the preview root"})
        if not _static_path_allowed(sub, fs):
            return self._send_json(404, {"error": "path is not available from this preview"})
        if os.path.isdir(fs):
            index_path = None
            for idx in ("index.html", "index.htm"):
                cand = os.path.join(fs, idx)
                if os.path.isfile(cand) and _static_path_allowed(sub, cand):
                    index_path = cand
                    break
            if index_path is None:
                return self._send_json(404, {"error": "no index page in directory"})
            if not raw.endswith("/"):
                # Mirror the base class's trailing-slash redirect so relative
                # asset URLs inside the page resolve under the right prefix.
                query = self.path.partition("?")[2]
                self.send_response(301)
                self.send_header("Location", raw + "/" + ("?" + query if query else ""))
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            fs = index_path
        if not _path_is_within(ROOT, fs):
            return self._send_json(403, {"error": "path escapes the preview root"})
        fs = os.path.realpath(fs)
        if not _path_is_within(GIT_ROOT, fs):
            return self._send_json(403, {"error": "path escapes the git repository"})
        rel = os.path.relpath(fs, os.path.realpath(GIT_ROOT))
        before_ref = (review or {}).get("beforeRef")
        if not before_ref:
            return self._send_json(409, {"error": "no review round active"})
        # beforeRef flows into `git show` as a rev - it MUST be a bare SHA. An
        # unvalidated value beginning with `-` would be parsed by git as an
        # option (git argument injection → arbitrary file write). Reject
        # anything that isn't a hex SHA, AND pass --end-of-options as a second
        # guard so even a hypothetical leading-`-` ref can't be read as a flag.
        if not isinstance(before_ref, str) or not _SHA_RE.fullmatch(before_ref):
            return self._send_json(409, {"error": "review.json beforeRef is not a valid commit SHA"})
        object_name = "{}:{}".format(before_ref, rel.replace(os.sep, "/"))
        try:
            size_probe = subprocess.run(
                ["git", "-C", GIT_ROOT, "cat-file", "-s", object_name],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
            )
            if size_probe.returncode != 0:
                return self._send_json(404, {"error": "not in before snapshot"})
            blob_size = int(size_probe.stdout.strip())
            if blob_size > _MAX_BEFORE_BODY:
                return self._send_json(413, {"error": "before snapshot file exceeds 30 MB"})
            if fs.endswith((".html", ".htm")) and blob_size > _MAX_TRANSFORM_HTML:
                return self._send_json(
                    413,
                    {"error": "before snapshot HTML exceeds the 8 MB transformation limit"},
                )
            p = subprocess.run(
                ["git", "-C", GIT_ROOT, "show", "--end-of-options",
                 object_name],
                capture_output=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired, ValueError):
            p = None
        if p is None or p.returncode != 0:
            return self._send_json(404, {"error": "not in before snapshot"})
        body = p.stdout
        if fs.endswith((".html", ".htm")):
            try:
                html = body.decode("utf-8")
            except UnicodeDecodeError:
                return self._send_bytes(body, "text/html; charset=utf-8")
            prefix = _before_prefix(review)
            try:
                html = stamp(html)
                html = _rewrite_before_html(
                    html, prefix, _MAX_TRANSFORMED_BEFORE_HTML
                )
                html = inject(html, "before", prefix)
            except _TransformTooLarge:
                return self._send_json(413, {
                    "error": "transformed before snapshot HTML exceeds 16 MB"
                })
            except _CSPTransformError:
                return self._send_json(422, {
                    "error": "before snapshot CSP could not be adapted safely for the Webkit overlay"
                })
            payload = html.encode("utf-8")
            if len(payload) > _MAX_TRANSFORMED_BEFORE_HTML:
                return self._send_json(413, {
                    "error": "transformed before snapshot HTML exceeds 16 MB"
                })
            return self._send_bytes(payload, "text/html; charset=utf-8")
        ctype = mimetypes.guess_type(fs)[0] or "application/octet-stream"
        # External stylesheets are served verbatim from git - but any
        # root-absolute url()/@import inside them would escape to the current
        # tree, so run the same CSS rewrite the HTML path uses (the href that
        # loaded this CSS was already rewritten to come through /__wk/before/).
        if ctype == "text/css" or fs.endswith(".css"):
            try:
                text = body.decode("utf-8")
            except UnicodeDecodeError:
                return self._send_bytes(body, "text/css; charset=utf-8")
            try:
                text = _rewrite_before_css(
                    text, _before_prefix(review), _MAX_TRANSFORMED_BEFORE_CSS
                )
            except _TransformTooLarge:
                return self._send_json(413, {
                    "error": "transformed before snapshot CSS exceeds 32 MB"
                })
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
        if raw == "/__wk/transition":
            return self._wk_transition()
        return self._send_json(404, {"error": "unknown webkit endpoint: " + raw})

    def _wk_voice_note(self):
        if FEEDBACK_DIR is None:
            return self._send_json(503, {"error": "not a git repository - feedback loop disabled"})
        note_id = parse_qs(urlparse(self.path).query).get("id", [""])[0]
        if not _VOICE_ID.fullmatch(note_id):
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
        directory = _voice_notes_directory(create=True)
        if directory is None:
            return self._send_json(409, {"error": "voice-note directory is unsafe"})
        filename = note_id + extension
        path = os.path.join(directory, filename)
        with _WRITE_LOCK:
            try:
                used, upload_count, entry_count = _gc_voice_notes(
                    include_counts=True
                )
            except _VoiceStorageError as exc:
                return self._send_json(409, {"error": str(exc)})
            try:
                existing = os.lstat(path)
            except FileNotFoundError:
                existing = None
            except OSError:
                return self._send_json(409, {"error": "voice-note path is unsafe"})
            if existing is not None and (
                not stat.S_ISREG(existing.st_mode) or existing.st_size != length
            ):
                self.close_connection = True
                return self._send_json(409, {
                    "error": "voice-note id already exists with different bytes"
                })
            if existing is None and used + length > _MAX_VOICE_DIRECTORY_BYTES:
                self.close_connection = True
                return self._send_json(507, {
                    "error": "voice-note storage exceeds the 100 MB session limit"
                })
            if existing is None and upload_count >= _MAX_VOICE_FILES:
                self.close_connection = True
                return self._send_json(507, {
                    "error": "voice-note storage exceeds the {}-file session limit".format(
                        _MAX_VOICE_FILES
                    )
                })
            if existing is None and entry_count + 2 > _MAX_VOICE_SCAN_ENTRIES:
                self.close_connection = True
                return self._send_json(507, {
                    "error": "voice-note storage cannot reserve safe temporary entries"
                })
        body = self.rfile.read(length)
        if len(body) != length:
            return self._send_json(400, {"error": "voice note upload was incomplete"})
        created = False
        with _WRITE_LOCK:
            try:
                used, upload_count, entry_count = _gc_voice_notes(
                    include_counts=True
                )
                try:
                    existing = os.lstat(path)
                except FileNotFoundError:
                    existing = None
                if existing is not None:
                    if not _safe_file_equals(path, body):
                        return self._send_json(409, {
                            "error": "voice-note id already exists with different bytes"
                        })
                elif used + length > _MAX_VOICE_DIRECTORY_BYTES:
                    return self._send_json(507, {
                        "error": "voice-note storage exceeds the 100 MB session limit"
                    })
                elif upload_count >= _MAX_VOICE_FILES:
                    return self._send_json(507, {
                        "error": "voice-note storage exceeds the {}-file session limit".format(
                            _MAX_VOICE_FILES
                        )
                    })
                elif entry_count + 2 > _MAX_VOICE_SCAN_ENTRIES:
                    return self._send_json(507, {
                        "error": "voice-note storage cannot reserve safe temporary entries"
                    })
                else:
                    fd, tmp = tempfile.mkstemp(
                        dir=directory, prefix="." + filename + ".", suffix=".tmp"
                    )
                    try:
                        with os.fdopen(fd, "wb") as handle:
                            written = handle.write(body)
                            if written != len(body):
                                raise OSError("voice-note temporary write was incomplete")
                            handle.flush()
                            os.fsync(handle.fileno())
                        os.link(tmp, path, follow_symlinks=False)
                        created = True
                    finally:
                        try:
                            os.unlink(tmp)
                        except OSError:
                            pass
            except _VoiceStorageError as exc:
                return self._send_json(409, {"error": str(exc)})
            except FileExistsError:
                try:
                    if not _safe_file_equals(path, body):
                        return self._send_json(409, {
                            "error": "voice-note id already exists with different bytes"
                        })
                except OSError:
                    return self._send_json(409, {"error": "voice-note path is unsafe"})
            except OSError as exc:
                return self._send_json(507, {
                    "error": "voice note could not be stored: {}".format(exc)
                })
        relative = os.path.relpath(path, GIT_ROOT).replace(os.sep, "/")
        return self._send_json(201 if created else 200, {
            "ok": True,
            "idempotent": not created,
            "voiceNote": {"path": relative, "mimeType": content_type, "bytes": length},
        })

    def _wk_voice_note_delete(self):
        incoming = self._read_json_body()
        if incoming is None:
            return
        relative = incoming.get("path")
        if not isinstance(relative, str):
            return self._send_json(400, {"error": "voice-note path is required"})
        directory = _voice_notes_directory()
        if directory is None:
            return self._send_json(409, {"error": "voice-note directory is unsafe"})
        candidate = os.path.abspath(os.path.join(GIT_ROOT, relative))
        stem, extension = os.path.splitext(os.path.basename(candidate))
        if (
            os.path.dirname(candidate) != directory
            or not _VOICE_ID.fullmatch(stem)
            or extension not in set(_VOICE_TYPES.values())
        ):
            return self._send_json(400, {"error": "invalid voice-note path"})
        with _WRITE_LOCK:
            for name in _DATA_FILES:
                if _references_voice_path(_load_data(name), relative):
                    return self._send_json(409, {
                        "error": "voice note is referenced by the active protocol round"
                    })
            try:
                candidate_stat = os.lstat(candidate)
            except FileNotFoundError:
                candidate_stat = None
            if candidate_stat is not None:
                if not stat.S_ISREG(candidate_stat.st_mode):
                    return self._send_json(409, {"error": "voice-note path is unsafe"})
                try:
                    os.unlink(candidate)
                except FileNotFoundError:
                    pass
        return self._send_json(200, {"ok": True})

    def _read_json_body(self):
        """Read+parse the POST body. Returns the object, or None after having
        already sent the error response (503/411/400/413)."""
        if FEEDBACK_DIR is None:
            self._send_json(503, {"error": "not a git repository - feedback loop disabled"})
            return None
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._send_json(415, {"error": "Content-Type must be application/json"})
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
        if length < 0:
            self._send_json(400, {"error": "bad Content-Length"})
            return None
        if length > _MAX_BODY:
            self._send_json(413, {"error": "body exceeds 2 MB"})
            return None
        raw = self.rfile.read(length)
        if len(raw) != length:
            self._send_json(400, {"error": "request body was incomplete"})
            return None
        try:
            obj = json.loads(
                raw.decode("utf-8"), parse_constant=_reject_json_constant
            )
            _reject_json_surrogates(obj)
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
        if incoming.get("version") != 1 or incoming.get("kind") != "feedback":
            return self._send_json(400, {"error": "expected a version-1 feedback batch"})
        batch_id = incoming.get("batchId")
        if not isinstance(batch_id, str) or not _BATCH_ID_RE.fullmatch(batch_id):
            return self._send_json(400, {"error": "feedback batchId is invalid"})
        points = incoming.get("points")
        if not isinstance(points, list) or not points:
            return self._send_json(400, {"error": "batch has no points"})
        if not _valid_point_objects(points):
            return self._send_json(400, {
                "error": "feedback point ids must be unique safe tokens up to 128 characters"
            })
        if not _valid_round(incoming.get("round")):
            return self._send_json(400, {"error": "feedback round must be a positive integer"})
        schema_error = _feedback_schema_error(incoming)
        if schema_error:
            return self._send_json(400, {"error": schema_error})
        with _WRITE_LOCK:
            existing = _load_data("feedback.json")
            review = _load_data("review.json")
            verdicts = _load_data("verdicts.json")
            if existing is None:
                if review is not None or verdicts is not None:
                    return self._send_json(
                        409, {"error": "the feedback inbox is transitioning"}
                    )
                if incoming.get("round") != 1:
                    return self._send_json(400, {"error": "a fresh feedback batch must start at round 1"})
                # Fresh inbox after archive. A delayed network retry must not
                # resurrect a batch that the agent has already completed.
                if _history_contains_batch(batch_id):
                    return self._send_json(
                        409, {"error": "feedback batchId was already completed"}
                    )
                if any(not _valid_voice_note(point.get("voiceNote")) for point in points):
                    return self._send_json(400, {
                        "error": "feedback voiceNote must reference an existing safe inbox upload"
                    })
                fresh = dict(incoming)
                fresh["points"] = _dedupe_points([], points)
                fresh["pages"] = list(incoming["pages"])
                _atomic_write("feedback.json", fresh)
                return self._send_json(200, {"ok": True, "batchId": batch_id})

            existing_batch_id = existing.get("batchId")
            existing_round = existing.get("round", 1)
            existing_schema_error = _feedback_schema_error(existing)
            if existing_schema_error:
                return self._send_json(409, {
                    "error": "the live feedback batch is invalid: {}".format(
                        existing_schema_error
                    )
                })
            if not isinstance(existing_batch_id, str) or not _BATCH_ID_RE.fullmatch(existing_batch_id):
                return self._send_json(409, {"error": "the live feedback batch is invalid"})
            if not _valid_round(existing_round):
                return self._send_json(409, {"error": "the live feedback round is invalid"})
            if not _valid_point_objects(existing.get("points")):
                return self._send_json(409, {"error": "the live feedback points are invalid"})
            if batch_id != existing_batch_id:
                return self._send_json(409, {
                    "error": "batchId mismatch: the live feedback batch is {}".format(
                        existing_batch_id
                    )
                })
            if incoming.get("round") != existing_round:
                return self._send_json(409, {
                    "error": "round mismatch: the live feedback round is {}".format(
                        existing_round
                    )
                })
            old_points = list(existing.get("points"))
            existing_by_id = {point["id"]: point for point in old_points}
            for point in points:
                persisted = existing_by_id.get(point["id"])
                if persisted is None:
                    continue
                comparable = dict(point)
                # The server owns final display numbering when concurrent tabs
                # submit colliding numbers. All other fields remain immutable.
                comparable["number"] = persisted.get("number")
                if comparable != persisted:
                    return self._send_json(409, {
                        "error": "feedback point id was reused with a different payload"
                    })
            novel = _dedupe_points(old_points, points)
            if not novel:
                return self._send_json(200, {"ok": True, "batchId": existing_batch_id})
            if any(not _valid_voice_note(point.get("voiceNote")) for point in novel):
                return self._send_json(400, {
                    "error": "feedback voiceNote must reference an existing safe inbox upload"
                })

            active_review = review is not None and review.get("batchId") == existing_batch_id
            if review is not None and not active_review:
                return self._send_json(409, {"error": "review.json does not match feedback.json"})
            if review is not None:
                review_error = _review_schema_error(review, existing)
                if review_error:
                    return self._send_json(409, {
                        "error": "the live review is invalid: {}".format(review_error)
                    })
            expected_round = review.get("round") if active_review else existing_round
            if not _valid_round(expected_round) or expected_round != existing_round:
                return self._send_json(409, {"error": "the live feedback and review rounds do not match"})
            if verdicts is not None and not (
                verdicts.get("kind") == "feedback_update"
                and verdicts.get("batchId") == existing_batch_id
                and verdicts.get("round") == expected_round
            ):
                return self._send_json(
                    409,
                    {"error": "the current review transition must finish before more feedback is sent"},
                )

            nums = [
                point.get("number") for point in old_points
                if isinstance(point, dict)
                and isinstance(point.get("number"), int)
                and not isinstance(point.get("number"), bool)
            ]
            base = max(nums) if nums else len(old_points)
            for point in novel:
                base += 1
                point["number"] = base
                old_points.append(point)
            merged = dict(existing)
            merged["points"] = old_points
            pages = []
            for page in list(existing.get("pages") or []) + list(incoming.get("pages") or []):
                if page not in pages:
                    pages.append(page)
            merged["pages"] = pages
            merged["updatedAt"] = incoming.get("updatedAt") or _now_iso()

            previous_ids = []
            if verdicts is not None:
                for point_id in verdicts.get("addedPointIds") or []:
                    if (
                        isinstance(point_id, str)
                        and _POINT_ID_RE.fullmatch(point_id)
                        and point_id not in previous_ids
                    ):
                        previous_ids.append(point_id)
            added_ids = [point["id"] for point in novel]
            combined_ids = previous_ids + [
                point_id for point_id in added_ids if point_id not in previous_ids
            ]
            marker = {
                "version": 1,
                "kind": "feedback_update",
                "batchId": existing_batch_id,
                "round": expected_round,
                "sentAt": _now_iso(),
                "addedPointIds": combined_ids,
            }
            try:
                _atomic_write("feedback.json", merged)
                _atomic_write("verdicts.json", marker)
            except Exception:
                _atomic_write("feedback.json", existing)
                raise
            batch_id = existing_batch_id
        return self._send_json(200, {"ok": True, "batchId": batch_id})

    def _wk_verdicts(self):
        incoming = self._read_json_body()
        if incoming is None:
            return
        if incoming.get("version") != 1 or incoming.get("kind") != "verdicts":
            return self._send_json(400, {"error": "expected version-1 verdicts"})
        incoming_batch_id = incoming.get("batchId")
        if (
            not isinstance(incoming_batch_id, str)
            or not _BATCH_ID_RE.fullmatch(incoming_batch_id)
        ):
            return self._send_json(400, {"error": "verdict batchId is invalid"})
        if not _valid_round(incoming.get("round")):
            return self._send_json(400, {"error": "verdict round must be a positive integer"})
        verdict_items = incoming.get("verdicts")
        if not isinstance(verdict_items, list):
            return self._send_json(400, {"error": "verdicts must be a list"})
        verdict_ids = []
        for item in verdict_items:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("pointId"), str)
                or not _POINT_ID_RE.fullmatch(item.get("pointId"))
                or item.get("verdict") not in ("accept", "delete", "redo")
            ):
                return self._send_json(400, {
                    "error": "each verdict needs a safe pointId and accept, delete, or redo"
                })
            verdict = item["verdict"]
            if "redoText" in item and (
                not isinstance(item["redoText"], str)
                or len(item["redoText"]) > _MAX_REDO_TEXT
            ):
                return self._send_json(400, {
                    "error": "redoText must be a string no longer than 10000 characters"
                })
            if verdict != "redo" and (
                "redoText" in item or "redoVoiceNote" in item
            ):
                return self._send_json(400, {
                    "error": "redo fields are allowed only on a redo verdict"
                })
            if verdict != "accept" and "chosenLetter" in item:
                return self._send_json(400, {
                    "error": "chosenLetter is allowed only on an accept verdict"
                })
            verdict_ids.append(item["pointId"])
        if len(verdict_ids) != len(set(verdict_ids)):
            return self._send_json(400, {"error": "verdict pointIds must be unique"})
        with _WRITE_LOCK:
            feedback = _load_data("feedback.json")
            review = _load_data("review.json")
            if feedback is None or review is None:
                return self._send_json(409, {"error": "no review round active"})
            if feedback.get("batchId") != review.get("batchId"):
                return self._send_json(409, {"error": "feedback.json and review.json do not match"})
            if feedback.get("round", 1) != review.get("round"):
                return self._send_json(409, {"error": "feedback.json and review.json rounds do not match"})
            review_error = _review_schema_error(review, feedback)
            if review_error:
                return self._send_json(409, {
                    "error": "the active review is invalid: {}".format(review_error)
                })
            review_points = review.get("points")
            if not _valid_point_objects(review_points):
                return self._send_json(409, {"error": "the active review points are invalid"})
            review_ids = _point_id_list(review_points)
            if set(verdict_ids) != set(review_ids) or len(verdict_ids) != len(review_ids):
                return self._send_json(400, {
                    "error": "verdicts must cover exactly the active review point ids"
                })
            verdict_error = _persisted_verdict_error(incoming, review)
            if verdict_error:
                return self._send_json(400, {"error": verdict_error})
            review_by_id = {point["id"]: point for point in review_points}
            for item in verdict_items:
                if item["verdict"] != "accept":
                    continue
                review_point = review_by_id[item["pointId"]]
                if review_point.get("handled") == "abc":
                    abc = review_point.get("abc")
                    letters = abc.get("letters") if isinstance(abc, dict) else None
                    if (
                        not isinstance(letters, str)
                        or not 1 <= len(letters) <= 10
                        or len(set(letters)) != len(letters)
                    ):
                        return self._send_json(409, {
                            "error": "the active ABC review metadata is invalid"
                        })
                    chosen = item.get("chosenLetter")
                    if not isinstance(chosen, str) or len(chosen) != 1 or chosen not in letters:
                        return self._send_json(400, {
                            "error": "ABC accepts require one allowed chosenLetter"
                        })
                elif "chosenLetter" in item:
                    return self._send_json(400, {
                        "error": "non-ABC accepts cannot include chosenLetter"
                    })
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
            # verdicts whose pointIds don't match the current round - accepting
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
            pending = _load_data("verdicts.json")
            if pending is not None:
                if pending.get("kind") == "feedback_update":
                    return self._send_json(
                        409,
                        {"error": "new feedback is waiting for the agent before verdicts can be submitted"},
                    )
                if pending == incoming:
                    return self._send_json(200, {"ok": True})
                return self._send_json(
                    409, {"error": "verdicts were already submitted for this review round"}
                )
            if any(
                item["verdict"] == "redo"
                and not _valid_voice_note(item.get("redoVoiceNote"))
                for item in verdict_items
            ):
                return self._send_json(400, {
                    "error": "redoVoiceNote must reference an existing safe inbox upload"
                })
            _atomic_write("verdicts.json", incoming)
        return self._send_json(200, {"ok": True})

    def _wk_transition(self):
        incoming = self._read_json_body()
        if incoming is None:
            return
        mode = incoming.get("mode")
        batch_id = incoming.get("batchId")
        round_number = incoming.get("round")
        if incoming.get("version") != 1 or mode not in ("feedback-update", "redo", "complete"):
            return self._send_json(400, {"error": "transition mode or version is invalid"})
        if not isinstance(batch_id, str) or not _BATCH_ID_RE.fullmatch(batch_id):
            return self._send_json(400, {"error": "transition batchId is invalid"})
        if not _valid_round(round_number):
            return self._send_json(400, {"error": "transition round must be a positive integer"})

        with _WRITE_LOCK:
            try:
                _gc_voice_notes()
            except _VoiceStorageError as exc:
                return self._send_json(409, {
                    "error": "voice_storage_unsafe", "reason": str(exc)
                })
            receipt_conflict = None
            prior = None
            try:
                prior = _verified_transition_receipt(
                    batch_id, round_number, mode, allow_interim=False
                )
            except _TransitionConflict as exc:
                if mode != "feedback-update":
                    return self._send_json(409, {"error": "transition_conflict", "reason": str(exc)})
                receipt_conflict = exc
            if prior is not None:
                return self._send_json(
                    503 if prior.get("voiceCleanupPending") else 200, prior
                )

            feedback = _load_data("feedback.json")
            review = _load_data("review.json")
            verdicts = _load_data("verdicts.json")
            if mode == "feedback-update" and verdicts is None:
                try:
                    prior = _verified_transition_receipt(
                        batch_id, round_number, mode, allow_interim=True
                    )
                except _TransitionConflict as exc:
                    return self._send_json(409, {"error": "transition_conflict", "reason": str(exc)})
                if prior is not None:
                    return self._send_json(
                        503 if prior.get("voiceCleanupPending") else 200, prior
                    )
            if receipt_conflict is not None:
                return self._send_json(409, {
                    "error": "transition_conflict", "reason": str(receipt_conflict)
                })
            if feedback is None or review is None or verdicts is None:
                return self._send_json(409, {"error": "transition_conflict", "reason": "live F/R/V are required"})
            if not (
                feedback.get("batchId") == batch_id
                and review.get("batchId") == batch_id
                and verdicts.get("batchId") == batch_id
                and feedback.get("round", 1) == round_number
                and review.get("round") == round_number
                and verdicts.get("round") == round_number
            ):
                return self._send_json(409, {"error": "transition_conflict", "reason": "live batch or round does not match"})
            if not _valid_point_objects(feedback.get("points")):
                return self._send_json(409, {
                    "error": "transition_conflict", "reason": "live feedback points are invalid"
                })
            if not _valid_point_objects(review.get("points")):
                return self._send_json(409, {
                    "error": "transition_conflict", "reason": "live review points are invalid"
                })
            review_error = _review_schema_error(review, feedback)
            if review_error:
                return self._send_json(409, {
                    "error": "transition_conflict",
                    "reason": "live review is invalid: {}".format(review_error),
                })

            redo_ids = set()
            if mode == "feedback-update":
                marker_error = _feedback_update_marker_error(
                    verdicts, batch_id, round_number
                )
                if marker_error:
                    return self._send_json(409, {
                        "error": "transition_conflict", "reason": marker_error
                    })
            else:
                if verdicts.get("kind") != "verdicts":
                    return self._send_json(409, {"error": "transition_conflict", "reason": "user verdicts are required"})
                verdict_error = _persisted_verdict_error(verdicts, review)
                if verdict_error:
                    return self._send_json(409, {
                        "error": "transition_conflict",
                        "reason": "persisted verdicts are invalid: {}".format(verdict_error),
                    })
                verdict_items = verdicts.get("verdicts")
                if not isinstance(verdict_items, list):
                    return self._send_json(409, {"error": "transition_conflict", "reason": "verdict list is invalid"})
                has_redo = any(
                    isinstance(item, dict) and item.get("verdict") == "redo"
                    for item in verdict_items
                )
                redo_ids = {
                    item.get("pointId") for item in verdict_items
                    if isinstance(item, dict)
                    and item.get("verdict") == "redo"
                    and isinstance(item.get("pointId"), str)
                }
                if mode == "redo" and not has_redo:
                    return self._send_json(409, {"error": "transition_conflict", "reason": "redo mode needs a redo verdict"})
                if mode == "complete" and has_redo:
                    return self._send_json(409, {"error": "transition_conflict", "reason": "complete mode cannot contain redo verdicts"})

            current_review_ids = set(_point_id_list(review.get("points")))
            try:
                archived_review_ids = _archived_review_point_ids(batch_id)
            except _HistoryLimit as exc:
                return self._send_json(507, {
                    "error": "history_capacity_exceeded",
                    "reason": str(exc),
                })
            seen_ids = current_review_ids | archived_review_ids
            missing_points = []
            missing_ids = set()
            for point in feedback.get("points") or []:
                if not isinstance(point, dict):
                    return self._send_json(409, {"error": "transition_conflict", "reason": "feedback contains a malformed point"})
                point_id = point.get("id")
                if not isinstance(point_id, str) or not point_id:
                    return self._send_json(409, {"error": "transition_conflict", "reason": "feedback contains a malformed point id"})
                if point_id not in seen_ids and point_id not in missing_ids:
                    missing_ids.add(point_id)
                    missing_points.append(point)

            if mode in ("redo", "complete") and missing_points:
                return self._send_json(409, {
                    "error": "transition_conflict",
                    "reason": "feedback points are missing from review history",
                    "missingPoints": missing_points,
                })

            next_review = incoming.get("nextReview")
            if mode in ("feedback-update", "redo") and (mode == "redo" or missing_points):
                if not isinstance(next_review, dict):
                    return self._send_json(409, {
                        "error": "transition_conflict",
                        "reason": "nextReview is required",
                        "missingPoints": missing_points,
                    })
                next_ids = _point_id_list(next_review.get("points"))
                next_id_set = set(next_ids)
                required_ids = (
                    current_review_ids | missing_ids
                    if mode == "feedback-update"
                    else redo_ids
                )
                if (
                    next_review.get("version") != 1
                    or next_review.get("kind") != "review"
                    or next_review.get("batchId") != batch_id
                    or next_review.get("round") != round_number + 1
                    or next_review.get("beforeRef") != review.get("beforeRef")
                    or not _valid_point_objects(next_review.get("points"))
                    or _review_schema_error(next_review, feedback)
                    or next_id_set != required_ids
                    or len(next_ids) != len(required_ids)
                ):
                    return self._send_json(409, {
                        "error": "transition_conflict",
                        "reason": "nextReview does not cover the current transition",
                        "missingPoints": missing_points,
                        "requiredPointIds": sorted(required_ids),
                    })

            response = {
                "ok": True,
                "mode": mode,
                "batchId": batch_id,
                "round": round_number,
                "idempotent": False,
            }
            copies = []
            moves = []
            next_feedback = None
            installed_review = None
            receipt_name = "transition.json"
            archive_names = {}

            if mode == "feedback-update" and not missing_points:
                nonce = secrets.token_hex(8)
                verdict_target = "verdicts-feedback-update-{}.json".format(nonce)
                receipt_name = "transition-feedback-update-{}.json".format(nonce)
                moves = [("verdicts.json", verdict_target)]
                archive_names = {verdict_target: _sha256_file(_data_path("verdicts.json"))}
                response.update({
                    "phase": "reviewing",
                    "nextRound": round_number,
                    "reviewArchived": False,
                    "missingPoints": [],
                })
            elif mode == "feedback-update":
                moves = [("review.json", "review.json"), ("verdicts.json", "verdicts.json")]
                archive_names = {
                    "review.json": _sha256_file(_data_path("review.json")),
                    "verdicts.json": _sha256_file(_data_path("verdicts.json")),
                }
                next_feedback = dict(feedback)
                next_feedback["round"] = round_number + 1
                installed_review = next_review
                response.update({
                    "phase": "reviewing",
                    "nextRound": round_number + 1,
                    "reviewArchived": True,
                    "missingPoints": missing_points,
                })
            elif mode == "redo":
                copies = [("feedback.json", "feedback.json")]
                moves = [("review.json", "review.json"), ("verdicts.json", "verdicts.json")]
                archive_names = {
                    "feedback.json": _sha256_file(_data_path("feedback.json")),
                    "review.json": _sha256_file(_data_path("review.json")),
                    "verdicts.json": _sha256_file(_data_path("verdicts.json")),
                }
                next_feedback = dict(feedback)
                next_feedback["round"] = round_number + 1
                installed_review = next_review
                response.update({"phase": "reviewing", "nextRound": round_number + 1})
            else:
                moves = [
                    ("feedback.json", "feedback.json"),
                    ("review.json", "review.json"),
                    ("verdicts.json", "verdicts.json"),
                ]
                archive_names = {
                    "feedback.json": _sha256_file(_data_path("feedback.json")),
                    "review.json": _sha256_file(_data_path("review.json")),
                    "verdicts.json": _sha256_file(_data_path("verdicts.json")),
                }
                response.update({"phase": "collecting"})

            voice_note_paths = _collect_voice_paths(feedback, review, verdicts)
            receipt = {
                "version": 1,
                "mode": mode,
                "batchId": batch_id,
                "round": round_number,
                "files": archive_names,
                "response": response,
                "voiceNotePaths": voice_note_paths,
            }
            try:
                archived = _archive_transaction(
                    batch_id,
                    round_number,
                    moves,
                    copies=copies,
                    feedback=next_feedback,
                    review=installed_review,
                    receipt_name=receipt_name,
                    receipt=receipt,
                )
            except _HistoryLimit as exc:
                return self._send_json(507, {
                    "error": "history_capacity_exceeded",
                    "reason": str(exc),
                    "recoveryRequired": False,
                })
            except OSError as exc:
                code = 409 if "already exists" in str(exc) else 500
                return self._send_json(code, {
                    "error": "transition_conflict" if code == 409 else "transition_failed",
                    "reason": str(exc),
                    "recoveryRequired": False,
                })
            response["archive"] = archived
            cleanup_pending = _cleanup_voice_notes(voice_note_paths)
            if cleanup_pending:
                response.update({
                    "error": "voice_cleanup_pending",
                    "transitionDurable": True,
                    "voiceCleanupPending": cleanup_pending,
                })
                return self._send_json(503, response)
            return self._send_json(200, response)


if __name__ == "__main__":
    os.chdir(ROOT)
    if FEEDBACK_DIR:
        with _WRITE_LOCK:
            try:
                _gc_voice_notes()
            except _VoiceStorageError as exc:
                sys.exit(
                    "preview-server.py: voice-note storage is unsafe: {}".format(exc)
                )
    # flush=True: agents run this in the background with stdout redirected to
    # a log and then read the log to confirm boot - without the flush the
    # block-buffered messages only appear at exit.
    print(
        "serving {} on :{} with tab color {} ({})".format(ROOT, PORT, COLOR, SLUG),
        flush=True,
    )
    if FEEDBACK_DIR:
        print("feedback inbox: {}".format(FEEDBACK_DIR), flush=True)
    else:
        print(
            "WARNING: {} is not inside a git repository - static serving only "
            "(feedback + before-mode endpoints will answer 503).".format(ROOT),
            flush=True,
        )
    # Bind loopback ONLY. The browser, overlay, and agent are all on this
    # machine, so nothing needs remote access - and binding all interfaces
    # would expose the entire served working tree AND the /__wk/feedback
    # channel (whose `text` fields the agent applies and commits) to anyone on
    # the LAN, i.e. unauthenticated remote prompt-injection into an agent with
    # write access. bind_host is configurable for deliberate LAN use, but SETUP
    # documents that 0.0.0.0 forfeits both protections.
    HOST = BIND_HOST
    # ThreadingHTTPServer: a browser tab that holds a connection open (WebGL
    # pages keep-alive) must not block other requests (e.g. headless QA) - the
    # single-threaded HTTPServer would hang the whole server on one stuck client.
    server = PreviewHTTPServer((HOST, PORT), Handler)
    if CLAIM is not None:
        try:
            _register_preview_instance(
                PORT, CLAIM[1], os.path.abspath(CLAIM[0]), SLUG, CLAIM[2],
                INSTANCE_TOKEN, os.getpid(),
            )
        except _RuntimeRegistryError as exc:
            server.server_close()
            sys.exit(
                "preview-server.py: could not publish the preview server identity: {}"
                .format(exc)
            )
    heartbeat_stop = threading.Event()
    heartbeat_thread = None

    def stop_on_signal(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, stop_on_signal)
    signal.signal(signal.SIGTERM, stop_on_signal)
    try:
        _publish_transition_token()
        if CLAIM is not None:
            heartbeat_thread = threading.Thread(
                target=_claim_heartbeat,
                args=(heartbeat_stop,),
                name="webkit-claim-heartbeat",
                daemon=True,
            )
            heartbeat_thread.start()
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        heartbeat_stop.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=5)
        _remove_transition_token()
        server.server_close()
