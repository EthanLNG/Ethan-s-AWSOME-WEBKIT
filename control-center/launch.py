#!/usr/bin/env python3
"""Start or reopen the local Control Center without leaving a terminal open."""

import json
import os
import platform
import secrets
import socket
import subprocess
import sys
import time
import urllib.parse
import webbrowser
from pathlib import Path


HERE = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get("WKCC_STATE_DIR", "~/.awesome-webkit")).expanduser()
RUNTIME_FILE = STATE_DIR / "control-center-runtime.json"
LOG_FILE = STATE_DIR / "control-center.log"


def process_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def port_open(port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.2)
    try:
        return sock.connect_ex(("127.0.0.1", int(port))) == 0
    finally:
        sock.close()


def free_port(start=8790):
    for port in range(start, start + 100):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", port))
            return port
        except OSError:
            continue
        finally:
            sock.close()
    raise RuntimeError("No free Control Center port was found.")


def open_runtime(runtime):
    url = "http://127.0.0.1:{}/?token={}".format(
        runtime["port"], urllib.parse.quote(runtime["token"])
    )
    if platform.system().lower() == "darwin":
        browser_app = os.environ.get("WKCC_BROWSER_APP", "Google Chrome")
        result = subprocess.run(
            ["open", "-a", browser_app, url], text=True, capture_output=True, check=False
        )
        if result.returncode == 0:
            return
    webbrowser.open(url, new=2)


def main():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if RUNTIME_FILE.exists():
        try:
            runtime = json.loads(RUNTIME_FILE.read_text(encoding="utf-8"))
            if process_alive(runtime.get("pid")) and port_open(runtime.get("port")):
                open_runtime(runtime)
                return 0
        except (OSError, ValueError, TypeError):
            pass

    port = free_port(int(os.environ.get("WKCC_PORT", "8790")))
    token = secrets.token_urlsafe(32)
    env = os.environ.copy()
    env["WKCC_TOKEN"] = token
    env["WKCC_STATE_DIR"] = str(STATE_DIR)
    with LOG_FILE.open("a", encoding="utf-8") as log:
        process = subprocess.Popen(
            [sys.executable, str(HERE / "server.py"), "--port", str(port), "--state-dir", str(STATE_DIR), "--no-browser"],
            cwd=str(HERE),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    deadline = time.time() + 8
    while time.time() < deadline and process.poll() is None:
        if port_open(port):
            runtime = {"pid": process.pid, "port": port, "token": token}
            RUNTIME_FILE.write_text(json.dumps(runtime, indent=2) + "\n", encoding="utf-8")
            try:
                os.chmod(str(RUNTIME_FILE), 0o600)
            except OSError:
                pass
            open_runtime(runtime)
            return 0
        time.sleep(0.15)
    raise RuntimeError("Control Center failed to start. See {}".format(LOG_FILE))


if __name__ == "__main__":
    raise SystemExit(main())
