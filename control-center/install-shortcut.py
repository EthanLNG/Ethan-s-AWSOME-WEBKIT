#!/usr/bin/env python3
"""Install a clickable desktop shortcut for the local Control Center."""

import base64
import hashlib
import json
import math
import os
import platform
import plistlib
import shutil
import stat
import struct
import subprocess
import tempfile
import zlib
from functools import lru_cache
from pathlib import Path


HERE = Path(__file__).resolve().parent
KIT_ROOT = HERE.parent

MACOS_APPLE_EVENTS_DESCRIPTION = (
    "AWESOME WEBKIT uses Chrome automation to reuse and refresh its local "
    "Control Center tab."
)
WEBKIT_MACOS_PRODUCT = {
    "display_name": "AWESOME WEBKIT",
    "bundle_id": "dev.ethanlang.awesome-webkit",
    "icon_name": "AppIcon.icns",
    "apple_events_description": MACOS_APPLE_EVENTS_DESCRIPTION,
}
MACOS_SHORTCUT_SCHEMA_VERSION = 2
MACOS_SHORTCUT_SCHEMA_KEY = "WKCCShortcutSchemaVersion"
MACOS_SHORTCUT_LAUNCHER_KEY = "WKCCLauncherPath"
MACOS_SHORTCUT_SOURCE_KEY = "WKCCShortcutSourceSHA256"
MACOS_APPLET_EXECUTABLE = "applet"
MACOS_APPLET_SOURCE = r'''ObjC.import("Foundation");

var launcherPath = __LAUNCHER_PATH__;

function runTask(executable, args, extraEnvironment) {
  var task = $.NSTask.alloc.init;
  var outputPipe = $.NSPipe.pipe;
  var environment = $.NSProcessInfo.processInfo.environment.mutableCopy;
  if (extraEnvironment) {
    Object.keys(extraEnvironment).forEach(function (key) {
      environment.setObjectForKey($(String(extraEnvironment[key])), $(key));
    });
  }
  task.launchPath = executable;
  task.arguments = $(args || []);
  task.environment = environment;
  task.standardOutput = outputPipe;
  task.standardError = $.NSFileHandle.fileHandleWithNullDevice;
  try {
    task.launch;
    var data = outputPipe.fileHandleForReading.readDataToEndOfFile;
    task.waitUntilExit;
    if (task.terminationStatus !== 0) return null;
    var value = $.NSString.alloc.initWithDataEncoding(data, $.NSUTF8StringEncoding);
    return value.isNil() ? null : value.js.replace(/[\r\n]+$/, "");
  } catch (error) {
    return null;
  }
}

function runStatus(executable, args) {
  var task = $.NSTask.alloc.init;
  task.launchPath = executable;
  task.arguments = $(args || []);
  task.standardOutput = $.NSFileHandle.fileHandleWithNullDevice;
  task.standardError = $.NSFileHandle.fileHandleWithNullDevice;
  try {
    task.launch;
    task.waitUntilExit;
    return task.terminationStatus;
  } catch (error) {
    return -1;
  }
}

function screenIsLocked() {
  var details = runTask("/usr/sbin/ioreg", ["-n", "Root", "-d1"], null);
  return details === null || details.indexOf("CGSSessionScreenIsLocked\"=Yes") !== -1;
}

function parseHandoff(value) {
  if (value === null || /[\r\n]/.test(value)) return null;
  var fields = value.split("\t");
  if (fields.length !== 3) return null;
  var targetPrefix = fields[0];
  var targetURL = fields[1];
  var lockPath = fields[2];
  var prefixMatch = targetPrefix.match(/^http:\/\/127\.0\.0\.1:([0-9]{1,5})\/$/);
  if (prefixMatch === null) return null;
  var port = Number(prefixMatch[1]);
  if (port < 1 || port > 65535) return null;
  var suffix = targetURL.slice(targetPrefix.length);
  if (targetURL.indexOf(targetPrefix) !== 0) return null;
  if (!/^\?token=[A-Za-z0-9_-]{20,256}&launch=[0-9a-f]{16}$/.test(suffix)) return null;
  if (!/^\/[^\t\r\n]+\/control-center-browser\.lock$/.test(lockPath)) return null;
  if (lockPath.split("/").indexOf("..") !== -1) return null;
  return {prefix: targetPrefix, url: targetURL, lockPath: lockPath};
}

function acquireBrowserLock(lockPath) {
  var ownerPID = String($.NSProcessInfo.processInfo.processIdentifier);
  for (var attempt = 0; attempt < 200; attempt += 1) {
    if (runStatus("/usr/bin/shlock", ["-f", lockPath, "-p", ownerPID]) === 0) {
      return true;
    }
    $.NSThread.sleepForTimeInterval(0.05);
  }
  return false;
}

function appleScriptString(value) {
  return "\"" + String(value)
    .replace(/\\/g, "\\\\")
    .replace(/\"/g, "\\\"")
    .replace(/\r/g, "\\r")
    .replace(/\n/g, "\\n") + "\"";
}

function chromeLookupScript(targetPrefix, targetURL) {
  return [
    "with timeout of 30 seconds",
    "if application \"Google Chrome\" is not running then return \"WKCC_MISS\"",
    "tell application \"Google Chrome\"",
    "set targetPrefix to " + appleScriptString(targetPrefix),
    "set targetURL to " + appleScriptString(targetURL),
    "repeat with windowNumber from 1 to count of windows",
    "repeat with tabNumber from 1 to count of tabs of window windowNumber",
    "set candidateURL to URL of tab tabNumber of window windowNumber",
    "if candidateURL starts with targetPrefix then",
    "set URL of tab tabNumber of window windowNumber to targetURL",
    "set active tab index of window windowNumber to tabNumber",
    "set minimized of window windowNumber to false",
    "set index of window windowNumber to 1",
    "activate",
    "return \"WKCC_REUSED\"",
    "end if",
    "end repeat",
    "end repeat",
    "return \"WKCC_MISS\"",
    "end tell",
    "end timeout"
  ].join("\n");
}

function chromeOpenScript(targetURL) {
  return [
    "with timeout of 30 seconds",
    "tell application \"Google Chrome\"",
    "set targetURL to " + appleScriptString(targetURL),
    "if count of windows is 0 then",
    "set targetWindow to make new window",
    "set URL of active tab of targetWindow to targetURL",
    "set active tab index of targetWindow to 1",
    "else",
    "set targetWindow to window 1",
    "tell targetWindow",
    "make new tab at end of tabs with properties {URL:targetURL}",
    "set active tab index to count of tabs",
    "set minimized to false",
    "set index to 1",
    "end tell",
    "end if",
    "activate",
    "return \"WKCC_OPENED\"",
    "end tell",
    "end timeout"
  ].join("\n");
}

function runAppleScript(source) {
  var script = $.NSAppleScript.alloc.initWithSource($(source));
  var scriptError = Ref();
  var result = script.executeAndReturnError(scriptError);
  if (result.isNil()) return null;
  var value = result.stringValue;
  return value.isNil() ? null : value.js;
}

function run() {
  var handoff = runTask(launcherPath, [], {WKCC_BROWSER_HANDOFF: "1"});
  var target = parseHandoff(handoff);
  if (target === null || screenIsLocked()) return;
  if (!acquireBrowserLock(target.lockPath)) return;
  try {
    var outcome = runAppleScript(chromeLookupScript(target.prefix, target.url));
    if (outcome === "WKCC_MISS") {
      runAppleScript(chromeOpenScript(target.url));
    }
  } finally {
    runStatus("/bin/rm", ["-f", target.lockPath]);
  }
}
'''


def _inside_rounded_rect(x, y, left, top, right, bottom, radius):
    if x < left or x >= right or y < top or y >= bottom:
        return False
    cx = min(max(x, left + radius), right - radius)
    cy = min(max(y, top + radius), bottom - radius)
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2


def _distance_to_segment(x, y, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    length = dx * dx + dy * dy
    if not length:
        return math.hypot(x - x1, y - y1)
    amount = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / length))
    return math.hypot(x - (x1 + amount * dx), y - (y1 + amount * dy))


@lru_cache(maxsize=7)
def brand_icon_png(size):
    """Render the Control Center's black W mark with its blue shadow."""
    size = int(size)
    if size < 16 or size > 1024:
        raise ValueError("icon size must be between 16 and 1024")
    scale = size / 512.0
    shadow = tuple(int(value) for value in (50, 148, 226, 255))
    ink = tuple(int(value) for value in (23, 23, 23, 255))
    white = (255, 255, 255, 255)
    rows = []
    segments = [
        (154, 150, 213, 336),
        (213, 336, 274, 216),
        (274, 216, 333, 336),
        (333, 336, 392, 150),
    ]
    stroke = 25 * scale
    for py in range(size):
        row = bytearray([0])
        y = (py + 0.5) / scale
        for px in range(size):
            x = (px + 0.5) / scale
            color = (0, 0, 0, 0)
            if _inside_rounded_rect(x, y, 86, 82, 448, 444, 82):
                color = shadow
            if _inside_rounded_rect(x, y, 62, 58, 424, 420, 82):
                color = ink
                if any(
                    _distance_to_segment(
                        px + 0.5, py + 0.5,
                        x1 * scale, y1 * scale, x2 * scale, y2 * scale,
                    ) <= stroke
                    for x1, y1, x2, y2 in segments
                ):
                    color = white
            row.extend(color)
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(kind, data):
        return (
            struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    header = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return header + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


@lru_cache(maxsize=1)
def brand_icon_icns():
    entries = []
    for kind, size in (
        (b"icp4", 16), (b"icp5", 32), (b"icp6", 64),
        (b"ic07", 128), (b"ic08", 256), (b"ic09", 512), (b"ic10", 1024),
    ):
        png = brand_icon_png(size)
        entries.append(kind + struct.pack(">I", len(png) + 8) + png)
    body = b"".join(entries)
    return b"icns" + struct.pack(">I", len(body) + 8) + body


def quoted_shell(path):
    return "'{}'".format(str(path).replace("'", "'\\''"))


def windows_launcher(script_path):
    encoded_path = base64.b64encode(
        str(script_path).encode("utf-8", "surrogatepass")
    ).decode("ascii")
    invoke = (
        "import base64,subprocess,sys;"
        "path=base64.b64decode(sys.argv[1]).decode('utf-8','surrogatepass');"
        "raise SystemExit(subprocess.call([sys.executable,path]+sys.argv[2:]))"
    )
    return (
        "@echo off\n"
        "where py >nul 2>nul\n"
        "if not errorlevel 1 py -3 -c \"import sys; raise SystemExit(sys.version_info[0] != 3 or sys.version_info[1] not in range(7, 100))\" >nul 2>nul\n"
        "if not errorlevel 1 goto use_py\n"
        "where python3 >nul 2>nul\n"
        "if not errorlevel 1 python3 -c \"import sys; raise SystemExit(sys.version_info[0] != 3 or sys.version_info[1] not in range(7, 100))\" >nul 2>nul\n"
        "if not errorlevel 1 goto use_python3\n"
        "where python >nul 2>nul\n"
        "if not errorlevel 1 python -c \"import sys; raise SystemExit(sys.version_info[0] != 3 or sys.version_info[1] not in range(7, 100))\" >nul 2>nul\n"
        "if not errorlevel 1 goto use_python\n"
        "echo AWESOME WEBKIT requires Python 3.7 or newer. Install Python and try again. 1>&2\n"
        "exit /b 1\n"
        ":use_py\n"
        "py -3 -c \"{}\" \"{}\" %*\n"
        "exit /b %errorlevel%\n"
        ":use_python3\n"
        "python3 -c \"{}\" \"{}\" %*\n"
        "exit /b %errorlevel%\n"
        ":use_python\n"
        "python -c \"{}\" \"{}\" %*\n"
        "exit /b %errorlevel%\n"
    ).format(invoke, encoded_path, invoke, encoded_path, invoke, encoded_path)


def windows_launcher_bytes(script_path):
    return windows_launcher(script_path).replace("\n", "\r\n").encode("utf-8")


def desktop_exec_quote(path):
    value = str(path).replace("%", "%%")
    exec_escaped = "".join("\\" + char if char in '\\"`$' else char for char in value)
    entry_escaped = exec_escaped.replace("\\", "\\\\")
    entry_escaped = entry_escaped.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return '"{}"'.format(entry_escaped)


def windows_desktop_directory():
    try:
        import ctypes
        from ctypes import wintypes

        buffer = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buffer) == 0 and buffer.value:
            return Path(buffer.value)
    except (AttributeError, OSError):
        pass
    return Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"


def linux_desktop_directory():
    executable = shutil.which("xdg-user-dir")
    if executable:
        result = subprocess.run(
            [executable, "DESKTOP"],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        value = result.stdout.strip()
        if result.returncode == 0 and value:
            return Path(value).expanduser()
    return Path.home() / "Desktop"


def desktop_directory(system=None):
    system = (system or platform.system()).lower()
    if system == "windows":
        return windows_desktop_directory()
    if system == "linux":
        return linux_desktop_directory()
    return Path.home() / "Desktop"


def regular_file_matches(path, data, mode=None):
    try:
        listed = path.lstat()
        if not stat.S_ISREG(listed.st_mode):
            return False
    except OSError:
        return False

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(str(path), flags)
    except OSError:
        return False
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            return False
        if (listed.st_dev, listed.st_ino) != (opened.st_dev, opened.st_ino):
            return False
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            matches = handle.read(len(data) + 1) == data
        try:
            current = path.lstat()
        except OSError:
            return False
        if not matches or (current.st_dev, current.st_ino) != (
            opened.st_dev,
            opened.st_ino,
        ):
            return False
        if (
            mode is not None
            and os.name == "posix"
            and mode & stat.S_IXUSR
            and not (current.st_mode & stat.S_IXUSR)
        ):
            return False
        return True
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _unlink_owned_shortcut(shortcut, created):
    """Remove only the exact file inode created by this installer attempt."""
    try:
        current = shortcut.lstat()
        if stat.S_ISREG(current.st_mode) and (current.st_dev, current.st_ino) == (
            created.st_dev,
            created.st_ino,
        ):
            os.unlink(str(shortcut))
    except OSError:
        pass


def write_shortcut_exclusive(shortcut, data, mode):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(str(shortcut), flags, mode)
    created = None
    try:
        os.set_inheritable(descriptor, False)
        created = os.fstat(descriptor)
        try:
            os.fchmod(descriptor, mode)
        except (AttributeError, OSError) as error:
            if os.name == "posix" and mode & stat.S_IXUSR:
                raise RuntimeError(
                    "Shortcut permissions could not be made executable"
                ) from error
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        if created is not None:
            _unlink_owned_shortcut(shortcut, created)
        raise
    try:
        current = shortcut.lstat()
    except OSError as error:
        raise RuntimeError("Shortcut path disappeared during installation") from error
    if (created.st_dev, created.st_ino) != (current.st_dev, current.st_ino):
        raise RuntimeError("Shortcut path changed during installation")


def numbered_shortcut(preferred, number):
    if number == 1:
        return preferred
    return preferred.with_name(
        "{} ({}){}".format(preferred.stem, number, preferred.suffix)
    )


def install_without_overwrite(preferred, data, mode, limit=1000):
    for number in range(1, limit + 1):
        shortcut = numbered_shortcut(preferred, number)
        if regular_file_matches(shortcut, data, mode):
            return shortcut, False
        try:
            write_shortcut_exclusive(shortcut, data, mode)
        except FileExistsError:
            if regular_file_matches(shortcut, data, mode):
                return shortcut, False
            continue
        return shortcut, True
    raise RuntimeError(
        "Could not find a free shortcut name after {} attempts".format(limit)
    )


def macos_app_payload(launcher):
    executable = (
        "#!/bin/sh\nexec {}\n".format(quoted_shell(launcher))
    ).encode("utf-8")
    info = plistlib.dumps({
        "CFBundleDevelopmentRegion": "en",
        "CFBundleDisplayName": "AWESOME WEBKIT",
        "CFBundleExecutable": "awesome-webkit",
        "CFBundleIconFile": "AppIcon.icns",
        "CFBundleIdentifier": "dev.ethanlang.awesome-webkit",
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": "AWESOME WEBKIT",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "1.0",
        "CFBundleVersion": "1",
        "LSMinimumSystemVersion": "10.13",
        "NSHighResolutionCapable": True,
    }, fmt=plistlib.FMT_XML, sort_keys=True)
    return {
        Path("Contents/Info.plist"): (info, 0o644),
        Path("Contents/MacOS/awesome-webkit"): (executable, 0o755),
        Path("Contents/Resources/AppIcon.icns"): (brand_icon_icns(), 0o644),
    }


def _validated_macos_product(product):
    product = dict(WEBKIT_MACOS_PRODUCT if product is None else product)
    required = {"display_name", "bundle_id", "icon_name"}
    if set(product) - (required | {"apple_events_description"}):
        raise ValueError("macOS shortcut product contains an unknown key")
    if not required.issubset(product):
        raise ValueError("macOS shortcut product is incomplete")
    for key in required:
        if not isinstance(product[key], str) or not product[key].strip():
            raise ValueError("macOS shortcut product {} is invalid".format(key))
    if "/" in product["icon_name"] or product["icon_name"] in {".", ".."}:
        raise ValueError("macOS shortcut icon name must be one file name")
    description = product.get("apple_events_description")
    if description is None:
        description = (
            "{} uses Chrome automation to reuse and refresh its local Control "
            "Center tab."
        ).format(product["display_name"])
    if not isinstance(description, str) or not description.strip():
        raise ValueError("macOS Apple Events description is invalid")
    product["apple_events_description"] = description
    return product


def _validated_native_launcher(launcher):
    launcher = Path(launcher)
    if not launcher.is_absolute():
        raise ValueError("macOS shortcut launcher must be an absolute path")
    try:
        launcher = launcher.resolve(strict=True)
        details = launcher.lstat()
    except (OSError, RuntimeError) as error:
        raise RuntimeError("macOS shortcut launcher is unavailable") from error
    if not stat.S_ISREG(details.st_mode):
        raise RuntimeError("macOS shortcut launcher must be a regular file")
    if not details.st_mode & stat.S_IXUSR:
        raise RuntimeError("macOS shortcut launcher must be executable")
    return launcher


def native_macos_jxa_source(launcher):
    """Return the native applet source with one safely encoded launcher path."""
    launcher = Path(launcher)
    if not launcher.is_absolute():
        raise ValueError("macOS shortcut launcher must be an absolute path")
    encoded = json.dumps(str(launcher), ensure_ascii=True)
    return MACOS_APPLET_SOURCE.replace("__LAUNCHER_PATH__", encoded)


def _source_sha256(source):
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _read_regular_file(path, limit):
    try:
        listed = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(listed.st_mode) or stat.S_ISLNK(listed.st_mode):
        return None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(str(path), flags)
    except OSError:
        return None
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (
            opened.st_dev, opened.st_ino
        ) != (listed.st_dev, listed.st_ino):
            return None
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            data = handle.read(limit + 1)
        if len(data) > limit:
            return None
        current = path.lstat()
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            return None
        return data
    except OSError:
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _bundle_entries_are_safe(path):
    try:
        for root, directories, files in os.walk(str(path), followlinks=False):
            root_path = Path(root)
            if not _real_directory(root_path):
                return False
            for name in directories:
                if not _real_directory(root_path / name):
                    return False
            for name in files:
                details = (root_path / name).lstat()
                if not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode):
                    return False
    except OSError:
        return False
    return True


def _mach_o_executable(path):
    data = _read_regular_file(path, 16 * 1024 * 1024)
    if data is None or len(data) < 4:
        return False
    magic = data[:4]
    if magic not in {
        b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf",
        b"\xbe\xba\xfe\xca", b"\xbf\xba\xfe\xca",
        b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf",
        b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe",
    }:
        return False
    try:
        return bool(path.lstat().st_mode & stat.S_IXUSR)
    except OSError:
        return False


def _decompiled_native_source(path):
    script = path / "Contents" / "Resources" / "Scripts" / "main.scpt"
    if _read_regular_file(script, 4 * 1024 * 1024) is None:
        return None
    try:
        result = subprocess.run(
            ["/usr/bin/osadecompile", str(script)],
            text=True,
            encoding="utf-8",
            errors="strict",
            capture_output=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.rstrip("\r\n") + "\n"


def _native_bundle_signature_is_valid(path):
    try:
        result = subprocess.run(
            ["/usr/bin/codesign", "--verify", "--strict", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def native_macos_app_status(path, launcher, icon_data=None, product=None):
    """Return current, outdated, or None for a native shortcut bundle."""
    path = Path(path)
    launcher = Path(launcher)
    product = _validated_macos_product(product)
    icon_data = brand_icon_icns() if icon_data is None else bytes(icon_data)
    if not _real_directory(path) or not _bundle_entries_are_safe(path):
        return None
    for relative in (
        Path("Contents"), Path("Contents/MacOS"), Path("Contents/Resources"),
        Path("Contents/Resources/Scripts"),
    ):
        if not _real_directory(path / relative):
            return None
    plist_data = _read_regular_file(path / "Contents" / "Info.plist", 256 * 1024)
    if plist_data is None:
        return None
    try:
        info = plistlib.loads(plist_data)
    except (ValueError, TypeError, plistlib.InvalidFileException):
        return None
    expected_values = {
        "CFBundleDisplayName": product["display_name"],
        "CFBundleExecutable": MACOS_APPLET_EXECUTABLE,
        "CFBundleIconFile": product["icon_name"],
        "CFBundleIdentifier": product["bundle_id"],
        "CFBundleName": product["display_name"],
        "CFBundlePackageType": "APPL",
        "NSAppleEventsUsageDescription": product["apple_events_description"],
        MACOS_SHORTCUT_SCHEMA_KEY: MACOS_SHORTCUT_SCHEMA_VERSION,
        MACOS_SHORTCUT_LAUNCHER_KEY: str(launcher),
    }
    if any(info.get(key) != value for key, value in expected_values.items()):
        return None
    recorded_hash = info.get(MACOS_SHORTCUT_SOURCE_KEY)
    if not isinstance(recorded_hash, str) or not all(
        char in "0123456789abcdef" for char in recorded_hash
    ) or len(recorded_hash) != 64:
        return None
    executable = path / "Contents" / "MacOS" / MACOS_APPLET_EXECUTABLE
    icon = path / "Contents" / "Resources" / product["icon_name"]
    if not _mach_o_executable(executable):
        return None
    if not regular_file_matches(icon, icon_data, 0o644):
        return None
    source = _decompiled_native_source(path)
    if source is None or _source_sha256(source) != recorded_hash:
        return None
    encoded_launcher = json.dumps(str(launcher), ensure_ascii=True)
    if "var launcherPath = {};".format(encoded_launcher) not in source:
        return None
    required_fragments = (
        'ObjC.import("Foundation")',
        'WKCC_BROWSER_HANDOFF: "1"',
        '"/usr/bin/shlock"',
        '$.NSAppleScript.alloc.initWithSource',
        '"with timeout of 30 seconds"',
    )
    if any(fragment not in source for fragment in required_fragments):
        return None
    if not _native_bundle_signature_is_valid(path):
        return None
    expected_source = native_macos_jxa_source(launcher)
    return "current" if recorded_hash == _source_sha256(expected_source) else "outdated"


def build_native_macos_app(
    destination, launcher, icon_data=None, product=None,
):
    """Compile, brand, sign, and verify one native JXA applet bundle."""
    destination = Path(destination)
    launcher = _validated_native_launcher(launcher)
    product = _validated_macos_product(product)
    icon_data = brand_icon_icns() if icon_data is None else bytes(icon_data)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(str(destination))
    if not _real_directory(destination.parent):
        raise RuntimeError("macOS shortcut destination parent is unavailable")
    source = native_macos_jxa_source(launcher)
    source_hash = _source_sha256(source)
    descriptor, source_name = tempfile.mkstemp(
        prefix=".wkcc-applet-", suffix=".js", dir=str(destination.parent)
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = None
            handle.write(source)
            handle.flush()
            os.fsync(handle.fileno())
        result = subprocess.run(
            [
                "/usr/bin/osacompile", "-l", "JavaScript", "-o",
                str(destination), source_name,
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=45,
        )
        if result.returncode != 0:
            raise RuntimeError("macOS shortcut applet could not be compiled")
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        raise
    finally:
        try:
            os.unlink(source_name)
        except OSError:
            pass
    try:
        info_path = destination / "Contents" / "Info.plist"
        info_data = _read_regular_file(info_path, 256 * 1024)
        if info_data is None:
            raise RuntimeError("compiled macOS shortcut has no valid property list")
        info = plistlib.loads(info_data)
        info.update({
            "CFBundleDisplayName": product["display_name"],
            "CFBundleExecutable": MACOS_APPLET_EXECUTABLE,
            "CFBundleIconFile": product["icon_name"],
            "CFBundleIconName": Path(product["icon_name"]).stem,
            "CFBundleIdentifier": product["bundle_id"],
            "CFBundleName": product["display_name"],
            "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": "2.0",
            "CFBundleVersion": "2",
            "NSAppleEventsUsageDescription": product["apple_events_description"],
            "NSHighResolutionCapable": True,
            "OSAAppletShowStartupScreen": False,
            MACOS_SHORTCUT_SCHEMA_KEY: MACOS_SHORTCUT_SCHEMA_VERSION,
            MACOS_SHORTCUT_LAUNCHER_KEY: str(launcher),
            MACOS_SHORTCUT_SOURCE_KEY: source_hash,
        })
        info_path.write_bytes(
            plistlib.dumps(info, fmt=plistlib.FMT_XML, sort_keys=True)
        )
        icon_path = destination / "Contents" / "Resources" / product["icon_name"]
        if icon_path.exists() and not stat.S_ISREG(icon_path.lstat().st_mode):
            raise RuntimeError("compiled macOS shortcut icon path is unsafe")
        icon_path.write_bytes(icon_data)
        os.chmod(str(icon_path), 0o644)
        signed = subprocess.run(
            [
                "/usr/bin/codesign", "--force", "--sign", "-",
                "--timestamp=none", str(destination),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=30,
        )
        if signed.returncode != 0:
            raise RuntimeError("macOS shortcut applet could not be signed")
        if native_macos_app_status(
            destination, launcher, icon_data=icon_data, product=product
        ) != "current":
            raise RuntimeError("compiled macOS shortcut applet failed verification")
    except BaseException:
        if (
            destination.exists()
            and _real_directory(destination)
            and _bundle_entries_are_safe(destination)
        ):
            shutil.rmtree(str(destination))
        raise
    return destination


def _real_directory(path):
    try:
        details = path.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(details.st_mode) and not stat.S_ISLNK(details.st_mode)


def macos_app_matches(path, payload):
    if not _real_directory(path):
        return False
    expected_directories = {
        Path("."), Path("Contents"), Path("Contents/MacOS"), Path("Contents/Resources")
    }
    for directory in expected_directories:
        candidate = path if directory == Path(".") else path / directory
        if not _real_directory(candidate):
            return False
    expected_children = {
        path: {"Contents"},
        path / "Contents": {"Info.plist", "MacOS", "Resources"},
        path / "Contents" / "MacOS": {"awesome-webkit"},
        path / "Contents" / "Resources": {"AppIcon.icns"},
    }
    try:
        if any(
            {entry.name for entry in directory.iterdir()} != names
            for directory, names in expected_children.items()
        ):
            return False
    except OSError:
        return False
    return all(
        regular_file_matches(path / relative, data, mode)
        for relative, (data, mode) in payload.items()
    )


def _remove_created_app(path, files, directories, root_stat):
    for file_path, created in reversed(files):
        _unlink_owned_shortcut(file_path, created)
    for directory, created in reversed(directories):
        try:
            current = directory.lstat()
            if stat.S_ISDIR(current.st_mode) and (current.st_dev, current.st_ino) == (
                created.st_dev, created.st_ino,
            ):
                directory.rmdir()
        except OSError:
            pass
    try:
        current = path.lstat()
        if stat.S_ISDIR(current.st_mode) and (current.st_dev, current.st_ino) == (
            root_stat.st_dev, root_stat.st_ino,
        ):
            path.rmdir()
    except OSError:
        pass


def write_macos_app_exclusive(path, payload):
    os.mkdir(str(path), 0o755)
    root_stat = path.lstat()
    directories = []
    files = []
    try:
        for relative in (Path("Contents"), Path("Contents/MacOS"), Path("Contents/Resources")):
            directory = path / relative
            os.mkdir(str(directory), 0o755)
            directories.append((directory, directory.lstat()))
        for relative, (data, mode) in payload.items():
            target = path / relative
            write_shortcut_exclusive(target, data, mode)
            files.append((target, target.lstat()))
    except BaseException:
        _remove_created_app(path, files, directories, root_stat)
        raise


def install_macos_app_without_overwrite(preferred, payload, limit=1000):
    for number in range(1, limit + 1):
        shortcut = numbered_shortcut(preferred, number)
        if macos_app_matches(shortcut, payload):
            return shortcut, False
        try:
            write_macos_app_exclusive(shortcut, payload)
        except FileExistsError:
            if macos_app_matches(shortcut, payload):
                return shortcut, False
            continue
        return shortcut, True
    raise RuntimeError(
        "Could not find a free shortcut name after {} attempts".format(limit)
    )


def _place_native_candidate_exclusive(candidate, preferred):
    """Place a verified candidate without overwriting any existing path."""
    os.mkdir(str(preferred), 0o755)
    installed_root = preferred.lstat()
    try:
        candidate_contents = candidate / "Contents"
        if not _real_directory(candidate_contents):
            raise RuntimeError("compiled macOS shortcut has no Contents directory")
        os.rename(str(candidate_contents), str(preferred / "Contents"))
        candidate.rmdir()
    except BaseException:
        try:
            current = preferred.lstat()
            contents = preferred / "Contents"
            if (
                stat.S_ISDIR(current.st_mode)
                and (current.st_dev, current.st_ino)
                == (installed_root.st_dev, installed_root.st_ino)
                and not contents.exists()
            ):
                preferred.rmdir()
        except OSError:
            pass
        raise
    return installed_root


def _native_existing_kind(path, launcher, icon_data, product, legacy_payload):
    status = native_macos_app_status(
        path, launcher, icon_data=icon_data, product=product
    )
    if status is not None:
        return status
    if legacy_payload is not None and macos_app_matches(path, legacy_payload):
        return "legacy"
    return None


def install_native_macos_app(
    preferred, launcher, icon_data=None, product=None, legacy_payload=None,
):
    """Install or safely update the one stable native macOS shortcut path."""
    preferred = Path(preferred)
    launcher = _validated_native_launcher(launcher)
    product = _validated_macos_product(product)
    icon_data = brand_icon_icns() if icon_data is None else bytes(icon_data)
    if legacy_payload is None and product == _validated_macos_product(
        WEBKIT_MACOS_PRODUCT
    ):
        legacy_payload = macos_app_payload(launcher)

    existing_kind = None
    try:
        preferred.lstat()
    except FileNotFoundError:
        pass
    except OSError as error:
        raise RuntimeError("macOS shortcut path is unavailable") from error
    else:
        existing_kind = _native_existing_kind(
            preferred, launcher, icon_data, product, legacy_payload
        )
        if existing_kind == "current":
            return preferred, "current"
        if existing_kind not in {"outdated", "legacy"}:
            raise RuntimeError(
                "The preferred macOS shortcut path is already occupied. "
                "It was preserved and no duplicate shortcut was created: {}".format(
                    preferred
                )
            )

    with tempfile.TemporaryDirectory(
        prefix=".wkcc-shortcut-", dir=str(preferred.parent)
    ) as temporary:
        temporary = Path(temporary)
        candidate = temporary / "Candidate.app"
        build_native_macos_app(
            candidate, launcher, icon_data=icon_data, product=product
        )
        if existing_kind is None:
            try:
                installed_root = _place_native_candidate_exclusive(
                    candidate, preferred
                )
            except FileExistsError:
                raced_kind = _native_existing_kind(
                    preferred, launcher, icon_data, product, legacy_payload
                )
                if raced_kind == "current":
                    return preferred, "current"
                raise RuntimeError(
                    "The preferred macOS shortcut path became occupied. It was "
                    "preserved and no duplicate shortcut was created: {}".format(
                        preferred
                    )
                )
            if native_macos_app_status(
                preferred, launcher, icon_data=icon_data, product=product
            ) != "current":
                current = preferred.lstat()
                if (current.st_dev, current.st_ino) != (
                    installed_root.st_dev, installed_root.st_ino,
                ):
                    raise RuntimeError("installed macOS shortcut changed")
                failed = temporary / "Failed.app"
                os.rename(str(preferred), str(failed))
                raise RuntimeError("installed macOS shortcut failed verification")
            return preferred, "installed"

        before = preferred.lstat()
        if _native_existing_kind(
            preferred, launcher, icon_data, product, legacy_payload
        ) != existing_kind:
            raise RuntimeError("macOS shortcut changed during update")
        current = preferred.lstat()
        if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
            raise RuntimeError("macOS shortcut changed during update")
        previous = temporary / "Previous.app"
        os.rename(str(preferred), str(previous))
        moved = previous.lstat()
        if (moved.st_dev, moved.st_ino) != (before.st_dev, before.st_ino):
            os.rename(str(previous), str(preferred))
            raise RuntimeError("macOS shortcut changed during update")
        if _native_existing_kind(
            previous, launcher, icon_data, product, legacy_payload
        ) != existing_kind:
            os.rename(str(previous), str(preferred))
            raise RuntimeError("macOS shortcut changed during update")
        installed_root = None
        try:
            installed_root = _place_native_candidate_exclusive(candidate, preferred)
            if native_macos_app_status(
                preferred, launcher, icon_data=icon_data, product=product
            ) != "current":
                raise RuntimeError("updated macOS shortcut failed verification")
        except BaseException:
            failed = temporary / "Failed.app"
            try:
                current = preferred.lstat() if preferred.exists() else None
                if (
                    current is not None
                    and installed_root is not None
                    and (current.st_dev, current.st_ino) != (
                        installed_root.st_dev, installed_root.st_ino,
                    )
                ):
                    raise RuntimeError("updated macOS shortcut changed")
                if (
                    current is not None
                    and installed_root is not None
                    and not failed.exists()
                ):
                    os.rename(str(preferred), str(failed))
                if not preferred.exists():
                    os.rename(str(previous), str(preferred))
            except OSError:
                pass
            raise
        return preferred, "updated"


def main():
    system = platform.system().lower()
    desktop = desktop_directory(system)
    desktop.mkdir(parents=True, exist_ok=True)
    launcher = KIT_ROOT / "launch-control-center.sh"
    if system == "darwin":
        preferred = desktop / "AWESOME WEBKIT.app"
        shortcut, action = install_native_macos_app(
            preferred,
            launcher,
            icon_data=brand_icon_icns(),
            product=WEBKIT_MACOS_PRODUCT,
            legacy_payload=macos_app_payload(launcher),
        )
    elif system == "windows":
        preferred = desktop / "AWESOME WEBKIT.cmd"
        data = windows_launcher_bytes(HERE / "launch.py")
        mode = 0o600
    else:
        preferred = desktop / "awesome-webkit.desktop"
        data = (
            "[Desktop Entry]\nType=Application\nName=AWESOME WEBKIT\n"
            "Comment=Open the local Webkit Control Center\nExec={}\nTerminal=false\n"
            "Categories=Development;\n".format(desktop_exec_quote(launcher))
        ).encode("utf-8")
        mode = 0o755
    if system != "darwin":
        shortcut, created = install_without_overwrite(preferred, data, mode)
        action = "installed" if created else "current"
    if action == "installed":
        print("Installed shortcut: {}".format(shortcut))
    elif action == "updated":
        print("Updated shortcut: {}".format(shortcut))
    else:
        print("Shortcut already installed: {}".format(shortcut))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
