#!/usr/bin/env python3
"""Install a clickable desktop shortcut for the local Control Center."""

import os
import platform
import stat
from pathlib import Path


HERE = Path(__file__).resolve().parent
KIT_ROOT = HERE.parent


def quoted_shell(path):
    return "'{}'".format(str(path).replace("'", "'\\''"))


def main():
    desktop = Path.home() / "Desktop"
    desktop.mkdir(parents=True, exist_ok=True)
    system = platform.system().lower()
    launcher = KIT_ROOT / "launch-control-center.sh"
    if system == "darwin":
        shortcut = desktop / "AWESOME WEBKIT.command"
        shortcut.write_text("#!/bin/sh\nexec {}\n".format(quoted_shell(launcher)), encoding="utf-8")
        shortcut.chmod(shortcut.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    elif system == "windows":
        shortcut = desktop / "AWESOME WEBKIT.cmd"
        shortcut.write_text('@echo off\r\npython "{}"\r\n'.format(HERE / "launch.py"), encoding="utf-8")
    else:
        shortcut = desktop / "awesome-webkit.desktop"
        shortcut.write_text(
            "[Desktop Entry]\nType=Application\nName=AWESOME WEBKIT\n"
            "Comment=Open the local Webkit Control Center\nExec={}\nTerminal=false\n"
            "Categories=Development;\n".format(launcher),
            encoding="utf-8",
        )
        shortcut.chmod(shortcut.stat().st_mode | stat.S_IXUSR)
    print("Installed shortcut: {}".format(shortcut))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
