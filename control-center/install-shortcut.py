#!/usr/bin/env python3
"""Install a clickable desktop shortcut for the local Control Center."""

import base64
import os
import platform
import shutil
import stat
import subprocess
from pathlib import Path


HERE = Path(__file__).resolve().parent
KIT_ROOT = HERE.parent


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


def main():
    system = platform.system().lower()
    desktop = desktop_directory(system)
    desktop.mkdir(parents=True, exist_ok=True)
    launcher = KIT_ROOT / "launch-control-center.sh"
    if system == "darwin":
        preferred = desktop / "AWESOME WEBKIT.command"
        data = "#!/bin/sh\nexec {}\n".format(quoted_shell(launcher)).encode("utf-8")
        mode = 0o755
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
    shortcut, created = install_without_overwrite(preferred, data, mode)
    if created:
        print("Installed shortcut: {}".format(shortcut))
    else:
        print("Shortcut already installed: {}".format(shortcut))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
