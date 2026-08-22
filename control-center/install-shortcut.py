#!/usr/bin/env python3
"""Install a clickable desktop shortcut for the local Control Center."""

import base64
import math
import os
import platform
import plistlib
import shutil
import stat
import struct
import subprocess
import zlib
from functools import lru_cache
from pathlib import Path


HERE = Path(__file__).resolve().parent
KIT_ROOT = HERE.parent


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


def main():
    system = platform.system().lower()
    desktop = desktop_directory(system)
    desktop.mkdir(parents=True, exist_ok=True)
    launcher = KIT_ROOT / "launch-control-center.sh"
    if system == "darwin":
        preferred = desktop / "AWESOME WEBKIT.app"
        shortcut, created = install_macos_app_without_overwrite(
            preferred, macos_app_payload(launcher)
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
    if created:
        print("Installed shortcut: {}".format(shortcut))
    else:
        print("Shortcut already installed: {}".format(shortcut))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
