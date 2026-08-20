#!/usr/bin/env python3
"""Core services for the AWESOME WEBKIT local control center.

The module intentionally uses only the Python standard library.  It owns the
filesystem and process boundary; the browser UI only calls the narrow methods
exposed by ``ControlCenter`` through ``server.py``.
"""

import json
import base64
import hashlib
import html
import http.client
import os
import platform
import queue
import re
import signal
import stat
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


_RUNTIME_SCRIPTS = Path(__file__).resolve().parents[1] / "webkit" / "scripts"
if str(_RUNTIME_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_RUNTIME_SCRIPTS))
from runtime_registry import (  # noqa: E402
    RegistryError as RuntimeRegistryError,
    ReservationBusy as RuntimeReservationBusy,
    claim_color as claim_runtime_color,
    color_session_status,
    inspect_reservation as inspect_runtime_reservation,
    read_color_lock as read_runtime_color_lock,
    release_color as release_runtime_color,
    runtime_registry_path,
    touch_reservation as touch_runtime_reservation,
)


WEBKIT_POINTER = """## Webkit

This project uses Ethan's AWESOME WEBKIT for design iteration. At the start of
every website-working session, and whenever the user asks to launch, open,
preview, show, or test the website in a browser, read and follow
`webkit/SETUP.md` exactly. Use its claimed color, stamping preview server, and
`webkit/scripts/open-preview.sh`; never substitute a generic HTTP server, a
manually opened browser tab, the Codex/ChatGPT in-app browser, a Claude preview
pane, an IDE webview, or another embedded browser. The configured external
desktop browser is the only preview surface unless the user explicitly
overrides it. If the Webkit session is already active, reuse it instead of
claiming again. Read `webkit/LOOP.md` for browser feedback rounds.

"""


CONTROL_CENTER_POINTER = """## Webkit Control Center

When `WK_CONTROL_CENTER=1`, the local Control Center already owns the color,
worktree, preview process, browser tab, and waiting. Read
`webkit/CONTROL-CENTER.md`, process one finite feedback or chat transition, and
exit; never merge or discard the controller-owned branch yourself.
"""


CONTROL_CENTER_PROMPT = """You are the {emoji} ({color}) AWESOME WEBKIT background agent.
You are already running inside this color's isolated Git worktree. The local
Control Center owns the preview process, color lock, browser tab, and waiting.

Read webkit/CONTROL-CENTER.md and webkit/LOOP.md, then process exactly the
current Webkit state transition in .webkit/feedback/{color}/. If feedback.json
is awaiting the agent, apply the points, commit one point at a time, publish an
atomic review.json, and then exit. If verdicts.json is present, process the
verdicts, archive the round correctly, and then exit. Do not run wait-for-file,
do not merge to any target branch, do not discard the worktree, and do not open another
agent app. The controller will invoke you again for the next transition.
"""


DEFAULT_SETTINGS = {
    "dictationMode": "speech",
    "interactionMode": "browse-default",
    "toggleHotkey": "KeyC",
    "dictateHotkey": "KeyV",
}

HOTKEY_CODE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,31}$")
MODIFIER_HOTKEYS = {
    "AltLeft", "AltRight", "ControlLeft", "ControlRight",
    "MetaLeft", "MetaRight", "ShiftLeft", "ShiftRight", "Escape",
}

COLOR_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
GIT_REMOTE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
PROVIDER_THREAD_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
BIDI_CONTROL_CLASSES = {"LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI"}
MAX_PROVIDER_PROMPT_BYTES = 16 * 1024
MAX_PROVIDER_STREAM_LINE_CHARS = 64 * 1024
MAX_PREVIEW_LOG_CHARS = 64 * 1024
MAX_PREVIEW_LOG_TAIL_CHARS = 8000
MAX_PROJECT_BRIEF_CHARS = 20 * 1000
MAX_SEED_NOTES_CHARS = 12 * 1000
MAX_EVENT_TEXT_BYTES = 32 * 1024
MAX_EVENT_BATCH = 100
MAX_EVENT_LOG_BYTES = 4 * 1024 * 1024
EVENT_LOG_RETAIN_BYTES = 3 * 1024 * 1024
MAX_EVENT_LINE_BYTES = 128 * 1024
EVENT_CURSOR = re.compile(r"^([0-9a-f]{32}):(\d+)$")
MAX_AGENT_RESULT_BYTES = 16 * 1024
MAX_SEED_MANIFEST_BYTES = 256 * 1024
MAX_WEBKIT_CONFIG_BYTES = 256 * 1024
MAX_OUTGOING_CONFIG_CHANGES = 512
MAX_OUTGOING_CONFIG_TOTAL_BYTES = 16 * 1024 * 1024
MAX_STATE_BYTES = 8 * 1024 * 1024
MAX_RETAINED_TERMINAL_SESSIONS = 50
MAX_CHAT_ATTACHMENTS = 20
MAX_CHAT_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_CHAT_ATTACHMENTS_TOTAL_BYTES = 20 * 1024 * 1024
MAX_SESSION_CHAT_ATTACHMENT_BYTES = 100 * 1024 * 1024
MAX_SESSION_CHAT_ATTACHMENT_FILES = 2000
MAX_SESSION_CHAT_ATTACHMENT_DIRECTORIES = 512
MAX_PROJECT_ASSETS = 500
MAX_PROJECT_ASSET_BYTES = 15 * 1024 * 1024
MAX_PROJECT_ASSETS_TOTAL_BYTES = 20 * 1024 * 1024
MAX_SECRET_SCAN_BYTES = 2 * 1024 * 1024
MAX_STAGED_PATH_LIST_BYTES = 16 * 1024 * 1024
MAX_STAGED_SECRET_SCAN_FILES = 20000
MAX_HISTORY_SECRET_SCAN_COMMITS = 10000
MAX_HISTORY_SECRET_SCAN_TOTAL_BYTES = 256 * 1024 * 1024
MAX_MUTABLE_SUPPORT_BYTES = 8 * 1024 * 1024
MINIMUM_GIT_VERSION = (2, 30, 0)
MIN_CLAIM_GRACE_SECONDS = 30
MAX_CLAIM_GRACE_SECONDS = 86400
TERMINAL_SESSION_STATUSES = {"merged", "discarded"}
STANDARD_PRIVATE_IGNORES = (
    ".webkit/", "__pycache__/", "*.pyc", ".DS_Store", "node_modules/",
    ".venv/", "venv/", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/",
)
WINDOWS_RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *("com{}".format(index) for index in range(1, 10)),
    *("lpt{}".format(index) for index in range(1, 10)),
}
SECRET_CONTENT_PATTERNS = (
    (re.compile(br"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"), "private key material"),
    (re.compile(br"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])"), "an AWS access key"),
    (re.compile(br"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{30,}(?![A-Za-z0-9])"), "a GitHub token"),
    (re.compile(br"(?<![A-Za-z0-9])github_pat_[A-Za-z0-9_]{30,}(?![A-Za-z0-9])"), "a GitHub token"),
    (re.compile(br"(?<![A-Za-z0-9])sk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])"), "an API key"),
    (re.compile(br"(?<![A-Za-z0-9])AIza[0-9A-Za-z_-]{35}(?![A-Za-z0-9_-])"), "a Google API key"),
    (re.compile(br"(?<![A-Za-z0-9])xox[baprs]-[A-Za-z0-9-]{20,}(?![A-Za-z0-9-])"), "a Slack token"),
)
SECRET_SCAN_SUFFIXES = {
    "", ".c", ".cc", ".cfg", ".cjs", ".clj", ".cljs", ".conf", ".cpp",
    ".cs", ".css", ".dart", ".edn", ".env", ".erl", ".ex", ".exs", ".fs",
    ".fsx", ".go", ".gradle", ".gql", ".graphql", ".groovy", ".h", ".hpp",
    ".hrl", ".htm", ".html", ".ini", ".java", ".js", ".json", ".jsx", ".kt", ".kts",
    ".less", ".lock", ".lua", ".m", ".map", ".md", ".mdx", ".mjs", ".mm", ".mts", ".php",
    ".pl", ".pm", ".properties", ".proto", ".py", ".r", ".rb", ".rs", ".sass",
    ".scss", ".sh", ".sql", ".svelte", ".svg", ".swift", ".tf", ".tfvars",
    ".astro", ".coffee", ".cts", ".toml", ".ts", ".tsx", ".txt", ".vue", ".xhtml",
    ".xml", ".yaml", ".yml",
}


def valid_hotkey(value):
    return (
        isinstance(value, str)
        and HOTKEY_CODE.fullmatch(value) is not None
        and value not in MODIFIER_HOTKEYS
    )


def parsed_git_version(value):
    match = re.search(r"\bgit version\s+(\d+)\.(\d+)(?:\.(\d+))?", str(value or ""), re.I)
    if not match:
        return None
    return tuple(int(part or 0) for part in match.groups())


def valid_color_label(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 32:
        return False
    return all(
        not char.isspace()
        and char not in "<>&\"'`"
        and unicodedata.category(char) != "Cc"
        and unicodedata.bidirectional(char) not in BIDI_CONTROL_CLASSES
        for char in value
    )


def secret_file_risk(file_name, data=None):
    lower = str(file_name or "").lower()
    suffix = Path(lower).suffix
    stem = Path(lower).stem
    if lower == ".env" or (
        lower.startswith(".env.")
        and not lower.endswith((".example", ".sample", ".template"))
    ):
        return "an environment secrets file"
    if lower.endswith((".pem", ".key")) or lower.startswith(
        ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519")
    ):
        return "a key file"
    if lower in {".npmrc", ".pypirc", ".netrc", "credentials", "credential", "token", "secrets"}:
        return "a credential file"
    if any(
        word in stem
        for word in (
            "credential", "access-token", "refresh-token", "auth-token", "service-account"
        )
    ):
        return "a credential file"
    if data is not None and suffix in SECRET_SCAN_SUFFIXES:
        sample = bytes(data[:MAX_SECRET_SCAN_BYTES])
        for pattern, description in SECRET_CONTENT_PATTERNS:
            if pattern.search(sample):
                return description
    return None


def choose_folder(initial=None, prompt="Choose a folder"):
    """Open the host OS folder picker and return an absolute path or None.

    Browsers deliberately do not reveal an absolute directory path from a
    regular file input. The Control Center is local software, so its localhost
    backend can safely ask the operating system and return only the folder the
    user explicitly selected.
    """
    candidate = Path(initial or "").expanduser()
    start = candidate if candidate.is_dir() else Path.home()
    system = platform.system()
    if system == "Darwin":
        script = """on run argv
set promptText to item 1 of argv
set startPath to POSIX file (item 2 of argv)
try
  set selectedFolder to choose folder with prompt promptText default location startPath
  return POSIX path of selectedFolder
on error number -128
  return "__WK_CANCELLED__"
end try
end run"""
        command = ["osascript", "-e", script, prompt, str(start)]
    elif system == "Windows":
        script = (
            "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$picker = New-Object System.Windows.Forms.FolderBrowserDialog; "
            "$picker.Description = $args[0]; $picker.SelectedPath = $args[1]; "
            "if ($picker.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) "
            "{ $picker.SelectedPath } else { '__WK_CANCELLED__' }"
        )
        executable = shutil.which("powershell.exe") or shutil.which("powershell")
        if not executable:
            raise ControlCenterError("The Windows folder picker is unavailable.", 501)
        command = [executable, "-NoProfile", "-Command", script, prompt, str(start)]
    else:
        zenity = shutil.which("zenity")
        kdialog = shutil.which("kdialog")
        if zenity:
            command = [
                zenity, "--file-selection", "--directory",
                "--title={}".format(prompt), "--filename={}/".format(start),
            ]
        elif kdialog:
            command = [kdialog, "--getexistingdirectory", str(start), "--title", prompt]
        else:
            raise ControlCenterError(
                "No native folder picker was found. Install Zenity or KDialog and try again.", 501
            )
    try:
        result = subprocess.run(
            command, text=True, encoding="utf-8", errors="replace",
            capture_output=True, timeout=600,
        )
    except subprocess.TimeoutExpired:
        raise ControlCenterError("The folder picker timed out. Please try again.", 408)
    except OSError as exc:
        raise ControlCenterError("Could not open the folder picker: {}".format(exc), 500)
    selected = result.stdout.strip()
    if selected == "__WK_CANCELLED__" or (result.returncode != 0 and not selected):
        return None
    if result.returncode != 0:
        raise ControlCenterError(
            "The folder picker failed: {}".format(result.stderr.strip() or "unknown error"), 500
        )
    selected_path = Path(selected).expanduser().resolve()
    if not selected_path.is_dir():
        raise ControlCenterError("The selected folder is no longer available.", 404)
    return str(selected_path)


def normalized_settings(value):
    value = value if isinstance(value, dict) else {}
    dictation_mode = value.get("dictationMode", DEFAULT_SETTINGS["dictationMode"])
    interaction_mode = value.get("interactionMode", DEFAULT_SETTINGS["interactionMode"])
    toggle_hotkey = value.get("toggleHotkey", DEFAULT_SETTINGS["toggleHotkey"])
    dictate_hotkey = value.get("dictateHotkey", DEFAULT_SETTINGS["dictateHotkey"])
    toggle_hotkey = toggle_hotkey if valid_hotkey(toggle_hotkey) else "KeyC"
    dictate_hotkey = dictate_hotkey if valid_hotkey(dictate_hotkey) else "KeyV"
    if dictate_hotkey == toggle_hotkey:
        dictate_hotkey = "KeyC" if toggle_hotkey == "KeyV" else "KeyV"
    return {
        "dictationMode": dictation_mode if dictation_mode in ("speech", "voice-note") else "speech",
        "interactionMode": interaction_mode
        if interaction_mode in ("browse-default", "draw-default")
        else "browse-default",
        "toggleHotkey": toggle_hotkey,
        "dictateHotkey": dictate_hotkey,
    }


class ControlCenterError(RuntimeError):
    def __init__(self, message, status=400, details=None):
        super().__init__(message)
        self.status = status
        self.details = details or {}


class _RecoveryCancelled(RuntimeError):
    """Internal signal that startup recovery no longer owns permission to spawn."""


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def slugify(value):
    value = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    value = value or "website"
    return "website-" + value if value in WINDOWS_RESERVED_NAMES else value


def sanitize_remote_url(value):
    """Return a display-safe Git remote URL without embedded credentials."""
    value = str(value or "").strip()
    try:
        parsed = urlsplit(value)
    except ValueError:
        parsed = None
    if parsed and parsed.scheme and parsed.hostname:
        host = parsed.hostname
        try:
            port = parsed.port
        except ValueError:
            port = None
        if port:
            host += ":{}".format(port)
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    scp = re.match(r"^(?:[^/@]+@)?([^/:]+)[:/](.+)$", value)
    if scp:
        return "ssh://{}/{}".format(scp.group(1), scp.group(2))
    return re.sub(r"^[^/@\s]+@", "", value)


def valid_git_remote_name(value):
    """Accept only option-safe, ref-safe Git remote tokens."""
    return isinstance(value, str) and GIT_REMOTE_NAME.fullmatch(value) is not None


def github_repository_identity(value):
    """Return a normalized owner/repository identity for an exact GitHub URL."""
    value = str(value or "").strip()
    if remote_hostname(value) != "github.com":
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        parsed = None
    if parsed and parsed.scheme:
        path = parsed.path
    else:
        match = re.match(r"^(?:[^/@\s]+@)?github\.com:(.+)$", value, re.I)
        path = match.group(1) if match else ""
    parts = path.strip("/").split("/")
    if len(parts) != 2:
        return None
    owner, repository = parts
    if repository.lower().endswith(".git"):
        repository = repository[:-4]
    if not owner or not repository or any(
        re.fullmatch(r"[A-Za-z0-9_.-]+", part) is None
        for part in (owner, repository)
    ):
        return None
    return "{}/{}".format(owner.lower(), repository.lower())


def safe_pinned_remote_url(value):
    """Accept a credential-free GitHub URL that is safe to pin in argv."""
    value = str(value or "").strip()
    if not value or any(character.isspace() or ord(character) < 32 for character in value):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme:
        if parsed.query or parsed.fragment or parsed.password:
            return None
        scheme = parsed.scheme.lower()
        try:
            port = parsed.port
        except ValueError:
            return None
        if scheme == "https":
            if parsed.username or port not in (None, 443):
                return None
        elif scheme == "ssh":
            if parsed.username != "git" or port not in (None, 22):
                return None
        else:
            return None
        if github_repository_identity(value) is None:
            return None
        return value
    if re.fullmatch(
        r"git@github\.com:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?",
        value,
        re.I,
    ):
        return value
    return None


def sanitize_git_error(value, fallback="Git command failed."):
    """Remove credentials and URL query data from Git diagnostics."""
    text = str(value or fallback).strip()
    url_pattern = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s'\"<>]+")
    text = url_pattern.sub(lambda match: sanitize_remote_url(match.group(0)), text)
    text = re.sub(
        r"(?i)([a-z][a-z0-9+.-]*://)[^/@\s]+@",
        r"\1",
        text,
    )
    text = re.sub(
        r"(?i)(?<![A-Za-z0-9_.-])[^/@\s:'\"]+@([A-Za-z0-9.-]+)(?=[:/])",
        r"\1",
        text,
    )
    return text[:4000] or fallback


def limited_text(value, limit, label):
    text = str(value or "").strip()
    if len(text) > limit:
        raise ControlCenterError(
            "{} must be {} characters or fewer.".format(label, limit), 413
        )
    return text


def bounded_provider_prompt(prompt, label="Agent prompt"):
    text = str(prompt)
    if len(text.encode("utf-8")) > MAX_PROVIDER_PROMPT_BYTES:
        raise ControlCenterError(
            "{} must be {} KB or smaller.".format(
                label, MAX_PROVIDER_PROMPT_BYTES // 1024
            ),
            413,
        )
    return text


def validated_provider_thread_id(value, required=False):
    if value in (None, "") and not required:
        return None
    if not isinstance(value, str) or PROVIDER_THREAD_ID.fullmatch(value) is None:
        raise ControlCenterError("Provider thread ID is unsafe or too long.", 409)
    return value


def remote_hostname(value):
    value = str(value or "").strip()
    try:
        parsed = urlsplit(value)
    except ValueError:
        parsed = None
    if parsed and parsed.scheme and parsed.hostname:
        return parsed.hostname.lower().rstrip(".")
    scp = re.match(r"^(?:[^/@\s]+@)?([^/:\s]+):.+$", value)
    return scp.group(1).lower().rstrip(".") if scp else None


def strict_json_loads(value):
    def reject_constant(constant):
        raise ValueError("Nonstandard JSON constant {} is not allowed.".format(constant))

    def reject_surrogates(item):
        if isinstance(item, str):
            if any(0xD800 <= ord(character) <= 0xDFFF for character in item):
                raise ValueError("Unicode surrogate code points are not allowed.")
            return
        if isinstance(item, list):
            for child in item:
                reject_surrogates(child)
            return
        if isinstance(item, dict):
            for key, child in item.items():
                reject_surrogates(key)
                reject_surrogates(child)

    parsed = json.loads(value, parse_constant=reject_constant)
    reject_surrogates(parsed)
    return parsed


_WINDOWS_SPLIT_STAT_IDENTITIES = os.name == "nt"
_BINARY_OPEN_FLAG = getattr(os, "O_BINARY", 0)
_WINDOWS_FILE_API = None


def _stat_identity(details):
    """Return an identity comparable across repeated stats of the same kind."""
    return (
        details.st_dev,
        details.st_ino,
        stat.S_IFMT(details.st_mode),
    )


def _descriptor_file_details(descriptor):
    """Return a stable file identity and size for one open descriptor."""
    if os.name != "nt":
        details = os.fstat(descriptor)
        return _stat_identity(details), details.st_size

    global _WINDOWS_FILE_API
    import ctypes
    import msvcrt
    from ctypes import wintypes

    if _WINDOWS_FILE_API is None:
        class ByHandleFileInformation(ctypes.Structure):
            _fields_ = [
                ("fileAttributes", wintypes.DWORD),
                ("creationTimeLow", wintypes.DWORD),
                ("creationTimeHigh", wintypes.DWORD),
                ("accessTimeLow", wintypes.DWORD),
                ("accessTimeHigh", wintypes.DWORD),
                ("writeTimeLow", wintypes.DWORD),
                ("writeTimeHigh", wintypes.DWORD),
                ("volumeSerialNumber", wintypes.DWORD),
                ("fileSizeHigh", wintypes.DWORD),
                ("fileSizeLow", wintypes.DWORD),
                ("numberOfLinks", wintypes.DWORD),
                ("fileIndexHigh", wintypes.DWORD),
                ("fileIndexLow", wintypes.DWORD),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_information = kernel32.GetFileInformationByHandle
        get_information.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(ByHandleFileInformation),
        ]
        get_information.restype = wintypes.BOOL
        _WINDOWS_FILE_API = (ByHandleFileInformation, get_information)
    ByHandleFileInformation, get_information = _WINDOWS_FILE_API
    information = ByHandleFileInformation()
    native_handle = msvcrt.get_osfhandle(descriptor)
    if native_handle == -1 or not get_information(
        wintypes.HANDLE(native_handle), ctypes.byref(information)
    ):
        error_number = ctypes.get_last_error()
        raise OSError(
            error_number,
            "GetFileInformationByHandle failed for an installer file.",
        )
    identity = (
        information.volumeSerialNumber,
        (information.fileIndexHigh << 32) | information.fileIndexLow,
    )
    size = (information.fileSizeHigh << 32) | information.fileSizeLow
    return identity, size


def _stat_stable_signature(details):
    return _stat_identity(details) + (
        details.st_size,
        stat.S_IMODE(details.st_mode),
        details.st_nlink,
        details.st_mtime_ns,
        details.st_ctime_ns,
    )


def _opened_path_matches(before, opened, path=None, descriptor=None):
    """Verify that a path still resolves to an already opened file."""
    if not _WINDOWS_SPLIT_STAT_IDENTITIES:
        return _stat_identity(before) == _stat_identity(opened)
    if stat.S_IFMT(before.st_mode) != stat.S_IFMT(opened.st_mode):
        return False
    if path is None or descriptor is None:
        return before.st_size == opened.st_size
    flags = os.O_RDONLY | _BINARY_OPEN_FLAG
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    probe = None
    try:
        probe = os.open(str(path), flags)
        opened_identity, opened_size = _descriptor_file_details(descriptor)
        path_identity, path_size = _descriptor_file_details(probe)
        return (
            opened_identity == path_identity
            and opened_size == path_size
            and before.st_size == path_size
        )
    except OSError:
        return False
    finally:
        if probe is not None:
            os.close(probe)


def read_stable_regular_text(path, limit, label="File"):
    """Read one bounded regular file without following or racing a replacement."""
    path = Path(path)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("file read limit must be a positive integer")
    try:
        before = os.lstat(str(path))
    except OSError as exc:
        raise ControlCenterError("{} is unavailable: {}".format(label, exc), 409)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ControlCenterError("{} must be a regular file, not a link or special file.".format(label), 409)
    if before.st_size > limit:
        raise ControlCenterError("{} is larger than {} bytes.".format(label, limit), 413)
    flags = os.O_RDONLY | _BINARY_OPEN_FLAG
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(str(path), flags)
    except OSError as exc:
        raise ControlCenterError("{} could not be opened safely: {}".format(label, exc), 409)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ControlCenterError("{} must remain a regular file.".format(label), 409)
        if not _opened_path_matches(before, opened, path, descriptor):
            raise ControlCenterError("{} changed while it was opened.".format(label), 409)
        chunks = []
        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after_read = os.fstat(descriptor)
        try:
            current = os.lstat(str(path))
        except OSError as exc:
            raise ControlCenterError("{} changed while it was read: {}".format(label, exc), 409)
        opened_signature = _stat_stable_signature(opened)
        if (
            opened_signature != _stat_stable_signature(after_read)
            or _stat_stable_signature(before) != _stat_stable_signature(current)
            or (
                not _WINDOWS_SPLIT_STAT_IDENTITIES
                and opened_signature != _stat_stable_signature(current)
            )
        ):
            raise ControlCenterError("{} changed while it was read.".format(label), 409)
        data = b"".join(chunks)
        if len(data) > limit:
            raise ControlCenterError("{} is larger than {} bytes.".format(label, limit), 413)
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ControlCenterError("{} must be UTF-8 text: {}".format(label, exc), 409)
    finally:
        os.close(descriptor)


def read_agent_result(path, label):
    value = strict_json_loads(
        read_stable_regular_text(path, MAX_AGENT_RESULT_BYTES, label)
    )
    if not isinstance(value, dict) or set(value) - {"status", "message"}:
        raise ControlCenterError(
            "{} must contain only status and an optional message.".format(label), 409
        )
    status = value.get("status")
    message = value.get("message", "")
    if status not in ("ready", "conflict"):
        raise ControlCenterError("{} status must be ready or conflict.".format(label), 409)
    if not isinstance(message, str) or len(message) > 2000:
        raise ControlCenterError("{} message must be at most 2000 characters.".format(label), 409)
    return {"status": status, "message": message}


def safe_project_target(project_root, target):
    """Validate that a project write cannot traverse a symbolic link."""
    raw_root = Path(project_root).expanduser()
    if not raw_root.is_absolute():
        raw_root = Path(os.path.abspath(str(raw_root)))
    target = Path(target)
    if not target.is_absolute():
        target = raw_root / target
    try:
        relative = target.relative_to(raw_root)
    except ValueError:
        raise ControlCenterError("Installer destination must stay inside the project.", 409)
    project_root = raw_root.resolve()
    current = raw_root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ControlCenterError(
                "Installer destination cannot traverse symbolic link {}.".format(current), 409
            )
        if current != target and current.exists() and not current.is_dir():
            raise ControlCenterError(
                "Installer destination parent must be a folder: {}.".format(current), 409
            )
    if not path_is_within(target, project_root):
        raise ControlCenterError("Installer destination must stay inside the project.", 409)
    return target


def configured_lock_dir(config, system=None, temp_root=None):
    """Resolve the shared color registry on the current host.

    Project configs use the portable POSIX-style /tmp default because the
    manual agent flow runs through Bash. Native Windows Control Center runs
    map that default to the Windows temporary directory and pass the resolved
    path to the preview server explicitly.
    """
    config = config if isinstance(config, dict) else {}
    project_name = slugify(str(config.get("project_name") or "webkit"))
    value = str(
        config.get("lock_dir") or "/tmp/{}-agent-colors".format(project_name)
    ).strip()
    system_name = (system or platform.system()).lower()
    portable = value.replace("\\", "/")
    if system_name == "windows" and (portable == "/tmp" or portable.startswith("/tmp/")):
        suffix = portable[len("/tmp/"):] if portable != "/tmp" else ""
        parts = [part for part in suffix.split("/") if part not in ("", ".")]
        if any(part == ".." for part in parts):
            return Path(value).expanduser()
        return Path(temp_root or tempfile.gettempdir()).joinpath(*parts)
    return Path(value).expanduser()


def path_is_within(path, root):
    """Return whether path is root or a descendant, including on Python 3.7."""
    path = Path(path).resolve()
    root = Path(root).resolve()
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _validated_relative_path(value, field, root, require_directory=False, require_file=False):
    if not isinstance(value, str) or not value.strip():
        raise ControlCenterError("Webkit {} must be a non-empty relative path.".format(field), 409)
    raw = value.strip().replace("\\", "/")
    candidate = Path(raw)
    if (
        candidate.is_absolute()
        or raw.startswith("/")
        or re.match(r"^[A-Za-z]:/", raw) is not None
        or any(part == ".." or part.lower() == ".git" for part in candidate.parts)
    ):
        raise ControlCenterError("Webkit {} must stay inside the project.".format(field), 409)
    target = (Path(root).resolve() / candidate).resolve()
    if not path_is_within(target, root):
        raise ControlCenterError("Webkit {} must stay inside the project.".format(field), 409)
    if require_directory and not target.is_dir():
        raise ControlCenterError("The configured Webkit {} does not exist.".format(field), 409)
    if require_file and not target.is_file():
        raise ControlCenterError("The configured Webkit {} does not exist.".format(field), 409)
    normalized = candidate.as_posix().strip("/")
    return normalized or ".", target


def load_webkit_config(config_path, project_root=None, require_default_page=False):
    """Load and validate the Control Center subset of webkit.config.json."""
    config_path = Path(config_path)
    project_root = Path(project_root or config_path.parents[1])
    config_path = safe_project_target(project_root, config_path)
    project_root = project_root.resolve()
    try:
        config = strict_json_loads(
            read_stable_regular_text(
                config_path, MAX_WEBKIT_CONFIG_BYTES, "Webkit preview configuration"
            )
        )
    except (ControlCenterError, OSError, UnicodeDecodeError, ValueError) as exc:
        raise ControlCenterError("Webkit preview configuration is unreadable: {}".format(exc), 409)
    if not isinstance(config, dict):
        raise ControlCenterError("Webkit preview configuration must be a JSON object.", 409)

    normalized = dict(config)
    grace_seconds = config.get("grace_seconds", 180)
    if (
        isinstance(grace_seconds, bool)
        or not isinstance(grace_seconds, int)
        or not MIN_CLAIM_GRACE_SECONDS <= grace_seconds <= MAX_CLAIM_GRACE_SECONDS
    ):
        raise ControlCenterError(
            "Webkit grace_seconds must be an integer from 30 through 86400.",
            409,
        )
    site_root, served_root = _validated_relative_path(
        config.get("site_root", "."), "site_root", project_root, require_directory=True
    )
    default_page, _default_target = _validated_relative_path(
        config.get("default_page", "index.html"),
        "default_page",
        served_root,
        require_file=require_default_page,
    )
    feedback_dir, _feedback_target = _validated_relative_path(
        config.get("feedback_dir", ".webkit/feedback"), "feedback_dir", project_root
    )
    if feedback_dir == ".":
        raise ControlCenterError(
            "Webkit feedback_dir must be a dedicated folder, not the repository root.",
            409,
        )

    lock_dir = configured_lock_dir(config)
    if not lock_dir.is_absolute():
        raise ControlCenterError("Webkit lock_dir must be absolute.", 409)
    if lock_dir.is_symlink():
        raise ControlCenterError("Webkit lock_dir cannot be a symbolic link.", 409)
    resolved_lock_dir = lock_dir.resolve()
    if resolved_lock_dir == Path(resolved_lock_dir.anchor):
        raise ControlCenterError("Webkit lock_dir cannot be a filesystem root.", 409)

    raw_palette = config.get("palette")
    if not isinstance(raw_palette, list) or not raw_palette:
        raise ControlCenterError("Webkit palette must contain at least one color.", 409)
    palette = []
    slugs = set()
    emojis = set()
    ports = set()
    for raw_entry in raw_palette:
        if not isinstance(raw_entry, dict):
            raise ControlCenterError("Every Webkit palette entry must be an object.", 409)
        slug = raw_entry.get("slug")
        emoji = raw_entry.get("emoji")
        port = raw_entry.get("port")
        if not isinstance(slug, str) or COLOR_SLUG.fullmatch(slug) is None:
            raise ControlCenterError(
                "Webkit palette slugs must start with a letter or number and contain only letters, numbers, underscores, or hyphens.",
                409,
            )
        if not valid_color_label(emoji):
            raise ControlCenterError(
                "Every Webkit palette entry needs a short label without whitespace, control characters, bidirectional controls, or markup.",
                409,
            )
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ControlCenterError("Webkit preview ports must be integers between 1 and 65535.", 409)
        if slug in slugs or emoji in emojis or port in ports:
            raise ControlCenterError("Webkit palette slugs, emoji labels, and preview ports must be unique.", 409)
        slugs.add(slug)
        emojis.add(emoji)
        ports.add(port)
        entry = dict(raw_entry)
        entry.update({"slug": slug, "emoji": emoji, "port": port})
        palette.append(entry)

    normalized.update({
        "site_root": site_root,
        "default_page": default_page,
        "feedback_dir": feedback_dir,
        "lock_dir": str(resolved_lock_dir),
        "grace_seconds": grace_seconds,
        "palette": palette,
    })
    return normalized


def run_command(args, cwd=None, env=None, check=True, timeout=120):
    try:
        result = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise ControlCenterError("Required command is not installed: {}".format(args[0]), 409)
    except subprocess.TimeoutExpired:
        raise ControlCenterError("Command timed out: {}".format(" ".join(args)), 500)
    if check and result.returncode != 0:
        message = (result.stderr or result.stdout or "Command failed").strip()
        raise ControlCenterError(message, 409, {"command": args, "exitCode": result.returncode})
    return result


def private_runtime_roots(config, extra_feedback_dir=None):
    roots = [".webkit"]
    candidates = [config.get("feedback_dir")]
    if extra_feedback_dir:
        candidates.append(extra_feedback_dir)
    for value in candidates:
        portable = str(value or "").strip().replace("\\", "/")
        if (
            portable.startswith("/")
            or re.match(r"^[A-Za-z]:/", portable) is not None
        ):
            raise ControlCenterError("Webkit feedback_dir is unsafe.", 409)
        raw = portable.strip("/")
        if not raw or raw == ".":
            raise ControlCenterError(
                "Webkit feedback_dir must be a dedicated repository-relative folder.",
                409,
            )
        candidate = Path(raw)
        if (
            candidate.is_absolute()
            or any(
                part in ("", ".", "..") or part.lower() == ".git"
                for part in candidate.parts
            )
            or re.fullmatch(r"[A-Za-z0-9._/-]+", raw) is None
        ):
            raise ControlCenterError("Webkit feedback_dir is unsafe.", 409)
        normalized = candidate.as_posix()
        if normalized not in roots and not normalized.startswith(".webkit/"):
            roots.append(normalized)
    return roots


def _literal_git_pathspec(relative, exclude=False):
    magic = "top,literal" + (",exclude" if exclude else "")
    return ":({}){}".format(magic, relative)


def require_private_runtime_paths_safe(project_path, config, extra_feedback_dir=None):
    """Require runtime roots to be ignored and absent from the Git index."""
    project_path = Path(project_path).resolve()
    roots = private_runtime_roots(config, extra_feedback_dir)
    for relative in roots:
        tracked = run_command(
            ["git", "ls-files", "-z", "--", _literal_git_pathspec(relative)],
            cwd=project_path, check=False,
        )
        if tracked.returncode != 0:
            raise ControlCenterError("Could not inspect private runtime paths.", 409)
        if tracked.stdout:
            raise ControlCenterError(
                "Private Webkit runtime path {} must not be tracked or staged.".format(relative),
                409,
            )
        probes = (
            "feedback.json", "review.json", "verdicts.json",
            "voice-note.webm", "attachments/reference.txt",
        )
        for name in probes:
            probe = relative.rstrip("/") + "/" + name
            ignored = run_command(
                ["git", "check-ignore", "--quiet", "--no-index", "--", probe],
                cwd=project_path, check=False,
            )
            if ignored.returncode != 0:
                raise ControlCenterError(
                    "Private Webkit runtime path {} must ignore normal feedback protocol files.".format(
                        relative
                    ),
                    409,
                )
    return roots


def private_safe_git_status(project_path, config, extra_feedback_dir=None):
    roots = require_private_runtime_paths_safe(
        project_path, config, extra_feedback_dir
    )
    command = ["git", "status", "--porcelain", "--", "."]
    command.extend(_literal_git_pathspec(root, exclude=True) for root in roots)
    return run_command(command, cwd=project_path)


def stage_without_private_runtime(project_path, config, extra_feedback_dir=None):
    roots = require_private_runtime_paths_safe(
        project_path, config, extra_feedback_dir
    )
    project_path = Path(project_path).resolve()
    # Stage tracked edits and removals first. Private roots are already proven
    # untracked, so -u cannot add their ignored runtime files.
    tracked = ProjectManager._bounded_git_stdout(
        ["git", "ls-files", "-z", "--"],
        project_path,
        MAX_STAGED_PATH_LIST_BYTES,
        "The tracked project path list",
    )
    if tracked:
        run_command(["git", "add", "-u", "--", "."], cwd=project_path)
    untracked = ProjectManager._bounded_git_stdout(
        ["git", "ls-files", "--others", "--exclude-standard", "-z", "--"],
        project_path,
        MAX_STAGED_PATH_LIST_BYTES,
        "The untracked project path list",
    )
    paths = [value for value in untracked.split(b"\0") if value]
    if len(paths) > MAX_STAGED_SECRET_SCAN_FILES:
        raise ControlCenterError(
            "Too many untracked project files were present to stage safely.", 409
        )
    normalized_roots = [root.rstrip("/") for root in roots]
    for raw_path in paths:
        relative = os.fsdecode(raw_path).replace("\\", "/").strip("/")
        if (
            not relative
            or any(
                relative == root or relative.startswith(root + "/")
                for root in normalized_roots
            )
        ):
            raise ControlCenterError(
                "A private Webkit runtime path appeared in the staging list.", 409
            )
    if paths:
        try:
            staged = subprocess.run(
                ["git", "update-index", "--add", "-z", "--stdin"],
                cwd=str(project_path),
                input=b"\0".join(paths) + b"\0",
                capture_output=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ControlCenterError(
                "Untracked project files could not be staged safely: {}".format(exc),
                409,
            )
        if staged.returncode != 0:
            raise ControlCenterError(
                (staged.stderr or staged.stdout or b"Could not stage project files.")
                .decode("utf-8", "replace")
                .strip(),
                409,
            )
    require_private_runtime_paths_safe(project_path, config, extra_feedback_dir)


def require_session_history_private(
    worktree, base_branch, config, original_feedback_dir=None
):
    """Reject any session commit that ever touched config or private runtime data."""
    paths = ["webkit/webkit.config.json"]
    paths.extend(private_runtime_roots(config, original_feedback_dir))
    revision_range = "{}..HEAD".format(base_branch)
    for relative in dict.fromkeys(paths):
        touched = run_command(
            [
                "git", "log", "-1", "--format=%H", revision_range, "--",
                _literal_git_pathspec(relative),
            ],
            cwd=worktree,
            check=False,
        )
        if touched.returncode != 0:
            raise ControlCenterError("Could not inspect session commit history.", 409)
        if touched.stdout.strip():
            raise ControlCenterError(
                "Active sessions must not commit Webkit config or private runtime path {}. ".format(
                    relative
                )
                + "Make configuration changes outside an active session.",
                409,
            )


def rename_directory_noreplace(source, target):
    """Atomically publish a filesystem entry without replacing its target."""
    source = Path(source)
    target = Path(target)
    if os.name == "nt":
        os.rename(str(source), str(target))
        return
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(str(source))
    target_bytes = os.fsencode(str(target))
    if sys.platform == "darwin" and hasattr(libc, "renamex_np"):
        result = libc.renamex_np(source_bytes, target_bytes, 0x00000004)
    elif hasattr(libc, "renameat2"):
        result = libc.renameat2(-100, source_bytes, -100, target_bytes, 1)
    else:
        raise ControlCenterError(
            "This platform cannot safely publish a new filesystem path.", 501
        )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), str(target))


def provider_process_kwargs():
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)}
    return {"start_new_session": True}


def attach_windows_kill_job(process):
    """Put a Windows provider in a job that kills descendants when closed."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimitInformation),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise ControlCenterError("Windows could not create a provider process job.", 500)
    try:
        information = ExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = 0x00002000
        if not kernel32.SetInformationJobObject(
            job, 9, ctypes.byref(information), ctypes.sizeof(information)
        ):
            raise ControlCenterError("Windows could not secure the provider process job.", 500)
        process_handle = wintypes.HANDLE(int(process._handle))
        if not kernel32.AssignProcessToJobObject(job, process_handle):
            raise ControlCenterError("Windows could not isolate the provider process tree.", 500)
        process._webkit_windows_job = (kernel32, job)
        return job
    except BaseException:
        kernel32.CloseHandle(job)
        raise


def close_windows_kill_job(process):
    record = getattr(process, "_webkit_windows_job", None)
    if not isinstance(record, tuple) or len(record) != 2:
        return False
    process._webkit_windows_job = None
    kernel32, handle = record
    kernel32.CloseHandle(handle)
    return True


def scrubbed_child_environment():
    """Copy the host environment without controller or preview capabilities."""
    environment = os.environ.copy()
    private_keys = {
        "WK_CONFIG", "WK_COLOR_OWNER", "WK_COLOR_LOCKDIR", "WK_PORT_LOCKDIR",
        "WK_MUTATION_TOKEN", "WK_PREVIEW_INSTANCE_TOKEN", "WK_TRANSITION_TOKEN",
        "WK_CONTROL_CENTER", "WK_SESSION_COLOR", "WK_ENABLE_API_PROXY",
        "WK_COLOR_FORCE",
    }
    for key in list(environment):
        if key.startswith("WKCC_") or key in private_keys:
            environment.pop(key, None)
    return environment


def stop_process_tree(process, timeout=5, grace=0.25):
    """Terminate, wait for, and if needed kill one provider process tree."""
    if process is None:
        return None
    stop_lock = getattr(process, "_webkit_stop_lock", None)
    if stop_lock is None or not hasattr(stop_lock, "__enter__"):
        stop_lock = threading.Lock()
        try:
            process._webkit_stop_lock = stop_lock
        except (AttributeError, TypeError):
            pass
    with stop_lock:
        return _stop_process_tree_locked(process, timeout, grace)


def _stop_process_tree_locked(process, timeout, grace):

    # Never signal by PID after Popen has reaped the leader. Its PID could
    # already belong to an unrelated process. Callers clean the group before
    # wait(), while the unreaped group leader still reserves that identity.
    returncode = getattr(process, "returncode", None)
    if isinstance(returncode, int):
        close_windows_kill_job(process)
        return returncode
    pid = getattr(process, "pid", None)
    if not isinstance(pid, int):
        if close_windows_kill_job(process):
            try:
                return process.wait(timeout=timeout)
            except (OSError, subprocess.TimeoutExpired):
                return getattr(process, "returncode", None)
        try:
            process.terminate()
        except (AttributeError, OSError):
            pass
        return process.wait()

    def terminate(force=False):
        try:
            if os.name == "posix":
                os.killpg(pid, signal.SIGKILL if force else signal.SIGTERM)
            elif force and close_windows_kill_job(process):
                return
            elif shutil.which("taskkill"):
                command = ["taskkill", "/PID", str(pid), "/T"]
                if force:
                    command.append("/F")
                subprocess.run(
                    command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=timeout,
                    check=False,
                )
            elif force:
                process.kill()
            else:
                process.terminate()
        except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
            pass

    terminate(False)
    grace = max(0, min(float(grace), float(timeout)))
    if os.name != "posix":
        try:
            code = process.wait(timeout=grace)
            close_windows_kill_job(process)
            return code
        except subprocess.TimeoutExpired:
            pass
        except (ChildProcessError, OSError):
            return getattr(process, "returncode", None)
    else:
        time.sleep(grace)
    terminate(True)
    try:
        return process.wait(timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return getattr(process, "returncode", None)


def atomic_write_json(path, value, mode=0o644):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = None
    tmp_name = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=".{}-".format(path.name), suffix=".tmp", dir=str(path.parent)
        )
        if os.name == "posix":
            os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = None
            handle.write(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, str(path))
        tmp_name = None
    finally:
        if fd is not None:
            os.close(fd)
        if tmp_name is not None:
            unlink_if_exists(tmp_name)


def unlink_if_exists(path):
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass


def public_session(value):
    session = dict(value)
    for key in (
        "mutationToken", "seedPrompt", "seedFinalizePrompt", "operationId",
        "pendingOperation", "pendingPrompt",
    ):
        session.pop(key, None)
    return session


def _read_mutable_support_text(path):
    path = Path(path)
    try:
        before = os.lstat(str(path))
    except FileNotFoundError:
        return None, None, 0o644
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ControlCenterError(
            "Mutable installer support files must be regular files with exactly one hard link.",
            409,
        )
    if before.st_size > MAX_MUTABLE_SUPPORT_BYTES:
        raise ControlCenterError("Installer support files must be 8 MB or smaller.", 413)
    flags = os.O_RDONLY | _BINARY_OPEN_FLAG
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(str(path), flags)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (not _WINDOWS_SPLIT_STAT_IDENTITIES and opened.st_nlink != 1)
            or not _opened_path_matches(before, opened, path, descriptor)
        ):
            raise ControlCenterError(
                "Installer support file changed while it was opened.", 409
            )
        chunks = []
        remaining = MAX_MUTABLE_SUPPORT_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > MAX_MUTABLE_SUPPORT_BYTES:
            raise ControlCenterError("Installer support files must be 8 MB or smaller.", 413)
        after = os.fstat(descriptor)
        current = os.lstat(str(path))
        signature = _stat_stable_signature(opened)
        if (
            signature != _stat_stable_signature(after)
            or _stat_stable_signature(before) != _stat_stable_signature(current)
            or (
                not _WINDOWS_SPLIT_STAT_IDENTITIES
                and signature != _stat_stable_signature(current)
            )
            or current.st_nlink != 1
        ):
            raise ControlCenterError(
                "Installer support file changed while it was read.", 409
            )
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ControlCenterError(
                "Installer support file must be UTF-8 text: {}".format(exc), 409
            )
        fingerprint_source = (
            current if _WINDOWS_SPLIT_STAT_IDENTITIES else opened
        )
        fingerprint = (
            fingerprint_source.st_dev,
            fingerprint_source.st_ino,
            fingerprint_source.st_size,
            stat.S_IMODE(fingerprint_source.st_mode),
            fingerprint_source.st_nlink,
            fingerprint_source.st_mtime_ns,
            hashlib.sha256(data).hexdigest(),
        )
        return text, fingerprint, stat.S_IMODE(opened.st_mode)
    finally:
        os.close(descriptor)


def _mutate_support_text(path, transform, journal=None):
    path = Path(path)
    existing, fingerprint, mode = _read_mutable_support_text(path)
    if journal is not None:
        journal.validate_support_before_mutation(path, fingerprint)
    original = existing or ""
    updated = transform(original)
    if updated == original:
        return False
    data = updated.encode("utf-8")
    if len(data) > MAX_MUTABLE_SUPPORT_BYTES:
        raise ControlCenterError("Installer support files must be 8 MB or smaller.", 413)
    path.parent.mkdir(parents=True, exist_ok=True)
    if fingerprint is None:
        writer = journal
        if writer is None:
            writer_root = path.parent.resolve()
            writer_path = writer_root / path.name
            writer = ProjectMutationJournal(writer_root)
            writer.watch_support_file(writer_path)
        else:
            writer_path = path
        writer.write_new_support_file(writer_path, data, mode=mode)
        return True
    current_text, current_fingerprint, _current_mode = _read_mutable_support_text(path)
    if current_text != existing or current_fingerprint != fingerprint:
        raise ControlCenterError(
            "Installer support file changed concurrently and was preserved.", 409
        )
    descriptor = None
    temporary = None
    temporary_expected = None
    temporary_identity = None
    quarantine = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=".{}-".format(path.name), suffix=".tmp", dir=str(path.parent)
        )
        temporary_details = os.fstat(descriptor)
        temporary_identity = (
            temporary_details.st_dev, temporary_details.st_ino
        )
        if os.name == "posix":
            os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_expected = ProjectMutationJournal._fingerprint(temporary)
        _latest_text, latest_fingerprint, _latest_mode = _read_mutable_support_text(path)
        if latest_fingerprint != fingerprint:
            raise ControlCenterError(
                "Installer support file changed concurrently and was preserved.", 409
            )
        quarantine = ProjectMutationJournal._claim_path(path)
        try:
            claimed = ProjectMutationJournal._fingerprint(quarantine)
        except (FileNotFoundError, ControlCenterError, OSError) as exc:
            recovery = ProjectMutationJournal._return_claim(quarantine, path)
            quarantine = None if recovery is None else quarantine
            raise ControlCenterError(
                recovery
                or "Installer support file changed concurrently and was preserved: {}".format(
                    exc
                ),
                409,
            )
        if claimed != fingerprint:
            recovery = ProjectMutationJournal._return_claim(quarantine, path)
            quarantine = None if recovery is None else quarantine
            raise ControlCenterError(
                recovery
                or "Installer support file changed concurrently and was preserved.",
                409,
            )
        try:
            rename_directory_noreplace(temporary, path)
        except OSError as exc:
            recovery = ProjectMutationJournal._return_claim(quarantine, path)
            quarantine = None if recovery is None else quarantine
            raise ControlCenterError(
                recovery
                or "Installer support file changed concurrently and was preserved: {}".format(
                    exc
                ),
                409,
            )
        temporary = None
        if journal is not None:
            journal.record_support_output(path, temporary_expected)
        cleanup_errors = []
        ProjectMutationJournal._cleanup_claimed_file(
            quarantine, fingerprint, path, cleanup_errors
        )
        quarantine = None
        if cleanup_errors:
            raise ControlCenterError(cleanup_errors[0], 409)
    except Exception as primary_error:
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
        cleanup_errors = []
        if temporary is not None and temporary_identity is not None:
            try:
                temporary_current = ProjectMutationJournal._fingerprint(temporary)
            except FileNotFoundError:
                temporary_current = None
            except (ControlCenterError, OSError) as exc:
                cleanup_errors.append(
                    "temporary installer file {} was preserved: {}".format(
                        temporary, exc
                    )
                )
            if temporary_current is not None:
                if temporary_current[:2] != temporary_identity:
                    cleanup_errors.append(
                        "temporary installer path {} changed and was preserved".format(
                            temporary
                        )
                    )
                else:
                    ProjectMutationJournal._remove_owned_file(
                        Path(temporary), temporary_current, cleanup_errors
                    )
        if cleanup_errors:
            raise ControlCenterError(
                "{}. Cleanup note: {}".format(primary_error, cleanup_errors[0]),
                getattr(primary_error, "status", 409),
            ) from primary_error
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return True


def append_section(path, heading, section, project_root=None, journal=None):
    path = safe_project_target(project_root, path) if project_root else Path(path)

    def update(existing):
        if heading in existing:
            return existing
        separator = "" if not existing else (
            "\n" if existing.endswith("\n") else "\n\n"
        )
        return existing + separator + section.rstrip() + "\n"

    return _mutate_support_text(path, update, journal=journal)


class StateStore:
    def __init__(self, state_dir):
        raw_state_dir = Path(state_dir).expanduser()
        if raw_state_dir.is_symlink():
            raise ControlCenterError("Control Center state directory cannot be a symbolic link.", 409)
        if raw_state_dir.exists() and not raw_state_dir.is_dir():
            raise ControlCenterError("Control Center state path must be a directory.", 409)
        self.state_dir = raw_state_dir.resolve()
        self.path = self.state_dir / "state.json"
        self.lock = threading.RLock()
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            os.chmod(str(self.state_dir), 0o700)
        if self.path.is_symlink():
            raise ControlCenterError("Control Center state file cannot be a symbolic link.", 409)
        if self.path.exists() and not self.path.is_file():
            raise ControlCenterError("Control Center state file must be a regular file.", 409)
        if not self.path.exists():
            atomic_write_json(self.path, {
                "version": 1, "providers": [], "projects": [], "sessions": [],
                "settings": dict(DEFAULT_SETTINGS),
            }, mode=0o600)
        elif os.name == "posix":
            os.chmod(str(self.path), 0o600)

    def read(self):
        with self.lock:
            try:
                state = strict_json_loads(
                    read_stable_regular_text(
                        self.path, MAX_STATE_BYTES, "Control Center state file"
                    )
                )
            except (ControlCenterError, ValueError, OSError) as exc:
                raise ControlCenterError("Control Center state is unreadable: {}".format(exc), 500)
            if not isinstance(state, dict):
                raise ControlCenterError("Control Center state must be a JSON object.", 500)
            return state

    @staticmethod
    def _prune_terminal_sessions(state):
        sessions = state.get("sessions", [])
        if not isinstance(sessions, list):
            return []
        terminal = [
            (index, session)
            for index, session in enumerate(sessions)
            if isinstance(session, dict)
            and session.get("status") in TERMINAL_SESSION_STATUSES
        ]
        terminal.sort(
            key=lambda item: (
                str(item[1].get("updatedAt") or item[1].get("createdAt") or ""),
                str(item[1].get("id") or ""),
            ),
            reverse=True,
        )
        removed_indexes = {
            index for index, _session in terminal[MAX_RETAINED_TERMINAL_SESSIONS:]
        }
        if not removed_indexes:
            return []
        removed_ids = {
            str(sessions[index].get("id") or "") for index in removed_indexes
        }
        state["sessions"] = [
            session for index, session in enumerate(sessions)
            if index not in removed_indexes
        ]
        for project in state.get("projects", []):
            if not isinstance(project, dict):
                continue
            onboarding = project.get("onboarding")
            if isinstance(onboarding, dict) and onboarding.get("sessionId") in removed_ids:
                onboarding.pop("sessionId", None)
        return sorted(session_id for session_id in removed_ids if session_id)

    def _remove_pruned_logs(self, session_ids):
        logs_dir = self.state_dir / "logs"
        for session_id in session_ids:
            if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", session_id) is None:
                continue
            try:
                unlink_if_exists(logs_dir / (session_id + ".jsonl"))
            except OSError:
                # State pruning is already durable. Old logs are optional cleanup.
                continue

    def update(self, mutator):
        with self.lock:
            state = self.read()
            result = mutator(state)
            pruned = self._prune_terminal_sessions(state)
            encoded_size = len(
                (json.dumps(state, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
            )
            if encoded_size > MAX_STATE_BYTES:
                raise ControlCenterError(
                    "Control Center state reached its {} MB safety limit.".format(
                        MAX_STATE_BYTES // (1024 * 1024)
                    ),
                    507,
                )
            atomic_write_json(self.path, state, mode=0o600)
            self._remove_pruned_logs(pruned)
            return result


class EventLog:
    _locks_guard = threading.Lock()
    _locks = {}

    def __init__(self, state_dir, session_id):
        state_dir = Path(state_dir).resolve()
        logs_dir = safe_project_target(state_dir, state_dir / "logs")
        self.path = safe_project_target(logs_dir, logs_dir / (session_id + ".jsonl"))
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            os.chmod(str(self.path.parent), 0o700)
        key = str(self.path)
        with self._locks_guard:
            self.lock = self._locks.setdefault(key, threading.Lock())

    @staticmethod
    def _header(generation):
        return (
            json.dumps({"_awesomeWebkitEventLog": 1, "generation": generation}) + "\n"
        ).encode("utf-8")

    def _replace_locked(self, data):
        descriptor = None
        temporary = None
        try:
            descriptor, temporary = tempfile.mkstemp(
                prefix=".{}-".format(self.path.name),
                suffix=".tmp",
                dir=str(self.path.parent),
            )
            if os.name == "posix":
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = None
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, str(self.path))
            temporary = None
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary is not None:
                unlink_if_exists(temporary)

    def _open_binary_locked(self):
        flags = os.O_RDONLY | _BINARY_OPEN_FLAG
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(str(self.path), flags)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ControlCenterError("The session event log is unreadable: {}".format(exc), 500)
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            os.close(descriptor)
            raise ControlCenterError("The session event log must be a regular file.", 500)
        return os.fdopen(descriptor, "rb")

    def _retained_tail_locked(self, header_end=0):
        handle = self._open_binary_locked()
        if handle is None:
            return b""
        with handle:
            size = os.fstat(handle.fileno()).st_size
            start = max(int(header_end), size - EVENT_LOG_RETAIN_BYTES)
            handle.seek(start)
            if start > int(header_end):
                handle.readline(MAX_EVENT_LINE_BYTES + 1)
            return handle.read(EVENT_LOG_RETAIN_BYTES)

    def _ensure_log_locked(self):
        handle = self._open_binary_locked()
        if handle is None:
            generation = uuid.uuid4().hex
            header = self._header(generation)
            self._replace_locked(header)
            return generation, len(header)
        with handle:
            first = handle.readline(MAX_EVENT_LINE_BYTES + 1)
            size = os.fstat(handle.fileno()).st_size
        try:
            header_value = strict_json_loads(first.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            header_value = None
        if (
            isinstance(header_value, dict)
            and header_value.get("_awesomeWebkitEventLog") == 1
            and isinstance(header_value.get("generation"), str)
            and re.fullmatch(r"[0-9a-f]{32}", header_value["generation"])
        ):
            generation = header_value["generation"]
            header_end = len(first)
            if size <= MAX_EVENT_LOG_BYTES:
                return generation, header_end
        else:
            header_end = 0
        retained = self._retained_tail_locked(header_end)
        generation = uuid.uuid4().hex
        header = self._header(generation)
        self._replace_locked(header + retained)
        return generation, len(header)

    @staticmethod
    def _parse_cursor(after, generation, header_end):
        if isinstance(after, bool):
            raise ControlCenterError("Event cursor is invalid.", 400)
        reset = False
        if isinstance(after, int):
            if after < 0:
                raise ControlCenterError("Event cursor must be non-negative.", 400)
            offset = after
        else:
            match = EVENT_CURSOR.fullmatch(str(after))
            if match is None:
                raise ControlCenterError("Event cursor is invalid.", 400)
            requested_generation, raw_offset = match.groups()
            offset = int(raw_offset)
            if requested_generation != generation:
                offset = header_end
                reset = True
        if offset == 0:
            offset = header_end
        return offset, reset

    def append(self, role, text, kind="message", meta=None):
        original = str(text)
        suffix = "\n[output truncated by AWESOME WEBKIT]"
        raw = original.encode("utf-8")
        truncated = len(raw) > MAX_EVENT_TEXT_BYTES
        if truncated:
            budget = max(0, MAX_EVENT_TEXT_BYTES - len(suffix.encode("utf-8")))
            text = raw[:budget].decode("utf-8", errors="ignore") + suffix
        else:
            text = original

        def serialize(candidate):
            value = {
                "time": utc_now(),
                "role": role,
                "kind": kind,
                "text": candidate,
            }
            if meta:
                value["meta"] = meta
            return value, (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8")

        event, encoded = serialize(text)
        if len(encoded) > MAX_EVENT_LINE_BYTES:
            base = text[:-len(suffix)] if truncated and text.endswith(suffix) else text
            empty_event, empty_encoded = serialize(suffix)
            if len(empty_encoded) > MAX_EVENT_LINE_BYTES:
                raise ControlCenterError("The session event metadata is too large to record.", 413)
            low = 0
            high = len(base)
            event, encoded = empty_event, empty_encoded
            while low <= high:
                middle = (low + high) // 2
                candidate_event, candidate_encoded = serialize(base[:middle] + suffix)
                if len(candidate_encoded) <= MAX_EVENT_LINE_BYTES:
                    event, encoded = candidate_event, candidate_encoded
                    low = middle + 1
                else:
                    high = middle - 1
        with self.lock:
            generation, header_end = self._ensure_log_locked()
            try:
                current_size = self.path.stat().st_size
            except OSError as exc:
                raise ControlCenterError("The session event log is unreadable: {}".format(exc), 500)
            if current_size + len(encoded) > MAX_EVENT_LOG_BYTES:
                retained = self._retained_tail_locked(header_end)
                generation = uuid.uuid4().hex
                header = self._header(generation)
                while retained and len(header) + len(retained) + len(encoded) > MAX_EVENT_LOG_BYTES:
                    newline = retained.find(b"\n")
                    retained = b"" if newline < 0 else retained[newline + 1:]
                self._replace_locked(header + retained)
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | _BINARY_OPEN_FLAG
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(str(self.path), flags, 0o600)
            if os.name == "posix":
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "ab") as handle:
                handle.write(encoded)
        return event

    def read_after(self, after=0):
        with self.lock:
            generation, header_end = self._ensure_log_locked()
            offset, reset = self._parse_cursor(after, generation, header_end)
            events = []
            handle = self._open_binary_locked()
            if handle is None:
                return {"events": [], "next": "{}:{}".format(generation, header_end)}
            with handle:
                size = os.fstat(handle.fileno()).st_size
                if offset < header_end or offset > size:
                    offset = header_end
                    reset = True
                if offset > header_end:
                    handle.seek(offset - 1)
                    if handle.read(1) != b"\n":
                        raise ControlCenterError("Event cursor does not point to an event boundary.", 400)
                handle.seek(offset)
                while len(events) < MAX_EVENT_BATCH:
                    index = handle.tell()
                    line = handle.readline(MAX_EVENT_LINE_BYTES + 1)
                    if not line:
                        break
                    if len(line) > MAX_EVENT_LINE_BYTES:
                        event = None
                    else:
                        try:
                            event = strict_json_loads(line.decode("utf-8"))
                        except (UnicodeDecodeError, ValueError):
                            event = None
                    try:
                        valid_event = isinstance(event, dict) and not event.get(
                            "_awesomeWebkitEventLog"
                        )
                    except AttributeError:
                        valid_event = False
                    if not valid_event:
                        event = {
                            "time": utc_now(),
                            "role": "system",
                            "kind": "error",
                            "text": "Unreadable event log entry.",
                        }
                    event["index"] = "{}:{}".format(generation, index)
                    events.append(event)
                next_cursor = "{}:{}".format(generation, handle.tell())
        return {"events": events, "next": next_cursor, "reset": reset}


class ProjectMutationJournal:
    """Roll back only paths whose installer-owned identity and data are unchanged."""

    MAX_SUPPORT_BYTES = 8 * 1024 * 1024

    def __init__(self, project_root):
        input_root = Path(project_root).expanduser()
        if not input_root.is_absolute():
            input_root = Path(os.path.abspath(str(input_root)))
        self.input_root = input_root
        self.project_root = input_root.resolve()
        self.support = {}
        self.created_files = []
        self.created_dirs = []
        self.git_identity = None
        self.expected_git_config = None
        self.expected_git_manifest = None
        self.expected_head = None

    def _target(self, path):
        target = Path(path).expanduser()
        if not target.is_absolute():
            target = self.input_root / target
        try:
            relative = target.relative_to(self.input_root)
        except ValueError:
            canonical = target
        else:
            canonical = self.project_root / relative
        return safe_project_target(self.project_root, canonical)

    @staticmethod
    def _stable_signature(details):
        return _stat_stable_signature(details)

    @classmethod
    def _snapshot(
        cls, path, limit=None, require_single_link=False, include_data=True
    ):
        """Read one regular file from a stable inode without following links."""
        path = Path(path)
        before = os.lstat(str(path))
        if not stat.S_ISREG(before.st_mode):
            raise ControlCenterError(
                "Installer journal paths must be regular files.", 409
            )
        if require_single_link and before.st_nlink != 1:
            raise ControlCenterError(
                "Installer support files must have exactly one hard link.", 409
            )
        if limit is not None and before.st_size > limit:
            raise ControlCenterError(
                "Installer support files must be 8 MB or smaller.", 413
            )
        flags = os.O_RDONLY | _BINARY_OPEN_FLAG
        if hasattr(os, "O_NONBLOCK"):
            flags |= os.O_NONBLOCK
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(str(path), flags)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise ControlCenterError(
                    "Installer journal paths must be regular files.", 409
                )
            if (
                not _opened_path_matches(before, opened, path, descriptor)
                or (
                    require_single_link
                    and not _WINDOWS_SPLIT_STAT_IDENTITIES
                    and opened.st_nlink != 1
                )
            ):
                raise ControlCenterError(
                    "Installer support file changed while it was opened.", 409
                )
            if limit is not None and opened.st_size > limit:
                raise ControlCenterError(
                    "Installer support files must be 8 MB or smaller.", 413
                )
            digest = hashlib.sha256()
            chunks = []
            total = 0
            while True:
                request = 1024 * 1024
                if limit is not None:
                    request = min(request, limit + 1 - total)
                    if request <= 0:
                        raise ControlCenterError(
                            "Installer support files must be 8 MB or smaller.", 413
                        )
                chunk = os.read(descriptor, request)
                if not chunk:
                    break
                total += len(chunk)
                if limit is not None and total > limit:
                    raise ControlCenterError(
                        "Installer support files must be 8 MB or smaller.", 413
                    )
                if include_data:
                    chunks.append(chunk)
                digest.update(chunk)
            after = os.fstat(descriptor)
            try:
                current = os.lstat(str(path))
            except OSError as exc:
                raise ControlCenterError(
                    "Installer support file changed while it was read: {}.".format(exc),
                    409,
                )
            signature = cls._stable_signature(opened)
            if (
                signature != cls._stable_signature(after)
                or cls._stable_signature(before) != cls._stable_signature(current)
                or (
                    not _WINDOWS_SPLIT_STAT_IDENTITIES
                    and signature != cls._stable_signature(current)
                )
                or (require_single_link and current.st_nlink != 1)
                or total != opened.st_size
            ):
                raise ControlCenterError(
                    "Installer support file changed while it was read.", 409
                )
            fingerprint_source = (
                current if _WINDOWS_SPLIT_STAT_IDENTITIES else opened
            )
            fingerprint = (
                fingerprint_source.st_dev,
                fingerprint_source.st_ino,
                fingerprint_source.st_size,
                stat.S_IMODE(fingerprint_source.st_mode),
                fingerprint_source.st_nlink,
                fingerprint_source.st_mtime_ns,
                digest.hexdigest(),
            )
            return {
                "data": b"".join(chunks) if include_data else None,
                "mode": stat.S_IMODE(opened.st_mode),
                "fingerprint": fingerprint,
            }
        finally:
            os.close(descriptor)

    @classmethod
    def _fingerprint(cls, path):
        return cls._snapshot(path, include_data=False)["fingerprint"]

    def watch_support_file(self, path):
        path = self._target(path)
        key = str(path)
        if key in self.support:
            return
        try:
            details = os.lstat(str(path))
        except FileNotFoundError:
            self.support[key] = {
                "path": path, "exists": False, "pre": None, "post": None
            }
            return
        if not stat.S_ISREG(details.st_mode):
            raise ControlCenterError(
                "Installer support paths must be regular files.", 409
            )
        snapshot = self._snapshot(
            path, limit=self.MAX_SUPPORT_BYTES, require_single_link=True
        )
        self.support[key] = {
            "path": path,
            "exists": True,
            "data": snapshot["data"],
            "mode": snapshot["mode"],
            "pre": snapshot["fingerprint"],
            "post": None,
        }

    def validate_support_before_mutation(self, path, current_fingerprint):
        path = self._target(path)
        record = self.support.get(str(path))
        if record is None:
            raise ControlCenterError(
                "Installer support file was not registered with its rollback journal.",
                409,
            )
        expected = record.get("post")
        if expected is None:
            expected = record.get("pre")
        if current_fingerprint != expected:
            raise ControlCenterError(
                "Installer support file changed concurrently and was preserved.", 409
            )

    def record_support_output(self, path, fingerprint):
        path = self._target(path)
        record = self.support.get(str(path))
        if record is None:
            raise ControlCenterError(
                "Installer support file was not registered with its rollback journal.",
                409,
            )
        record["post"] = fingerprint

    def mark_support_written(self, path):
        path = self._target(path)
        record = self.support[str(path)]
        current = self._snapshot(
            path,
            limit=self.MAX_SUPPORT_BYTES,
            require_single_link=True,
            include_data=False,
        )["fingerprint"]
        if record.get("post") is not None and current != record["post"]:
            raise ControlCenterError(
                "Installer support file changed concurrently and was preserved.", 409
            )
        record["post"] = current

    def write_new_support_file(self, path, data, mode=0o644):
        """Create a watched absent support file without replacing a concurrent file."""
        path = self._target(path)
        if not isinstance(data, bytes):
            raise TypeError("installer support data must be bytes")
        if len(data) > self.MAX_SUPPORT_BYTES:
            raise ControlCenterError(
                "Installer support files must be 8 MB or smaller.", 413
            )
        self.validate_support_before_mutation(path, None)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _BINARY_OPEN_FLAG
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = None
        identity = None
        opened_identity = None
        offset = 0
        try:
            descriptor = os.open(str(path), flags, mode)
            opened = os.fstat(descriptor)
            created = os.lstat(str(path))
            opened_identity, _opened_size = _descriptor_file_details(descriptor)
            identity = _stat_identity(created)
            if (
                not stat.S_ISREG(opened.st_mode)
                or not stat.S_ISREG(created.st_mode)
                or created.st_nlink != 1
                or not _opened_path_matches(created, opened, path, descriptor)
            ):
                raise ControlCenterError(
                    "Installer support file changed concurrently and was preserved.",
                    409,
                )
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, mode)
            while offset < len(data):
                written = os.write(descriptor, data[offset:])
                if written <= 0:
                    raise OSError("Installer support write made no forward progress.")
                offset += written
            os.fsync(descriptor)
            details = os.fstat(descriptor)
            details_identity, details_size = _descriptor_file_details(descriptor)
            if (
                not stat.S_ISREG(details.st_mode)
                or (
                    not _WINDOWS_SPLIT_STAT_IDENTITIES
                    and details.st_nlink != 1
                )
                or details_identity != opened_identity
                or details_size != len(data)
            ):
                raise ControlCenterError(
                    "Installer support file changed concurrently and was preserved.",
                    409,
                )
            current = self._snapshot(
                path,
                limit=self.MAX_SUPPORT_BYTES,
                require_single_link=True,
                include_data=False,
            )["fingerprint"]
            current_details = os.lstat(str(path))
            if (
                not _opened_path_matches(current_details, details, path, descriptor)
                or (
                    not _WINDOWS_SPLIT_STAT_IDENTITIES
                    and _stat_identity(current_details) != identity
                )
                or current[2] != len(data)
                or current[-1] != hashlib.sha256(data).hexdigest()
            ):
                raise ControlCenterError(
                    "Installer support file changed concurrently and was preserved.",
                    409,
                )
            os.close(descriptor)
            descriptor = None
            closed_current = self._snapshot(
                path,
                limit=self.MAX_SUPPORT_BYTES,
                require_single_link=True,
                include_data=False,
            )["fingerprint"]
            if closed_current != current:
                raise ControlCenterError(
                    "Installer support file changed concurrently and was preserved.",
                    409,
                )
            self.record_support_output(path, closed_current)
        except FileExistsError as exc:
            raise ControlCenterError(
                "Installer support file was created concurrently and was preserved.",
                409,
            ) from exc
        except Exception as primary_error:
            partial_expected = None
            if descriptor is not None:
                try:
                    details = os.fstat(descriptor)
                    details_identity, _details_size = _descriptor_file_details(
                        descriptor
                    )
                    descriptor_still_owned = (
                        stat.S_ISREG(details.st_mode)
                        and details_identity == opened_identity
                    )
                except OSError:
                    descriptor_still_owned = False
                if descriptor_still_owned and identity is not None:
                    try:
                        current = self._snapshot(
                            path,
                            limit=self.MAX_SUPPORT_BYTES,
                            require_single_link=True,
                            include_data=False,
                        )["fingerprint"]
                        current_details = os.lstat(str(path))
                    except (FileNotFoundError, ControlCenterError, OSError):
                        current = None
                    if (
                        current is not None
                        and _opened_path_matches(
                            current_details, details, path, descriptor
                        )
                        and (
                            _WINDOWS_SPLIT_STAT_IDENTITIES
                            or _stat_identity(current_details) == identity
                        )
                        and current[2] == offset
                        and current[-1]
                        == hashlib.sha256(data[:offset]).hexdigest()
                    ):
                        partial_expected = current
                os.close(descriptor)
                descriptor = None
                if partial_expected is not None:
                    try:
                        closed_partial = self._snapshot(
                            path,
                            limit=self.MAX_SUPPORT_BYTES,
                            require_single_link=True,
                            include_data=False,
                        )["fingerprint"]
                    except (FileNotFoundError, ControlCenterError, OSError):
                        partial_expected = None
                    else:
                        if closed_partial != partial_expected:
                            partial_expected = None
            if partial_expected is not None:
                cleanup_errors = []
                self._remove_owned_file(path, partial_expected, cleanup_errors)
                if cleanup_errors:
                    raise ControlCenterError(
                        "{}. Cleanup note: {}".format(
                            primary_error, cleanup_errors[0]
                        ),
                        getattr(primary_error, "status", 409),
                    ) from primary_error
            raise
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def ensure_directory(self, path):
        path = self._target(path)
        missing = []
        current = path
        while current != self.project_root and not current.exists():
            missing.append(current)
            current = current.parent
        if current.is_symlink() or not current.is_dir():
            raise ControlCenterError(
                "Installer destination parent must be a real directory.", 409
            )
        for directory in reversed(missing):
            temporary = None
            identity = None
            identity_descriptor = None
            for _attempt in range(16):
                candidate = directory.parent / (".wk-mkdir-" + uuid.uuid4().hex)
                try:
                    candidate.mkdir()
                    temporary = candidate
                    break
                except FileExistsError:
                    continue
            if temporary is None:
                raise ControlCenterError(
                    "Installer could not allocate a private directory path.", 409
                )
            try:
                details = os.lstat(str(temporary))
                identity = _stat_identity(details)
                if os.name == "posix":
                    directory_flags = os.O_RDONLY
                    directory_flags |= getattr(os, "O_DIRECTORY", 0)
                    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
                    identity_descriptor = os.open(str(temporary), directory_flags)
                    os.set_inheritable(identity_descriptor, False)
                    if _stat_identity(os.fstat(identity_descriptor)) != identity:
                        raise ControlCenterError(
                            "Installer destination directory changed while it was opened.",
                            409,
                        )
                try:
                    rename_directory_noreplace(temporary, directory)
                except FileExistsError as exc:
                    raise ControlCenterError(
                        "Installer destination directory was created concurrently "
                        "and was preserved: {}.".format(directory),
                        409,
                    ) from exc
                temporary = None
                published = os.lstat(str(directory))
                if _stat_identity(published) != identity:
                    raise ControlCenterError(
                        "Installer destination directory changed while it was published.",
                        409,
                    )
                self.created_dirs.append({
                    "path": directory,
                    "identity": identity,
                    "identityDescriptor": identity_descriptor,
                })
                identity_descriptor = None
            except Exception as primary_error:
                if temporary is not None and identity is not None:
                    cleanup_errors = []
                    self._remove_owned_directory(
                        temporary, identity, cleanup_errors,
                        identity_descriptor=identity_descriptor,
                    )
                    if cleanup_errors:
                        raise ControlCenterError(
                            "{}. Cleanup note: {}".format(
                                primary_error, cleanup_errors[0]
                            ),
                            getattr(primary_error, "status", 409),
                        ) from primary_error
                elif temporary is not None:
                    raise ControlCenterError(
                        "{}. Cleanup note: temporary directory {} was preserved "
                        "because its identity could not be verified.".format(
                            primary_error, temporary
                        ),
                        getattr(primary_error, "status", 409),
                    ) from primary_error
                if identity_descriptor is not None:
                    os.close(identity_descriptor)
                raise

    def begin_created_file(self, path, details):
        path = self._target(path)
        expected = (
            details.st_dev, details.st_ino, 0,
            stat.S_IMODE(details.st_mode), details.st_nlink,
            details.st_mtime_ns, hashlib.sha256(b"").hexdigest(),
        )
        record = {"path": path, "expected": expected}
        self.created_files.append(record)
        return record

    def finish_created_file(self, record, expected=None):
        record["expected"] = (
            expected if expected is not None else self._fingerprint(record["path"])
        )

    @staticmethod
    def _git_metadata_manifest(git_dir):
        git_dir = Path(git_dir)
        manifest = {}
        for current, directory_names, file_names in os.walk(
            str(git_dir), topdown=True, followlinks=False
        ):
            current_path = Path(current)
            directory_names.sort()
            file_names.sort()
            for name in list(directory_names):
                path = current_path / name
                details = os.lstat(str(path))
                relative = path.relative_to(git_dir).as_posix()
                if stat.S_ISLNK(details.st_mode):
                    manifest[relative] = (
                        "link", stat.S_IMODE(details.st_mode), os.readlink(str(path))
                    )
                    directory_names.remove(name)
                elif stat.S_ISDIR(details.st_mode):
                    manifest[relative] = (
                        "dir", stat.S_IMODE(details.st_mode), details.st_dev, details.st_ino
                    )
                else:
                    manifest[relative] = (
                        "special", details.st_mode, details.st_dev, details.st_ino
                    )
                    directory_names.remove(name)
            for name in file_names:
                path = current_path / name
                details = os.lstat(str(path))
                relative = path.relative_to(git_dir).as_posix()
                if stat.S_ISREG(details.st_mode):
                    manifest[relative] = (
                        "file",
                        stat.S_IMODE(details.st_mode),
                        ProjectMutationJournal._fingerprint(path),
                    )
                elif stat.S_ISLNK(details.st_mode):
                    manifest[relative] = (
                        "link", stat.S_IMODE(details.st_mode), os.readlink(str(path))
                    )
                else:
                    manifest[relative] = (
                        "special", details.st_mode, details.st_dev, details.st_ino
                    )
        return manifest

    def record_git_directory(self):
        git_dir = safe_project_target(self.project_root, self.project_root / ".git")
        details = os.lstat(str(git_dir))
        if not stat.S_ISDIR(details.st_mode):
            raise ControlCenterError("Git initialization did not create a real directory.", 409)
        self.git_identity = (details.st_dev, details.st_ino)
        self.record_git_metadata()

    def record_git_metadata(self):
        config_path = self.project_root / ".git" / "config"
        self.expected_git_config = self._fingerprint(config_path)
        self.expected_git_manifest = self._git_metadata_manifest(
            self.project_root / ".git"
        )

    def record_initial_commit(self):
        result = run_command(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=self.project_root,
        )
        self.expected_head = result.stdout.strip()
        self.record_git_metadata()

    def owns_removable_git_directory(self):
        git_dir = self.project_root / ".git"
        try:
            details = os.lstat(str(git_dir))
        except FileNotFoundError:
            return True
        if (
            self.git_identity is None
            or not stat.S_ISDIR(details.st_mode)
            or (details.st_dev, details.st_ino) != self.git_identity
        ):
            return False
        try:
            if self._fingerprint(git_dir / "config") != self.expected_git_config:
                return False
            if self._git_metadata_manifest(git_dir) != self.expected_git_manifest:
                return False
        except (FileNotFoundError, ControlCenterError, OSError):
            return False
        head = run_command(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=self.project_root, check=False,
        )
        if self.expected_head is None:
            refs = run_command(
                ["git", "for-each-ref", "--format=%(refname)"],
                cwd=self.project_root, check=False,
            )
            status = run_command(
                [
                    "git", "--no-optional-locks", "status", "--porcelain=v1",
                    "--untracked-files=all", "--ignore-submodules=none",
                ],
                cwd=self.project_root, check=False,
            )
            return (
                head.returncode != 0
                and refs.returncode == 0
                and not refs.stdout.strip()
                and status.returncode == 0
                and not status.stdout.strip()
            )
        if head.returncode != 0 or head.stdout.strip() != self.expected_head:
            return False
        count = run_command(
            ["git", "rev-list", "--all", "--count"],
            cwd=self.project_root, check=False,
        )
        status = run_command(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=self.project_root, check=False,
        )
        return (
            count.returncode == 0
            and count.stdout.strip() == "1"
            and status.returncode == 0
            and not status.stdout.strip()
        )

    @staticmethod
    def _claim_path(path):
        """Atomically move a path to an invocation-unique quarantine name."""
        path = Path(path)
        for _attempt in range(16):
            quarantine = path.parent / (".wk-rollback-" + uuid.uuid4().hex)
            try:
                rename_directory_noreplace(path, quarantine)
                return quarantine
            except FileExistsError:
                continue
        raise ControlCenterError(
            "Installer rollback could not allocate a private quarantine path.", 409
        )

    @staticmethod
    def _return_claim(quarantine, path):
        try:
            rename_directory_noreplace(quarantine, path)
            return None
        except OSError as exc:
            return (
                "{} was preserved at {} because its original path could not be "
                "restored: {}".format(path, quarantine, exc)
            )

    @classmethod
    def _remove_owned_file(cls, path, expected, errors):
        """Remove a file only after atomically claiming and validating its inode."""
        try:
            quarantine = cls._claim_path(path)
        except FileNotFoundError:
            return
        except (ControlCenterError, OSError) as exc:
            errors.append("could not claim {} for cleanup: {}".format(path, exc))
            return
        try:
            current = cls._fingerprint(quarantine)
        except (FileNotFoundError, ControlCenterError, OSError) as exc:
            recovery = cls._return_claim(quarantine, path)
            errors.append(
                recovery
                or "{} changed during rollback and was preserved: {}".format(path, exc)
            )
            return
        if current != expected:
            recovery = cls._return_claim(quarantine, path)
            errors.append(
                recovery
                or "{} changed after installation and was preserved".format(path)
            )
            return
        try:
            quarantine.unlink()
        except OSError as exc:
            recovery = cls._return_claim(quarantine, path)
            errors.append(
                recovery
                or "could not remove {} during rollback: {}".format(path, exc)
            )

    @classmethod
    def _remove_owned_directory(
        cls, path, expected, errors, identity_descriptor=None
    ):
        """Remove a directory only after atomically claiming its exact inode."""
        try:
            quarantine = cls._claim_path(path)
        except FileNotFoundError:
            return
        except (ControlCenterError, OSError) as exc:
            errors.append("could not claim directory {}: {}".format(path, exc))
            return
        try:
            details = os.lstat(str(quarantine))
            current = _stat_identity(details)
            opened = None
            if identity_descriptor is not None:
                opened = os.fstat(identity_descriptor)
        except OSError as exc:
            recovery = cls._return_claim(quarantine, path)
            errors.append(
                recovery or "could not inspect directory {}: {}".format(path, exc)
            )
            return
        if (
            not stat.S_ISDIR(details.st_mode)
            or current != expected
            or (
                opened is not None
                and (
                    not stat.S_ISDIR(opened.st_mode)
                    or _stat_identity(opened) != expected
                )
            )
        ):
            recovery = cls._return_claim(quarantine, path)
            errors.append(
                recovery
                or "directory {} changed after installation and was preserved".format(
                    path
                )
            )
            return
        try:
            quarantine.rmdir()
        except OSError as exc:
            recovery = cls._return_claim(quarantine, path)
            errors.append(
                recovery or "could not remove directory {}: {}".format(path, exc)
            )

    @classmethod
    def _prepare_restore_file(cls, path, record):
        descriptor = None
        temporary = None
        identity = None
        try:
            descriptor, temporary = tempfile.mkstemp(
                prefix=".wk-restore-", dir=str(Path(path).parent)
            )
            details = os.fstat(descriptor)
            identity = (details.st_dev, details.st_ino)
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, record["mode"])
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = None
                handle.write(record["data"])
                handle.flush()
                os.fsync(handle.fileno())
            expected = cls._fingerprint(temporary)
            return Path(temporary), expected
        except Exception as primary_error:
            if descriptor is not None:
                os.close(descriptor)
            if temporary is not None and identity is not None:
                cleanup_errors = []
                try:
                    current = cls._fingerprint(temporary)
                except FileNotFoundError:
                    current = None
                except (ControlCenterError, OSError) as exc:
                    cleanup_errors.append(
                        "temporary rollback file {} was preserved: {}".format(
                            temporary, exc
                        )
                    )
                    current = None
                if current is not None:
                    if current[:2] == identity:
                        cls._remove_owned_file(temporary, current, cleanup_errors)
                    else:
                        cleanup_errors.append(
                            "temporary rollback path {} changed and was preserved".format(
                                temporary
                            )
                        )
                if cleanup_errors:
                    raise ControlCenterError(
                        "{}. Cleanup note: {}".format(
                            primary_error, cleanup_errors[0]
                        ),
                        getattr(primary_error, "status", 409),
                    ) from primary_error
            raise

    @classmethod
    def _cleanup_claimed_file(cls, quarantine, expected, path, errors):
        """Remove a quarantined invocation-owned file, preserving any replacement."""
        try:
            current = cls._fingerprint(quarantine)
        except (FileNotFoundError, ControlCenterError, OSError) as exc:
            errors.append(
                "{} changed during rollback and was preserved at {}: {}".format(
                    path, quarantine, exc
                )
            )
            return
        if current != expected:
            errors.append(
                "{} changed during rollback and was preserved at {}".format(
                    path, quarantine
                )
            )
            return
        try:
            quarantine.unlink()
        except OSError as exc:
            errors.append(
                "could not remove the claimed installer file {}: {}".format(
                    quarantine, exc
                )
            )

    @classmethod
    def _rollback_support_file(cls, record, errors):
        path = record["path"]
        post = record.get("post")
        if post is None:
            return
        restore_path = None
        restore_expected = None
        if record["exists"]:
            try:
                restore_path, restore_expected = cls._prepare_restore_file(path, record)
            except (ControlCenterError, OSError) as exc:
                errors.append("could not prepare restoration for {}: {}".format(path, exc))
                return
        try:
            try:
                quarantine = cls._claim_path(path)
            except FileNotFoundError:
                errors.append("{} changed after installation and was preserved".format(path))
                return
            except (ControlCenterError, OSError) as exc:
                errors.append("could not claim {} for rollback: {}".format(path, exc))
                return
            try:
                current = cls._fingerprint(quarantine)
            except (FileNotFoundError, ControlCenterError, OSError) as exc:
                recovery = cls._return_claim(quarantine, path)
                errors.append(
                    recovery
                    or "{} changed after installation and was preserved: {}".format(
                        path, exc
                    )
                )
                return
            if current != post:
                recovery = cls._return_claim(quarantine, path)
                errors.append(
                    recovery
                    or "{} changed after installation and was preserved".format(path)
                )
                return
            if not record["exists"]:
                cls._cleanup_claimed_file(quarantine, post, path, errors)
                return
            try:
                rename_directory_noreplace(restore_path, path)
                restore_path = None
            except OSError as exc:
                errors.append(
                    "{} was not restored because its path changed during rollback: {}".format(
                        path, exc
                    )
                )
                cls._cleanup_claimed_file(quarantine, post, path, errors)
                return
            try:
                restored = cls._fingerprint(path)
            except (FileNotFoundError, ControlCenterError, OSError) as exc:
                errors.append("{} changed while it was restored: {}".format(path, exc))
            else:
                if restored != restore_expected:
                    errors.append("{} changed while it was restored and was preserved".format(path))
            cls._cleanup_claimed_file(quarantine, post, path, errors)
        finally:
            if restore_path is not None and restore_expected is not None:
                cls._remove_owned_file(restore_path, restore_expected, errors)

    def rollback(self):
        errors = []
        for record in reversed(self.created_files):
            self._remove_owned_file(record["path"], record["expected"], errors)
        for record in reversed(list(self.support.values())):
            self._rollback_support_file(record, errors)
        for record in reversed(self.created_dirs):
            identity_descriptor = record.get("identityDescriptor")
            try:
                self._remove_owned_directory(
                    record["path"], record["identity"], errors,
                    identity_descriptor=identity_descriptor,
                )
            finally:
                if identity_descriptor is not None:
                    os.close(identity_descriptor)
                    record["identityDescriptor"] = None
        if errors:
            raise ControlCenterError(
                "Installer rollback was incomplete: {}".format("; ".join(errors[:12])),
                409,
            )

    def close(self):
        for record in self.created_dirs:
            identity_descriptor = record.get("identityDescriptor")
            if identity_descriptor is not None:
                try:
                    os.close(identity_descriptor)
                except OSError:
                    pass
                record["identityDescriptor"] = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def exclusive_copy_file(source, destination, journal=None):
    """Copy one new regular file without ever replacing an existing path."""
    source = Path(source)
    destination = Path(destination)
    source_flags = os.O_RDONLY | _BINARY_OPEN_FLAG
    destination_flags = (
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _BINARY_OPEN_FLAG
    )
    if hasattr(os, "O_NOFOLLOW"):
        source_flags |= os.O_NOFOLLOW
        destination_flags |= os.O_NOFOLLOW
    source_descriptor = None
    destination_descriptor = None
    destination_identity = None
    destination_path_identity = None
    destination_digest = hashlib.sha256()
    destination_size = 0
    destination_expected = None
    journal_record = None
    try:
        source_descriptor = os.open(str(source), source_flags)
        source_details = os.fstat(source_descriptor)
        if not stat.S_ISREG(source_details.st_mode):
            raise ControlCenterError(
                "Installer source must be a regular file: {}.".format(source), 409
            )
        try:
            destination_descriptor = os.open(
                str(destination), destination_flags, 0o600
            )
        except FileExistsError as exc:
            raise ControlCenterError(
                "Installer destination was created concurrently and was preserved: {}.".format(
                    destination
                ),
                409,
            ) from exc
        destination_details = os.fstat(destination_descriptor)
        destination_identity, _destination_opened_size = _descriptor_file_details(
            destination_descriptor
        )
        destination_path_details = os.lstat(str(destination))
        destination_path_identity = _stat_identity(destination_path_details)
        if (
            not stat.S_ISREG(destination_details.st_mode)
            or not stat.S_ISREG(destination_path_details.st_mode)
            or destination_path_details.st_nlink != 1
            or not _opened_path_matches(
                destination_path_details,
                destination_details,
                destination,
                destination_descriptor,
            )
        ):
            raise ControlCenterError(
                "Installer destination changed while it was opened and was preserved.",
                409,
            )
        if journal is not None:
            journal_record = journal.begin_created_file(
                destination, destination_path_details
            )
        while True:
            chunk = os.read(source_descriptor, 1024 * 1024)
            if not chunk:
                break
            offset = 0
            while offset < len(chunk):
                written = os.write(destination_descriptor, chunk[offset:])
                if written <= 0:
                    raise OSError("Installer copy made no forward progress.")
                destination_digest.update(chunk[offset:offset + written])
                destination_size += written
                offset += written
        if hasattr(os, "fchmod"):
            os.fchmod(destination_descriptor, stat.S_IMODE(source_details.st_mode))
        os.fsync(destination_descriptor)
        final_details = os.fstat(destination_descriptor)
        final_identity, final_size = _descriptor_file_details(
            destination_descriptor
        )
        if (
            not stat.S_ISREG(final_details.st_mode)
            or (
                not _WINDOWS_SPLIT_STAT_IDENTITIES
                and final_details.st_nlink != 1
            )
            or final_identity != destination_identity
            or final_size != destination_size
        ):
            raise ControlCenterError(
                "Installer destination changed while it was copied and was preserved.",
                409,
            )
        current_expected = ProjectMutationJournal._snapshot(
            destination,
            require_single_link=True,
            include_data=False,
        )["fingerprint"]
        current_path_details = os.lstat(str(destination))
        if (
            not _opened_path_matches(
                current_path_details,
                final_details,
                destination,
                destination_descriptor,
            )
            or (
                not _WINDOWS_SPLIT_STAT_IDENTITIES
                and _stat_identity(current_path_details)
                != destination_path_identity
            )
            or current_expected[2] != destination_size
            or current_expected[-1] != destination_digest.hexdigest()
        ):
            raise ControlCenterError(
                "Installer destination changed while it was copied and was preserved.",
                409,
            )
        os.close(destination_descriptor)
        destination_descriptor = None
        closed_expected = ProjectMutationJournal._snapshot(
            destination,
            require_single_link=True,
            include_data=False,
        )["fingerprint"]
        if closed_expected != current_expected:
            raise ControlCenterError(
                "Installer destination changed while it was copied and was preserved.",
                409,
            )
        destination_expected = closed_expected
        if journal is not None:
            journal.finish_created_file(journal_record, expected=destination_expected)
    except Exception as primary_error:
        if destination_descriptor is not None:
            try:
                partial_details = os.fstat(destination_descriptor)
                partial_identity, _partial_size = _descriptor_file_details(
                    destination_descriptor
                )
                if (
                    stat.S_ISREG(partial_details.st_mode)
                    and partial_identity == destination_identity
                ):
                    descriptor_still_owned = True
                else:
                    descriptor_still_owned = False
            except OSError:
                descriptor_still_owned = False
            if (
                descriptor_still_owned
                and destination_path_identity is not None
            ):
                try:
                    current = ProjectMutationJournal._snapshot(
                        destination,
                        require_single_link=True,
                        include_data=False,
                    )["fingerprint"]
                    current_path_details = os.lstat(str(destination))
                except (FileNotFoundError, ControlCenterError, OSError):
                    current = None
                if (
                    current is not None
                    and _opened_path_matches(
                        current_path_details,
                        partial_details,
                        destination,
                        destination_descriptor,
                    )
                    and (
                        _WINDOWS_SPLIT_STAT_IDENTITIES
                        or _stat_identity(current_path_details)
                        == destination_path_identity
                    )
                    and current[2] == destination_size
                    and current[-1] == destination_digest.hexdigest()
                ):
                    destination_expected = current
            try:
                os.close(destination_descriptor)
            except OSError:
                pass
            destination_descriptor = None
            if destination_expected is not None:
                try:
                    closed_expected = ProjectMutationJournal._snapshot(
                        destination,
                        require_single_link=True,
                        include_data=False,
                    )["fingerprint"]
                except (FileNotFoundError, ControlCenterError, OSError):
                    destination_expected = None
                else:
                    if closed_expected != destination_expected:
                        destination_expected = None
        cleanup_error = None
        if destination_expected is not None:
            cleanup_errors = []
            ProjectMutationJournal._remove_owned_file(
                destination, destination_expected, cleanup_errors
            )
            if cleanup_errors:
                cleanup_error = cleanup_errors[0]
        elif destination_identity is not None:
            try:
                current = os.lstat(str(destination))
            except FileNotFoundError:
                current = None
            except OSError as exc:
                current = None
                cleanup_error = "the partial destination could not be inspected: {}".format(
                    exc
                )
            if current is not None:
                cleanup_error = "the partial destination changed and was preserved"
        if cleanup_error:
            raise ControlCenterError(
                "{}. Copy cleanup was incomplete because {}.".format(
                    primary_error, cleanup_error
                ),
                getattr(primary_error, "status", 409),
            ) from primary_error
        raise
    finally:
        if source_descriptor is not None:
            os.close(source_descriptor)
        if destination_descriptor is not None:
            os.close(destination_descriptor)


class ProjectManager:
    def __init__(self, kit_root, store):
        self.kit_root = Path(kit_root).resolve()
        self.store = store
        self._project_lock = threading.RLock()
        self._port_lock = threading.Lock()
        self._allocated_ports = set()

    @staticmethod
    def _resolved_commit(repository_path, reference, label):
        resolved = run_command(
            ["git", "rev-parse", "--verify", "{}^{{commit}}".format(reference)],
            cwd=repository_path,
            check=False,
        )
        sha = resolved.stdout.strip() if resolved.returncode == 0 else ""
        if re.fullmatch(r"[0-9a-fA-F]{40,64}", sha) is None:
            raise ControlCenterError("{} could not be resolved safely.".format(label), 409)
        return sha.lower()

    @classmethod
    def _create_owned_worktree(cls, repository_path, worktree, branch, start_sha):
        """Create one generated worktree and return its immutable ownership record."""
        repository_path = Path(repository_path).resolve()
        worktree = Path(worktree)
        start_sha = str(start_sha or "").lower()
        if re.fullmatch(r"[0-9a-f]{40,64}", start_sha) is None:
            raise ControlCenterError("The worktree start commit is invalid.", 409)
        if worktree.exists() or worktree.is_symlink():
            raise ControlCenterError(
                "The generated worktree path already exists and was preserved.", 409
            )
        branch_ref = "refs/heads/{}".format(branch)
        existing = run_command(
            ["git", "show-ref", "--verify", "--quiet", branch_ref],
            cwd=repository_path,
            check=False,
        )
        if existing.returncode == 0:
            raise ControlCenterError(
                "The generated worktree branch already exists and was preserved.", 409
            )
        created_ref = run_command(
            [
                "git", "update-ref", branch_ref, start_sha,
                "0" * len(start_sha),
            ],
            cwd=repository_path,
            check=False,
        )
        if created_ref.returncode != 0:
            raise ControlCenterError(
                "The generated worktree branch was created concurrently and was preserved.",
                409,
            )
        try:
            run_command(
                ["git", "worktree", "add", str(worktree), branch],
                cwd=repository_path,
            )
            details = os.lstat(str(worktree))
            if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
                raise ControlCenterError(
                    "Git did not create the expected real worktree directory.", 409
                )
            identity = (details.st_dev, details.st_ino)
            head = cls._resolved_commit(worktree, "HEAD", "The new worktree HEAD")
            branch_sha = cls._resolved_commit(
                worktree, branch_ref, "The new worktree branch"
            )
            current = run_command(
                ["git", "branch", "--show-current"], cwd=worktree, check=False
            )
            status = run_command(
                [
                    "git", "--no-optional-locks", "status", "--porcelain=v1",
                    "--untracked-files=all", "--ignore-submodules=none",
                ],
                cwd=worktree,
                check=False,
            )
            if (
                head != start_sha
                or branch_sha != start_sha
                or current.returncode != 0
                or current.stdout.strip() != branch
                or status.returncode != 0
                or status.stdout
            ):
                raise ControlCenterError(
                    "The generated worktree changed during creation and was preserved.",
                    409,
                )
            return {
                "dev": identity[0],
                "ino": identity[1],
                "startSha": start_sha,
            }
        except Exception as primary_error:
            cleanup_note = None
            try:
                details = os.lstat(str(worktree))
                identity = (details.st_dev, details.st_ino)
            except OSError:
                identity = None
            if identity is not None:
                removed, reason = cls._remove_owned_worktree(
                    repository_path, worktree, branch, start_sha, identity
                )
                if not removed:
                    cleanup_note = reason
            elif not worktree.exists() and not worktree.is_symlink():
                branch_ref = "refs/heads/{}".format(branch)
                branch_result = run_command(
                    ["git", "rev-parse", "--verify", branch_ref + "^{commit}"],
                    cwd=repository_path,
                    check=False,
                )
                branch_sha = (
                    branch_result.stdout.strip().lower()
                    if branch_result.returncode == 0 else None
                )
                try:
                    branch_worktree = cls._worktree_for_branch(
                        repository_path, branch
                    )
                except ControlCenterError:
                    branch_worktree = Path("unsafe-unknown-worktree")
                if branch_sha == start_sha and branch_worktree is None:
                    deleted = run_command(
                        ["git", "update-ref", "-d", branch_ref, start_sha],
                        cwd=repository_path,
                        check=False,
                    )
                    if deleted.returncode != 0:
                        cleanup_note = "the partial generated branch changed and was preserved"
                elif branch_sha is not None:
                    cleanup_note = "the partial generated branch or worktree changed and was preserved"
            if cleanup_note:
                raise ControlCenterError(
                    "{}. Cleanup was intentionally stopped: {}".format(
                        primary_error, cleanup_note
                    ),
                    getattr(primary_error, "status", 409),
                ) from primary_error
            raise

    @classmethod
    def _remove_owned_worktree(
        cls, repository_path, worktree, branch, expected_sha, identity
    ):
        """Remove only an unchanged worktree that still has its recorded identity."""
        repository_path = Path(repository_path).resolve()
        worktree = Path(worktree)
        expected_sha = str(expected_sha or "").lower()
        if (
            not isinstance(identity, (tuple, list))
            or len(identity) != 2
            or re.fullmatch(r"[0-9a-f]{40,64}", expected_sha) is None
        ):
            return False, "worktree ownership metadata is incomplete"
        try:
            details = os.lstat(str(worktree))
        except FileNotFoundError:
            return False, "the recorded worktree path is already missing"
        except OSError as exc:
            return False, "the worktree identity could not be read: {}".format(exc)
        if (
            not stat.S_ISDIR(details.st_mode)
            or stat.S_ISLNK(details.st_mode)
            or (details.st_dev, details.st_ino) != tuple(identity)
        ):
            return False, "the worktree path no longer has its recorded identity"
        listed = cls._worktree_for_branch(repository_path, branch)
        if listed != worktree.resolve():
            return False, "Git no longer associates the expected branch with this worktree"
        try:
            head = cls._resolved_commit(worktree, "HEAD", "The worktree HEAD")
            branch_sha = cls._resolved_commit(
                worktree, "refs/heads/{}".format(branch), "The worktree branch"
            )
        except ControlCenterError as exc:
            return False, str(exc)
        operation = cls._git_operation_in_progress(worktree)
        status = run_command(
            [
                "git", "--no-optional-locks", "status", "--porcelain=v1",
                "--untracked-files=all", "--ignore-submodules=none",
            ],
            cwd=worktree,
            check=False,
        )
        if (
            head != expected_sha
            or branch_sha != expected_sha
            or operation
            or status.returncode != 0
            or status.stdout
        ):
            return False, "the worktree has changed or has a Git operation in progress"
        removed = run_command(
            ["git", "worktree", "remove", "--", str(worktree)],
            cwd=repository_path,
            check=False,
        )
        if removed.returncode != 0 or worktree.exists() or worktree.is_symlink():
            return False, "Git could not remove the unchanged worktree safely"
        deleted = run_command(
            [
                "git", "update-ref", "-d", "refs/heads/{}".format(branch),
                expected_sha,
            ],
            cwd=repository_path,
            check=False,
        )
        if deleted.returncode != 0:
            return False, "the generated branch changed and was preserved"
        return True, None

    @classmethod
    def _discard_owned_worktree(
        cls, repository_path, worktree, branch, identity
    ):
        """Destructively remove only the exact generated worktree recorded by a session."""
        repository_path = Path(repository_path).resolve()
        worktree = Path(worktree)
        if (
            not isinstance(branch, str)
            or not branch.startswith("webkit/")
            or not isinstance(identity, (tuple, list))
            or len(identity) != 2
            or not all(
                isinstance(value, int) and not isinstance(value, bool)
                for value in identity
            )
        ):
            return False, "worktree ownership metadata is incomplete"
        branch_ref = "refs/heads/{}".format(branch)
        valid_ref = run_command(
            ["git", "check-ref-format", branch_ref],
            cwd=repository_path,
            check=False,
        )
        if valid_ref.returncode != 0:
            return False, "the recorded worktree branch is invalid"
        try:
            details = os.lstat(str(worktree))
        except FileNotFoundError:
            return False, "the recorded worktree path is already missing"
        except OSError as exc:
            return False, "the worktree identity could not be read: {}".format(exc)
        if (
            not stat.S_ISDIR(details.st_mode)
            or stat.S_ISLNK(details.st_mode)
            or (details.st_dev, details.st_ino) != tuple(identity)
        ):
            return False, "the worktree path no longer has its recorded identity"
        try:
            associated = cls._worktree_for_branch(repository_path, branch)
        except ControlCenterError as exc:
            return False, str(exc)
        if associated != worktree.resolve():
            return False, "Git associates the recorded branch with another worktree"
        try:
            head = cls._resolved_commit(worktree, "HEAD", "The discard worktree HEAD")
            branch_sha = cls._resolved_commit(
                worktree, branch_ref, "The discard worktree branch"
            )
        except ControlCenterError as exc:
            return False, str(exc)
        current = run_command(
            ["git", "branch", "--show-current"], cwd=worktree, check=False
        )
        if (
            current.returncode != 0
            or current.stdout.strip() != branch
            or head != branch_sha
        ):
            return False, "the worktree branch or HEAD changed"
        operation = cls._git_operation_in_progress(worktree)
        if operation:
            return False, "the worktree has a Git operation in progress"
        removed = run_command(
            ["git", "worktree", "remove", "--force", "--", str(worktree)],
            cwd=repository_path,
            check=False,
        )
        if removed.returncode != 0 or worktree.exists() or worktree.is_symlink():
            return False, "Git could not remove the owned worktree safely"
        try:
            reassociated = cls._worktree_for_branch(repository_path, branch)
        except ControlCenterError as exc:
            return False, str(exc)
        if reassociated is not None:
            return False, "the generated branch became associated with another worktree"
        deleted = run_command(
            ["git", "update-ref", "-d", branch_ref, branch_sha],
            cwd=repository_path,
            check=False,
        )
        if deleted.returncode != 0:
            return False, "the generated branch changed and was preserved"
        remaining = run_command(
            ["git", "show-ref", "--verify", "--quiet", branch_ref],
            cwd=repository_path,
            check=False,
        )
        if remaining.returncode == 0:
            return False, "the generated branch was recreated and was preserved"
        return True, None

    @classmethod
    def _require_owned_worktree_at_sha(
        cls, repository_path, worktree, branch, expected_sha, identity, label
    ):
        """Require exact generated worktree ownership before irreversible integration."""
        repository_path = Path(repository_path).resolve()
        worktree = Path(worktree)
        expected_sha = str(expected_sha or "").lower()
        if (
            not isinstance(identity, (tuple, list))
            or len(identity) != 2
            or not all(isinstance(value, int) for value in identity)
            or re.fullmatch(r"[0-9a-f]{40,64}", expected_sha) is None
        ):
            raise ControlCenterError("{} ownership metadata is invalid.".format(label), 409)
        try:
            details = os.lstat(str(worktree))
        except OSError as exc:
            raise ControlCenterError(
                "{} identity could not be read: {}".format(label, exc), 409
            )
        if (
            not stat.S_ISDIR(details.st_mode)
            or stat.S_ISLNK(details.st_mode)
            or (details.st_dev, details.st_ino) != tuple(identity)
        ):
            raise ControlCenterError("{} ownership changed.".format(label), 409)
        head = cls._resolved_commit(worktree, "HEAD", "{} HEAD".format(label))
        branch_sha = cls._resolved_commit(
            worktree, "refs/heads/{}".format(branch), "{} branch".format(label)
        )
        current = run_command(
            ["git", "branch", "--show-current"], cwd=worktree, check=False
        )
        if (
            head != expected_sha
            or branch_sha != expected_sha
            or current.returncode != 0
            or current.stdout.strip() != branch
            or cls._git_operation_in_progress(worktree)
        ):
            raise ControlCenterError(
                "{} is detached, changed, or has a Git operation in progress.".format(
                    label
                ),
                409,
            )
        if cls._worktree_for_branch(repository_path, branch) != worktree.resolve():
            raise ControlCenterError(
                "{} Git worktree association changed.".format(label), 409
            )
        return True

    def system_status(self):
        return {
            "git": self._tool_status("git", ["git", "--version"]),
            "codex": self._tool_status("codex", ["codex", "--version"]),
            "claude": self._tool_status("claude", ["claude", "--version"]),
            "github": self._github_cli_status(),
            "platform": platform.system().lower(),
            "voiceTranscription": self._voice_engine_status(),
        }

    @staticmethod
    def _github_cli_status():
        executable = shutil.which("gh")
        if not executable:
            return {"installed": False, "authenticated": False, "path": None}
        result = run_command([executable, "auth", "status", "--hostname", "github.com"], check=False, timeout=15)
        return {
            "installed": True,
            "authenticated": result.returncode == 0,
            "path": executable,
        }

    @staticmethod
    def _voice_engine_status():
        ffmpeg = shutil.which("ffmpeg")
        whisper = shutil.which("whisper")
        whisper_cpp = shutil.which("whisper-cli")
        model = os.environ.get("WHISPER_MODEL")
        if whisper and ffmpeg:
            return {"available": True, "engine": "local Whisper"}
        if whisper_cpp and model and Path(model).is_file() and ffmpeg:
            return {"available": True, "engine": "local whisper.cpp"}
        if (whisper or whisper_cpp) and not ffmpeg:
            help_text = "Install ffmpeg so the local Whisper engine can decode browser audio."
        elif whisper_cpp and (not model or not Path(model).is_file()):
            help_text = "Set WHISPER_MODEL to an existing local whisper.cpp model file."
        else:
            help_text = "Install local Whisper before using agent voice notes."
        return {
            "available": False,
            "engine": None,
            "help": help_text,
        }

    @staticmethod
    def _tool_status(name, command):
        executable = shutil.which(name)
        if not executable:
            return {"installed": False, "path": None, "version": None}
        result = run_command(command, check=False, timeout=15)
        version = (result.stdout or result.stderr).strip().splitlines()
        version_text = version[0] if version else None
        installed = result.returncode == 0
        status = {
            "installed": installed,
            "path": executable,
            "version": version_text,
        }
        if name == "git" and installed:
            parsed = parsed_git_version(version_text)
            supported = parsed is not None and parsed >= MINIMUM_GIT_VERSION
            status.update({
                "installed": supported,
                "supported": supported,
                "minimumVersion": ".".join(
                    str(part) for part in MINIMUM_GIT_VERSION
                ),
            })
            if not supported:
                status["help"] = "Git 2.30 or newer is required."
        return status

    def validated_providers(self, providers):
        allowed = {"codex", "claude"}
        normalized = []
        for provider in providers or []:
            if provider in allowed and provider not in normalized:
                normalized.append(provider)
        if not normalized:
            raise ControlCenterError("Choose Codex, Claude Code, or both.")
        status = self.system_status()
        missing = [p for p in normalized if not status[p]["installed"]]
        if missing:
            raise ControlCenterError("Install the selected CLI first: {}".format(", ".join(missing)), 409)
        return normalized

    def save_providers(self, providers):
        normalized = self.validated_providers(providers)
        self.store.update(lambda state: state.update({"providers": normalized}))
        return normalized

    def list_projects(self):
        state = self.store.read()
        projects = []
        for project in state.get("projects", []):
            item = dict(project)
            item["exists"] = Path(project["path"]).is_dir()
            item["github"] = self.github_sync_status(project)
            try:
                config = load_webkit_config(
                    Path(project["path"]) / "webkit" / "webkit.config.json",
                    project["path"],
                )
                item["palette"] = [
                    {"slug": entry["slug"], "emoji": entry["emoji"]}
                    for entry in config["palette"]
                ]
                item.pop("configError", None)
            except ControlCenterError as exc:
                item["palette"] = []
                item["configError"] = str(exc)
            item["sessions"] = [
                public_session(s) for s in state.get("sessions", [])
                if s.get("projectId") == project["id"]
                and s.get("status") in ("active", "busy", "merging", "discarding", "error")
            ]
            projects.append(item)
        return projects

    def get_project(self, project_id):
        for project in self.store.read().get("projects", []):
            if project["id"] == project_id:
                return dict(project)
        raise ControlCenterError("Unknown project.", 404)

    def create_project(self, name, parent, provider, onboarding=None):
        with self._project_lock:
            return self._create_project(name, parent, provider, onboarding)

    def _create_project(self, name, parent, provider, onboarding=None):
        self._validate_provider(provider)
        self._require_git()
        if not (name or "").strip():
            raise ControlCenterError("Enter a website name.")
        if len(str(name).strip()) > 120:
            raise ControlCenterError("Website names must be 120 characters or shorter.")
        if not (parent or "").strip():
            raise ControlCenterError("Choose a parent folder for the new website.")
        safe_name = slugify(name)
        parent_path = Path(parent).expanduser().resolve()
        if not parent_path.is_dir():
            raise ControlCenterError("The selected parent folder does not exist.", 404)
        project_path = parent_path / safe_name
        if project_path.is_symlink():
            raise ControlCenterError("The project folder cannot be a symbolic link.", 409)
        if project_path.exists() and not project_path.is_dir():
            raise ControlCenterError("The project path must be a folder.", 409)
        if project_path.exists() and any(project_path.iterdir()):
            raise ControlCenterError("That project folder already exists and is not empty.", 409)
        restore_empty_directory = project_path.exists()
        empty_directory_backup = None
        if restore_empty_directory:
            empty_directory_backup = parent_path / ".{}-webkit-empty-{}".format(
                safe_name, uuid.uuid4().hex
            )
            try:
                project_path.rename(empty_directory_backup)
            except OSError as error:
                raise ControlCenterError(
                    "The empty project folder could not be prepared safely: {}".format(error),
                    409,
                )
        staging_path = parent_path / ".{}-webkit-create-{}".format(
            safe_name, uuid.uuid4().hex
        )
        staging_identity = None
        staging_journal = None
        published = False
        with self._port_lock:
            allocated_before = set(self._allocated_ports)
        try:
            staging_path.mkdir()
            staging_details = os.lstat(str(staging_path))
            staging_identity = (staging_details.st_dev, staging_details.st_ino)
            self._git_init(staging_path)
            staging_journal = ProjectMutationJournal(staging_path)
            staging_journal.record_git_directory()
            self._ensure_git_identity(staging_path)
            staging_journal.record_git_metadata()
            index = staging_path / "index.html"
            if not index.exists():
                index.write_text(self._starter_html(name.strip() or safe_name), encoding="utf-8")
            self._install_kit(
                staging_path, provider, config_repository_path=project_path
            )
            self._save_project_context(staging_path, onboarding or {})
            config = load_webkit_config(
                staging_path / "webkit" / "webkit.config.json",
                staging_path,
                require_default_page=True,
            )
            stage_without_private_runtime(staging_path, config)
            initial_sha = self._commit_validated_index(
                staging_path, "Create website with AWESOME WEBKIT"
            )
            staging_journal.record_initial_commit()
            rename_directory_noreplace(staging_path, project_path)
            published = True
            project = self._register(
                project_path, name.strip() or safe_name, provider,
                source_path=project_path, base_branch="main", target_branch="main", managed=False,
            )
        except Exception as primary_error:
            with self._port_lock:
                self._allocated_ports.intersection_update(allocated_before)
            recovery = []
            if not published and staging_path.exists():
                try:
                    details = os.lstat(str(staging_path))
                    staging_unchanged = (
                        stat.S_ISDIR(details.st_mode)
                        and staging_identity == (details.st_dev, details.st_ino)
                        and staging_journal is not None
                        and staging_journal.expected_head is not None
                        and staging_journal.owns_removable_git_directory()
                    )
                except (OSError, ControlCenterError):
                    staging_unchanged = False
                if staging_unchanged:
                    try:
                        shutil.rmtree(str(staging_path))
                    except (OSError, shutil.Error) as exc:
                        recovery.append(
                            "private staging folder could not be removed: {}".format(exc)
                        )
                else:
                    recovery.append(
                        "private staging folder was preserved at {} because it changed or was incomplete".format(
                            staging_path
                        )
                    )
            if restore_empty_directory and empty_directory_backup is not None:
                if not project_path.exists() and empty_directory_backup.exists():
                    try:
                        rename_directory_noreplace(empty_directory_backup, project_path)
                        empty_directory_backup = None
                    except OSError as exc:
                        recovery.append(
                            "the original empty folder could not be restored: {}".format(exc)
                        )
                elif empty_directory_backup.exists():
                    recovery.append(
                        "the original empty folder was preserved at {}".format(
                            empty_directory_backup
                        )
                    )
            if published:
                recovery.append(
                    "the committed local project was preserved at {}".format(project_path)
                )
            if recovery:
                raise ControlCenterError(
                    "{}. Recovery note: {}. Inspect these paths before retrying.".format(
                        primary_error, "; ".join(recovery)
                    ),
                    getattr(primary_error, "status", 409),
                ) from primary_error
            raise
        if empty_directory_backup is not None:
            try:
                empty_directory_backup.rmdir()
            except OSError:
                pass
            empty_directory_backup = None
        try:
            github_setup = self._ensure_github_repo(
                project_path, safe_name, "main", "main",
                validated_sha=initial_sha,
            )
        except Exception as exc:
            github_setup = {
                "connected": False,
                "remote": None,
                "url": None,
                "attempted": True,
                "pushed": False,
                "verified": False,
                "error": sanitize_git_error(
                    str(exc), "GitHub setup did not complete."
                ),
            }
        if isinstance(github_setup, dict):
            project["githubSetup"] = github_setup
        return project

    @staticmethod
    def _save_project_context(project_path, onboarding):
        if not isinstance(onboarding, dict):
            onboarding = {}
        brief = limited_text(
            onboarding.get("brief"), MAX_PROJECT_BRIEF_CHARS,
            "Brand and design brief",
        )
        assets = onboarding.get("assets") if isinstance(onboarding.get("assets"), list) else []
        if len(assets) > MAX_PROJECT_ASSETS:
            raise ControlCenterError(
                "Project references are limited to {} files.".format(MAX_PROJECT_ASSETS), 413
            )
        if not brief and not assets:
            return
        total = 0
        used_names = set()
        prepared = []
        for item in assets:
            if not isinstance(item, dict):
                raise ControlCenterError("Every project reference must be a file object.")
            try:
                data = base64.b64decode(item.get("data") or "", validate=True)
            except (ValueError, TypeError):
                raise ControlCenterError("A project reference was not valid base64 data.")
            total += len(data)
            if (
                len(data) > MAX_PROJECT_ASSET_BYTES
                or total > MAX_PROJECT_ASSETS_TOTAL_BYTES
            ):
                raise ControlCenterError(
                    "Project references must be 15 MB each and 20 MB total or smaller.", 413
                )
            raw_path = str(item.get("path") or item.get("name") or "reference").replace("\\", "/")
            raw_parts = [part for part in raw_path.split("/") if part not in ("", ".")]
            if not raw_parts or any(part == ".." for part in raw_parts):
                raise ControlCenterError("A project reference contained an unsafe folder path.")
            risk = secret_file_risk(raw_parts[-1], data)
            if risk:
                display_name = re.sub(r"[\x00-\x1f\x7f]+", "?", raw_parts[-1])[:120]
                raise ControlCenterError(
                    "Project reference {} looks like {} and was not added. Remove credentials and choose a safe reference file.".format(
                        display_name, risk
                    ),
                    409,
                )
            clean_parts = [
                re.sub(r"[^A-Za-z0-9._-]+", "-", part).strip("-.") or "reference"
                for part in raw_parts
            ]
            if any(
                part.split(".", 1)[0].lower() in WINDOWS_RESERVED_NAMES
                for part in clean_parts
            ):
                raise ControlCenterError(
                    "A project reference contained a Windows reserved device name."
                )
            name = clean_parts[-1]
            stem, suffix = Path(name).stem, Path(name).suffix
            candidate_parts = clean_parts
            candidate = "/".join(candidate_parts)
            index = 2
            while candidate.lower() in used_names:
                candidate_parts = clean_parts[:-1] + ["{}-{}{}".format(stem, index, suffix)]
                candidate = "/".join(candidate_parts)
                index += 1
            used_names.add(candidate.lower())
            prepared.append((candidate_parts, data))

        folder = Path(project_path) / "project-context"
        asset_folder = folder / "assets"
        folder.mkdir(parents=True, exist_ok=True)
        if brief:
            (folder / "BRAND-AND-DESIGN.md").write_text(
                "# Brand and design direction\n\n" + brief + "\n", encoding="utf-8"
            )
        for candidate_parts, data in prepared:
            target = asset_folder.joinpath(*candidate_parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

    def add_existing(self, path, provider, update_webkit=False):
        with self._project_lock:
            return self._add_existing(path, provider, update_webkit=update_webkit)

    def _add_existing(self, path, provider, update_webkit=False):
        self._validate_provider(provider)
        if not isinstance(update_webkit, bool):
            raise ControlCenterError("The Webkit update choice must be true or false.")
        self._require_git()
        if not (path or "").strip():
            raise ControlCenterError("Choose an existing project folder.")
        selected_path = Path(path).expanduser().resolve()
        if not selected_path.is_dir():
            raise ControlCenterError("Project folder does not exist.", 404)
        forbidden_targets = {
            Path(selected_path.anchor).resolve(),
            Path.home().resolve(),
            self.kit_root.resolve(),
            self.store.state_dir.resolve(),
        }
        if selected_path in forbidden_targets:
            raise ControlCenterError(
                "Choose a dedicated website folder, not a filesystem, home, kit, or Control Center state root.",
                409,
            )
        git_root = self._git_root(selected_path)
        if git_root is None:
            raise ControlCenterError(
                "Initialize Git in this folder and make at least one commit before adding it.",
                409,
            )
        if git_root and git_root != selected_path:
            raise ControlCenterError(
                "Choose the Git repository root, not a folder inside it: {}".format(git_root), 409
            )
        if run_command(
            ["git", "rev-parse", "--verify", "HEAD"], cwd=git_root, check=False
        ).returncode != 0:
            raise ControlCenterError(
                "This Git repository has no commits. Commit its current files first, then add it again.",
                409,
            )
        existing = self._registered_source(git_root)
        if existing:
            def update_provider(state):
                for project in state.get("projects", []):
                    if project["id"] == existing["id"]:
                        project["provider"] = provider
                        break
            self.store.update(update_provider)
            return self.get_project(existing["id"])
        project_path = None
        base_branch = None
        checkout_ownership = None
        project_id = uuid.uuid4().hex[:12]
        with self._port_lock:
            allocated_before = set(self._allocated_ports)
        try:
            (
                project_path, base_branch, target_branch, checkout_ownership,
            ) = self._create_controller_checkout(git_root, project_id)
            had_config = (project_path / "webkit" / "webkit.config.json").is_file()
            if had_config:
                self._reserve_config_ports(
                    project_path / "webkit" / "webkit.config.json"
                )
            version_update = self._existing_kit_update(project_path)
            if version_update is not None:
                if not update_webkit:
                    self._raise_existing_kit_update_required(version_update)
                self._upgrade_existing_kit(project_path, version_update)
            self._ensure_git_identity(project_path)
            changed = self._install_kit(project_path, provider)
            config = load_webkit_config(
                project_path / "webkit" / "webkit.config.json",
                project_path,
                require_default_page=True,
            )
            require_private_runtime_paths_safe(project_path, config)
            if changed:
                stage_without_private_runtime(project_path, config)
                staged = run_command(["git", "diff", "--cached", "--quiet"], cwd=project_path, check=False)
                if staged.returncode == 1:
                    self._commit_validated_index(
                        project_path,
                        "Update AWESOME WEBKIT to v{}".format(
                            version_update["requiredVersion"]
                        ) if version_update else "Install AWESOME WEBKIT",
                    )
                elif staged.returncode != 0:
                    raise ControlCenterError(
                        "The staged Webkit installation could not be inspected.", 409
                    )
            installed_sha = self._resolved_commit(
                project_path,
                "refs/heads/{}".format(base_branch),
                "The installed managed branch",
            )
            integration = self.integrate_managed_target({
                "path": str(project_path),
                "sourcePath": str(git_root),
                "baseBranch": base_branch,
                "targetBranch": target_branch,
                "managedCheckout": True,
            }, validated_sha=installed_sha, defer_if_target_busy=True)
            registered = self._register(
                project_path,
                git_root.name,
                provider,
                source_path=git_root,
                base_branch=base_branch,
                target_branch=target_branch,
                managed=True,
                project_id=project_id,
                source_integration_pending=integration.get("pending", False),
            )
            if version_update:
                registered["webkitUpdated"] = dict(version_update)
            return registered
        except Exception as primary_error:
            with self._port_lock:
                self._allocated_ports.intersection_update(allocated_before)
            if project_path is not None and checkout_ownership is not None:
                removed, reason = self._remove_owned_worktree(
                    git_root,
                    project_path,
                    base_branch,
                    checkout_ownership["startSha"],
                    (checkout_ownership["dev"], checkout_ownership["ino"]),
                )
                if not removed:
                    raise ControlCenterError(
                        "{}. The managed checkout was preserved because {}.".format(
                            primary_error, reason
                        ),
                        getattr(primary_error, "status", 409),
                        getattr(primary_error, "details", None),
                    ) from primary_error
            raise

    def _register(
        self, project_path, name, provider, source_path=None, base_branch="main",
        target_branch="main", managed=False, project_id=None,
        source_integration_pending=False,
    ):
        project_path = str(Path(project_path).resolve())
        source_path = str(Path(source_path or project_path).resolve())

        def mutate(state):
            for project in state.get("projects", []):
                if project["path"] == project_path or project.get("sourcePath") == source_path:
                    project["provider"] = provider
                    project["name"] = name
                    return dict(project)
            project = {
                "id": project_id or uuid.uuid4().hex[:12],
                "name": name,
                "slug": slugify(name),
                "path": project_path,
                "sourcePath": source_path,
                "baseBranch": base_branch,
                "targetBranch": target_branch,
                "managedCheckout": bool(managed),
                "sourceIntegrationPending": bool(source_integration_pending),
                "provider": provider,
                "createdAt": utc_now(),
            }
            state.setdefault("projects", []).append(project)
            return dict(project)

        return self.store.update(mutate)

    def _registered_source(self, source_path):
        source_path = str(Path(source_path).resolve())
        for project in self.store.read().get("projects", []):
            if project.get("sourcePath", project.get("path")) == source_path:
                return dict(project)
        return None

    def _create_controller_checkout(self, git_root, project_id):
        slug = slugify(Path(git_root).name)
        branch = "webkit/control-center/{}-{}".format(slug, project_id[:6])
        checkout = self.store.state_dir / "projects" / (slug + "-" + project_id[:6])
        checkout.parent.mkdir(parents=True, exist_ok=True)
        base_ref, target_branch = self._default_branch_ref(git_root)
        start_sha = self._resolved_commit(
            git_root, base_ref, "The selected repository branch"
        )
        ownership = self._create_owned_worktree(
            git_root, checkout, branch, start_sha
        )
        try:
            github = self.github_status(git_root)
            if github.get("connected"):
                upstream = "{}/{}".format(github["remote"], target_branch)
                configured = run_command(
                    ["git", "branch", "--set-upstream-to={}".format(upstream), branch],
                    cwd=checkout, check=False,
                )
                if configured.returncode != 0:
                    raise ControlCenterError(
                        "The managed checkout could not retain its selected GitHub upstream.",
                        409,
                    )
            return checkout.resolve(), branch, target_branch, ownership
        except Exception as primary_error:
            removed, reason = self._remove_owned_worktree(
                git_root,
                checkout,
                branch,
                ownership["startSha"],
                (ownership["dev"], ownership["ino"]),
            )
            if not removed:
                raise ControlCenterError(
                    "{}. Cleanup was intentionally stopped: {}".format(
                        primary_error, reason
                    ),
                    getattr(primary_error, "status", 409),
                ) from primary_error
            raise

    @staticmethod
    def _worktree_for_branch(repository_path, branch):
        listed = run_command(
            ["git", "worktree", "list", "--porcelain"],
            cwd=repository_path, check=False,
        )
        if listed.returncode != 0:
            raise ControlCenterError("Could not inspect repository worktrees.", 409)
        wanted = "refs/heads/{}".format(branch)
        for record in listed.stdout.split("\n\n"):
            values = {}
            for line in record.splitlines():
                key, separator, value = line.partition(" ")
                if separator:
                    values[key] = value
            if values.get("branch") == wanted and values.get("worktree"):
                return Path(values["worktree"]).resolve()
        return None

    def _set_source_integration_pending(self, project, pending):
        project_id = project.get("id")
        if not project_id:
            return

        def mutate(state):
            for registered in state.get("projects", []):
                if registered.get("id") == project_id:
                    registered["sourceIntegrationPending"] = bool(pending)
                    break

        self.store.update(mutate)

    def integrate_managed_target(
        self, project, validated_sha=None, defer_if_target_busy=False
    ):
        """Fast-forward the real local target for a managed existing project."""
        if not project.get("managedCheckout"):
            return {"integrated": False, "managed": False}
        managed_path = Path(project["path"])
        repository_path = Path(project.get("sourcePath") or managed_path)
        base_branch = project.get("baseBranch", "main")
        target_branch = project.get("targetBranch", "main")
        branch_ref = "refs/heads/{}".format(base_branch)
        branch_sha = self._resolved_commit(
            managed_path, branch_ref, "The managed Control Center branch"
        )
        if validated_sha is None:
            base_sha = branch_sha
        else:
            base_sha = str(validated_sha or "").lower()
            if (
                re.fullmatch(r"[0-9a-f]{40,64}", base_sha) is None
                or branch_sha != base_sha
            ):
                raise ControlCenterError(
                    "The managed Control Center branch changed after validation.", 409
                )
        target_ref = "refs/heads/{}".format(target_branch)
        target = run_command(
            ["git", "rev-parse", "--verify", target_ref + "^{commit}"],
            cwd=managed_path, check=False,
        )
        target_sha = target.stdout.strip() if target.returncode == 0 else ""
        target_worktree = self._worktree_for_branch(
            repository_path, target_branch
        )
        if not target_sha:
            if target_worktree is not None:
                raise ControlCenterError(
                    "The local target worktree has no resolvable branch commit.", 409
                )
            created = run_command(
                ["git", "update-ref", target_ref, base_sha, "0" * len(base_sha)],
                cwd=managed_path, check=False,
            )
            if created.returncode != 0:
                raise ControlCenterError(
                    "The local target branch could not be created atomically.", 409
                )
            if self._resolved_commit(
                managed_path, target_ref, "The local target branch"
            ) != base_sha:
                raise ControlCenterError(
                    "The local target branch did not retain the validated commit.",
                    409,
                )
            self._set_source_integration_pending(project, False)
            return {
                "integrated": True, "pending": False,
                "target": target_branch, "sha": base_sha,
            }
        ancestor = run_command(
            ["git", "merge-base", "--is-ancestor", target_sha, base_sha],
            cwd=managed_path, check=False,
        )
        if ancestor.returncode != 0:
            raise ControlCenterError(
                "The local {} branch advanced or diverged from the managed checkout. Integrate those commits before retrying.".format(
                    target_branch
                ),
                409,
            )
        if target_sha == base_sha:
            self._set_source_integration_pending(project, False)
            return {
                "integrated": False, "pending": False,
                "target": target_branch, "sha": base_sha,
            }
        if target_worktree is None:
            advanced = run_command(
                ["git", "update-ref", target_ref, base_sha, target_sha],
                cwd=managed_path, check=False,
            )
            if advanced.returncode != 0:
                raise ControlCenterError(
                    "The local target branch changed while it was being integrated.",
                    409,
                )
            if self._resolved_commit(
                managed_path, target_ref, "The local target branch"
            ) != base_sha:
                raise ControlCenterError(
                    "The local target branch did not retain the validated commit.",
                    409,
                )
        else:
            if self._git_operation_in_progress(target_worktree):
                if defer_if_target_busy:
                    self._set_source_integration_pending(project, True)
                    return {
                        "integrated": False, "pending": True,
                        "reason": "operation", "target": target_branch,
                        "sha": base_sha,
                    }
                raise ControlCenterError(
                    "The local target worktree already has a Git operation in progress.",
                    409,
                )
            status = run_command(
                [
                    "git", "--no-optional-locks", "status", "--porcelain=v1",
                    "--untracked-files=all", "--ignore-submodules=none",
                ],
                cwd=target_worktree, check=False,
            )
            if status.returncode != 0:
                raise ControlCenterError(
                    "The local target worktree could not be inspected before integration.",
                    409,
                )
            if status.stdout:
                if defer_if_target_busy:
                    self._set_source_integration_pending(project, True)
                    return {
                        "integrated": False, "pending": True,
                        "reason": "dirty", "target": target_branch,
                        "sha": base_sha,
                    }
                raise ControlCenterError(
                    "Commit or stash changes in the local target worktree before integration.",
                    409,
                )
            target_head = self._resolved_commit(
                target_worktree, "HEAD", "The local target worktree HEAD"
            )
            current_branch = run_command(
                ["git", "branch", "--show-current"],
                cwd=target_worktree,
                check=False,
            )
            if (
                target_head != target_sha
                or current_branch.returncode != 0
                or current_branch.stdout.strip() != target_branch
            ):
                raise ControlCenterError(
                    "The local target worktree changed before integration.", 409
                )
            merged = run_command(
                ["git", "merge", "--ff-only", "--", base_sha],
                cwd=target_worktree, check=False,
            )
            if merged.returncode != 0:
                raise ControlCenterError(
                    "The local target branch changed while it was being integrated.",
                    409,
                )
            after_head = self._resolved_commit(
                target_worktree, "HEAD", "The integrated target worktree HEAD"
            )
            after_ref = self._resolved_commit(
                target_worktree, target_ref, "The integrated target branch"
            )
            after = run_command(
                [
                    "git", "--no-optional-locks", "status", "--porcelain=v1",
                    "--untracked-files=all", "--ignore-submodules=none",
                ],
                cwd=target_worktree, check=False,
            )
            if (
                after_head != base_sha
                or after_ref != base_sha
                or after.returncode != 0
                or after.stdout
            ):
                raise ControlCenterError(
                    "The local target worktree changed during integration and was preserved.",
                    409,
                )
        if self._resolved_commit(
            managed_path, branch_ref, "The managed Control Center branch"
        ) != base_sha:
            raise ControlCenterError(
                "The managed Control Center branch changed during target integration.",
                409,
            )
        self._set_source_integration_pending(project, False)
        return {
            "integrated": True, "pending": False,
            "target": target_branch, "sha": base_sha,
        }

    @classmethod
    def _default_branch_ref(cls, path):
        github = cls.github_status(path)
        if github["connected"]:
            remote = github["remote"]
            symbolic = run_command(
                ["git", "symbolic-ref", "--quiet", "--short", "refs/remotes/{}/HEAD".format(remote)],
                cwd=path,
                check=False,
            )
            prefix = remote + "/"
            if symbolic.returncode == 0 and symbolic.stdout.strip().startswith(prefix):
                target = symbolic.stdout.strip()[len(prefix):]
                local = run_command(
                    ["git", "rev-parse", "--verify", "--quiet", target], cwd=path, check=False
                )
                return (target if local.returncode == 0 else symbolic.stdout.strip()), target
        for ref, target in (
            ("main", "main"),
            ("master", "master"),
            ("origin/main", "main"),
            ("origin/master", "master"),
        ):
            exists = run_command(
                ["git", "rev-parse", "--verify", "--quiet", ref], cwd=path, check=False
            )
            if exists.returncode == 0:
                return ref, target
        current = run_command(
            ["git", "branch", "--show-current"], cwd=path, check=False
        ).stdout.strip()
        if current:
            return current, current
        raise ControlCenterError(
            "Could not determine this repository's default branch. Check out a named branch first.", 409
        )

    @staticmethod
    def _validated_github_remote(path, remote):
        if not valid_git_remote_name(remote):
            raise ControlCenterError("The configured Git remote name is unsafe.", 409)
        fetched = run_command(
            ["git", "remote", "get-url", "--", remote], cwd=path, check=False
        )
        pushed = run_command(
            ["git", "remote", "get-url", "--push", "--", remote],
            cwd=path, check=False,
        )
        fetch_url = fetched.stdout.strip() if fetched.returncode == 0 else ""
        push_url = pushed.stdout.strip() if pushed.returncode == 0 else ""
        fetch_identity = github_repository_identity(fetch_url)
        push_identity = github_repository_identity(push_url)
        pinned_fetch_url = safe_pinned_remote_url(fetch_url)
        pinned_url = safe_pinned_remote_url(push_url)
        if not fetch_identity:
            raise ControlCenterError("The Git remote is not an exact GitHub repository.", 409)
        if not push_identity or push_identity != fetch_identity:
            raise ControlCenterError(
                "The Git remote push destination does not match its GitHub fetch repository.",
                409,
            )
        if pinned_fetch_url is None:
            raise ControlCenterError(
                "The GitHub fetch URL must not contain embedded credentials, query data, or unsafe characters.",
                409,
            )
        if pinned_url is None:
            raise ControlCenterError(
                "The GitHub push URL must not contain embedded credentials, query data, or unsafe characters.",
                409,
            )
        return {
            "remote": remote,
            "url": sanitize_remote_url(fetch_url),
            "fetchUrl": pinned_fetch_url,
            "pushUrl": pinned_url,
            "repository": fetch_identity,
        }

    @classmethod
    def github_status(cls, path):
        if not Path(path).is_dir():
            return {"connected": False, "remote": None, "url": None}
        remotes = run_command(["git", "remote"], cwd=path, check=False)
        if remotes.returncode != 0:
            return {"connected": False, "remote": None, "url": None}
        candidates = []
        errors = []
        for remote in remotes.stdout.splitlines():
            remote = remote.strip()
            if not valid_git_remote_name(remote):
                continue
            try:
                candidates.append(cls._validated_github_remote(path, remote))
            except ControlCenterError as exc:
                errors.append(str(exc))
        if not candidates:
            result = {"connected": False, "remote": None, "url": None}
            if errors:
                result["error"] = errors[0]
            return result
        upstream = run_command(
            [
                "git", "rev-parse", "--abbrev-ref", "--symbolic-full-name",
                "@{upstream}",
            ],
            cwd=path, check=False,
        )
        upstream_name = upstream.stdout.strip() if upstream.returncode == 0 else ""
        if upstream_name:
            upstream_remote = upstream_name.split("/", 1)[0]
            matched = [
                candidate for candidate in candidates
                if candidate["remote"] == upstream_remote
            ]
            if len(matched) == 1:
                return dict(matched[0], connected=True)
        if len(candidates) == 1:
            return dict(candidates[0], connected=True)
        return {
            "connected": False,
            "remote": None,
            "url": None,
            "error": "Multiple GitHub remotes are configured and no unique upstream remote is selected.",
        }

    def github_sync_status(self, project):
        """Describe local commits waiting to land on the project's GitHub target branch."""
        project_path = Path(project["path"])
        status = self.github_status(project_path)
        status.update({
            "ahead": 0,
            "behind": 0,
            "unpushed": False,
            "branch": project.get("baseBranch", "main"),
            "targetBranch": project.get("targetBranch", "main"),
        })
        if not status["connected"]:
            return status
        local_ref = status["branch"]
        remote_ref = "{}/{}".format(status["remote"], status["targetBranch"])
        counts = run_command(
            ["git", "rev-list", "--left-right", "--count", "{}...{}".format(remote_ref, local_ref)],
            cwd=project_path, check=False,
        )
        if counts.returncode != 0:
            # A connected but empty/new remote may not have a tracking ref yet.
            local_count = run_command(
                ["git", "rev-list", "--count", local_ref], cwd=project_path, check=False
            )
            if local_count.returncode == 0:
                status["ahead"] = int(local_count.stdout.strip() or "0")
                status["unpushed"] = status["ahead"] > 0
            return status
        parts = counts.stdout.strip().split()
        if len(parts) == 2:
            status["behind"], status["ahead"] = (int(parts[0]), int(parts[1]))
            status["unpushed"] = status["ahead"] > 0
        return status

    @classmethod
    def _verified_push(
        cls, project_path, remote, branch, target_branch, set_upstream=False,
        validated_sha=None, validated_push_url=None, validated_remote_sha=None,
    ):
        if not valid_git_remote_name(remote):
            return {
                "pushed": False,
                "verified": False,
                "error": "The configured Git remote name is unsafe.",
            }
        try:
            destination = cls._validated_github_remote(project_path, remote)
        except ControlCenterError as exc:
            return {"pushed": False, "verified": False, "error": str(exc)}
        if validated_push_url and destination["pushUrl"] != validated_push_url:
            return {
                "pushed": False,
                "verified": False,
                "error": "The GitHub push destination changed during validation.",
            }
        if validated_sha is None:
            local = run_command(
                [
                    "git", "rev-parse", "--verify",
                    "refs/heads/{}^{{commit}}".format(branch),
                ],
                cwd=project_path, check=False,
            )
            validated_sha = local.stdout.strip() if local.returncode == 0 else ""
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", str(validated_sha or "")):
            return {
                "pushed": False,
                "verified": False,
                "error": "The local branch could not be pinned to an immutable commit.",
            }
        validated_sha = str(validated_sha).lower()
        if validated_remote_sha is not None:
            validated_remote_sha = str(validated_remote_sha).lower()
            if re.fullmatch(r"[0-9a-f]{40,64}", validated_remote_sha) is None:
                return {
                    "pushed": False,
                    "verified": False,
                    "error": "The remote branch lease is invalid.",
                }
            ancestor = run_command(
                [
                    "git", "merge-base", "--is-ancestor",
                    validated_remote_sha, validated_sha,
                ],
                cwd=project_path,
                check=False,
            )
            if ancestor.returncode != 0:
                return {
                    "pushed": False,
                    "verified": False,
                    "error": "The validated push is not a fast-forward update.",
                }
        remote_ref = "refs/heads/{}".format(target_branch)
        command = [
            "git", "push",
            "--force-with-lease={}:{}".format(
                remote_ref, validated_remote_sha or ""
            ),
            "--", destination["pushUrl"],
            "{}:{}".format(validated_sha, remote_ref),
        ]
        pushed = run_command(command, cwd=project_path, check=False, timeout=120)
        if pushed.returncode != 0:
            return {
                "pushed": False,
                "verified": False,
                "error": sanitize_git_error(
                    pushed.stderr or pushed.stdout, "GitHub push failed."
                ),
            }
        remote_head = run_command(
            [
                "git", "ls-remote", "--exit-code", "--",
                destination["pushUrl"], remote_ref,
            ],
            cwd=project_path, check=False, timeout=120,
        )
        remote_sha = ""
        if remote_head.returncode == 0:
            for line in remote_head.stdout.splitlines():
                fields = line.split()
                if len(fields) == 2 and fields[1] == remote_ref:
                    remote_sha = fields[0]
                    break
        if remote_sha != validated_sha:
            return {
                "pushed": False,
                "verified": False,
                "error": "The remote branch could not be verified at the local commit.",
            }
        local_after = run_command(
            [
                "git", "rev-parse", "--verify",
                "refs/heads/{}^{{commit}}".format(branch),
            ],
            cwd=project_path,
            check=False,
        )
        if (
            local_after.returncode != 0
            or local_after.stdout.strip().lower() != validated_sha
        ):
            return {
                "pushed": False,
                "verified": False,
                "error": "The local branch changed during the validated push; the remote received only the pinned commit.",
            }
        tracking_ref = "refs/remotes/{}/{}".format(remote, target_branch)
        run_command(
            ["git", "update-ref", tracking_ref, validated_sha],
            cwd=project_path, check=False,
        )
        if set_upstream:
            run_command(
                [
                    "git", "branch", "--set-upstream-to={}/{}".format(
                        remote, target_branch
                    ), branch,
                ],
                cwd=project_path, check=False,
            )
        return {"pushed": True, "verified": True, "error": None}

    def _ensure_github_repo(
        self, project_path, repo_name, branch, target_branch="main",
        validated_sha=None,
    ):
        status = self.github_status(project_path)
        if status["connected"]:
            result = self._validated_push_boundary(
                project_path, status["remote"], branch, target_branch,
                validated_sha=validated_sha,
            )
            return dict(status, attempted=True, **result)
        if status.get("error"):
            return dict(
                status, attempted=False, pushed=False, verified=False
            )
        cli = self._github_cli_status()
        if not cli["authenticated"]:
            return dict(
                status, attempted=False, pushed=False, verified=False, error=None
            )
        existing = run_command(["git", "remote"], cwd=project_path, check=False).stdout.splitlines()
        remote = "github" if "origin" in existing else "origin"
        created = run_command(
            [cli["path"], "repo", "create", slugify(repo_name), "--private", "--source", ".", "--remote", remote],
            cwd=project_path, check=False, timeout=120,
        )
        if created.returncode != 0:
            return dict(
                status,
                attempted=True,
                pushed=False,
                verified=False,
                error=sanitize_git_error(
                    created.stderr or created.stdout,
                    "GitHub repository creation failed.",
                ),
            )
        result = self._validated_push_boundary(
            project_path, remote, branch, target_branch, set_upstream=True,
            validated_sha=validated_sha,
        )
        connected = self.github_status(project_path)
        return dict(connected, attempted=True, **result)

    @classmethod
    def _require_remote_tree_private(
        cls, project_path, remote_ref, current_config
    ):
        roots = private_runtime_roots(current_config)
        merge_base = run_command(
            ["git", "merge-base", "HEAD", remote_ref],
            cwd=project_path, check=False,
        )
        if merge_base.returncode != 0 or not re.fullmatch(
            r"[0-9a-fA-F]{40,64}", merge_base.stdout.strip()
        ):
            raise ControlCenterError(
                "The GitHub branch has no safe merge base with this project.", 409
            )
        base_sha = merge_base.stdout.strip()
        config_history = run_command(
            [
                "git", "log", "--format=%H", "--reverse",
                "--max-count={}".format(MAX_OUTGOING_CONFIG_CHANGES + 1),
                "{}..{}".format(base_sha, remote_ref), "--",
                _literal_git_pathspec("webkit/webkit.config.json"),
            ],
            cwd=project_path, check=False,
        )
        if config_history.returncode != 0:
            raise ControlCenterError(
                "Could not inspect the GitHub Webkit config history.", 409
            )
        config_commits = [
            line.strip() for line in config_history.stdout.splitlines()
            if line.strip()
        ]
        if len(config_commits) > MAX_OUTGOING_CONFIG_CHANGES:
            raise ControlCenterError(
                "The GitHub Webkit config history has too many changes to validate safely.",
                409,
            )
        for root in cls._historical_feedback_roots(
            project_path, [base_sha, remote_ref] + config_commits
        ):
            if root not in roots:
                roots.append(root)
        for root in roots:
            touched = run_command(
                [
                    "git", "log", "-1", "--format=%H",
                    "{}..{}".format(base_sha, remote_ref), "--",
                    _literal_git_pathspec(root),
                ],
                cwd=project_path, check=False,
            )
            if touched.returncode != 0:
                raise ControlCenterError(
                    "Could not inspect GitHub private runtime history.", 409
                )
            if touched.stdout.strip():
                raise ControlCenterError(
                    "The GitHub branch history touched private Webkit runtime path {}. Sync was refused.".format(
                        root
                    ),
                    409,
                )
            exists = run_command(
                ["git", "cat-file", "-e", "{}:{}".format(remote_ref, root)],
                cwd=project_path, check=False,
            )
            if exists.returncode == 0:
                raise ControlCenterError(
                    "The GitHub branch tracks private Webkit runtime path {}. Sync was refused.".format(
                        root
                    ),
                    409,
                )

    def sync_from_github(self, project):
        with self._project_lock:
            return self._sync_from_github(project)

    @staticmethod
    def _git_operation_in_progress(project_path):
        for reference in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD"):
            present = run_command(
                ["git", "rev-parse", "--verify", "--quiet", reference],
                cwd=project_path, check=False,
            )
            if present.returncode == 0:
                return reference
        for marker in ("rebase-merge", "rebase-apply", "sequencer", "BISECT_LOG"):
            resolved = run_command(
                ["git", "rev-parse", "--git-path", marker],
                cwd=project_path, check=False,
            )
            if resolved.returncode == 0 and resolved.stdout.strip():
                marker_path = Path(resolved.stdout.strip())
                if not marker_path.is_absolute():
                    marker_path = Path(project_path) / marker_path
                if marker_path.exists():
                    return marker
        return None

    def _sync_from_github(self, project):
        project_path = Path(project["path"])
        current_config = load_webkit_config(
            project_path / "webkit" / "webkit.config.json",
            project_path,
            require_default_page=True,
        )
        status = self.github_status(project_path)
        if not status["connected"]:
            return {
                "connected": False,
                "synced": False,
                "error": status.get("error"),
            }
        if not valid_git_remote_name(status.get("remote")):
            return {
                "connected": True,
                "synced": False,
                "error": "The configured Git remote name is unsafe.",
            }
        require_private_runtime_paths_safe(project_path, current_config)
        if private_safe_git_status(project_path, current_config).stdout.strip():
            return {
                "connected": True,
                "synced": False,
                "error": "The controller checkout has uncommitted changes.",
            }
        operation = self._git_operation_in_progress(project_path)
        if operation:
            return {
                "connected": True,
                "synced": False,
                "error": "Git operation {} is already in progress; sync was not started.".format(
                    operation
                ),
            }
        target = project.get("targetBranch", "main")
        remote_ref = "refs/heads/{}".format(target)
        advertised = run_command(
            [
                "git", "ls-remote", "--exit-code", "--",
                status["fetchUrl"], remote_ref,
            ],
            cwd=project_path, check=False, timeout=120,
        )
        fields = advertised.stdout.split() if advertised.returncode == 0 else []
        if (
            advertised.returncode != 0
            or len(fields) < 2
            or fields[1] != remote_ref
            or re.fullmatch(r"[0-9a-fA-F]{40,64}", fields[0]) is None
        ):
            return {
                "connected": True,
                "synced": False,
                "error": sanitize_git_error(
                    advertised.stderr or advertised.stdout,
                    "The GitHub target branch could not be resolved safely.",
                ),
            }
        advertised_sha = fields[0]
        validation_ref = "refs/awesome-webkit/sync/{}".format(uuid.uuid4().hex)
        local_sha = self._resolved_commit(
            project_path, "HEAD", "The controller checkout HEAD"
        )
        try:
            fetched = run_command(
                [
                    "git", "fetch", "--no-tags", "--no-write-fetch-head", "--",
                    status["fetchUrl"],
                    "+{}:{}".format(remote_ref, validation_ref),
                ],
                cwd=project_path, check=False, timeout=120,
            )
            if fetched.returncode != 0:
                return {
                    "connected": True,
                    "synced": False,
                    "error": sanitize_git_error(fetched.stderr or fetched.stdout),
                }
            remote_sha = self._resolved_commit(
                project_path, validation_ref, "The fetched GitHub target"
            )
            if remote_sha != advertised_sha.lower():
                return {
                    "connected": True,
                    "synced": False,
                    "error": "The GitHub target branch changed during sync. Retry.",
                }
            if self._resolved_commit(
                project_path, "HEAD", "The controller checkout HEAD"
            ) != local_sha or private_safe_git_status(
                project_path, current_config
            ).stdout.strip():
                return {
                    "connected": True,
                    "synced": False,
                    "error": "The controller checkout changed during GitHub fetch.",
                }
            remote_is_ancestor = run_command(
                ["git", "merge-base", "--is-ancestor", remote_sha, local_sha],
                cwd=project_path, check=False,
            )
            if remote_is_ancestor.returncode == 0:
                self._require_remote_tree_private(
                    project_path, remote_sha, current_config,
                )
                if self._resolved_commit(
                    project_path, "HEAD", "The controller checkout HEAD"
                ) != local_sha or private_safe_git_status(
                    project_path, current_config
                ).stdout.strip():
                    raise ControlCenterError(
                        "The controller checkout changed during GitHub sync.", 409
                    )
                tracked = run_command(
                    [
                        "git", "update-ref",
                        "refs/remotes/{}/{}".format(status["remote"], target),
                        remote_sha,
                    ],
                    cwd=project_path, check=False,
                )
                if tracked.returncode != 0:
                    raise ControlCenterError(
                        "The validated GitHub tracking reference could not be updated.",
                        409,
                    )
                return {"connected": True, "synced": True}
            self._require_remote_tree_private(
                project_path, remote_sha, current_config
            )
            merged = run_command(
                [
                    "git", "merge", "--no-commit", "--no-ff", "--no-edit", "--",
                    remote_sha,
                ],
                cwd=project_path, check=False, timeout=120,
            )
            if merged.returncode != 0:
                return {
                    "connected": True,
                    "synced": False,
                    "error": sanitize_git_error(
                        merged.stderr or merged.stdout,
                        "GitHub sync needs manual recovery; its merge state was preserved.",
                    ),
                }
            merged_config = load_webkit_config(
                project_path / "webkit" / "webkit.config.json",
                project_path,
                require_default_page=True,
            )
            require_private_runtime_paths_safe(project_path, merged_config)
            try:
                merged_sha = self._commit_validated_index(
                    project_path,
                    "Sync GitHub {} into AWESOME WEBKIT".format(target),
                )
            except ControlCenterError as exc:
                return {
                    "connected": True,
                    "synced": False,
                    "error": "The validated GitHub merge could not be committed; recovery state was preserved: {}".format(
                        exc
                    ),
                }
            final_config = load_webkit_config(
                project_path / "webkit" / "webkit.config.json",
                project_path,
                require_default_page=True,
            )
            require_private_runtime_paths_safe(project_path, final_config)
            if (
                self._resolved_commit(
                    project_path, "HEAD", "The synced controller checkout HEAD"
                ) != merged_sha
                or private_safe_git_status(project_path, final_config).stdout.strip()
            ):
                raise ControlCenterError(
                    "The controller checkout changed during GitHub sync; it was preserved for recovery.",
                    409,
                )
            tracked = run_command(
                [
                    "git", "update-ref",
                    "refs/remotes/{}/{}".format(status["remote"], target),
                    remote_sha,
                ],
                cwd=project_path, check=False,
            )
            if tracked.returncode != 0:
                raise ControlCenterError(
                    "The validated GitHub tracking reference could not be updated.",
                    409,
                )
            return {"connected": True, "synced": True}
        finally:
            run_command(
                ["git", "update-ref", "-d", validation_ref],
                cwd=project_path, check=False,
            )

    @staticmethod
    def _historical_feedback_roots(project_path, references):
        roots = []
        total_bytes = 0
        for reference in references:
            spec = "{}:webkit/webkit.config.json".format(reference)
            size = run_command(
                ["git", "cat-file", "-s", spec], cwd=project_path, check=False
            )
            if size.returncode != 0:
                continue
            try:
                blob_size = int(size.stdout.strip())
            except ValueError:
                blob_size = MAX_WEBKIT_CONFIG_BYTES + 1
            if blob_size > MAX_WEBKIT_CONFIG_BYTES:
                raise ControlCenterError(
                    "An outgoing Webkit config is too large to validate safely.", 409
                )
            total_bytes += blob_size
            if total_bytes > MAX_OUTGOING_CONFIG_TOTAL_BYTES:
                raise ControlCenterError(
                    "Outgoing Webkit config history is too large to validate safely.", 409
                )
            try:
                shown = subprocess.run(
                    ["git", "cat-file", "blob", spec],
                    cwd=str(project_path),
                    capture_output=True,
                    timeout=30,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ControlCenterError(
                    "Could not read outgoing Webkit config history: {}".format(exc),
                    409,
                )
            if shown.returncode != 0 or len(shown.stdout) != blob_size:
                raise ControlCenterError(
                    "Could not read an outgoing Webkit config safely.", 409
                )
            try:
                historical = strict_json_loads(shown.stdout.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise ControlCenterError(
                    "An outgoing Webkit config is invalid: {}".format(exc), 409
                )
            if not isinstance(historical, dict):
                raise ControlCenterError(
                    "An outgoing Webkit config must be a JSON object.", 409
                )
            for root in private_runtime_roots(historical):
                if root not in roots:
                    roots.append(root)
        return roots

    @classmethod
    def _require_outgoing_history_private(
        cls, project_path, destination_url, branch, target, current_config,
        validated_sha=None,
    ):
        project_path = Path(project_path)
        if safe_pinned_remote_url(destination_url) != destination_url:
            raise ControlCenterError("The GitHub push destination is unsafe.", 409)
        local = run_command(
            ["git", "rev-parse", "--verify", "refs/heads/{}^{{commit}}".format(branch)],
            cwd=project_path, check=False,
        )
        if local.returncode != 0 or not re.fullmatch(r"[0-9a-fA-F]{40,64}", local.stdout.strip()):
            raise ControlCenterError("The managed branch could not be resolved safely.", 409)
        branch_sha = local.stdout.strip().lower()
        if validated_sha is None:
            local_sha = branch_sha
        else:
            local_sha = str(validated_sha or "").lower()
            if (
                re.fullmatch(r"[0-9a-f]{40,64}", local_sha) is None
                or branch_sha != local_sha
            ):
                raise ControlCenterError(
                    "The managed branch changed after immutable validation.", 409
                )
        remote_ref = "refs/heads/{}".format(target)
        advertised = run_command(
            [
                "git", "ls-remote", "--exit-code", "--",
                destination_url, remote_ref,
            ],
            cwd=project_path, check=False, timeout=120,
        )
        remote_sha = None
        validation_ref = None
        if advertised.returncode == 0:
            fields = advertised.stdout.split()
            if len(fields) < 2 or fields[1] != remote_ref or not re.fullmatch(
                r"[0-9a-fA-F]{40,64}", fields[0]
            ):
                raise ControlCenterError(
                    "The GitHub target branch could not be resolved safely.", 409
                )
            advertised_sha = fields[0]
            validation_ref = "refs/awesome-webkit/validation/{}".format(
                uuid.uuid4().hex
            )
            validation_ready = False
            try:
                fetched = run_command(
                    [
                        "git", "fetch", "--no-tags", "--no-write-fetch-head", "--",
                        destination_url,
                        "+{}:{}".format(remote_ref, validation_ref),
                    ],
                    cwd=project_path, check=False, timeout=120,
                )
                if fetched.returncode != 0:
                    raise ControlCenterError(
                        sanitize_git_error(
                            fetched.stderr or fetched.stdout,
                            "The GitHub target branch could not be fetched.",
                        ),
                        409,
                    )
                resolved = run_command(
                    [
                        "git", "rev-parse", "--verify",
                        "{}^{{commit}}".format(validation_ref),
                    ],
                    cwd=project_path, check=False,
                )
                remote_sha = resolved.stdout.strip() if resolved.returncode == 0 else ""
                if (
                    not re.fullmatch(r"[0-9a-fA-F]{40,64}", remote_sha)
                    or remote_sha.lower() != advertised_sha.lower()
                ):
                    raise ControlCenterError(
                        "The GitHub target branch changed during validation. Retry the push.",
                        409,
                    )
                remote_sha = remote_sha.lower()
                validation_ready = True
            finally:
                if not validation_ready:
                    run_command(
                        ["git", "update-ref", "-d", validation_ref],
                        cwd=project_path, check=False,
                    )
                    validation_ref = None
        elif advertised.returncode != 2:
            raise ControlCenterError(
                sanitize_git_error(
                    advertised.stderr or advertised.stdout,
                    "The GitHub target branch could not be inspected.",
                ),
                409,
            )

        try:
            if remote_sha:
                ancestor = run_command(
                    ["git", "merge-base", "--is-ancestor", remote_sha, local_sha],
                    cwd=project_path,
                    check=False,
                )
                if ancestor.returncode != 0:
                    raise ControlCenterError(
                        "The GitHub target is not an ancestor of the validated local commit. Sync before pushing.",
                        409,
                    )
            cls._reject_history_secrets(
                project_path,
                remote_sha,
                local_sha,
                "Outgoing commit history",
            )
            revision_range = (
                "{}..{}".format(remote_sha, local_sha) if remote_sha else local_sha
            )
            config_history = run_command(
                [
                    "git", "log", "--format=%H", "--reverse",
                    "--max-count={}".format(MAX_OUTGOING_CONFIG_CHANGES + 1),
                    revision_range, "--",
                    _literal_git_pathspec("webkit/webkit.config.json"),
                ],
                cwd=project_path, check=False,
            )
            if config_history.returncode != 0:
                raise ControlCenterError(
                    "Could not inspect outgoing Webkit config history.", 409
                )
            config_commits = [
                value.strip() for value in config_history.stdout.splitlines()
                if value.strip()
            ]
            if len(config_commits) > MAX_OUTGOING_CONFIG_CHANGES:
                raise ControlCenterError(
                    "Outgoing Webkit config history has too many changes to validate safely.",
                    409,
                )
            config_references = ([remote_sha] if remote_sha else []) + config_commits
            roots = private_runtime_roots(current_config)
            for root in cls._historical_feedback_roots(
                project_path, config_references
            ):
                if root not in roots:
                    roots.append(root)
            for root in roots:
                touched = run_command(
                    [
                        "git", "log", "-1", "--format=%H", revision_range, "--",
                        _literal_git_pathspec(root),
                    ],
                    cwd=project_path, check=False,
                )
                if touched.returncode != 0:
                    raise ControlCenterError(
                        "Could not inspect outgoing private runtime history.", 409
                    )
                if touched.stdout.strip():
                    raise ControlCenterError(
                        "Outgoing commits touched private Webkit runtime path {}. Push was refused.".format(
                            root
                        ),
                        409,
                    )
            return {
                "localSha": local_sha,
                "remoteSha": remote_sha,
                "pushUrl": destination_url,
            }
        finally:
            if validation_ref:
                run_command(
                    ["git", "update-ref", "-d", validation_ref],
                    cwd=project_path, check=False,
                )

    def _validated_push_boundary(
        self, project_path, remote, branch, target, set_upstream=False,
        validated_sha=None,
    ):
        project_path = Path(project_path)
        destination = self._validated_github_remote(project_path, remote)
        config = load_webkit_config(
            project_path / "webkit" / "webkit.config.json",
            project_path,
            require_default_page=True,
        )
        require_private_runtime_paths_safe(project_path, config)
        validation = self._require_outgoing_history_private(
            project_path, destination["pushUrl"], branch, target, config,
            validated_sha=validated_sha,
        )
        return self._verified_push(
            project_path, remote, branch, target,
            set_upstream=set_upstream,
            validated_sha=validation["localSha"],
            validated_push_url=validation["pushUrl"],
            validated_remote_sha=validation["remoteSha"],
        )

    def push_to_github(self, project, validated_sha=None):
        with self._project_lock:
            return self._push_to_github(project, validated_sha=validated_sha)

    def _push_to_github(self, project, validated_sha=None):
        project_path = Path(project["path"])
        status = self.github_status(project_path)
        if not status["connected"]:
            return {
                "connected": False,
                "pushed": False,
                "error": status.get("error"),
            }
        base = project.get("baseBranch", "main")
        target = project.get("targetBranch", "main")
        result = self._validated_push_boundary(
            project_path, status["remote"], base, target,
            validated_sha=validated_sha,
        )
        return dict(result, **{
            "connected": True,
            "remote": status["remote"],
        })

    def push_project(self, project_id):
        project = self.get_project(project_id)
        self.integrate_managed_target(project)
        before = self.github_sync_status(project)
        if not before["connected"]:
            raise ControlCenterError(
                "Connect this project to GitHub before pushing.", 409
            )
        if not before["unpushed"]:
            return {"pushed": False, "alreadyCurrent": True, "github": before}
        result = self.push_to_github(project)
        if not result["pushed"]:
            raise ControlCenterError(
                result.get("error") or "GitHub push failed. Check your GitHub authentication.", 409
            )
        after = self.github_sync_status(project)
        return {"pushed": True, "alreadyCurrent": False, "github": after}

    def _validate_provider(self, provider):
        enabled = self.store.read().get("providers", [])
        if provider not in ("codex", "claude") or provider not in enabled:
            raise ControlCenterError("Choose one enabled provider for this project.")

    @staticmethod
    def _require_git():
        executable = shutil.which("git")
        if not executable:
            raise ControlCenterError("Git is required. Use the Install Git button first.", 409)
        result = run_command([executable, "--version"], check=False, timeout=15)
        parsed = parsed_git_version(result.stdout or result.stderr)
        if result.returncode != 0 or parsed is None or parsed < MINIMUM_GIT_VERSION:
            raise ControlCenterError(
                "Git 2.30 or newer is required. Update Git and try again.", 409
            )

    @staticmethod
    def _git_root(path):
        result = run_command(["git", "rev-parse", "--show-toplevel"], cwd=path, check=False)
        if result.returncode != 0:
            return None
        return Path(result.stdout.strip()).resolve()

    @staticmethod
    def _git_init(path):
        result = run_command(["git", "init", "-b", "main"], cwd=path, check=False)
        if result.returncode != 0:
            run_command(["git", "init"], cwd=path)
            run_command(["git", "branch", "-M", "main"], cwd=path)

    @staticmethod
    def _ensure_main_branch(path):
        current = run_command(["git", "branch", "--show-current"], cwd=path).stdout.strip()
        if current == "main":
            return
        has_main = run_command(["git", "show-ref", "--verify", "--quiet", "refs/heads/main"], cwd=path, check=False)
        if has_main.returncode == 0:
            raise ControlCenterError("Switch this repository to its main branch before adding it.", 409)
        run_command(["git", "branch", "-M", "main"], cwd=path)

    @staticmethod
    def _ensure_git_identity(path):
        name = run_command(["git", "config", "user.name"], cwd=path, check=False).stdout.strip()
        email = run_command(["git", "config", "user.email"], cwd=path, check=False).stdout.strip()
        if not name:
            fallback = os.environ.get("USER") or os.environ.get("USERNAME") or "AWESOME WEBKIT User"
            run_command(["git", "config", "user.name", fallback], cwd=path)
        if not email:
            fallback = slugify(os.environ.get("USER") or os.environ.get("USERNAME") or "webkit")
            run_command(["git", "config", "user.email", fallback + "@localhost"], cwd=path)

    def _preflight_add_existing_project(
        self, project_path, provider, allow_git_metadata=False
    ):
        project_path = Path(project_path).resolve()
        git_marker = project_path / ".git"
        if not allow_git_metadata and (git_marker.exists() or git_marker.is_symlink()):
            raise ControlCenterError(
                "This folder contains invalid Git metadata. Repair or remove .git before adding it.",
                409,
            )

        self._validate_existing_kit_version(project_path)
        target = project_path / "webkit"
        self._validate_missing_tree_targets(
            self.kit_root / "webkit", target, project_path
        )

        support_files = [
            (project_path / "AGENTS.md", "AGENTS.md"),
            (project_path / ".gitignore", ".gitignore"),
        ]
        if provider == "claude":
            support_files.append((project_path / "CLAUDE.md", "CLAUDE.md"))
        for path, label in support_files:
            path = safe_project_target(project_path, path)
            if path.exists() and not path.is_file():
                raise ControlCenterError(
                    "Installer support file {} must be a regular file.".format(label), 409
                )
            if path.exists():
                try:
                    if path.stat().st_size > ProjectMutationJournal.MAX_SUPPORT_BYTES:
                        raise ControlCenterError(
                            "Installer support file {} must be 8 MB or smaller.".format(label),
                            413,
                        )
                    path.read_text(encoding="utf-8")
                except ControlCenterError:
                    raise
                except (OSError, UnicodeDecodeError) as error:
                    raise ControlCenterError(
                        "Installer support file {} is unreadable: {}".format(label, error),
                        409,
                    )

        if provider == "claude":
            skills_root = safe_project_target(
                project_path, project_path / ".claude" / "skills"
            )
            if skills_root.exists() and not skills_root.is_dir():
                raise ControlCenterError(
                    "The existing .claude/skills path must be a folder.", 409
                )
            for source in (self.kit_root / "webkit" / "skills").iterdir():
                if not source.is_dir():
                    continue
                installed = safe_project_target(project_path, skills_root / source.name)
                if installed.exists() and not installed.is_dir():
                    raise ControlCenterError(
                        "Installed Claude skill path must be a folder: {}.".format(
                            installed
                        ),
                        409,
                    )

        config_path = safe_project_target(
            project_path, target / "webkit.config.json"
        )
        if config_path.exists():
            load_webkit_config(
                config_path, project_path, require_default_page=True
            )
            return

        site_root, default_page = self._find_site_entry(project_path)
        _site_root, served_root = _validated_relative_path(
            site_root, "site_root", project_path, require_directory=True
        )
        _validated_relative_path(
            default_page, "default_page", served_root, require_file=True
        )
        with self._port_lock:
            reserved = self._reserved_preview_ports() | self._allocated_ports
            self._find_port_block(5311, reserved)

    def _install_kit(
        self, project_path, provider, journal=None, config_repository_path=None
    ):
        changed = False
        project_path = Path(project_path).resolve()
        mutable_support = [project_path / "AGENTS.md", project_path / ".gitignore"]
        if provider == "claude":
            mutable_support.append(project_path / "CLAUDE.md")
        for support_path in mutable_support:
            safe_project_target(project_path, support_path)
            _read_mutable_support_text(support_path)
        target = project_path / "webkit"
        if target.is_symlink() or (target.exists() and not target.is_dir()):
            raise ControlCenterError("The existing webkit path must be a folder.", 409)
        self._validate_existing_kit_version(project_path)
        changed = self._copy_missing_tree(
            self.kit_root / "webkit", target, journal=journal
        ) or changed
        control_center_source = self.kit_root / "webkit" / "CONTROL-CENTER.md"
        control_center_target = target / "CONTROL-CENTER.md"
        if not control_center_target.exists():
            if journal is not None:
                journal.ensure_directory(control_center_target.parent)
            exclusive_copy_file(
                control_center_source, control_center_target, journal=journal
            )
            changed = True
        config_path = target / "webkit.config.json"
        safe_project_target(project_path, config_path)
        if not config_path.exists():
            if journal is not None:
                journal.watch_support_file(config_path)
            config = self._make_config(
                project_path, repository_path=config_repository_path
            )
            config_writer = journal
            if config_writer is None:
                config_writer = ProjectMutationJournal(project_path)
                config_writer.watch_support_file(config_path)
            config_writer.write_new_support_file(
                config_path,
                (json.dumps(config, indent=2, ensure_ascii=False) + "\n").encode(
                    "utf-8"
                ),
            )
            if journal is not None:
                journal.mark_support_written(config_path)
            changed = True
        config = load_webkit_config(config_path, project_path)
        agents_path = project_path / "AGENTS.md"
        if journal is not None:
            journal.watch_support_file(agents_path)
        agents_changed = append_section(
            agents_path, "## Webkit", WEBKIT_POINTER, project_path,
            journal=journal,
        )
        if journal is not None and agents_changed:
            journal.mark_support_written(agents_path)
        changed = agents_changed or changed
        agents_changed = append_section(
            agents_path, "## Webkit Control Center", CONTROL_CENTER_POINTER, project_path,
            journal=journal,
        )
        if journal is not None and agents_changed:
            journal.mark_support_written(agents_path)
        changed = agents_changed or changed
        if provider == "claude":
            claude_path = project_path / "CLAUDE.md"
            if journal is not None:
                journal.watch_support_file(claude_path)
            claude_changed = append_section(
                claude_path, "## Webkit", WEBKIT_POINTER, project_path,
                journal=journal,
            )
            if journal is not None and claude_changed:
                journal.mark_support_written(claude_path)
            changed = claude_changed or changed
            claude_changed = append_section(
                claude_path, "## Webkit Control Center", CONTROL_CENTER_POINTER,
                project_path, journal=journal,
            )
            if journal is not None and claude_changed:
                journal.mark_support_written(claude_path)
            changed = claude_changed or changed
            changed = self._copy_claude_skills(
                project_path, journal=journal
            ) or changed
        gitignore_path = project_path / ".gitignore"
        if journal is not None:
            journal.watch_support_file(gitignore_path)
        ignores_changed = self._ensure_standard_ignores(
            project_path, journal=journal
        )
        if journal is not None and ignores_changed:
            journal.mark_support_written(gitignore_path)
        changed = ignores_changed or changed
        feedback_root = config["feedback_dir"].rstrip("/")
        if not feedback_root.startswith(".webkit/"):
            ignore_changed = self._append_gitignore(
                gitignore_path, feedback_root + "/", project_path,
                journal=journal,
            )
            if journal is not None and ignore_changed:
                journal.mark_support_written(gitignore_path)
            changed = ignore_changed or changed
        return changed

    def _existing_kit_update(self, project_path):
        project_path = Path(project_path).resolve()
        target = project_path / "webkit"
        if not target.exists():
            return None
        if target.is_symlink() or not target.is_dir():
            raise ControlCenterError("The existing webkit path must be a folder.", 409)
        expected_path = self.kit_root / "webkit" / "VERSION"
        version_path = safe_project_target(project_path, target / "VERSION")
        try:
            expected = expected_path.read_text(encoding="utf-8").strip()
            actual = version_path.read_text(encoding="utf-8").strip()
        except OSError:
            actual = ""
            expected = expected_path.read_text(encoding="utf-8").strip()
        if actual and actual == expected:
            return None
        return {
            "code": "webkit_update_required",
            "installedVersion": actual or "unknown",
            "requiredVersion": expected,
        }

    @staticmethod
    def _raise_existing_kit_update_required(version_update):
        raise ControlCenterError(
            "This project has Webkit version {} but the Control Center requires {}. Update it before adding it.".format(
                version_update["installedVersion"],
                version_update["requiredVersion"],
            ),
            409,
            dict(version_update),
        )

    def _validate_existing_kit_version(self, project_path):
        version_update = self._existing_kit_update(project_path)
        if version_update is not None:
            self._raise_existing_kit_update_required(version_update)

    def _upgrade_existing_kit(self, project_path, version_update):
        """Atomically replace an old vendored payload while preserving its config."""
        project_path = Path(project_path).resolve()
        target = safe_project_target(project_path, project_path / "webkit")
        if target.is_symlink() or not target.is_dir():
            raise ControlCenterError("The existing webkit path must be a folder.", 409)

        config_path = safe_project_target(
            project_path, target / "webkit.config.json"
        )
        config_snapshot = None
        if config_path.exists() or config_path.is_symlink():
            config_snapshot = ProjectMutationJournal._snapshot(
                config_path,
                limit=MAX_WEBKIT_CONFIG_BYTES,
                require_single_link=True,
            )
            load_webkit_config(config_path, project_path, require_default_page=True)

        start_head = self._resolved_commit(
            project_path, "HEAD", "The managed update checkout"
        )
        prepared = project_path.parent / (
            ".{}-webkit-update-{}".format(project_path.name, uuid.uuid4().hex)
        )
        backup = project_path.parent / (
            ".{}-webkit-backup-{}".format(project_path.name, uuid.uuid4().hex)
        )
        swapped = False
        backup_complete = False
        try:
            prepared.mkdir(mode=0o700)
            self._copy_current_kit_payload(prepared)
            if config_snapshot is not None:
                prepared_config = prepared / "webkit.config.json"
                writer = ProjectMutationJournal(prepared)
                writer.watch_support_file(prepared_config)
                writer.write_new_support_file(
                    prepared_config,
                    config_snapshot["data"],
                    mode=config_snapshot["mode"],
                )
                writer.close()

            current_head = self._resolved_commit(
                project_path, "HEAD", "The managed update checkout"
            )
            status = run_command(
                [
                    "git", "--no-optional-locks", "status", "--porcelain=v1",
                    "--untracked-files=all", "--ignore-submodules=none",
                ],
                cwd=project_path,
                check=False,
            )
            if (
                current_head != start_head
                or status.returncode != 0
                or status.stdout
            ):
                raise ControlCenterError(
                    "The managed checkout changed during Webkit update preparation and was preserved.",
                    409,
                )

            rename_directory_noreplace(target, backup)
            backup_complete = True
            try:
                rename_directory_noreplace(prepared, target)
                swapped = True
            except Exception:
                rename_directory_noreplace(backup, target)
                raise

            current_update = self._existing_kit_update(project_path)
            installed_version = (target / "VERSION").read_text(
                encoding="utf-8"
            ).strip()
            if (
                current_update is not None
                or installed_version != version_update["requiredVersion"]
            ):
                raise ControlCenterError(
                    "The prepared Webkit update did not install the required version.",
                    409,
                )
            if config_snapshot is not None:
                updated_config = ProjectMutationJournal._snapshot(
                    config_path,
                    limit=MAX_WEBKIT_CONFIG_BYTES,
                    require_single_link=True,
                )
                if (
                    updated_config["data"] != config_snapshot["data"]
                    or updated_config["mode"] != config_snapshot["mode"]
                ):
                    raise ControlCenterError(
                        "webkit.config.json changed during the Webkit update.", 409
                    )
            backup_complete = False
            shutil.rmtree(str(backup))
        except Exception as primary_error:
            recovery_error = None
            if (
                swapped and backup_complete
                and backup.exists() and not backup.is_symlink()
            ):
                failed_payload = None
                try:
                    failed_payload = ProjectMutationJournal._claim_path(target)
                    rename_directory_noreplace(backup, target)
                    shutil.rmtree(str(failed_payload))
                    swapped = False
                except Exception as error:
                    recovery_error = str(error)
            if recovery_error:
                raise ControlCenterError(
                    "{}. Update rollback was incomplete: {}.".format(
                        primary_error, recovery_error
                    ),
                    getattr(primary_error, "status", 409),
                ) from primary_error
            raise
        finally:
            if prepared.exists() and not prepared.is_symlink():
                shutil.rmtree(str(prepared), ignore_errors=True)
            if not swapped and backup.exists() and not backup.is_symlink():
                shutil.rmtree(str(backup), ignore_errors=True)

    @classmethod
    def _ensure_standard_ignores(cls, project_path, journal=None):
        changed = False
        for entry in STANDARD_PRIVATE_IGNORES:
            changed = cls._append_gitignore(
                Path(project_path) / ".gitignore", entry, project_path,
                journal=journal,
            ) or changed
        return changed

    @staticmethod
    def _bounded_git_stdout(
        command, project_path, limit, label, allow_truncated_prefix=False
    ):
        def stop_bounded_process(process):
            try:
                process.terminate()
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass
                try:
                    process.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    pass

        try:
            process = subprocess.Popen(
                command,
                cwd=str(project_path),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise ControlCenterError("{}: {}".format(label, exc), 409)
        try:
            output = process.stdout.read(limit + 1)
            if len(output) > limit:
                stop_bounded_process(process)
                if allow_truncated_prefix:
                    return output[:limit]
                raise ControlCenterError("{} is too large to validate safely.".format(label), 409)
            return_code = process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            stop_bounded_process(process)
            raise ControlCenterError("{} timed out.".format(label), 409)
        finally:
            if process.stdout is not None:
                process.stdout.close()
        if return_code != 0:
            raise ControlCenterError("{} failed.".format(label), 409)
        return output

    @classmethod
    def _reject_staged_secrets(cls, project_path, base_sha=None):
        """Inspect the immutable index blobs that the next commit would publish."""
        project_path = Path(project_path).resolve()
        if base_sha is None:
            head = run_command(
                ["git", "rev-parse", "--verify", "HEAD^{commit}"],
                cwd=project_path,
                check=False,
            )
            base_sha = head.stdout.strip().lower() if head.returncode == 0 else None
        else:
            base_sha = str(base_sha).lower()
        if base_sha is not None and re.fullmatch(r"[0-9a-f]{40,64}", base_sha) is None:
            raise ControlCenterError("The staged snapshot parent is invalid.", 409)
        before_tree = run_command(
            ["git", "write-tree"], cwd=project_path, check=False
        )
        expected_tree = before_tree.stdout.strip() if before_tree.returncode == 0 else ""
        if re.fullmatch(r"[0-9a-fA-F]{40,64}", expected_tree) is None:
            raise ControlCenterError(
                "The staged snapshot could not be pinned to an immutable tree.", 409
        )
        expected_tree = expected_tree.lower()
        immutable_entries = []
        if base_sha is None:
            listed = cls._bounded_git_stdout(
                ["git", "ls-tree", "-r", "-z", "--full-tree", expected_tree],
                project_path,
                MAX_STAGED_PATH_LIST_BYTES,
                "The staged tree path list",
            )
            for entry in [value for value in listed.split(b"\0") if value]:
                header, separator, raw_path = entry.partition(b"\t")
                fields = header.split()
                if (
                    not separator
                    or not raw_path
                    or len(fields) != 3
                    or re.fullmatch(br"[0-7]{6}", fields[0]) is None
                    or fields[1] not in (b"blob", b"commit")
                    or re.fullmatch(br"[0-9a-fA-F]{40,64}", fields[2]) is None
                ):
                    raise ControlCenterError(
                        "The staged tree path list is malformed.", 409
                    )
                immutable_entries.append((fields[2].decode("ascii").lower(), raw_path))
        else:
            base_tree_result = run_command(
                ["git", "rev-parse", "--verify", "{}^{{tree}}".format(base_sha)],
                cwd=project_path,
                check=False,
            )
            base_tree = (
                base_tree_result.stdout.strip().lower()
                if base_tree_result.returncode == 0 else ""
            )
            if re.fullmatch(r"[0-9a-f]{40,64}", base_tree) is None:
                raise ControlCenterError(
                    "The staged snapshot parent tree is invalid.", 409
                )
            changed = cls._bounded_git_stdout(
                [
                    "git", "diff-tree", "-r", "--no-commit-id", "--raw", "-z",
                    "--no-renames", "--diff-filter=AMT", base_tree, expected_tree,
                ],
                project_path,
                MAX_STAGED_PATH_LIST_BYTES,
                "The staged tree path list",
            )
            records = changed.split(b"\0")
            if records and records[-1] == b"":
                records.pop()
            raw_pattern = re.compile(
                br"^:([0-7]{6}) ([0-7]{6}) ([0-9a-fA-F]{40,64}) "
                br"([0-9a-fA-F]{40,64}) ([AMT])$"
            )
            if len(records) % 2:
                raise ControlCenterError(
                    "The staged tree path list is malformed.", 409
                )
            for offset in range(0, len(records), 2):
                matched = raw_pattern.fullmatch(records[offset])
                raw_path = records[offset + 1]
                if matched is None or not raw_path:
                    raise ControlCenterError(
                        "The staged tree path list is malformed.", 409
                    )
                immutable_entries.append(
                    (matched.group(4).decode("ascii").lower(), raw_path)
                )
        if len(immutable_entries) > MAX_STAGED_SECRET_SCAN_FILES:
            raise ControlCenterError(
                "Too many staged files were present to scan for secrets safely.", 409
            )
        sensitive = []
        for blob_sha, raw_relative in immutable_entries:
            relative = os.fsdecode(raw_relative)
            object_type = run_command(
                ["git", "cat-file", "-t", blob_sha],
                cwd=project_path,
                check=False,
            )
            if object_type.returncode != 0:
                raise ControlCenterError(
                    "A staged tree object could not be read safely.", 409
                )
            file_name = Path(relative).name
            risk = secret_file_risk(file_name)
            if (
                object_type.stdout.strip() == "blob"
                and risk is None
                and Path(file_name.lower()).suffix in SECRET_SCAN_SUFFIXES
            ):
                size = run_command(
                    ["git", "cat-file", "-s", blob_sha],
                    cwd=project_path, check=False,
                )
                try:
                    blob_size = int(size.stdout.strip()) if size.returncode == 0 else -1
                except ValueError:
                    blob_size = -1
                if blob_size < 0:
                    raise ControlCenterError(
                        "A staged blob size could not be validated.", 409
                    )
                prefix_limit = min(blob_size, MAX_SECRET_SCAN_BYTES)
                prefix = cls._bounded_git_stdout(
                    ["git", "cat-file", "blob", blob_sha],
                    project_path,
                    prefix_limit,
                    "A staged blob",
                    allow_truncated_prefix=blob_size > prefix_limit,
                )
                risk = secret_file_risk(file_name, prefix)
            if risk:
                sensitive.append(json.dumps(relative, ensure_ascii=True))
        if sensitive:
            raise ControlCenterError(
                "Refusing to commit possible secrets in {}. Remove them from the staged snapshot first.".format(
                    ", ".join(sorted(sensitive)[:12])
                ),
                409,
            )
        after_tree = run_command(
            ["git", "write-tree"], cwd=project_path, check=False
        )
        final_tree = after_tree.stdout.strip() if after_tree.returncode == 0 else ""
        final_head = run_command(
            ["git", "rev-parse", "--verify", "HEAD^{commit}"],
            cwd=project_path,
            check=False,
        )
        final_head_sha = (
            final_head.stdout.strip().lower() if final_head.returncode == 0 else None
        )
        if final_tree.lower() != expected_tree or final_head_sha != base_sha:
            raise ControlCenterError(
                "The staged snapshot or its parent changed while it was being scanned.",
                409,
            )
        return expected_tree

    @classmethod
    def _commit_validated_index(cls, project_path, message):
        """Commit exactly the index tree and parent set that passed validation."""
        project_path = Path(project_path).resolve()
        old_head_result = run_command(
            ["git", "rev-parse", "--verify", "HEAD^{commit}"],
            cwd=project_path,
            check=False,
        )
        old_head = (
            old_head_result.stdout.strip().lower()
            if old_head_result.returncode == 0 else None
        )
        if old_head is not None and re.fullmatch(r"[0-9a-f]{40,64}", old_head) is None:
            raise ControlCenterError("The current Git parent is invalid.", 409)
        expected_tree = cls._reject_staged_secrets(
            project_path, base_sha=old_head
        )
        merge_result = run_command(
            ["git", "rev-parse", "--verify", "MERGE_HEAD^{commit}"],
            cwd=project_path,
            check=False,
        )
        merge_head = (
            merge_result.stdout.strip().lower()
            if merge_result.returncode == 0 else None
        )
        if merge_head is not None and re.fullmatch(r"[0-9a-f]{40,64}", merge_head) is None:
            raise ControlCenterError("The Git merge parent is invalid.", 409)
        symbolic = run_command(
            ["git", "symbolic-ref", "--quiet", "HEAD"],
            cwd=project_path,
            check=False,
        )
        branch_ref = symbolic.stdout.strip() if symbolic.returncode == 0 else ""
        if not branch_ref.startswith("refs/heads/"):
            raise ControlCenterError(
                "The controller can commit only on a named local branch.", 409
            )
        run_command(["git", "commit", "-m", message], cwd=project_path)
        committed_sha = cls._resolved_commit(
            project_path, "HEAD", "The validated commit"
        )
        commit_object = cls._bounded_git_stdout(
            ["git", "cat-file", "commit", committed_sha],
            project_path,
            1024 * 1024,
            "The validated commit object",
        )
        tree = None
        parents = []
        for raw_line in commit_object.splitlines():
            line = raw_line.decode("ascii", "ignore")
            if not line:
                break
            key, separator, value = line.partition(" ")
            if key == "tree" and separator:
                tree = value.lower()
            elif key == "parent" and separator:
                parents.append(value.lower())
        expected_parents = []
        if old_head:
            expected_parents.append(old_head)
        if merge_head:
            expected_parents.append(merge_head)
        branch_sha = cls._resolved_commit(
            project_path, branch_ref, "The validated commit branch"
        )
        final_index = run_command(
            ["git", "write-tree"], cwd=project_path, check=False
        )
        final_tree = final_index.stdout.strip().lower() if final_index.returncode == 0 else ""
        status = run_command(
            [
                "git", "--no-optional-locks", "status", "--porcelain=v1",
                "--untracked-files=all", "--ignore-submodules=none",
            ],
            cwd=project_path,
            check=False,
        )
        if (
            tree != expected_tree
            or parents != expected_parents
            or branch_sha != committed_sha
            or final_tree != expected_tree
            or status.returncode != 0
            or status.stdout
        ):
            raise ControlCenterError(
                "A Git hook or concurrent process changed the validated commit; the repository was preserved for inspection.",
                409,
            )
        cls._reject_history_secrets(
            project_path,
            old_head,
            committed_sha,
            "Validated controller commit history",
        )
        return committed_sha

    @classmethod
    def _reject_history_secrets(
        cls, project_path, base_sha, tip_sha, label="Commit history"
    ):
        """Scan each immutable added or modified blob in one exact history range."""
        project_path = Path(project_path).resolve()
        tip_sha = str(tip_sha or "").lower()
        base_sha = str(base_sha or "").lower() or None
        if re.fullmatch(r"[0-9a-f]{40,64}", tip_sha) is None:
            raise ControlCenterError("{} tip is invalid.".format(label), 409)
        if base_sha is not None:
            if re.fullmatch(r"[0-9a-f]{40,64}", base_sha) is None:
                raise ControlCenterError("{} base is invalid.".format(label), 409)
            ancestor = run_command(
                ["git", "merge-base", "--is-ancestor", base_sha, tip_sha],
                cwd=project_path,
                check=False,
            )
            if ancestor.returncode != 0:
                raise ControlCenterError(
                    "{} is not a fast-forward history range.".format(label), 409
                )
            revision = "{}..{}".format(base_sha, tip_sha)
        else:
            revision = tip_sha
        commit_output = cls._bounded_git_stdout(
            [
                "git", "rev-list", "--reverse", "--topo-order",
                "--max-count={}".format(MAX_HISTORY_SECRET_SCAN_COMMITS + 1),
                revision,
            ],
            project_path,
            (MAX_HISTORY_SECRET_SCAN_COMMITS + 1) * 80,
            "{} commit list".format(label),
        )
        commits = [value for value in commit_output.decode("ascii").splitlines() if value]
        if len(commits) > MAX_HISTORY_SECRET_SCAN_COMMITS:
            raise ControlCenterError(
                "{} has too many commits to scan safely.".format(label), 409
            )
        if any(re.fullmatch(r"[0-9a-fA-F]{40,64}", value) is None for value in commits):
            raise ControlCenterError("{} commit list is invalid.".format(label), 409)
        scanned_files = 0
        scanned_bytes = 0
        sensitive = []
        raw_pattern = re.compile(
            br"^:([0-7]{6}) ([0-7]{6}) ([0-9a-fA-F]{40,64}) "
            br"([0-9a-fA-F]{40,64}) ([AMT])$"
        )
        for commit_sha in commits:
            raw = cls._bounded_git_stdout(
                [
                    "git", "diff-tree", "--root", "-r", "-m",
                    "--no-commit-id", "--raw", "-z", "--no-renames",
                    "--diff-filter=AMT", commit_sha,
                ],
                project_path,
                MAX_STAGED_PATH_LIST_BYTES,
                "{} changed path list".format(label),
            )
            records = raw.split(b"\0")
            if records and records[-1] == b"":
                records.pop()
            if len(records) % 2:
                raise ControlCenterError(
                    "{} changed path list is malformed.".format(label), 409
                )
            for offset in range(0, len(records), 2):
                matched = raw_pattern.fullmatch(records[offset])
                raw_path = records[offset + 1]
                if matched is None or not raw_path:
                    raise ControlCenterError(
                        "{} changed path list is malformed.".format(label), 409
                    )
                scanned_files += 1
                if scanned_files > MAX_STAGED_SECRET_SCAN_FILES:
                    raise ControlCenterError(
                        "{} has too many changed files to scan safely.".format(label),
                        409,
                    )
                blob_sha = matched.group(4).decode("ascii").lower()
                relative = os.fsdecode(raw_path)
                file_name = Path(relative).name
                risk = secret_file_risk(file_name)
                object_type = run_command(
                    ["git", "cat-file", "-t", blob_sha],
                    cwd=project_path,
                    check=False,
                )
                if object_type.returncode != 0:
                    raise ControlCenterError(
                        "{} contains an unreadable Git object.".format(label), 409
                    )
                if object_type.stdout.strip() == "blob" and risk is None:
                    size = run_command(
                        ["git", "cat-file", "-s", blob_sha],
                        cwd=project_path,
                        check=False,
                    )
                    try:
                        blob_size = int(size.stdout.strip()) if size.returncode == 0 else -1
                    except ValueError:
                        blob_size = -1
                    if blob_size < 0:
                        raise ControlCenterError(
                            "{} contains a blob with an invalid size.".format(label),
                            409,
                        )
                    prefix_limit = min(blob_size, MAX_SECRET_SCAN_BYTES)
                    if Path(file_name.lower()).suffix in SECRET_SCAN_SUFFIXES:
                        scanned_bytes += prefix_limit
                        if scanned_bytes > MAX_HISTORY_SECRET_SCAN_TOTAL_BYTES:
                            raise ControlCenterError(
                                "{} text-like blob data is too large to scan safely.".format(
                                    label
                                ),
                                409,
                            )
                        prefix = cls._bounded_git_stdout(
                            ["git", "cat-file", "blob", blob_sha],
                            project_path,
                            prefix_limit,
                            "{} blob".format(label),
                            allow_truncated_prefix=blob_size > prefix_limit,
                        )
                        risk = secret_file_risk(file_name, prefix)
                if risk:
                    sensitive.append(json.dumps(relative, ensure_ascii=True))
        if sensitive:
            raise ControlCenterError(
                "Refusing to publish possible secrets found in {}: {}.".format(
                    label.lower(), ", ".join(sorted(set(sensitive))[:12])
                ),
                409,
            )

    @staticmethod
    def _validate_missing_tree_targets(source_root, target_root, project_root):
        source_root = Path(source_root)
        target_root = safe_project_target(project_root, target_root)
        ignored = {"__pycache__", ".DS_Store", "webkit.config.json"}
        if target_root.exists() and not target_root.is_dir():
            raise ControlCenterError("The existing webkit path must be a folder.", 409)
        for source_dir, directory_names, file_names in os.walk(str(source_root)):
            directory_names[:] = [name for name in directory_names if name not in ignored]
            relative = Path(source_dir).relative_to(source_root)
            destination_dir = safe_project_target(
                project_root, target_root / relative
            )
            if destination_dir.exists() and not destination_dir.is_dir():
                raise ControlCenterError(
                    "Cannot install Webkit because {} is not a folder.".format(destination_dir), 409
                )
            for file_name in file_names:
                if file_name in ignored:
                    continue
                destination = safe_project_target(
                    project_root, destination_dir / file_name
                )
                if destination.exists() and not destination.is_file():
                    raise ControlCenterError(
                        "Cannot install Webkit because {} is not a regular file.".format(
                            destination
                        ),
                        409,
                    )

    @staticmethod
    def _copy_missing_tree(source_root, target_root, journal=None):
        """Fill missing kit files without replacing project-owned customizations."""
        source_root = Path(source_root)
        target_root = Path(target_root)
        ProjectManager._validate_missing_tree_targets(
            source_root, target_root, target_root.parent
        )
        changed = False
        ignored = {"__pycache__", ".DS_Store", "webkit.config.json"}
        if journal is not None:
            journal.ensure_directory(target_root)
        else:
            target_root.mkdir(parents=True, exist_ok=True)
        for source_dir, directory_names, file_names in os.walk(str(source_root)):
            directory_names[:] = [name for name in directory_names if name not in ignored]
            relative = Path(source_dir).relative_to(source_root)
            destination_dir = target_root / relative
            if not destination_dir.exists():
                if journal is not None:
                    journal.ensure_directory(destination_dir)
                else:
                    destination_dir.mkdir(parents=True)
                changed = True
            for file_name in file_names:
                if file_name in ignored:
                    continue
                source = Path(source_dir) / file_name
                destination = destination_dir / file_name
                if destination.exists():
                    continue
                exclusive_copy_file(source, destination, journal=journal)
                changed = True
        return changed

    def _copy_current_kit_payload(self, target_root):
        """Copy the committed kit tree when available, excluding local clone data."""
        target_root = Path(target_root).resolve()
        repository_root = self._git_root(self.kit_root)
        if repository_root != self.kit_root:
            return self._copy_missing_tree(
                self.kit_root / "webkit", target_root
            )

        source_commit = self._resolved_commit(
            self.kit_root, "HEAD", "The Control Center kit source"
        )
        tree = self._bounded_git_stdout(
            [
                "git", "ls-tree", "-r", "-z", "--full-tree",
                source_commit, "--", "webkit",
            ],
            self.kit_root,
            MAX_STAGED_PATH_LIST_BYTES,
            "The committed Webkit payload manifest",
        )
        entries = [entry for entry in tree.split(b"\0") if entry]
        if not entries:
            raise ControlCenterError(
                "The committed Control Center kit has no Webkit payload.", 409
            )

        writer = ProjectMutationJournal(target_root)
        ignored = {"__pycache__", ".DS_Store", "webkit.config.json"}
        copied = False
        try:
            for entry in entries:
                metadata, separator, raw_path = entry.partition(b"\t")
                fields = metadata.split()
                if separator != b"\t" or len(fields) != 3:
                    raise ControlCenterError(
                        "The committed Webkit payload manifest is invalid.", 409
                    )
                raw_mode, object_type, raw_sha = fields
                if object_type != b"blob" or raw_mode not in (b"100644", b"100755"):
                    raise ControlCenterError(
                        "The committed Webkit payload contains an unsupported entry.",
                        409,
                    )
                path_text = os.fsdecode(raw_path).replace("\\", "/")
                if not path_text.startswith("webkit/"):
                    raise ControlCenterError(
                        "The committed Webkit payload escaped its source folder.", 409
                    )
                relative = Path(path_text[len("webkit/"):])
                if (
                    not relative.parts
                    or any(
                        part in ("", ".", "..") or part.lower() == ".git"
                        for part in relative.parts
                    )
                ):
                    raise ControlCenterError(
                        "The committed Webkit payload contains an unsafe path.", 409
                    )
                if any(part in ignored for part in relative.parts):
                    if relative.name == "webkit.config.json":
                        raise ControlCenterError(
                            "The committed Webkit payload must not contain project configuration.",
                            409,
                        )
                    continue
                sha = raw_sha.decode("ascii", "strict")
                if re.fullmatch(r"[0-9a-f]{40,64}", sha) is None:
                    raise ControlCenterError(
                        "The committed Webkit payload contains an invalid object.", 409
                    )
                data = self._bounded_git_stdout(
                    ["git", "cat-file", "blob", sha],
                    self.kit_root,
                    ProjectMutationJournal.MAX_SUPPORT_BYTES,
                    "A committed Webkit payload file",
                )
                destination = safe_project_target(
                    target_root, target_root / relative
                )
                writer.ensure_directory(destination.parent)
                writer.watch_support_file(destination)
                writer.write_new_support_file(
                    destination,
                    data,
                    mode=0o755 if raw_mode == b"100755" else 0o644,
                )
                copied = True
        finally:
            writer.close()
        if not copied:
            raise ControlCenterError(
                "The committed Control Center kit has no installable Webkit files.",
                409,
            )
        return True

    def _copy_claude_skills(self, project_path, journal=None):
        changed = False
        project_path = Path(project_path).resolve()
        target_root = safe_project_target(project_path, project_path / ".claude" / "skills")
        source_root = self.kit_root / "webkit" / "skills"
        for source in source_root.iterdir():
            if not source.is_dir():
                continue
            target = target_root / source.name
            safe_project_target(project_path, target)
            if target.exists():
                continue
            changed = self._copy_missing_tree(
                source, target, journal=journal
            ) or changed
        return changed

    @staticmethod
    def _append_gitignore(path, entry, project_root=None, journal=None):
        path = safe_project_target(project_root, path) if project_root else Path(path)

        def update(existing):
            if entry in existing.splitlines():
                return existing
            separator = "" if not existing or existing.endswith("\n") else "\n"
            return existing + separator + entry + "\n"

        return _mutate_support_text(path, update, journal=journal)

    @staticmethod
    def _repository_lock_identity(project_path):
        project_path = Path(project_path).resolve()
        common = run_command(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=project_path, check=False,
        )
        if common.returncode == 0 and common.stdout.strip():
            common_path = Path(common.stdout.strip())
            if not common_path.is_absolute():
                common_path = project_path / common_path
            identity_path = common_path.resolve()
            repository_name = (
                identity_path.parent.name
                if identity_path.name == ".git"
                else project_path.name
            )
        else:
            identity_path = project_path
            repository_name = project_path.name
        digest = hashlib.sha256(str(identity_path).encode("utf-8")).hexdigest()[:10]
        return slugify(repository_name), digest

    def _make_config(self, project_path, repository_path=None):
        site_root, default_page = self._find_site_entry(project_path)
        with self._port_lock:
            reserved = self._reserved_preview_ports() | self._allocated_ports
            port_start = self._find_port_block(5311, reserved)
            self._allocated_ports.update(range(port_start, port_start + 5))
        if repository_path is None:
            slug, identity = self._repository_lock_identity(project_path)
        else:
            repository_path = Path(repository_path).resolve()
            slug = slugify(repository_path.name)
            identity = hashlib.sha256(
                str(repository_path / ".git").encode("utf-8")
            ).hexdigest()[:10]
        settings = normalized_settings(self.store.read().get("settings"))
        palette = []
        colors = [
            ("blue", "🔵"), ("red", "🔴"), ("green", "🟢"),
            ("orange", "🟠"), ("purple", "🟣"),
        ]
        for index, (color_slug, emoji) in enumerate(colors):
            palette.append({"slug": color_slug, "emoji": emoji, "port": port_start + index})
        return {
            "project_name": slug,
            "site_root": site_root,
            "default_page": default_page,
            "lock_dir": "/tmp/{}-{}-agent-colors".format(slug, identity),
            "grace_seconds": 180,
            "palette": palette,
            "browser": {"mode": "auto", "app_name": "Google Chrome"},
            "feedback_dir": ".webkit/feedback",
            "hotkeys": {
                "toggle": settings["toggleHotkey"],
                "dictate": settings["dictateHotkey"],
            },
            "dictation": {"mode": settings["dictationMode"]},
            "interaction": {"mode": settings["interactionMode"]},
        }

    @staticmethod
    def _find_site_entry(project_path):
        root_index = project_path / "index.html"
        if root_index.exists():
            return ".", "index.html"
        candidates = []
        ignored = {".git", "node_modules", ".webkit", ".claude"}
        for root, dirs, files in os.walk(str(project_path)):
            dirs[:] = [d for d in dirs if d not in ignored]
            rel_parts = Path(root).relative_to(project_path).parts
            if len(rel_parts) > 5:
                dirs[:] = []
                continue
            if "index.html" in files:
                rel = Path(root).relative_to(project_path) / "index.html"
                candidates.append(rel)
        if not candidates:
            raise ControlCenterError("No index.html was found in this website project.", 409)
        candidates.sort(key=lambda p: (len(p.parts), str(p)))
        chosen = candidates[0]
        document_roots = {"build", "dist", "docs", "public", "site", "www"}
        if len(chosen.parts) > 1 and chosen.parts[0].lower() in document_roots:
            return chosen.parts[0], Path(*chosen.parts[1:]).as_posix()
        return ".", chosen.as_posix()

    @staticmethod
    def _find_default_page(project_path):
        return ProjectManager._find_site_entry(project_path)[1]

    def _reserved_preview_ports(self):
        reserved = set()
        for project in self.store.read().get("projects", []):
            config_path = Path(project.get("path", "")) / "webkit" / "webkit.config.json"
            try:
                config = load_webkit_config(config_path, project.get("path", ""))
            except ControlCenterError:
                continue
            for entry in config.get("palette", []):
                reserved.add(entry["port"])
        return reserved

    def _reserve_config_ports(self, config_path):
        config_path = Path(config_path)
        config = load_webkit_config(config_path, config_path.parents[1], require_default_page=True)
        ports = [entry["port"] for entry in config["palette"]]
        lock_dir = str(Path(config["lock_dir"]).resolve())
        color_locks = {(lock_dir, entry["slug"]) for entry in config["palette"]}
        with self._port_lock:
            conflicts = set(ports) & (self._reserved_preview_ports() | self._allocated_ports)
            if conflicts:
                raise ControlCenterError(
                    "Webkit preview ports are already assigned to another project: {}.".format(
                        ", ".join(str(port) for port in sorted(conflicts))
                    ),
                    409,
                )
            lock_conflicts = color_locks & self._reserved_color_locks()
            if lock_conflicts:
                colors = sorted(color for _lock, color in lock_conflicts)
                raise ControlCenterError(
                    "Webkit color locks are already assigned to another project: {}.".format(
                        ", ".join(colors)
                    ),
                    409,
                )
            self._allocated_ports.update(ports)

    def _reserved_color_locks(self):
        reserved = set()
        for project in self.store.read().get("projects", []):
            config_path = Path(project.get("path", "")) / "webkit" / "webkit.config.json"
            try:
                config = load_webkit_config(config_path, project.get("path", ""))
            except ControlCenterError:
                continue
            lock_dir = str(Path(config["lock_dir"]).resolve())
            reserved.update((lock_dir, entry["slug"]) for entry in config["palette"])
        return reserved

    @staticmethod
    def _find_port_block(start, reserved=None):
        reserved = set(reserved or ())
        candidate = start
        while candidate < 64000:
            sockets = []
            try:
                for port in range(candidate, candidate + 5):
                    if port in reserved:
                        raise OSError("preview port is reserved")
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    try:
                        sock.bind(("127.0.0.1", port))
                    except OSError:
                        sock.close()
                        raise
                    sockets.append(sock)
                return candidate
            except OSError:
                candidate += 10
            finally:
                for sock in sockets:
                    sock.close()
        raise ControlCenterError("Could not find five free local preview ports.", 500)

    @staticmethod
    def _starter_html(name):
        title = html.escape(name, quote=True)
        return """<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>{0}</title>
  <style>
    :root {{ color-scheme: light; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; background: #f5f1e8; color: #171717; }}
    main {{ width: min(920px, calc(100% - 40px)); padding: 72px; border: 2px solid #171717; border-radius: 28px; background: #fffdf8; box-shadow: 12px 12px 0 #3294e2; }}
    p {{ max-width: 42rem; font-size: 1.2rem; line-height: 1.6; }}
    h1 {{ margin: 0 0 20px; font-size: clamp(3rem, 9vw, 7rem); line-height: .9; letter-spacing: -.06em; }}
  </style>
</head>
<body><main><h1>{0}</h1><p>Your new AWESOME WEBKIT website is ready. Open a color session and describe what you want to build.</p></main></body>
</html>
""".format(title)

    @staticmethod
    def git_install_action():
        system = platform.system().lower()
        git_executable = shutil.which("git")
        if git_executable:
            version_result = run_command(
                [git_executable, "--version"], check=False, timeout=15
            )
            version = parsed_git_version(
                version_result.stdout or version_result.stderr
            )
            if version is not None and version >= MINIMUM_GIT_VERSION:
                return {
                    "started": False,
                    "message": "Git {}.{}.{} is already installed and supported.".format(
                        *version
                    ),
                }
            detected = (
                "{}.{}.{}".format(*version) if version is not None else "an unknown version"
            )
            guidance = {
                "darwin": "Update Command Line Tools or install a current Git from https://git-scm.com/download/mac.",
                "windows": "Upgrade Git for Windows from https://git-scm.com/download/win.",
            }.get(
                system,
                "Upgrade Git with your package manager or https://git-scm.com/downloads.",
            )
            return {
                "started": False,
                "message": "Git {} is installed, but AWESOME WEBKIT requires Git 2.30 or newer. {}".format(
                    detected, guidance
                ),
            }
        if system == "darwin":
            result = run_command(["xcode-select", "--install"], check=False, timeout=30)
            message = (result.stderr or result.stdout or "Apple's Git installer was opened.").strip()
            return {"started": result.returncode == 0, "message": message}
        if system == "windows" and shutil.which("winget"):
            subprocess.Popen(["winget", "install", "--id", "Git.Git", "-e", "--source", "winget"])
            return {"started": True, "message": "The Git for Windows installer was started."}
        installers = [
            ("apt-get", "pkexec apt-get install -y git"),
            ("dnf", "pkexec dnf install -y git"),
            ("pacman", "pkexec pacman -S --needed git"),
        ]
        for binary, command in installers:
            if shutil.which(binary):
                return {"started": False, "message": "Run this command in a terminal: {}".format(command)}
        return {"started": False, "message": "Install Git from https://git-scm.com/downloads and reopen the Control Center."}

    def install_shortcut(self):
        script = self.kit_root / "control-center" / "install-shortcut.py"
        result = run_command([sys.executable, str(script)], timeout=30)
        message = result.stdout.strip() or "Installed the AWESOME WEBKIT desktop shortcut."
        return {"installed": True, "message": message}


class ProviderRunner:
    def __init__(self, session, event_log, persist_thread, set_process, allow_git=False):
        self.session = session
        self.event_log = event_log
        self.persist_thread = persist_thread
        self.set_process = set_process
        self.allow_git = allow_git

    def _codex_git_write_dirs(self):
        worktree = Path(self.session["worktree"]).resolve()

        def git_path(command):
            value = run_command(command, cwd=worktree).stdout.strip()
            if not value:
                raise ControlCenterError("Git did not return its metadata path.", 409)
            path = Path(value)
            return (worktree / path).resolve() if not path.is_absolute() else path.resolve()

        git_dir = git_path(["git", "rev-parse", "--git-dir"])
        common_dir = git_path(["git", "rev-parse", "--git-common-dir"])
        branch = str(self.session.get("branch") or "")
        branch_path = Path(branch)
        if (
            not branch
            or not branch.startswith("webkit/")
            or branch.startswith("-")
            or branch_path.is_absolute()
            or any(part in ("", ".", "..") for part in branch_path.parts)
            or re.fullmatch(r"[A-Za-z0-9._/-]+", branch) is None
        ):
            raise ControlCenterError("The session branch name is unsafe.", 409)
        ref_path = (common_dir / "refs" / "heads" / branch_path).resolve()
        log_path = (common_dir / "logs" / "refs" / "heads" / branch_path).resolve()
        heads_root = (common_dir / "refs" / "heads").resolve()
        logs_root = (common_dir / "logs" / "refs" / "heads").resolve()
        if not path_is_within(ref_path, heads_root) or not path_is_within(log_path, logs_root):
            raise ControlCenterError("The session branch metadata path is unsafe.", 409)
        ref_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        candidates = [git_dir, common_dir / "objects", ref_path.parent, log_path.parent]
        result = []
        seen = set()
        for candidate in candidates:
            candidate = Path(candidate).resolve()
            key = str(candidate)
            if key not in seen:
                seen.add(key)
                result.append(candidate)
        return result

    def run(self, prompt):
        prompt = bounded_provider_prompt(prompt)
        provider = self.session["provider"]
        worktree = self.session["worktree"]
        thread_id = validated_provider_thread_id(self.session.get("threadId"))
        reasoning = self.session.get("reasoningEffort", "medium")
        env = scrubbed_child_environment()
        env["WK_CONTROL_CENTER"] = "1"
        env["WK_SESSION_COLOR"] = self.session["color"]
        if provider == "codex":
            executable = shutil.which("codex")
            if not executable:
                raise ControlCenterError("Codex CLI is not installed.", 409)
            command = [
                executable, "exec", "--json", "--sandbox", "workspace-write",
            ]
            if self.allow_git:
                for path in self._codex_git_write_dirs():
                    command.extend(["--add-dir", str(path)])
            command.extend(["-c", 'model_reasoning_effort="{}"'.format(reasoning)])
            if thread_id:
                command.extend(["resume", thread_id, "-"])
            else:
                command.append("-")
        else:
            executable = shutil.which("claude")
            if not executable:
                raise ControlCenterError("Claude Code CLI is not installed.", 409)
            if not thread_id:
                thread_id = str(uuid.uuid4())
                self.session["threadId"] = thread_id
                self.persist_thread(thread_id)
            command = [
                executable, "-p", "--output-format", "stream-json", "--verbose",
                "--permission-mode", "auto", "--effort", reasoning,
            ]
            if self.session.get("hasRun"):
                command.extend(["--resume", thread_id])
            else:
                command.extend(["--session-id", thread_id])

        process = None
        stderr_thread = None
        exit_watchdog = None
        watchdog_stop = threading.Event()
        stderr_lines = deque(maxlen=20)
        code = None
        try:
            process = subprocess.Popen(
                command,
                cwd=worktree,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                **provider_process_kwargs()
            )
            if os.name == "nt" and isinstance(getattr(process, "pid", None), int):
                attach_windows_kill_job(process)
            accepted = self.set_process(process)
            if accepted is False:
                raise ControlCenterError("The session stopped before the agent could start.", 409)
            process.stdin.write(prompt)
            process.stdin.close()
            stderr_thread = threading.Thread(
                target=self._read_stderr, args=(process, stderr_lines), daemon=True
            )
            stderr_thread.start()
            if isinstance(getattr(process, "pid", None), int) and os.name == "posix" and all(
                hasattr(os, name) for name in ("waitid", "P_PID", "WEXITED", "WNOHANG", "WNOWAIT")
            ):
                exit_watchdog = threading.Thread(
                    target=self._watch_provider_exit,
                    args=(process, watchdog_stop),
                    daemon=True,
                )
                exit_watchdog.start()
            elif isinstance(getattr(process, "pid", None), int) and os.name == "nt":
                exit_watchdog = threading.Thread(
                    target=self._watch_windows_provider_exit,
                    args=(process, watchdog_stop),
                    daemon=True,
                )
                exit_watchdog.start()
            for raw in self._bounded_lines(process.stdout):
                line = raw.strip()
                if not line:
                    continue
                try:
                    event = strict_json_loads(line)
                except ValueError:
                    self.event_log.append("agent", line, "progress")
                    continue
                if not isinstance(event, dict):
                    self.event_log.append("agent", line, "progress")
                    continue
                if provider == "codex":
                    self._codex_event(event)
                else:
                    self._claude_event(event)
            if getattr(process, "_webkit_leader_exited", False):
                code = process.wait(timeout=5)
            else:
                code = stop_process_tree(process)
        finally:
            watchdog_stop.set()
            if process is not None and getattr(process, "returncode", None) is None:
                stop_process_tree(process)
            if process is not None:
                self.set_process(None)
            if exit_watchdog is not None and exit_watchdog is not threading.current_thread():
                exit_watchdog.join(timeout=1)
            if stderr_thread is not None and stderr_thread is not threading.current_thread():
                stderr_thread.join(timeout=5)
            if process is not None:
                close_windows_kill_job(process)
        if code != 0:
            if stderr_lines:
                self.event_log.append("system", "\n".join(stderr_lines[-20:]), "error")
            raise ControlCenterError("{} exited with code {}.".format(provider, code), 500)
        self.session["hasRun"] = True

    @staticmethod
    def _read_stderr(process, sink):
        for raw in ProviderRunner._bounded_lines(process.stderr):
            line = raw.strip()
            if line:
                sink.append(line)

    @staticmethod
    def _bounded_lines(stream):
        """Drain text streams without ever retaining an unbounded logical line."""
        marker = "\n[provider output line truncated by AWESOME WEBKIT]"
        while True:
            raw = stream.readline(MAX_PROVIDER_STREAM_LINE_CHARS + 1)
            if raw == "":
                return
            if len(raw) <= MAX_PROVIDER_STREAM_LINE_CHARS:
                yield raw
                continue
            prefix = raw[:MAX_PROVIDER_STREAM_LINE_CHARS]
            while raw and not raw.endswith("\n"):
                raw = stream.readline(MAX_PROVIDER_STREAM_LINE_CHARS + 1)
            yield prefix + marker

    @staticmethod
    def _watch_provider_exit(process, stop_event):
        """Close a provider's process group while its exited leader is unreaped."""
        options = os.WEXITED | os.WNOHANG | os.WNOWAIT
        while not stop_event.wait(0.02):
            try:
                exited = os.waitid(os.P_PID, process.pid, options)
            except (ChildProcessError, OSError):
                return
            if exited is not None:
                stop_process_tree(process)
                return

    @staticmethod
    def _watch_windows_provider_exit(process, stop_event):
        """Close the provider job when its exact Windows process handle exits."""
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        process_handle = wintypes.HANDLE(int(process._handle))
        while not stop_event.is_set():
            result = kernel32.WaitForSingleObject(process_handle, 20)
            if result == 0:
                process._webkit_leader_exited = True
                close_windows_kill_job(process)
                return
            if result not in (258,):
                return

    def _codex_event(self, event):
        kind = event.get("type", "")
        if kind == "thread.started" and event.get("thread_id"):
            try:
                thread_id = validated_provider_thread_id(
                    event.get("thread_id"), required=True
                )
            except ControlCenterError:
                self.event_log.append(
                    "system", "Codex returned an unsafe thread ID; it was ignored.", "error"
                )
                return
            self.session["threadId"] = thread_id
            self.persist_thread(thread_id)
            return
        item = event.get("item") or {}
        if not isinstance(item, dict):
            self.event_log.append(
                "agent", "Codex returned an unsupported item shape.", "progress"
            )
            return
        item_type = item.get("type", "")
        if kind == "item.completed" and item_type == "agent_message":
            self.event_log.append("agent", item.get("text", ""), "message")
        elif kind in ("item.started", "item.completed") and item_type in (
            "command_execution", "file_change", "mcp_tool_call", "web_search", "plan_update"
        ):
            text = item.get("command") or item.get("name") or item.get("text") or item_type.replace("_", " ")
            self.event_log.append(
                "agent", text, "activity",
                {
                    "status": str(item.get("status") or "")[:100],
                    "type": str(item_type)[:100],
                },
            )
        elif kind in ("error", "turn.failed"):
            self.event_log.append("system", event.get("message") or json.dumps(event), "error")

    def _claude_event(self, event):
        if event.get("session_id") and event.get("session_id") != self.session.get("threadId"):
            try:
                thread_id = validated_provider_thread_id(
                    event.get("session_id"), required=True
                )
            except ControlCenterError:
                self.event_log.append(
                    "system", "Claude returned an unsafe session ID; it was ignored.", "error"
                )
                thread_id = None
            if thread_id:
                self.session["threadId"] = thread_id
                self.persist_thread(thread_id)
        kind = event.get("type")
        if kind == "assistant":
            message = event.get("message") or {}
            if not isinstance(message, dict):
                self.event_log.append(
                    "agent", "Claude returned an unsupported message shape.", "progress"
                )
                return
            content = message.get("content") or []
            if not isinstance(content, list):
                self.event_log.append(
                    "agent", "Claude returned unsupported message content.", "progress"
                )
                return
            for block in content:
                if not isinstance(block, dict):
                    self.event_log.append(
                        "agent", "Claude returned an unsupported content block.", "progress"
                    )
                    continue
                if block.get("type") == "text" and block.get("text"):
                    self.event_log.append("agent", block["text"], "message")
                elif block.get("type") == "tool_use":
                    self.event_log.append("agent", block.get("name", "tool"), "activity")
        elif kind == "result":
            if event.get("is_error"):
                errors = event.get("errors") or [event.get("result") or "Claude Code failed."]
                self.event_log.append("system", "\n".join(str(x) for x in errors), "error")
            elif event.get("result"):
                self.event_log.append("agent", event["result"], "message")


class BoundedPreviewLog:
    """Drain preview output into a fixed-size in-memory character ring."""

    def __init__(self):
        self.lock = threading.Lock()
        self.chunks = deque()
        self.size = 0
        self.stream = None
        self.thread = None

    def start(self, stream):
        self.stream = stream
        self.thread = threading.Thread(
            target=self._drain, name="wk-preview-log", daemon=True
        )
        self.thread.start()

    def _drain(self):
        while True:
            try:
                chunk = self.stream.read(4096)
            except (OSError, ValueError):
                return
            if not isinstance(chunk, str) or not chunk:
                return
            with self.lock:
                self.chunks.append(chunk)
                self.size += len(chunk)
                while self.size > MAX_PREVIEW_LOG_CHARS and self.chunks:
                    excess = self.size - MAX_PREVIEW_LOG_CHARS
                    first = self.chunks[0]
                    if len(first) <= excess:
                        self.chunks.popleft()
                        self.size -= len(first)
                    else:
                        self.chunks[0] = first[excess:]
                        self.size -= excess

    def tail(self, limit=MAX_PREVIEW_LOG_TAIL_CHARS):
        with self.lock:
            return "".join(self.chunks)[-int(limit):]

    def close(self):
        if self.stream is not None:
            try:
                self.stream.close()
            except (OSError, ValueError):
                pass
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=1)


class SessionRuntime:
    def __init__(self, manager, session):
        self.manager = manager
        self.session = session
        self.mutation_token = session.get("mutationToken") or uuid.uuid4().hex
        self.session["mutationToken"] = self.mutation_token
        self.log = EventLog(manager.store.state_dir, session["id"])
        self.jobs = queue.Queue()
        self.stop_event = threading.Event()
        self.process_lock = threading.Lock()
        self.current_process = None
        self.preview_process = None
        self.preview_log = None
        self.claimed_lock = None
        self.claimed_owner = None
        self.claimed_port = None
        self.claimed_color = None
        self.enqueue_lock = threading.Lock()
        self.feedback_lock = threading.Lock()
        self.feedback_job_active = False
        self.feedback_revision = 0
        self.feedback_scheduled_revision = 0
        self.feedback_latest_inbox = None
        self.feedback_latest_key = None
        self.worker = threading.Thread(target=self._work_loop, name="wk-agent-" + session["id"], daemon=True)
        self.watcher = threading.Thread(target=self._watch_loop, name="wk-watch-" + session["id"], daemon=True)

    def start(self):
        self.worker.start()
        self.watcher.start()

    def enqueue(self, prompt, source, display=None):
        with self.enqueue_lock:
            if self.stop_event.is_set():
                raise ControlCenterError("That session is stopping and cannot accept more work.", 409)
            self.jobs.put({"prompt": prompt, "source": source, "display": display})

    def _feedback_prompt(self, inbox):
        return CONTROL_CENTER_PROMPT.format(
            emoji=self.session["emoji"], color=self.session["color"]
        ).replace(
            ".webkit/feedback/{}/".format(self.session["color"]),
            str(inbox) + "/",
        )

    def _feedback_job_locked(self):
        if (
            self.feedback_job_active
            or self.stop_event.is_set()
            or self.feedback_latest_key is None
            or self.feedback_scheduled_revision == self.feedback_revision
        ):
            return None
        self.feedback_job_active = True
        self.feedback_scheduled_revision = self.feedback_revision
        return {
            "prompt": self._feedback_prompt(self.feedback_latest_inbox),
            "source": "feedback",
            "display": None,
            "feedback_inbox": self.feedback_latest_inbox,
            "feedback_key": self.feedback_latest_key,
            "feedback_revision": self.feedback_revision,
        }

    def _record_feedback_state_locked(self, inbox, key):
        inbox = str(inbox)
        if inbox != self.feedback_latest_inbox or key != self.feedback_latest_key:
            self.feedback_revision += 1
            self.feedback_latest_inbox = inbox
            self.feedback_latest_key = key

    def _observe_feedback_phase(self, inbox, key):
        with self.feedback_lock:
            self._record_feedback_state_locked(inbox, key)
            job = self._feedback_job_locked()
        if job is not None:
            self.jobs.put(job)

    def _feedback_job_is_current(self, job):
        inbox = Path(job["feedback_inbox"])
        try:
            current_key = self._feedback_phase_key(inbox)
        except OSError:
            current_key = None
        with self.feedback_lock:
            self._record_feedback_state_locked(inbox, current_key)
            return (
                current_key is not None
                and current_key == job["feedback_key"]
                and self.feedback_revision == job["feedback_revision"]
            )

    def _finish_feedback_job(self):
        with self.feedback_lock:
            self.feedback_job_active = False
            follow_up = self._feedback_job_locked()
        if follow_up is not None:
            self.jobs.put(follow_up)

    def _work_loop(self):
        while not self.stop_event.is_set():
            try:
                job = self.jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            if job is None:
                break
            filesystem_feedback = job["source"] == "feedback" and "feedback_revision" in job
            try:
                if filesystem_feedback and not self._feedback_job_is_current(job):
                    continue
                self.manager._set_pending_operation(
                    self.session["id"], job["source"],
                    job["prompt"] if job["source"] == "feedback" else None,
                )
                self.manager._set_session_status(
                    self.session["id"],
                    "merging" if job["source"] in ("merge", "seed-finalize") else "busy",
                )
                shown = job["display"] if job.get("display") is not None else job["prompt"]
                self.log.append("user" if job["source"] == "chat" else "system", shown, job["source"])
                runner = ProviderRunner(
                    self.session,
                    self.log,
                    lambda thread_id: self.manager._set_thread(self.session["id"], thread_id),
                    self._set_process,
                    allow_git=job["source"] in ("chat", "feedback"),
                )
                try:
                    runner.run(job["prompt"])
                    if job["source"] == "merge":
                        self.manager._complete_agent_merge(self.session["id"])
                    elif job["source"] == "seed-generation":
                        self.manager._complete_seed_generation(self.session["id"])
                    elif job["source"] == "seed-finalize":
                        self.manager._complete_seed_onboarding(self.session["id"])
                    elif self.session.get("kind") == "seeds" and self.session.get("seedStage") == "finalizing":
                        marker = Path(self.session["worktree"]) / ".webkit" / "seed-selection.json"
                        if marker.exists():
                            self.manager._set_session_status(self.session["id"], "merging")
                            self.manager._complete_seed_onboarding(self.session["id"])
                        else:
                            self.manager._set_session_status(self.session["id"], "active")
                    elif self.session.get("kind") == "seeds" and self.session.get("seedStage") == "generating":
                        self.manager._set_session_status(self.session["id"], "active")
                        status = self.manager.seed_status(self.session["id"])
                        if status.get("ready"):
                            self.manager._complete_seed_generation(self.session["id"])
                    else:
                        self.manager._set_session_status(self.session["id"], "active")
                    self.manager._clear_pending_operation(self.session["id"])
                except Exception as exc:
                    self.log.append("system", str(exc), "error")
                    self.manager._set_session_status(self.session["id"], "error", str(exc))
                    self.manager._clear_pending_operation(self.session["id"])
                    if job["source"] in ("seed-generation", "seed-finalize"):
                        self.manager._set_project_onboarding(self.session["projectId"], {
                            "status": "error",
                            "sessionId": self.session["id"],
                            "seedCount": self.session.get("seedCount", 10),
                            "message": str(exc),
                        })
            finally:
                if filesystem_feedback:
                    self._finish_feedback_job()
                self.jobs.task_done()

    def _watch_loop(self):
        inbox = Path(self.session["worktree"]) / self.session.get("feedbackDir", ".webkit/feedback") / self.session["color"]
        while not self.stop_event.wait(1):
            if self.session.get("status") != "active":
                continue
            try:
                key = self._feedback_phase_key(inbox)
            except OSError:
                continue
            self._observe_feedback_phase(inbox, key)

    @staticmethod
    def _feedback_phase_key(inbox):
        missing = object()
        invalid = object()

        def load_document(name):
            path = Path(inbox) / name
            try:
                before = path.lstat()
                if not stat.S_ISREG(before.st_mode):
                    return invalid, None
                with path.open("r", encoding="utf-8") as handle:
                    opened = os.fstat(handle.fileno())
                    if not _opened_path_matches(
                        before, opened, path, handle.fileno()
                    ):
                        return invalid, None
                    raw = handle.read(1024 * 1024 + 1)
                    after_opened = os.fstat(handle.fileno())
                after = path.lstat()
                if (
                    _stat_stable_signature(after)
                    != _stat_stable_signature(before)
                    or _stat_stable_signature(after_opened)
                    != _stat_stable_signature(opened)
                    or (
                        not _WINDOWS_SPLIT_STAT_IDENTITIES
                        and _stat_stable_signature(opened)
                        != _stat_stable_signature(after)
                    )
                    or len(raw.encode("utf-8")) > 1024 * 1024
                ):
                    return invalid, None
                value = strict_json_loads(raw)
            except FileNotFoundError:
                return missing, None
            except (OSError, UnicodeDecodeError, ValueError):
                return invalid, None
            if not isinstance(value, dict):
                return invalid, None
            return value, hashlib.sha256(raw.encode("utf-8")).hexdigest()

        def valid_document(value, kind):
            if value in (missing, invalid) or value.get("version") != 1:
                return False
            if value.get("kind") != kind:
                return False
            batch_id = value.get("batchId")
            round_number = value.get("round")
            return (
                isinstance(batch_id, str)
                and bool(batch_id)
                and isinstance(round_number, int)
                and not isinstance(round_number, bool)
                and round_number >= 1
            )

        feedback, feedback_mtime = load_document("feedback.json")
        review, _review_mtime = load_document("review.json")
        verdicts, verdicts_mtime = load_document("verdicts.json")
        if invalid in (feedback, review, verdicts):
            return None

        feedback_valid = valid_document(feedback, "feedback")
        review_valid = valid_document(review, "review")
        verdict_kind = verdicts.get("kind") if verdicts is not missing else None
        verdicts_valid = verdict_kind in ("verdicts", "feedback_update") and valid_document(
            verdicts, verdict_kind
        )

        if verdicts is not missing:
            if not feedback_valid or not verdicts_valid:
                return None
            if verdicts.get("batchId") != feedback.get("batchId"):
                return None
            if review is missing:
                if (
                    verdict_kind == "feedback_update"
                    and verdicts.get("round") == feedback.get("round")
                ):
                    return "verdicts", verdicts_mtime
                return None
            if not review_valid:
                return None
            if not (
                review.get("batchId") == feedback.get("batchId")
                and verdicts.get("batchId") == review.get("batchId")
                and feedback.get("round") == review.get("round")
                and verdicts.get("round") == review.get("round")
            ):
                return None
            return "verdicts", verdicts_mtime

        if review is not missing:
            return None
        if feedback_valid:
            return "feedback", feedback_mtime
        return None

    def _set_process(self, process):
        rejected = False
        with self.process_lock:
            if process is not None and self.stop_event.is_set():
                rejected = True
            else:
                self.current_process = process
        if rejected:
            stop_process_tree(process)
            return False
        return True

    def preview_output(self):
        if not self.preview_log:
            return ""
        try:
            return self.preview_log.tail(MAX_PREVIEW_LOG_TAIL_CHARS)
        except (AttributeError, OSError, ValueError):
            return ""

    def stop_preview(self):
        if self.preview_process and self.preview_process.poll() is None:
            stop_process_tree(self.preview_process)
        self.preview_process = None
        if self.preview_log:
            try:
                self.preview_log.close()
            except OSError:
                pass
            self.preview_log = None

    def stop(self):
        self.stop_event.set()
        self.jobs.put(None)
        with self.process_lock:
            process = self.current_process
        stop_process_tree(process)
        with self.process_lock:
            if self.current_process is process:
                self.current_process = None
        self.stop_preview()
        current = threading.current_thread()
        for thread in (self.worker, self.watcher):
            if thread is not current and thread.is_alive():
                thread.join(timeout=6)


class SessionManager:
    def __init__(self, store, projects):
        self.store = store
        self.projects = projects
        self.runtimes = {}
        self.lock = threading.RLock()
        self.shutdown_event = threading.Event()

    def list_sessions(self, project_id=None):
        sessions = self.store.read().get("sessions", [])
        if project_id:
            sessions = [s for s in sessions if s.get("projectId") == project_id]
        return [public_session(session) for session in sessions]

    def start_session(self, project_id, color, reasoning_effort="medium"):
        project = self.projects.get_project(project_id)
        project_path = Path(project["path"])
        if not project_path.is_dir():
            raise ControlCenterError("Project folder is missing.", 404)
        with self.lock:
            if self.shutdown_event.is_set():
                raise ControlCenterError("The Control Center is shutting down.", 409)
            for session in self.list_sessions(project_id):
                if session.get("color") == color and session.get("status") in ("active", "busy", "merging", "error"):
                    return public_session(session)
            config = load_webkit_config(
                project_path / "webkit" / "webkit.config.json",
                project_path,
                require_default_page=True,
            )
            require_private_runtime_paths_safe(project_path, config)
            dirty = private_safe_git_status(project_path, config).stdout.strip()
            if dirty:
                raise ControlCenterError("The Control Center checkout has uncommitted changes.", 409)
            current_branch = run_command(["git", "branch", "--show-current"], cwd=project_path).stdout.strip()
            base_branch = project.get("baseBranch", "main")
            if current_branch != base_branch:
                raise ControlCenterError("The Control Center checkout is not on its managed base branch.", 409)
            sync = self.projects.sync_from_github(project)
            if sync.get("connected") and sync.get("error"):
                raise ControlCenterError(
                    "GitHub {} could not be synced: {}".format(
                        project.get("targetBranch", "main"), sync["error"]
                    ),
                    409,
                )
            config = load_webkit_config(
                project_path / "webkit" / "webkit.config.json",
                project_path,
                require_default_page=True,
            )
            require_private_runtime_paths_safe(project_path, config)
            if private_safe_git_status(project_path, config).stdout.strip():
                raise ControlCenterError(
                    "The Control Center checkout changed while syncing GitHub.", 409
                )
            palette = {entry["slug"]: entry for entry in config.get("palette", [])}
            if color not in palette:
                raise ControlCenterError("That color is not configured for this project.", 404)
            provider = project["provider"]
            allowed_efforts = ("low", "medium", "high", "xhigh") if provider == "codex" else ("low", "medium", "high", "xhigh", "max")
            if reasoning_effort not in allowed_efforts:
                raise ControlCenterError("Choose a supported {} reasoning level.".format(provider), 409)
            if not self.projects.system_status()[provider]["installed"]:
                raise ControlCenterError("{} CLI is not installed.".format(provider), 409)

            session_id = uuid.uuid4().hex[:12]
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            branch = "webkit/{}/{}-{}".format(color, stamp, session_id[:6])
            worktree = self.store.state_dir / "worktrees" / project["slug"] / (color + "-" + session_id[:6])
            worktree.parent.mkdir(parents=True, exist_ok=True)
            base_sha = self.projects._resolved_commit(
                project_path,
                "refs/heads/{}".format(base_branch),
                "The managed session base branch",
            )
            ownership = self.projects._create_owned_worktree(
                project_path, worktree, branch, base_sha
            )
            entry = palette[color]
            default_page = str(config.get("default_page", "index.html")).lstrip("/")
            preview_url = "http://127.0.0.1:{}/{}".format(entry["port"], default_page)
            session = {
                "id": session_id,
                "projectId": project_id,
                "projectName": project["name"],
                "provider": provider,
                "color": color,
                "emoji": entry["emoji"],
                "port": entry["port"],
                "branch": branch,
                "worktree": str(worktree),
                "baseSha": base_sha,
                "worktreeDev": ownership["dev"],
                "worktreeIno": ownership["ino"],
                "previewUrl": preview_url,
                "feedbackDir": config.get("feedback_dir", ".webkit/feedback"),
                "status": "active",
                "threadId": None,
                "hasRun": False,
                "reasoningEffort": reasoning_effort,
                "mutationToken": uuid.uuid4().hex,
                "createdAt": utc_now(),
            }
            runtime = SessionRuntime(self, session)
            try:
                self._claim_and_preview(runtime, config)
            except Exception as primary_error:
                try:
                    self._release(runtime)
                except Exception:
                    runtime.stop_preview()
                removed, reason = self.projects._remove_owned_worktree(
                    project_path,
                    worktree,
                    branch,
                    base_sha,
                    (ownership["dev"], ownership["ino"]),
                )
                if not removed:
                    raise ControlCenterError(
                        "{}. The failed session worktree was preserved because {}.".format(
                            primary_error, reason
                        ),
                        getattr(primary_error, "status", 409),
                    ) from primary_error
                raise
            self.store.update(lambda state: state.setdefault("sessions", []).append(dict(session)))
            self.runtimes[session_id] = runtime
            runtime.log.append("system", "{} {} session started in an isolated worktree.".format(entry["emoji"], provider), "status")
            runtime.start()
            return public_session(session)

    def start_seed_session(self, project_id, seed_count=10, brief=""):
        with self.lock:
            for existing in self.store.read().get("sessions", []):
                if (
                    existing.get("projectId") == project_id
                    and existing.get("kind") == "seeds"
                    and existing.get("status") in ("active", "busy", "merging", "error")
                ):
                    return public_session(existing)
            return self._start_seed_session(project_id, seed_count, brief)

    def _start_seed_session(self, project_id, seed_count=10, brief=""):
        project = self.projects.get_project(project_id)
        project_path = Path(project["path"])
        brief = limited_text(
            brief, MAX_PROJECT_BRIEF_CHARS, "Brand and design brief"
        )
        brief_path = project_path / "project-context" / "BRAND-AND-DESIGN.md"
        brief_available = brief_path.is_file() and not brief_path.is_symlink()
        if brief and not brief_available:
            raise ControlCenterError(
                "The brand and design brief was not preserved in project-context.", 409
            )
        config = load_webkit_config(
            project_path / "webkit" / "webkit.config.json",
            project_path,
            require_default_page=True,
        )
        site_root_value = str(config.get("site_root", ".")).strip().strip("/") or "."
        relative_default = str(config.get("default_page", "index.html")).lstrip("/")
        production_page = (
            relative_default if site_root_value == "."
            else site_root_value + "/" + relative_default
        )
        seed_directory = (
            "seed-directions" if site_root_value == "."
            else site_root_value + "/seed-directions"
        )
        palette = [entry.get("slug") for entry in config.get("palette", []) if entry.get("slug")]
        active_colors = {
            session.get("color") for session in self.store.read().get("sessions", [])
            if session.get("projectId") == project_id
            and session.get("status") in ("active", "busy", "merging", "error")
        }
        lock_dir = configured_lock_dir(config)
        color = next((
            slug for slug in palette
            if slug not in active_colors and not (lock_dir / (slug + ".lock")).exists()
        ), None)
        if not color:
            raise ControlCenterError(
                "All five colors are busy. Finish or discard one agent before generating seeds.", 409
            )
        try:
            seed_count = int(seed_count)
        except (TypeError, ValueError):
            seed_count = 10
        seed_count = max(2, min(20, seed_count))
        context_note = (
            "The complete optional brand/design brief is stored at "
            "`project-context/BRAND-AND-DESIGN.md`. Read it from that file; "
            "do not rely on a shortened copy in this prompt.\n"
            if brief_available else
            "The user did not provide a written brand/design note. Infer carefully from the project name and any files.\n"
        )
        prompt = """Create {count} genuinely different website design seeds for `{name}`.

This is an onboarding exploration inside an isolated worktree. Do not replace
the production `{production_page}` yet. Read every useful file under `project-context/`
before designing. {context}

Build {count} polished, browser-ready, responsive static directions under
`{seed_directory}/seed-01/index.html` through `{seed_directory}/seed-{last}/index.html`.
Each seed must be a meaningfully different art direction, layout system,
typographic attitude, palette, and interaction idea, rather than a recolor. Use the
provided assets when relevant. Keep every direction self-contained and usable
through the existing local preview server without a build step.

Finally write `{seed_directory}/manifest.json` with this exact shape:
{{"version":1,"seeds":[{{"id":"seed-01","title":"short name","direction":"one-line art direction","summary":"what makes it distinct","path":"seed-directions/seed-01/index.html"}}]}}
Manifest paths are relative to the configured document root `{site_root}`.
Include one entry for every seed and validate that every path opens. Do not run
Git commands: Codex intentionally protects worktree Git metadata in its safe
sandbox. The trusted Control Center will validate and commit the completed seed
folder after this turn. Do not merge, push, or modify the controller checkout.
""".format(
            count=seed_count,
            last=str(seed_count).zfill(2),
            name=project["name"],
            context=context_note,
            seed_directory=seed_directory,
            site_root=site_root_value,
            production_page=production_page,
        )
        prompt = bounded_provider_prompt(prompt, "Seed generation prompt")
        session = self.start_session(project_id, color, "high")
        session_id = session["id"]

        def mark_session(state):
            for item in state.get("sessions", []):
                if item["id"] == session_id:
                    item["kind"] = "seeds"
                    item["seedCount"] = seed_count
                    item["seedStage"] = "generating"
            for item in state.get("projects", []):
                if item["id"] == project_id:
                    item["onboarding"] = {
                        "status": "generating",
                        "sessionId": session_id,
                        "seedCount": seed_count,
                    }
        self.store.update(mark_session)
        runtime = self._runtime(session_id)
        runtime.session["kind"] = "seeds"
        runtime.session["seedCount"] = seed_count
        runtime.session["seedStage"] = "generating"
        def save_generation_prompt(state):
            for item in state.get("sessions", []):
                if item["id"] == session_id:
                    item["seedPrompt"] = prompt
                    break
        self.store.update(save_generation_prompt)
        runtime.session["seedPrompt"] = prompt
        self._set_session_status(session_id, "busy")
        runtime.enqueue(prompt, "seed-generation", display="Generating {} distinct design seeds…".format(seed_count))
        return public_session(self._get_session(session_id))

    def seed_status(self, session_id):
        session = self._get_session(session_id)
        if session.get("kind") != "seeds":
            raise ControlCenterError("This is not a seed onboarding session.", 409)
        project = self.projects.get_project(session["projectId"])
        onboarding = project.get("onboarding") or {}
        if onboarding.get("status") == "complete":
            return {"status": "complete", "complete": True, "ready": False, "seeds": []}
        worktree = Path(session["worktree"])
        config = load_webkit_config(
            worktree / "webkit" / "webkit.config.json", worktree, require_default_page=True
        )
        served_root = self._served_root(worktree, config)
        manifest_path = served_root / "seed-directions" / "manifest.json"
        result = {
            "status": onboarding.get("status") or session.get("status", "generating"),
            "sessionStatus": session.get("status"),
            "complete": False,
            "ready": False,
            "error": session.get("error"),
            "seeds": [],
        }
        try:
            manifest_path = safe_project_target(worktree, manifest_path)
            manifest = strict_json_loads(
                read_stable_regular_text(
                    manifest_path, MAX_SEED_MANIFEST_BYTES, "Seed manifest"
                )
            )
        except (ControlCenterError, ValueError) as exc:
            if manifest_path.exists() or manifest_path.is_symlink():
                result["error"] = "Seed manifest is invalid: {}".format(exc)
            return result
        if (
            not isinstance(manifest, dict)
            or set(manifest) != {"version", "seeds"}
            or manifest.get("version") != 1
            or not isinstance(manifest.get("seeds"), list)
            or not 1 <= len(manifest["seeds"]) <= 20
        ):
            result["error"] = "Seed manifest does not match the required version 1 schema."
            return result
        base = served_root.resolve()
        seen = set()
        seeds = []
        required_seed_keys = {"id", "title", "direction", "summary", "path"}
        for raw in manifest["seeds"]:
            if (
                not isinstance(raw, dict)
                or set(raw) != required_seed_keys
                or any(not isinstance(raw.get(key), str) for key in required_seed_keys)
            ):
                result["error"] = "Every seed manifest entry must match the required schema."
                return result
            seed_id = raw["id"].strip()
            rel = raw["path"].strip().replace("\\", "/")
            raw_target = served_root / rel
            try:
                raw_target = safe_project_target(worktree, raw_target)
            except ControlCenterError as exc:
                result["error"] = "Seed manifest path is invalid: {}".format(exc)
                return result
            target = raw_target.resolve()
            inside = path_is_within(target, base)
            if (
                not seed_id
                or len(seed_id) > 100
                or seed_id in seen
                or not rel
                or Path(rel).is_absolute()
                or any(part == ".." for part in Path(rel).parts)
                or not inside
                or raw_target.is_symlink()
                or not target.is_file()
            ):
                result["error"] = "Seed manifest contains a duplicate, missing, or unsafe seed."
                return result
            seen.add(seed_id)
            seeds.append({
                "id": seed_id,
                "title": raw["title"][:100],
                "direction": raw["direction"][:240],
                "summary": raw["summary"][:500],
                "path": rel,
                "previewUrl": "http://127.0.0.1:{}/{}?wk_seed_preview=1".format(session["port"], rel),
            })
        expected = int(session.get("seedCount") or 0)
        result["seeds"] = seeds
        if len(seeds) >= expected >= 2 and session.get("status") not in ("busy", "merging"):
            commit_error = self._commit_generated_seeds(
                worktree, expected,
                served_root.relative_to(worktree.resolve()).as_posix(),
                session.get("feedbackDir"),
            )
            if commit_error:
                result["error"] = commit_error
        try:
            clean = private_safe_git_status(
                worktree, config, session.get("feedbackDir")
            )
        except ControlCenterError as exc:
            result["error"] = str(exc)
            return result
        result["ready"] = (
            len(seeds) >= expected >= 2
            and session.get("status") != "busy"
            and clean.returncode == 0
            and not clean.stdout.strip()
        )
        if result["ready"]:
            result["status"] = "review"
        return result

    @staticmethod
    def _served_root(worktree, config):
        worktree = Path(worktree).resolve()
        served_root = (worktree / str(config.get("site_root", "."))).resolve()
        if not path_is_within(served_root, worktree):
            raise ControlCenterError("Webkit site_root must stay inside the project.", 409)
        if not served_root.is_dir():
            raise ControlCenterError("The configured Webkit site_root does not exist.", 409)
        return served_root

    @staticmethod
    def _worktree_change_paths(worktree):
        paths = set()
        commands = (
            ["git", "diff", "--name-only"],
            ["git", "diff", "--cached", "--name-only"],
            ["git", "ls-files", "--others", "--exclude-standard"],
        )
        for command in commands:
            inspected = run_command(command, cwd=worktree, check=False)
            if inspected.returncode != 0:
                raise ControlCenterError(
                    (inspected.stderr or inspected.stdout or "Could not inspect generated files.").strip(), 409
                )
            paths.update(line.strip() for line in inspected.stdout.splitlines() if line.strip())
        return paths

    def _commit_generated_seeds(
        self, worktree, expected, site_root=".", original_feedback_dir=None
    ):
        """Commit validated seed output outside the coding-agent sandbox."""
        seed_path = (
            "seed-directions" if site_root in ("", ".")
            else site_root.rstrip("/") + "/seed-directions"
        )
        with self.lock:
            try:
                config = load_webkit_config(
                    Path(worktree) / "webkit" / "webkit.config.json",
                    worktree,
                    require_default_page=True,
                )
                require_private_runtime_paths_safe(
                    worktree, config, original_feedback_dir
                )
                paths = self._worktree_change_paths(worktree)
                if not paths:
                    return None
                unexpected = sorted(
                    path for path in paths
                    if path != seed_path and not path.startswith(seed_path + "/")
                )
                if unexpected:
                    return "The seed agent changed files outside {}: {}".format(
                        seed_path, ", ".join(unexpected[:8])
                    )
                run_command(["git", "add", "-A", "--", seed_path], cwd=worktree)
                require_private_runtime_paths_safe(
                    worktree, config, original_feedback_dir
                )
                staged = run_command(["git", "diff", "--cached", "--quiet"], cwd=worktree, check=False)
                if staged.returncode == 1:
                    self.projects._commit_validated_index(
                        worktree, "Generate {} design seeds".format(expected)
                    )
                elif staged.returncode != 0:
                    return "The Control Center could not validate the generated seed commit."
            except ControlCenterError as exc:
                return "The Control Center could not commit the generated seeds: {}".format(exc)
        return None

    def choose_seeds(self, session_id, selected, notes=""):
        with self.lock:
            return self._choose_seeds(session_id, selected, notes)

    def _choose_seeds(self, session_id, selected, notes=""):
        session = self._get_session(session_id)
        if session.get("status") in ("busy", "merging"):
            raise ControlCenterError("Wait for the seed agent to finish first.", 409)
        status = self.seed_status(session_id)
        if not status["ready"]:
            raise ControlCenterError("The seed directions are not ready yet.", 409)
        allowed = {seed["id"]: seed for seed in status["seeds"]}
        selected_ids = []
        for value in selected if isinstance(selected, list) else []:
            value = str(value)
            if value in allowed and value not in selected_ids:
                selected_ids.append(value)
        if not selected_ids:
            raise ControlCenterError("Choose at least one seed to continue.")
        runtime = self._runtime(session_id)
        project = self.projects.get_project(session["projectId"])
        worktree = Path(session["worktree"])
        config = load_webkit_config(
            worktree / "webkit" / "webkit.config.json",
            worktree,
            require_default_page=True,
        )
        site_root_value = str(config.get("site_root", ".")).strip().strip("/") or "."
        seed_directory = (
            "seed-directions" if site_root_value == "."
            else site_root_value + "/seed-directions"
        )
        relative_default = str(config.get("default_page", "index.html")).lstrip("/")
        production_page = (
            relative_default if site_root_value == "."
            else site_root_value + "/" + relative_default
        )
        marker = safe_project_target(
            worktree, worktree / ".webkit" / "seed-selection.json"
        )
        notes_path = safe_project_target(
            worktree, worktree / ".webkit" / "seed-combination-notes.md"
        )
        selected_lines = "\n".join(
            "- {id}: {title} at {path}: {summary}".format(**allowed[seed_id])
            for seed_id in selected_ids
        )
        notes = limited_text(
            notes, MAX_SEED_NOTES_CHARS, "Seed combination notes"
        )
        notes_instruction = (
            "Read the complete notes from `.webkit/seed-combination-notes.md`."
            if notes else
            "No extra notes were provided. Preserve the clearest chosen direction."
        )
        prompt = """Turn the chosen onboarding seed direction into the production website.

Chosen seeds:
{selected}

Combination notes from the user:
{notes_instruction}

Inspect those seed pages and all `project-context/` references. If several
seeds were chosen, combine only their strongest relevant parts according to
the notes; produce one coherent design system, not a collage. Replace the real
production site starting at `{default_page}` with the finished responsive
website. Move any needed assets into sensible production locations and remove
the entire `{seed_directory}/` exploration folder when finished.

Do not run Git commands: Codex intentionally protects worktree Git metadata in
its safe sandbox. The trusted Control Center will commit the finished files,
integrate `{base}`, and update the controller checkout after validation. Do not
merge, push, delete this worktree, or change the controller checkout yourself.
When the production files are complete and `{seed_directory}/` is removed,
write exactly {{"status":"ready","message":"ready to finish onboarding"}}
to `{marker}`. If user input is required, write
{{"status":"conflict","message":"<short question>"}} instead.
""".format(
            selected=selected_lines,
            notes_instruction=notes_instruction,
            default_page=production_page,
            seed_directory=seed_directory,
            base=project.get("baseBranch", "main"),
            marker=str(marker),
        )
        prompt = bounded_provider_prompt(prompt, "Seed finalization prompt")
        if notes:
            notes_path.parent.mkdir(parents=True, exist_ok=True)
            safe_project_target(worktree, notes_path)
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _BINARY_OPEN_FLAG
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(str(notes_path), flags, 0o600)
            if os.name == "posix":
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(notes + "\n")
        else:
            unlink_if_exists(notes_path)
        unlink_if_exists(marker)
        def save_finalize_prompt(state):
            for item in state.get("sessions", []):
                if item["id"] == session_id:
                    item["seedFinalizePrompt"] = prompt
                    break
        self.store.update(save_finalize_prompt)
        runtime.session["seedFinalizePrompt"] = prompt
        self._set_seed_stage(session_id, "finalizing")
        self._set_project_onboarding(project["id"], {
            "status": "finalizing",
            "sessionId": session_id,
            "seedCount": session.get("seedCount", len(status["seeds"])),
            "selected": selected_ids,
        })
        self._set_session_status(session_id, "merging")
        runtime.enqueue(prompt, "seed-finalize", display="Building the chosen seed direction into the website…")
        return {"queued": True, "selected": selected_ids}

    def _set_project_onboarding(self, project_id, value):
        def mutate(state):
            for project in state.get("projects", []):
                if project["id"] == project_id:
                    project["onboarding"] = dict(value)
                    return
        self.store.update(mutate)

    def _set_seed_stage(self, session_id, stage):
        def mutate(state):
            for session in state.get("sessions", []):
                if session["id"] == session_id:
                    session["seedStage"] = stage
                    break
        self.store.update(mutate)
        if session_id in self.runtimes:
            self.runtimes[session_id].session["seedStage"] = stage

    def _complete_seed_generation(self, session_id):
        with self.lock:
            session = self._get_session(session_id)
            if session.get("seedStage") != "generating":
                return
            self._set_session_status(session_id, "active")
            status = self.seed_status(session_id)
            if not status["ready"]:
                raise ControlCenterError(
                    status.get("error") or
                    "The agent finished without a complete, valid seed manifest. Open its chat to continue.", 409
                )
            self._set_project_onboarding(session["projectId"], {
                "status": "review",
                "sessionId": session_id,
                "seedCount": session.get("seedCount", len(status["seeds"])),
            })
            self._set_seed_stage(session_id, "review")
            EventLog(self.store.state_dir, session_id).append(
                "system", "The design seeds are ready to review in the Control Center.", "status"
            )

    def _complete_seed_onboarding(self, session_id):
        with self.lock:
            return self._complete_seed_onboarding_locked(session_id)

    def _complete_seed_onboarding_locked(self, session_id):
        session = self._get_session(session_id)
        if session.get("status") != "merging" or session.get("seedStage") != "finalizing":
            return
        project = self.projects.get_project(session["projectId"])
        worktree = Path(session["worktree"])
        marker = safe_project_target(
            worktree, worktree / ".webkit" / "seed-selection.json"
        )
        try:
            result = read_agent_result(marker, "Seed finish result")
        except (ControlCenterError, ValueError) as exc:
            raise ControlCenterError("The seed agent did not return a valid finish result: {}".format(exc), 409)
        unlink_if_exists(marker)
        if result.get("status") != "ready":
            message = result.get("message") or "The seed agent needs a decision before it can finish."
            self._set_project_onboarding(project["id"], {
                "status": "error", "sessionId": session_id, "message": message,
            })
            raise ControlCenterError(message, 409)
        config = load_webkit_config(
            worktree / "webkit" / "webkit.config.json", worktree, require_default_page=True
        )
        served_root = self._served_root(worktree, config)
        default_page = served_root / str(config.get("default_page", "index.html")).lstrip("/")
        if not default_page.is_file():
            raise ControlCenterError("The seed agent did not produce the configured website entry page.", 409)
        if (served_root / "seed-directions").exists():
            raise ControlCenterError("The seed agent did not finish cleaning up the exploration folder.", 409)
        require_private_runtime_paths_safe(
            worktree, config, session.get("feedbackDir")
        )
        if private_safe_git_status(
            worktree, config, session.get("feedbackDir")
        ).stdout.strip():
            stage_without_private_runtime(
                worktree, config, session.get("feedbackDir")
            )
            self.projects._commit_validated_index(
                worktree, "Build website from selected design seeds"
            )
        initial_base_sha = str(session.get("baseSha") or "").lower()
        if re.fullmatch(r"[0-9a-f]{40,64}", initial_base_sha) is None:
            raise ControlCenterError(
                "This seed session has no immutable base pin and was preserved.", 409
            )
        require_session_history_private(
            worktree,
            initial_base_sha,
            config,
            session.get("feedbackDir"),
        )
        premerge_sha = self.projects._resolved_commit(
            worktree, "HEAD", "The finalized seed session"
        )
        self.projects._reject_history_secrets(
            worktree,
            initial_base_sha,
            premerge_sha,
            "Seed session commit history",
        )
        require_private_runtime_paths_safe(
            worktree, config, session.get("feedbackDir")
        )
        base_branch = project.get("baseBranch", "main")
        merge_base_sha = self.projects._resolved_commit(
            worktree,
            "refs/heads/{}".format(base_branch),
            "The current managed base branch",
        )
        run_command(
            ["git", "merge", "--no-edit", "--", merge_base_sha], cwd=worktree
        )
        config = load_webkit_config(
            worktree / "webkit" / "webkit.config.json",
            worktree,
            require_default_page=True,
        )
        require_private_runtime_paths_safe(
            worktree, config, session.get("feedbackDir")
        )
        require_session_history_private(
            worktree,
            merge_base_sha,
            config,
            session.get("feedbackDir"),
        )
        session_sha = self.projects._resolved_commit(
            worktree, "HEAD", "The integrated seed session"
        )
        self.projects._reject_history_secrets(
            worktree,
            merge_base_sha,
            session_sha,
            "Seed session commit history",
        )
        if run_command(
            ["git", "merge-base", "--is-ancestor", merge_base_sha, session_sha],
            cwd=worktree,
            check=False,
        ).returncode != 0:
            raise ControlCenterError(
                "The managed base commit was not integrated into the seed session.",
                409,
            )
        if private_safe_git_status(
            worktree, config, session.get("feedbackDir")
        ).stdout.strip():
            raise ControlCenterError("The finalized seed worktree is not clean after integration.", 409)
        project_path = Path(project["path"])
        self.projects._require_owned_worktree_at_sha(
            project_path,
            worktree,
            session["branch"],
            session_sha,
            (session.get("worktreeDev"), session.get("worktreeIno")),
            "The validated seed worktree",
        )
        project_config = load_webkit_config(
            project_path / "webkit" / "webkit.config.json",
            project_path,
            require_default_page=True,
        )
        require_private_runtime_paths_safe(project_path, project_config)
        if private_safe_git_status(project_path, project_config).stdout.strip():
            raise ControlCenterError("The controller checkout has uncommitted changes.", 409)
        current_base_sha = self.projects._resolved_commit(
            project_path,
            "refs/heads/{}".format(base_branch),
            "The controller base branch",
        )
        if current_base_sha != merge_base_sha:
            raise ControlCenterError(
                "The controller base branch changed after seed validation.", 409
            )
        run_command(
            ["git", "merge", "--ff-only", "--", session_sha], cwd=project_path
        )
        merged_config = load_webkit_config(
            project_path / "webkit" / "webkit.config.json",
            project_path,
            require_default_page=True,
        )
        require_private_runtime_paths_safe(
            project_path, merged_config, session.get("feedbackDir")
        )
        if (
            self.projects._resolved_commit(
                project_path, "HEAD", "The merged controller checkout HEAD"
            ) != session_sha
            or self.projects._resolved_commit(
                project_path,
                "refs/heads/{}".format(base_branch),
                "The merged controller base branch",
            ) != session_sha
            or private_safe_git_status(
                project_path, merged_config, session.get("feedbackDir")
            ).stdout.strip()
        ):
            raise ControlCenterError(
                "A Git hook changed the controller checkout after seed fast-forward; it was preserved.",
                409,
            )
        self.projects.integrate_managed_target(project, validated_sha=session_sha)
        github = self.projects.push_to_github(project, validated_sha=session_sha)
        if (
            self.projects._resolved_commit(
                project_path, "HEAD", "The post-push controller checkout HEAD"
            ) != session_sha
            or self.projects._resolved_commit(
                project_path,
                "refs/heads/{}".format(base_branch),
                "The post-push controller base branch",
            ) != session_sha
            or private_safe_git_status(
                project_path, merged_config, session.get("feedbackDir")
            ).stdout.strip()
        ):
            raise ControlCenterError(
                "The controller checkout changed during the GitHub push and was preserved.",
                409,
            )
        runtime = self.runtimes.get(session_id)
        if runtime:
            self._release(runtime)
        removed, reason = self.projects._remove_owned_worktree(
            project_path,
            worktree,
            session["branch"],
            session_sha,
            (session.get("worktreeDev"), session.get("worktreeIno")),
        )
        if not removed:
            raise ControlCenterError(
                "The merged seed session was preserved because {}.".format(reason),
                409,
            )
        self._set_session_status(session_id, "merged")
        self._set_seed_stage(session_id, "complete")
        self._set_project_onboarding(project["id"], {
            "status": "complete",
            "selected": (project.get("onboarding") or {}).get("selected", []),
            "completedAt": utc_now(),
            "githubPushed": bool(github.get("pushed")),
        })
        EventLog(self.store.state_dir, session_id).append(
            "system", "Seed onboarding finished and merged into {}{}.".format(
                project.get("targetBranch", "main"), "; GitHub updated" if github.get("pushed") else ""
            ), "status"
        )

    def _claim_and_preview(self, runtime, config, cancel_event=None):
        def cancelled():
            return self.shutdown_event.is_set() or (
                cancel_event is not None and cancel_event.is_set()
            )

        if cancelled():
            raise _RecoveryCancelled("Control Center startup was interrupted.")
        session = runtime.session
        worktree = Path(session["worktree"])
        config_path = worktree / "webkit" / "webkit.config.json"
        site_root = self._served_root(worktree, config)
        port = int(session["port"])
        env = scrubbed_child_environment()
        env["WK_CONFIG"] = str(config_path)
        owner = worktree.resolve()
        env["WK_COLOR_OWNER"] = str(owner)
        settings = normalized_settings(self.store.read().get("settings"))
        env["WK_DICTATION_MODE"] = settings["dictationMode"]
        env["WK_INTERACTION_MODE"] = settings["interactionMode"]
        env["WK_HOTKEY_TOGGLE"] = settings["toggleHotkey"]
        env["WK_HOTKEY_DICTATE"] = settings["dictateHotkey"]
        env["WK_MUTATION_TOKEN"] = runtime.mutation_token
        instance_token = uuid.uuid4().hex
        env["WK_PREVIEW_INSTANCE_TOKEN"] = instance_token
        env["PYTHONIOENCODING"] = "utf-8"
        env["WK_COLOR_LOCKDIR"] = str(configured_lock_dir(config))
        env["WK_PORT_LOCKDIR"] = str(runtime_registry_path())
        if runtime.claimed_lock is not None:
            claimed_lock = Path(runtime.claimed_lock)
            claimed_owner = Path(runtime.claimed_owner).resolve()
            try:
                existing_owner, reservation_token, _unused = read_runtime_color_lock(
                    claimed_lock
                )
                if Path(existing_owner).resolve() != owner or claimed_owner != owner:
                    raise RuntimeRegistryError("the saved color claim owner changed")
                if not reservation_token:
                    raise RuntimeRegistryError("the saved color claim has no port reservation")
                status, _record = inspect_runtime_reservation(
                    port, existing_owner, str(claimed_lock.absolute()),
                    session["color"], reservation_token,
                    config.get("grace_seconds", 180),
                )
                if status not in ("starting", "stale"):
                    raise RuntimeRegistryError(
                        "the saved port reservation is {}".format(status)
                    )
                if not touch_runtime_reservation(
                    port, existing_owner, str(claimed_lock.absolute()),
                    session["color"], reservation_token,
                ):
                    raise RuntimeRegistryError("the saved port reservation changed")
            except RuntimeRegistryError as exc:
                raise ControlCenterError(
                    "The existing preview claim is unsafe: {}".format(exc), 409
                )
        else:
            claimed_lock, claimed_owner = self._claim_lock(
                config, session["color"], owner
            )
        runtime.claimed_lock = claimed_lock
        runtime.claimed_owner = claimed_owner
        runtime.claimed_port = port
        runtime.claimed_color = session["color"]
        if not self._port_available(port):
            raise ControlCenterError(
                "Preview port {} is already in use after the atomic Webkit claim. "
                "Stop the process using it or assign this project a different palette."
                .format(port),
                409,
            )
        if cancelled():
            raise _RecoveryCancelled("Control Center startup was interrupted.")
        server = worktree / "webkit" / "server" / "preview-server.py"
        runtime.preview_log = BoundedPreviewLog()
        runtime.preview_process = subprocess.Popen(
            [sys.executable, str(server), session["emoji"]],
            cwd=str(worktree), env=env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
            **provider_process_kwargs()
        )
        runtime.preview_log.start(runtime.preview_process.stdout)
        if cancelled():
            raise _RecoveryCancelled("Control Center startup was interrupted.")
        deadline = time.time() + 8
        while time.time() < deadline:
            if cancelled():
                raise _RecoveryCancelled("Control Center startup was interrupted.")
            if runtime.preview_process.poll() is not None:
                output = runtime.preview_output()
                raise ControlCenterError(
                    "Preview server failed: {}".format(output.strip() or "unknown startup error"), 409
                )
            if self._preview_instance_ready(port, instance_token):
                return
            time.sleep(0.15)
        raise ControlCenterError("Preview server did not start on port {}.".format(session["port"]), 500)

    @staticmethod
    def _port_available(port):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", int(port)))
            return True
        except OSError:
            return False
        finally:
            sock.close()

    @staticmethod
    def _preview_instance_ready(port, token):
        connection = http.client.HTTPConnection("127.0.0.1", int(port), timeout=0.3)
        try:
            connection.request("GET", "/__wk/state")
            response = connection.getresponse()
            response.read()
            return response.getheader("X-WK-Preview-Instance") == token
        except (OSError, http.client.HTTPException):
            return False
        finally:
            connection.close()

    @staticmethod
    def _claim_lock(config, color, owner):
        if not isinstance(color, str) or COLOR_SLUG.fullmatch(color) is None:
            raise ControlCenterError("The requested Webkit color is unsafe.", 409)
        configured_entries = {
            entry.get("slug"): entry for entry in config.get("palette", [])
            if isinstance(entry, dict) and entry.get("slug")
        }
        if color not in configured_entries:
            raise ControlCenterError("That color is not configured for this project.", 404)
        raw_lock_dir = configured_lock_dir(config)
        if not raw_lock_dir.is_absolute():
            raise ControlCenterError("Webkit lock_dir must be absolute.", 409)
        if raw_lock_dir.is_symlink():
            raise ControlCenterError("Webkit lock_dir cannot be a symbolic link.", 409)
        lock_dir = raw_lock_dir.resolve()
        if lock_dir == Path(lock_dir.anchor):
            raise ControlCenterError("Webkit lock_dir cannot be a filesystem root.", 409)
        owner = Path(owner).resolve()
        try:
            lock, _reservation = claim_runtime_color(
                lock_dir, color, configured_entries[color]["port"], str(owner),
                config.get("grace_seconds", 180),
            )
        except RuntimeReservationBusy as exc:
            raise ControlCenterError(
                "{} is already in use by another Webkit session: {}."
                .format(color.capitalize(), exc.status),
                409,
            )
        except RuntimeRegistryError as exc:
            raise ControlCenterError(
                "The Webkit color and port claim is unsafe: {}".format(exc), 409
            )
        return lock, owner

    def send_message(self, session_id, message, attachments=None):
        with self.lock:
            message = (message or "").strip()
            if attachments is None:
                attachments = []
            if not isinstance(attachments, list):
                raise ControlCenterError("Attachments must be a list of files.")
            if not message and not attachments:
                raise ControlCenterError("Message cannot be empty.")
            if len(attachments) > MAX_CHAT_ATTACHMENTS:
                raise ControlCenterError(
                    "Chat messages are limited to {} attachments.".format(MAX_CHAT_ATTACHMENTS),
                    413,
                )
            session = self._get_session(session_id)
            if session.get("status") in ("busy", "merging", "discarding"):
                raise ControlCenterError("Wait for the current agent task to finish first.", 409)
            if session.get("status") in TERMINAL_SESSION_STATUSES:
                raise ControlCenterError("That session is already closed.", 409)
            runtime = self._runtime(session_id)
            worktree = Path(runtime.session["worktree"]).resolve()
            folder = safe_project_target(
                worktree, worktree / ".webkit" / "chat-attachments" / session_id
            )
            existing_total, existing_count = self._chat_attachment_storage_usage(folder)
            total = 0
            prepared = []
            for item in attachments:
                if not isinstance(item, dict):
                    raise ControlCenterError("Every attachment must be a file object.")
                name = re.sub(
                    r"[^A-Za-z0-9._-]+", "-",
                    Path(str(item.get("name") or "attachment")).name,
                ).strip("-.") or "attachment"
                name = name[:120]
                try:
                    data = base64.b64decode(item.get("data") or "", validate=True)
                except (ValueError, TypeError):
                    raise ControlCenterError("An attachment was not valid base64 data.")
                total += len(data)
                if len(data) > MAX_CHAT_ATTACHMENT_BYTES:
                    raise ControlCenterError("Each attachment must be 20 MB or smaller.", 413)
                if total > MAX_CHAT_ATTACHMENTS_TOTAL_BYTES:
                    raise ControlCenterError("Chat attachments must be 20 MB total or smaller.", 413)
                target = folder / (uuid.uuid4().hex[:8] + "-" + name)
                safe_project_target(worktree, target)
                prepared.append((target, data))
            if existing_total + total > MAX_SESSION_CHAT_ATTACHMENT_BYTES:
                raise ControlCenterError(
                    "This session is limited to {} MB of retained chat attachments.".format(
                        MAX_SESSION_CHAT_ATTACHMENT_BYTES // (1024 * 1024)
                    ),
                    413,
                )
            if existing_count + len(prepared) > MAX_SESSION_CHAT_ATTACHMENT_FILES:
                raise ControlCenterError(
                    "This session is limited to {} retained chat attachment files.".format(
                        MAX_SESSION_CHAT_ATTACHMENT_FILES
                    ),
                    413,
                )

            paths = [str(target) for target, _data in prepared]
            display_message = message
            prompt = message
            if paths:
                prefix = (
                    "Attached local files (inspect these paths as part of the request):\n"
                    + "\n".join("- " + path for path in paths)
                )
                prompt = prefix + ("\n\n" + message if message else "")
                display_message = (
                    (display_message + "\n" if display_message else "")
                    + "📎 {} attachment{}".format(
                        len(paths), "" if len(paths) == 1 else "s"
                    )
                )
            if len(prompt.encode("utf-8")) > MAX_PROVIDER_PROMPT_BYTES:
                raise ControlCenterError(
                    "Messages and attachment paths must fit within the {} KB agent prompt limit.".format(
                        MAX_PROVIDER_PROMPT_BYTES // 1024
                    ),
                    413,
                )

            saved = []
            try:
                if prepared:
                    folder.mkdir(parents=True, exist_ok=True)
                    safe_project_target(worktree, folder)
                for target, data in prepared:
                    flags = (
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _BINARY_OPEN_FLAG
                    )
                    if hasattr(os, "O_NOFOLLOW"):
                        flags |= os.O_NOFOLLOW
                    fd = os.open(str(target), flags, 0o600)
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(data)
                    saved.append(str(target))
                self._set_session_status(session_id, "busy")
                runtime.enqueue(prompt, "chat", display=display_message)
            except Exception:
                for path in saved:
                    unlink_if_exists(path)
                if self._get_session(session_id).get("status") == "busy":
                    self._set_session_status(session_id, "active")
                raise
            return {"queued": True, "attachments": saved}

    @staticmethod
    def _chat_attachment_storage_usage(folder):
        """Count retained regular files without following any links."""
        folder = Path(folder)
        if not folder.exists():
            return 0, 0
        if folder.is_symlink() or not folder.is_dir():
            raise ControlCenterError(
                "Chat attachment storage must be a real directory.", 409
            )
        total = 0
        count = 0
        directory_count = 1
        pending = [folder]
        try:
            while pending:
                current = pending.pop()
                with os.scandir(str(current)) as entries:
                    for entry in entries:
                        if entry.is_symlink():
                            raise ControlCenterError(
                                "Chat attachment storage cannot contain symbolic links.", 409
                            )
                        details = entry.stat(follow_symlinks=False)
                        if stat.S_ISDIR(details.st_mode):
                            directory_count += 1
                            if directory_count > MAX_SESSION_CHAT_ATTACHMENT_DIRECTORIES:
                                raise ControlCenterError(
                                    "This session has too many chat attachment directories.",
                                    413,
                                )
                            pending.append(Path(entry.path))
                        elif stat.S_ISREG(details.st_mode):
                            count += 1
                            if count > MAX_SESSION_CHAT_ATTACHMENT_FILES:
                                raise ControlCenterError(
                                    "This session has too many retained chat attachment files.", 413
                                )
                            total += details.st_size
                            if total > MAX_SESSION_CHAT_ATTACHMENT_BYTES:
                                return total, count
                        else:
                            raise ControlCenterError(
                                "Chat attachment storage contains a special file.", 409
                            )
        except ControlCenterError:
            raise
        except OSError as exc:
            raise ControlCenterError(
                "Chat attachment storage could not be inspected: {}".format(exc), 409
            )
        return total, count

    def set_reasoning(self, session_id, effort):
        session = self._get_session(session_id)
        allowed = ("low", "medium", "high", "xhigh") if session["provider"] == "codex" else ("low", "medium", "high", "xhigh", "max")
        if effort not in allowed:
            raise ControlCenterError("Choose a supported reasoning level.", 409)
        def mutate(state):
            for item in state.get("sessions", []):
                if item["id"] == session_id:
                    item["reasoningEffort"] = effort
        self.store.update(mutate)
        session["reasoningEffort"] = effort
        if session_id in self.runtimes:
            self.runtimes[session_id].session["reasoningEffort"] = effort
        return {"reasoningEffort": effort}

    def events(self, session_id, after=0):
        self._get_session(session_id)
        if isinstance(after, str) and after.isdigit():
            after = int(after)
        if not (
            isinstance(after, int)
            and not isinstance(after, bool)
            and after >= 0
        ) and not (
            isinstance(after, str) and EVENT_CURSOR.fullmatch(after) is not None
        ):
            raise ControlCenterError("Event cursor is invalid.")
        return EventLog(self.store.state_dir, session_id).read_after(after)

    def merge(self, session_id):
        with self.lock:
            return self._merge(session_id)

    def _merge(self, session_id):
        session = self._get_session(session_id)
        if session.get("kind") == "seeds":
            raise ControlCenterError(
                "Finish or discard seed onboarding from the seed review screen.", 409
            )
        project = self.projects.get_project(session["projectId"])
        runtime = self.runtimes.get(session_id)
        if session.get("status") in ("busy", "merging"):
            raise ControlCenterError("Wait for the agent to finish before merging.", 409)
        if not runtime:
            raise ControlCenterError("The coding agent is not running for this color.", 409)
        self._prepare_worktree_merge(session, project)
        marker = Path(session["worktree"]) / ".webkit" / "control-center-merge.json"
        unlink_if_exists(marker)
        prompt = """Validate our work for {target} and resolve any file conflicts carefully.

The trusted Control Center has already committed the session work and started
integrating the local base branch `{base}`. Inspect the working tree, resolve
the contents of any conflicted files, and validate the resulting website. Do
not run Git commands, delete this worktree, or update the controller checkout.

When the branch is clean and ready to integrate, write exactly:
{{"status":"ready","message":"ready to merge"}}
to `{marker}`. If a conflict needs the user, leave the worktree intact and
write {{"status":"conflict","message":"<short explanation and question>"}}
to that file instead. The Control Center will stage and commit the resolved
files, verify the integrated history, then perform the final fast-forward,
optional GitHub push, and lifecycle cleanup after your ready signal.
""".format(
            base=project.get("baseBranch", "main"),
            target=project.get("targetBranch", "main"),
            marker=str(marker),
        )
        self._set_session_status(session_id, "merging")
        runtime.enqueue(prompt, "merge")
        return {"queued": True, "agentManaged": False, "controllerManaged": True}

    @staticmethod
    def _git_merge_in_progress(worktree):
        result = run_command(
            ["git", "rev-parse", "--verify", "--quiet", "MERGE_HEAD"],
            cwd=worktree,
            check=False,
        )
        return result.returncode == 0

    @staticmethod
    def _unmerged_paths(worktree):
        result = run_command(
            ["git", "diff", "--name-only", "--diff-filter=U"],
            cwd=worktree,
            check=False,
        )
        if result.returncode != 0:
            raise ControlCenterError("Could not inspect merge conflicts.", 409)
        return [line for line in result.stdout.splitlines() if line.strip()]

    def _prepare_worktree_merge(self, session, project):
        worktree = Path(session["worktree"])
        base_branch = project.get("baseBranch", "main")
        initial_base_sha = str(session.get("baseSha") or "").lower()
        if re.fullmatch(r"[0-9a-f]{40,64}", initial_base_sha) is None:
            raise ControlCenterError(
                "This session predates immutable base pinning. Preserve its worktree and start a new session after committing any needed changes.",
                409,
            )
        config = load_webkit_config(
            worktree / "webkit" / "webkit.config.json",
            worktree,
            require_default_page=True,
        )
        require_private_runtime_paths_safe(
            worktree, config, session.get("feedbackDir")
        )
        if not self._git_merge_in_progress(worktree):
            if private_safe_git_status(
                worktree, config, session.get("feedbackDir")
            ).stdout.strip():
                stage_without_private_runtime(
                    worktree, config, session.get("feedbackDir")
                )
                staged = run_command(
                    ["git", "diff", "--cached", "--quiet"], cwd=worktree, check=False
                )
                if staged.returncode == 1:
                    self.projects._commit_validated_index(
                        worktree, "Prepare AWESOME WEBKIT session changes"
                    )
                elif staged.returncode != 0:
                    raise ControlCenterError("Could not validate the session changes.", 409)
            require_session_history_private(
                worktree, initial_base_sha, config, session.get("feedbackDir")
            )
            session_sha = self.projects._resolved_commit(
                worktree, "HEAD", "The prepared session commit"
            )
            self.projects._reject_history_secrets(
                worktree,
                initial_base_sha,
                session_sha,
                "Active session commit history",
            )
            merge_base_sha = self.projects._resolved_commit(
                worktree,
                "refs/heads/{}".format(base_branch),
                "The current managed base branch",
            )
            def save_merge_base(state):
                for item in state.get("sessions", []):
                    if item.get("id") == session["id"]:
                        item["mergeBaseSha"] = merge_base_sha
                        break
            self.store.update(save_merge_base)
            session["mergeBaseSha"] = merge_base_sha
            if session["id"] in self.runtimes:
                self.runtimes[session["id"]].session["mergeBaseSha"] = merge_base_sha
            merged = run_command(
                ["git", "merge", "--no-commit", "--no-edit", "--", merge_base_sha],
                cwd=worktree,
                check=False,
            )
            if merged.returncode != 0 and not self._unmerged_paths(worktree):
                run_command(["git", "merge", "--abort"], cwd=worktree, check=False)
                raise ControlCenterError(
                    (merged.stderr or merged.stdout or "Git could not prepare the base merge.").strip(),
                    409,
                )

    def _complete_agent_merge(self, session_id):
        with self.lock:
            return self._complete_agent_merge_locked(session_id)

    def _complete_agent_merge_locked(self, session_id):
        session = self._get_session(session_id)
        if session.get("status") != "merging" or session.get("kind") == "seeds":
            return
        project = self.projects.get_project(session["projectId"])
        worktree = Path(session["worktree"])
        config = load_webkit_config(
            worktree / "webkit" / "webkit.config.json",
            worktree,
            require_default_page=True,
        )
        require_private_runtime_paths_safe(
            worktree, config, session.get("feedbackDir")
        )
        marker = safe_project_target(
            worktree, worktree / ".webkit" / "control-center-merge.json"
        )
        try:
            result = read_agent_result(marker, "Merge result")
        except (ControlCenterError, ValueError) as exc:
            raise ControlCenterError("The agent did not return a valid merge result: {}".format(exc), 409)
        unlink_if_exists(marker)
        if result.get("status") != "ready":
            message = result.get("message") or "The agent needs help resolving a merge conflict."
            self._set_session_status(session_id, "error", message)
            EventLog(self.store.state_dir, session_id).append("system", message, "error")
            return
        unresolved = self._unmerged_paths(worktree)
        if unresolved:
            raise ControlCenterError(
                "The agent reported ready with unresolved conflicts: {}".format(
                    ", ".join(unresolved[:8])
                ),
                409,
            )
        stage_without_private_runtime(
            worktree, config, session.get("feedbackDir")
        )
        staged = run_command(
            ["git", "diff", "--cached", "--quiet"], cwd=worktree, check=False
        )
        if staged.returncode == 1:
            message = (
                "Integrate {} into AWESOME WEBKIT session".format(
                    project.get("baseBranch", "main")
                )
                if self._git_merge_in_progress(worktree)
                else "Finalize AWESOME WEBKIT session"
            )
            self.projects._commit_validated_index(worktree, message)
        elif staged.returncode != 0:
            raise ControlCenterError("Could not validate the resolved session changes.", 409)
        project_path = Path(project["path"])
        base_branch = project.get("baseBranch", "main")
        merge_base_sha = str(session.get("mergeBaseSha") or "").lower()
        if re.fullmatch(r"[0-9a-f]{40,64}", merge_base_sha) is None:
            raise ControlCenterError(
                "The session has no immutable merge-base pin and was preserved.", 409
            )
        session_sha = self.projects._resolved_commit(
            worktree, "HEAD", "The validated session branch"
        )
        require_session_history_private(
            worktree, merge_base_sha, config, session.get("feedbackDir")
        )
        self.projects._reject_history_secrets(
            worktree,
            merge_base_sha,
            session_sha,
            "Active session commit history",
        )
        ancestor = run_command(
            ["git", "merge-base", "--is-ancestor", merge_base_sha, session_sha],
            cwd=worktree,
            check=False,
        )
        if ancestor.returncode != 0:
            raise ControlCenterError(
                "The base branch changed or was not integrated. Retry the merge.", 409
            )
        require_private_runtime_paths_safe(
            worktree, config, session.get("feedbackDir")
        )
        if private_safe_git_status(
            worktree, config, session.get("feedbackDir")
        ).stdout.strip():
            raise ControlCenterError("The resolved color worktree is not clean.", 409)
        if self.projects._resolved_commit(
            worktree, "refs/heads/{}".format(session["branch"]),
            "The validated session branch",
        ) != session_sha:
            raise ControlCenterError(
                "The session branch changed after validation and was preserved.", 409
            )
        self.projects._require_owned_worktree_at_sha(
            project_path,
            worktree,
            session["branch"],
            session_sha,
            (session.get("worktreeDev"), session.get("worktreeIno")),
            "The validated session worktree",
        )
        project_config = load_webkit_config(
            project_path / "webkit" / "webkit.config.json",
            project_path,
            require_default_page=True,
        )
        require_private_runtime_paths_safe(project_path, project_config)
        if private_safe_git_status(project_path, project_config).stdout.strip():
            raise ControlCenterError("The controller checkout has uncommitted changes.", 409)
        current_base_sha = self.projects._resolved_commit(
            project_path,
            "refs/heads/{}".format(base_branch),
            "The controller base branch",
        )
        if current_base_sha != merge_base_sha:
            raise ControlCenterError(
                "The controller base branch changed after session validation. Retry the merge.",
                409,
            )
        run_command(
            ["git", "merge", "--ff-only", "--", session_sha], cwd=project_path
        )
        merged_config = load_webkit_config(
            project_path / "webkit" / "webkit.config.json",
            project_path,
            require_default_page=True,
        )
        require_private_runtime_paths_safe(
            project_path, merged_config, session.get("feedbackDir")
        )
        merged_head = self.projects._resolved_commit(
            project_path, "HEAD", "The merged controller checkout HEAD"
        )
        merged_branch = self.projects._resolved_commit(
            project_path,
            "refs/heads/{}".format(base_branch),
            "The merged controller base branch",
        )
        if (
            merged_head != session_sha
            or merged_branch != session_sha
            or private_safe_git_status(
                project_path, merged_config, session.get("feedbackDir")
            ).stdout.strip()
        ):
            raise ControlCenterError(
                "A Git hook changed the controller checkout after fast-forward; it was preserved for inspection.",
                409,
            )
        self.projects.integrate_managed_target(project, validated_sha=session_sha)
        github = self.projects.push_to_github(project, validated_sha=session_sha)
        if (
            self.projects._resolved_commit(
                project_path, "HEAD", "The post-push controller checkout HEAD"
            ) != session_sha
            or self.projects._resolved_commit(
                project_path,
                "refs/heads/{}".format(base_branch),
                "The post-push controller base branch",
            ) != session_sha
            or private_safe_git_status(
                project_path, merged_config, session.get("feedbackDir")
            ).stdout.strip()
        ):
            raise ControlCenterError(
                "The controller checkout changed during the GitHub push and was preserved.",
                409,
            )
        runtime = self.runtimes.get(session_id)
        if runtime:
            self._release(runtime)
        removed, reason = self.projects._remove_owned_worktree(
            project_path,
            worktree,
            session["branch"],
            session_sha,
            (session.get("worktreeDev"), session.get("worktreeIno")),
        )
        if not removed:
            raise ControlCenterError(
                "The merged session was preserved because {}.".format(reason), 409
            )
        self._set_session_status(session_id, "merged")
        EventLog(self.store.state_dir, session_id).append(
            "system",
            "Merged to {}{} and released the color.".format(
                project.get("targetBranch", "main"),
                "; GitHub updated" if github.get("pushed") else "",
            ),
            "status",
        )

    def _restart_preview(self, runtime):
        worktree = Path(runtime.session["worktree"])
        config_path = worktree / "webkit" / "webkit.config.json"
        config = load_webkit_config(config_path, worktree, require_default_page=True)
        runtime.stop_preview()
        try:
            self._claim_and_preview(runtime, config)
        except Exception:
            runtime.stop_preview()
            raise
        runtime.log.append("system", "Preview restarted to apply WebKit settings.", "status")

    def discard(self, session_id, confirmation):
        with self.lock:
            return self._discard(session_id, confirmation)

    def _discard(self, session_id, confirmation):
        session = self._get_session(session_id)
        if confirmation != session_id:
            raise ControlCenterError("Discard confirmation did not match the session.", 409)
        project = self.projects.get_project(session["projectId"])
        if session.get("status") in TERMINAL_SESSION_STATUSES:
            return {"discarded": session.get("status") == "discarded"}
        self._set_session_status(session_id, "discarding")
        runtime = self.runtimes.get(session_id)
        try:
            if runtime:
                self._release(runtime)
            project_path = Path(project["path"])
            worktree = Path(session["worktree"])
            removed, reason = self.projects._discard_owned_worktree(
                project_path,
                worktree,
                session.get("branch"),
                (session.get("worktreeDev"), session.get("worktreeIno")),
            )
            if not removed:
                raise ControlCenterError(
                    "Discard stopped because {}. The worktree and branch were preserved."
                    .format(reason),
                    409,
                )
            self._set_session_status(session_id, "discarded")
        except Exception as exc:
            self._set_session_status(session_id, "error", str(exc))
            raise
        if session.get("kind") == "seeds":
            self._set_project_onboarding(project["id"], {
                "status": "error",
                "seedCount": session.get("seedCount", 10),
                "message": "Seed onboarding was discarded. Start it again when ready.",
            })
        return {"discarded": True}

    def _release(self, runtime):
        session = runtime.session
        try:
            runtime.stop()
            lock = runtime.claimed_lock
            owner = runtime.claimed_owner
            if (
                isinstance(lock, (str, os.PathLike))
                and isinstance(owner, (str, os.PathLike))
            ):
                lock = Path(lock)
                port = runtime.claimed_port or session.get("port")
                color = runtime.claimed_color or session.get("color")
                if port is None or color is None:
                    raise ControlCenterError(
                        "The saved Webkit claim is missing its exact port or color; "
                        "it was not released.",
                        409,
                    )
                try:
                    release_runtime_color(
                        lock.parent, color, int(port), str(Path(owner).resolve())
                    )
                except (RuntimeRegistryError, RuntimeReservationBusy) as exc:
                    raise ControlCenterError(
                        "The Webkit claim could not be safely released: {}".format(exc),
                        409,
                    )
            runtime.claimed_lock = None
            runtime.claimed_owner = None
            runtime.claimed_port = None
            runtime.claimed_color = None
        finally:
            with self.lock:
                if self.runtimes.get(session["id"]) is runtime:
                    self.runtimes.pop(session["id"], None)

    def recover(self, cancel_event=None):
        """Recover sessions while retaining cleanup ownership until completion."""
        recovered_ids = []
        completed = False

        def cancelled():
            return self.shutdown_event.is_set() or (
                cancel_event is not None and cancel_event.is_set()
            )

        try:
            for session in self.store.read().get("sessions", []):
                if cancelled():
                    return False
                if session.get("status") not in ("active", "busy", "merging", "error"):
                    continue
                worktree = Path(session.get("worktree", ""))
                config_path = worktree / "webkit" / "webkit.config.json"
                if not worktree.is_dir() or not config_path.exists():
                    self._set_session_status(session["id"], "error", "Session worktree is missing.")
                    continue
                recovered_session = dict(session)
                if not recovered_session.get("mutationToken"):
                    recovered_session["mutationToken"] = uuid.uuid4().hex
                    self._set_session_mutation_token(
                        recovered_session["id"], recovered_session["mutationToken"]
                    )
                runtime = SessionRuntime(self, recovered_session)
                with self.lock:
                    if cancelled():
                        return False
                    if session["id"] in self.runtimes:
                        continue
                    self.runtimes[session["id"]] = runtime
                    recovered_ids.append(session["id"])
                try:
                    config = load_webkit_config(
                        config_path, worktree, require_default_page=True
                    )
                    self._claim_and_preview(runtime, config, cancel_event=cancel_event)
                    if cancelled():
                        raise _RecoveryCancelled("Control Center startup was interrupted.")
                    runtime.start()
                    runtime.log.append("system", "Session recovered after Control Center restart.", "status")
                    original_status = session.get("status")
                    if session.get("kind") == "seeds" and original_status in ("busy", "merging"):
                        if session.get("seedStage") == "finalizing" and session.get("seedFinalizePrompt"):
                            self._set_session_status(session["id"], "merging")
                            runtime.enqueue(
                                session["seedFinalizePrompt"], "seed-finalize",
                                display="Resuming the selected seed build after restart…",
                            )
                        elif session.get("seedStage") == "generating":
                            self._set_session_status(session["id"], "active")
                            status = self.seed_status(session["id"])
                            if status.get("ready"):
                                self._set_project_onboarding(session["projectId"], {
                                    "status": "review", "sessionId": session["id"],
                                    "seedCount": session.get("seedCount", len(status.get("seeds", []))),
                                })
                                self._set_seed_stage(session["id"], "review")
                            elif session.get("seedPrompt"):
                                self._set_session_status(session["id"], "busy")
                                runtime.enqueue(
                                    session["seedPrompt"], "seed-generation",
                                    display="Resuming seed generation after restart…",
                                )
                            else:
                                self._set_session_status(
                                    session["id"], "error",
                                    "Seed generation was interrupted and has no saved prompt to resume.",
                                )
                        else:
                            self._set_session_status(
                                session["id"], "error",
                                "Seed onboarding was interrupted in an unknown stage.",
                            )
                    elif original_status in ("busy", "merging"):
                        if session.get("pendingOperation") == "feedback":
                            self._clear_pending_operation(session["id"])
                            self._set_session_status(session["id"], "active")
                        else:
                            operation = session.get("pendingOperation") or "agent task"
                            self._set_session_status(
                                session["id"], "error",
                                "The {} was interrupted by a Control Center restart. Retry it.".format(
                                    operation
                                ),
                            )
                    if cancelled():
                        raise _RecoveryCancelled("Control Center startup was interrupted.")
                except _RecoveryCancelled:
                    self._release(runtime)
                    return False
                except Exception as exc:
                    try:
                        self._release(runtime)
                    except Exception:
                        runtime.stop_preview()
                    self._set_session_status(session["id"], "error", str(exc))
            completed = True
            return True
        finally:
            if not completed:
                for session_id in recovered_ids:
                    runtime = self.runtimes.get(session_id)
                    if runtime is not None:
                        self._release(runtime)

    def request_shutdown(self):
        self.shutdown_event.set()

    def shutdown(self):
        self.request_shutdown()
        with self.lock:
            runtimes = list(self.runtimes.values())
        for runtime in runtimes:
            self._release(runtime)

    def refresh_previews_for_settings(self):
        restarted = 0
        failed = []
        for session_id, runtime in list(self.runtimes.items()):
            try:
                self._restart_preview(runtime)
                restarted += 1
            except Exception as exc:
                failed.append({"sessionId": session_id, "error": str(exc)})
                runtime.log.append("system", "Preview restart failed: {}".format(exc), "error")
        return {"restarted": restarted, "deferred": 0, "failed": failed}

    def _runtime(self, session_id):
        runtime = self.runtimes.get(session_id)
        if not runtime:
            raise ControlCenterError("That session is not running in this Control Center process.", 409)
        return runtime

    def _get_session(self, session_id):
        for session in self.store.read().get("sessions", []):
            if session["id"] == session_id:
                return dict(session)
        raise ControlCenterError("Unknown session.", 404)

    def _set_thread(self, session_id, thread_id):
        def mutate(state):
            for session in state.get("sessions", []):
                if session["id"] == session_id:
                    session["threadId"] = thread_id
                    session["hasRun"] = True
        self.store.update(mutate)
        if session_id in self.runtimes:
            self.runtimes[session_id].session["threadId"] = thread_id
            self.runtimes[session_id].session["hasRun"] = True

    def _set_session_mutation_token(self, session_id, token):
        def mutate(state):
            for session in state.get("sessions", []):
                if session["id"] == session_id:
                    session["mutationToken"] = token
                    break
        self.store.update(mutate)
        if session_id in self.runtimes:
            self.runtimes[session_id].mutation_token = token
            self.runtimes[session_id].session["mutationToken"] = token

    def _set_pending_operation(self, session_id, operation, prompt=None):
        def mutate(state):
            for session in state.get("sessions", []):
                if session["id"] == session_id:
                    session["pendingOperation"] = operation
                    if prompt is not None:
                        session["pendingPrompt"] = prompt
                    else:
                        session.pop("pendingPrompt", None)
                    break
        self.store.update(mutate)
        runtime = self.runtimes.get(session_id)
        if runtime:
            runtime.session["pendingOperation"] = operation
            if prompt is not None:
                runtime.session["pendingPrompt"] = prompt
            else:
                runtime.session.pop("pendingPrompt", None)

    def _clear_pending_operation(self, session_id):
        def mutate(state):
            for session in state.get("sessions", []):
                if session["id"] == session_id:
                    session.pop("pendingOperation", None)
                    session.pop("pendingPrompt", None)
                    break
        self.store.update(mutate)
        runtime = self.runtimes.get(session_id)
        if runtime:
            runtime.session.pop("pendingOperation", None)
            runtime.session.pop("pendingPrompt", None)

    def _set_session_status(self, session_id, status, error=None):
        changed = []
        def mutate(state):
            for session in state.get("sessions", []):
                if session["id"] == session_id:
                    if (
                        session.get("status") in TERMINAL_SESSION_STATUSES
                        and status != session.get("status")
                    ):
                        return
                    session["status"] = status
                    session["updatedAt"] = utc_now()
                    if error:
                        session["error"] = error
                    elif "error" in session:
                        session.pop("error")
                    changed.append(True)
        self.store.update(mutate)
        if changed and session_id in self.runtimes:
            self.runtimes[session_id].session["status"] = status
            if error:
                self.runtimes[session_id].session["error"] = error
            else:
                self.runtimes[session_id].session.pop("error", None)
        return bool(changed)


class ControlCenter:
    def __init__(self, kit_root, state_dir, recover=True):
        self.store = StateStore(state_dir)
        self.projects = ProjectManager(kit_root, self.store)
        self.sessions = SessionManager(self.store, self.projects)
        if recover:
            try:
                self.sessions.recover()
            except BaseException:
                self.sessions.shutdown()
                raise

    def bootstrap(self):
        state = self.store.read()
        return {
            "providers": state.get("providers", []),
            "projects": self.projects.list_projects(),
            "sessions": [public_session(session) for session in state.get("sessions", [])],
            "system": self.projects.system_status(),
            "settings": normalized_settings(state.get("settings")),
        }

    def create_project(self, name, parent, provider, onboarding=None):
        onboarding = onboarding if isinstance(onboarding, dict) else {}
        project = self.projects.create_project(name, parent, provider, onboarding)
        github_setup = project.get("githubSetup") if isinstance(project, dict) else None
        try:
            session = self.sessions.start_seed_session(
                project["id"], onboarding.get("seedCount", 10), onboarding.get("brief", "")
            )
            project = self.projects.get_project(project["id"])
            return {
                "project": project,
                "seedSession": public_session(session),
                "githubSetup": github_setup,
            }
        except Exception as exc:
            self.sessions._set_project_onboarding(project["id"], {
                "status": "error", "message": str(exc), "seedCount": onboarding.get("seedCount", 10),
            })
            project = self.projects.get_project(project["id"])
            return {
                "project": project,
                "seedSession": None,
                "seedError": str(exc),
                "githubSetup": github_setup,
            }

    def start_project_seeds(self, project_id):
        project = self.projects.get_project(project_id)
        onboarding = project.get("onboarding") or {}
        session_id = onboarding.get("sessionId")
        if session_id:
            try:
                session = self.sessions._get_session(session_id)
                if session.get("status") in ("active", "busy", "merging", "error"):
                    return {"seedSession": public_session(session)}
            except ControlCenterError:
                pass
        session = self.sessions.start_seed_session(
            project_id, onboarding.get("seedCount", 10), ""
        )
        return {"seedSession": public_session(session)}

    def choose_folder(self, initial=None, purpose=None):
        prompt = (
            "Choose the parent folder for the new website"
            if purpose == "parent"
            else "Choose an existing website project"
        )
        selected = choose_folder(initial, prompt)
        return {"path": selected, "cancelled": selected is None}

    def _validated_settings(self, settings, current=None):
        submitted = settings if isinstance(settings, dict) else {}
        if current is None:
            current = normalized_settings(self.store.read().get("settings"))
        dictation_mode = submitted.get("dictationMode", current["dictationMode"])
        interaction_mode = submitted.get("interactionMode", current["interactionMode"])
        toggle_hotkey = submitted.get("toggleHotkey", current["toggleHotkey"])
        dictate_hotkey = submitted.get("dictateHotkey", current["dictateHotkey"])
        if dictation_mode not in ("speech", "voice-note"):
            raise ControlCenterError("Choose browser speech or agent voice notes.")
        if interaction_mode not in ("browse-default", "draw-default"):
            raise ControlCenterError("Choose normal website clicks or immediate rectangle drawing.")
        if not valid_hotkey(toggle_hotkey) or not valid_hotkey(dictate_hotkey):
            raise ControlCenterError("Choose a regular key, not a modifier or Escape, for each shortcut.")
        if toggle_hotkey == dictate_hotkey:
            raise ControlCenterError("Open/close and dictation need different shortcut keys.")
        saved = {
            "dictationMode": dictation_mode,
            "interactionMode": interaction_mode,
            "toggleHotkey": toggle_hotkey,
            "dictateHotkey": dictate_hotkey,
        }
        return saved

    def save_settings(self, settings):
        saved = self._validated_settings(settings)
        self.store.update(lambda state: state.update({"settings": saved}))
        refresh = self.sessions.refresh_previews_for_settings()
        return {"settings": saved, "previews": refresh}

    def save_preferences(self, preferences):
        submitted = preferences if isinstance(preferences, dict) else {}
        current_state = self.store.read()
        providers = self.projects.validated_providers(submitted.get("providers"))
        settings = self._validated_settings(
            submitted, normalized_settings(current_state.get("settings"))
        )
        self.store.update(
            lambda state: state.update({"providers": providers, "settings": settings})
        )
        refresh = self.sessions.refresh_previews_for_settings()
        return {"providers": providers, "settings": settings, "previews": refresh}
