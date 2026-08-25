#!/usr/bin/env python3
"""Start or reopen the local Control Center without leaving a terminal open."""

import contextlib
import errno
import http.client
import json
import os
import platform
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
import webbrowser
from pathlib import Path


HERE = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get("WKCC_STATE_DIR", "~/.awesome-webkit")).expanduser()
RUNTIME_FILE_NAME = "control-center-runtime.json"
LOG_FILE_NAME = "control-center.log"
RUNTIME_FILE = STATE_DIR / RUNTIME_FILE_NAME
LOG_FILE = STATE_DIR / LOG_FILE_NAME
STARTUP_LOCK_NAME = "control-center-startup.lock"
INSTANCE_LOCK_NAME = "control-center-instance.lock"
STATE_DIRECTORY_NAMES = frozenset(("logs", "projects", "worktrees"))
STATE_FILE_NAMES = frozenset((
    RUNTIME_FILE_NAME,
    LOG_FILE_NAME,
    STARTUP_LOCK_NAME,
    INSTANCE_LOCK_NAME,
    "state.json",
))
CHROME_REUSE_RESULT = "WKCC_REUSED"
CHROME_REUSE_SCRIPT = """
on run argv
    if (count of argv) is not 2 then return "WKCC_MISS"
    set targetURL to item 1 of argv
    set targetPrefix to item 2 of argv
    if application "Google Chrome" is not running then return "WKCC_MISS"
    tell application "Google Chrome"
        repeat with windowNumber from 1 to count of windows
            repeat with tabNumber from 1 to count of tabs of window windowNumber
                try
                    set candidateURL to URL of tab tabNumber of window windowNumber
                    if candidateURL starts with targetPrefix then
                        set URL of tab tabNumber of window windowNumber to targetURL
                        set active tab index of window windowNumber to tabNumber
                        set index of window windowNumber to 1
                        activate
                        return "WKCC_REUSED"
                    end if
                end try
            end repeat
        end repeat
    end tell
    return "WKCC_MISS"
end run
""".strip()


def validated_state_dir_path(value):
    """Return one absolute, normalized, non-root Control Center state path."""
    try:
        path = Path(value).expanduser()
    except (RuntimeError, TypeError, ValueError) as error:
        raise RuntimeError("Control Center state directory is invalid: {}".format(error))
    if not path.is_absolute():
        raise RuntimeError("Control Center state directory must be an absolute path")
    path = Path(os.path.abspath(str(path)))
    if path == Path(path.anchor):
        raise RuntimeError("Control Center state directory must not be the filesystem root")
    try:
        listed = os.lstat(str(path))
    except FileNotFoundError:
        try:
            parent = path.parent.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise RuntimeError(
                "Control Center state directory parent must already exist: {}".format(error)
            )
        try:
            parent_stat = os.lstat(str(parent))
        except OSError as error:
            raise RuntimeError(
                "Control Center state directory parent is unavailable: {}".format(error)
            )
        if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(parent_stat.st_mode):
            raise RuntimeError("Control Center state directory parent must be a real directory")
        path = parent / path.name
    except OSError as error:
        raise RuntimeError("Control Center state directory is unavailable: {}".format(error))
    else:
        if stat.S_ISLNK(listed.st_mode):
            raise RuntimeError(
                "Control Center state path must be a real directory, not a symlink or file: {}".format(
                    path
                )
            )
        try:
            path = path.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise RuntimeError("Control Center state directory is unavailable: {}".format(error))
    if path == Path(path.anchor):
        raise RuntimeError("Control Center state directory must not be the filesystem root")
    return path


def _validate_state_dir_layout(state_dir):
    """Reject an existing directory that does not look dedicated to Control Center."""
    try:
        entries = list(os.scandir(str(state_dir)))
    except OSError as error:
        raise RuntimeError("Control Center state directory is unavailable: {}".format(error))
    for entry in entries:
        name = entry.name
        is_temporary_file = (
            name.endswith(".tmp")
            and (
                name.startswith(".state.json-")
                or name.startswith(".{}-".format(RUNTIME_FILE_NAME))
            )
        )
        if name not in STATE_DIRECTORY_NAMES and name not in STATE_FILE_NAMES and not is_temporary_file:
            raise RuntimeError(
                "Control Center state directory contains an unexpected entry: {}".format(
                    name
                )
            )
        try:
            entry_stat = entry.stat(follow_symlinks=False)
        except OSError as error:
            raise RuntimeError(
                "Control Center state entry is unavailable: {}".format(error)
            )
        if name in STATE_DIRECTORY_NAMES:
            valid_kind = stat.S_ISDIR(entry_stat.st_mode)
        elif name in STATE_FILE_NAMES:
            valid_kind = stat.S_ISREG(entry_stat.st_mode) or stat.S_ISLNK(
                entry_stat.st_mode
            )
        else:
            valid_kind = stat.S_ISREG(entry_stat.st_mode)
        if not valid_kind:
            raise RuntimeError(
                "Control Center state entry has an invalid type: {}".format(name)
            )
        if os.name == "posix" and entry_stat.st_uid != os.getuid():
            raise RuntimeError(
                "Control Center state entries must be owned by the current user"
            )


def secure_state_dir(state_dir=None):
    state_dir = validated_state_dir_path(STATE_DIR if state_dir is None else state_dir)
    try:
        before = os.lstat(str(state_dir))
    except FileNotFoundError:
        try:
            state_dir.mkdir(mode=0o700)
        except FileExistsError:
            pass
    except OSError as error:
        raise RuntimeError("Control Center state directory is unavailable: {}".format(error))

    try:
        before = os.lstat(str(state_dir))
    except OSError as error:
        raise RuntimeError("Control Center state directory is unavailable: {}".format(error))
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
        raise RuntimeError(
            "Control Center state path must be a real directory, not a symlink or file: {}".format(
                state_dir
            )
        )
    if os.name == "posix" and before.st_uid != os.getuid():
        raise RuntimeError("Control Center state directory must be owned by the current user")

    _validate_state_dir_layout(state_dir)

    try:
        os.chmod(str(state_dir), 0o700)
    except OSError as error:
        if os.name == "posix":
            raise RuntimeError(
                "Control Center state directory could not be made private: {}".format(error)
            )
    try:
        after = os.lstat(str(state_dir))
    except OSError as error:
        raise RuntimeError("Control Center state directory changed: {}".format(error))
    if (
        stat.S_ISLNK(after.st_mode)
        or not stat.S_ISDIR(after.st_mode)
        or (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
        or (os.name == "posix" and after.st_uid != os.getuid())
    ):
        raise RuntimeError("Control Center state directory changed during validation")
    if os.name == "posix" and stat.S_IMODE(after.st_mode) & 0o077:
        raise RuntimeError("Control Center state directory permissions are not private")
    return state_dir


def _open_private_regular(path, flags):
    """Open one private state file without following a symlink."""
    try:
        before = os.lstat(str(path))
    except FileNotFoundError:
        before = None
    except OSError as error:
        raise RuntimeError("State file is unavailable: {}".format(error))
    if before is not None and (
        stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode)
    ):
        raise RuntimeError("State file must be a regular file: {}".format(path))

    open_flags = flags | os.O_CREAT | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        open_flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        open_flags |= os.O_CLOEXEC
    try:
        descriptor = os.open(str(path), open_flags, 0o600)
    except OSError as error:
        raise RuntimeError("State file could not be opened safely: {}".format(error))
    try:
        os.set_inheritable(descriptor, False)
        opened = os.fstat(descriptor)
        current = os.lstat(str(path))
        if not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(current.st_mode):
            raise RuntimeError("State file must remain a regular file: {}".format(path))
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise RuntimeError("State file changed while it was opened: {}".format(path))
        if before is not None and (opened.st_dev, opened.st_ino) != (
            before.st_dev,
            before.st_ino,
        ):
            raise RuntimeError("State file changed while it was opened: {}".format(path))
        try:
            os.fchmod(descriptor, 0o600)
        except (AttributeError, OSError):
            if os.name == "posix":
                raise RuntimeError("State file permissions could not be made private")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_private_regular(path, limit):
    try:
        before = os.lstat(str(path))
    except OSError as error:
        raise RuntimeError("State file is unavailable: {}".format(error))
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise RuntimeError("State file must be a regular file: {}".format(path))
    if before.st_size > limit:
        raise RuntimeError("State file is unexpectedly large: {}".format(path))
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(str(path), flags)
    try:
        opened = os.fstat(descriptor)
        current = os.lstat(str(path))
        if not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(current.st_mode):
            raise RuntimeError("State file must remain a regular file: {}".format(path))
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise RuntimeError("State file changed while it was opened: {}".format(path))
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise RuntimeError("State file changed while it was opened: {}".format(path))
        chunks = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after_read = os.fstat(descriptor)
        current = os.lstat(str(path))
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(after_read.st_mode)
            or not stat.S_ISREG(current.st_mode)
        ):
            raise RuntimeError("State file must remain a regular file: {}".format(path))
        opened_signature = (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        )
        if opened_signature != (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) or opened_signature != (
            after_read.st_dev,
            after_read.st_ino,
            after_read.st_size,
            after_read.st_mtime_ns,
        ) or opened_signature != (
            current.st_dev,
            current.st_ino,
            current.st_size,
            current.st_mtime_ns,
        ):
            raise RuntimeError("State file changed while it was read: {}".format(path))
        data = b"".join(chunks)
        if len(data) > limit:
            raise RuntimeError("State file is unexpectedly large: {}".format(path))
        return data.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise RuntimeError("State file could not be read safely: {}".format(error))
    finally:
        os.close(descriptor)


def _try_startup_file_lock(handle):
    """Try once to lock the first byte of the startup lock file."""
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError as error:
            if error.errno in (
                errno.EACCES,
                errno.EAGAIN,
                errno.EDEADLK,
            ) or getattr(error, "winerror", None) in (32, 33, 36):
                return False
            raise

    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as error:
        if error.errno in (errno.EACCES, errno.EAGAIN):
            return False
        raise


def _unlock_startup_file(handle):
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def acquire_instance_lock(state_dir=None):
    """Acquire the lifetime lock for one Control Center state directory."""
    state_dir = secure_state_dir(STATE_DIR if state_dir is None else state_dir)
    path = state_dir / INSTANCE_LOCK_NAME
    descriptor = _open_private_regular(path, os.O_RDWR)
    try:
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
        os.lseek(descriptor, 0, os.SEEK_SET)
        handle = os.fdopen(descriptor, "r+b", buffering=0)
    except BaseException:
        os.close(descriptor)
        raise
    try:
        if not _try_startup_file_lock(handle):
            handle.close()
            return None
        return handle
    except BaseException:
        handle.close()
        raise


def release_instance_lock(handle):
    if handle is None:
        return
    try:
        _unlock_startup_file(handle)
    except OSError:
        pass
    handle.close()


def instance_lock_held(state_dir=None):
    handle = acquire_instance_lock(state_dir)
    if handle is None:
        return True
    release_instance_lock(handle)
    return False


def launch_lock_timeout_seconds(state_dir=None):
    configured = os.environ.get("WKCC_LAUNCH_LOCK_TIMEOUT")
    if configured:
        try:
            seconds = float(configured)
            if 0 < seconds < float("inf"):
                return seconds
        except ValueError:
            pass
    return max(30.0, startup_timeout_seconds(state_dir) + 15.0)


@contextlib.contextmanager
def startup_lock(timeout=None, state_dir=None):
    """Serialize launchers while the runtime file and server are established."""
    state_dir = secure_state_dir(STATE_DIR if state_dir is None else state_dir)
    path = state_dir / STARTUP_LOCK_NAME
    descriptor = _open_private_regular(path, os.O_RDWR)
    try:
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
        os.lseek(descriptor, 0, os.SEEK_SET)
        handle = os.fdopen(descriptor, "r+b", buffering=0)
    except BaseException:
        os.close(descriptor)
        raise

    locked = False
    try:
        wait_seconds = (
            launch_lock_timeout_seconds(state_dir) if timeout is None else float(timeout)
        )
        if not 0 < wait_seconds < float("inf"):
            raise ValueError("startup lock timeout must be a positive finite number")
        deadline = time.monotonic() + wait_seconds
        while not _try_startup_file_lock(handle):
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "Timed out waiting for another Control Center launcher to finish."
                )
            time.sleep(0.05)
        locked = True
        yield path
    finally:
        if locked:
            try:
                _unlock_startup_file(handle)
            except OSError:
                pass
        handle.close()


def private_log_file(state_dir=None):
    state_dir = secure_state_dir(STATE_DIR if state_dir is None else state_dir)
    log_file = state_dir / LOG_FILE_NAME
    descriptor = _open_private_regular(log_file, os.O_RDWR)
    try:
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
        return os.fdopen(descriptor, "w", encoding="utf-8")
    except BaseException:
        os.close(descriptor)
        raise


def write_runtime(runtime, state_dir=None):
    state_dir = secure_state_dir(STATE_DIR if state_dir is None else state_dir)
    runtime_file = state_dir / RUNTIME_FILE_NAME
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".{}-".format(runtime_file.name),
        suffix=".tmp",
        dir=str(state_dir),
    )
    temporary = Path(temporary_name)
    try:
        os.set_inheritable(descriptor, False)
        try:
            os.fchmod(descriptor, 0o600)
        except (AttributeError, OSError):
            if os.name == "posix":
                raise RuntimeError("Runtime file permissions could not be made private")
        handle = os.fdopen(descriptor, "w", encoding="utf-8")
        descriptor = None
        with handle:
            handle.write(json.dumps(runtime, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(runtime_file))
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def process_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        return windows_process_alive(pid)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def windows_process_alive(pid):
    """Check a Windows PID without using os.kill, which terminates processes there."""
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def port_open(port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.2)
    try:
        return sock.connect_ex(("127.0.0.1", int(port))) == 0
    finally:
        sock.close()


def control_center_ready(port, token, pid, expected_version=None):
    """Verify the authenticated server identity instead of trusting a bare listener."""
    connection = None
    try:
        connection = http.client.HTTPConnection("127.0.0.1", int(port), timeout=0.35)
        connection.request(
            "GET", "/api/health", headers={"X-WKCC-Token": token}
        )
        response = connection.getresponse()
        if response.status != 200:
            response.read()
            return False
        payload = json.loads(response.read(4096).decode("utf-8"))
        return (
            payload.get("ok") is True
            and int(payload.get("pid")) == int(pid)
            and (
                expected_version is None
                or payload.get("kitVersion") == expected_version
            )
        )
    except (OSError, ValueError, TypeError, http.client.HTTPException):
        return False
    finally:
        if connection is not None:
            connection.close()


def runtime_ready(runtime, expected_version=None):
    if not isinstance(runtime, dict):
        return False
    pid = runtime.get("pid")
    return process_alive(pid) and control_center_ready(
        runtime.get("port"), runtime.get("token"), pid, expected_version
    )


def detached_process_kwargs(system=None):
    if (system or platform.system()).lower() == "windows":
        detached = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        process_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        return {"creationflags": detached | process_group}
    return {"start_new_session": True}


def startup_timeout_seconds(state_dir=None):
    """Allow enough time for the server to recover each active preview session."""
    configured = os.environ.get("WKCC_STARTUP_TIMEOUT")
    if configured:
        try:
            seconds = float(configured)
            if 0 < seconds < float("inf"):
                return seconds
        except ValueError:
            pass

    active_sessions = 0
    try:
        state_dir = validated_state_dir_path(STATE_DIR if state_dir is None else state_dir)
        state = json.loads(
            _read_private_regular(state_dir / "state.json", 8 * 1024 * 1024)
        )
        sessions = state.get("sessions", []) if isinstance(state, dict) else []
        active_sessions = sum(
            1
            for session in sessions
            if isinstance(session, dict)
            and session.get("status") in ("active", "busy", "merging", "error")
        )
    except (OSError, RuntimeError, ValueError, TypeError):
        pass
    return 10.0 + (10.0 * active_sessions)


def request_control_center_shutdown(port, token):
    connection = None
    try:
        connection = http.client.HTTPConnection("127.0.0.1", int(port), timeout=0.5)
        body = b"{}"
        connection.request(
            "POST", "/api/shutdown", body=body,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
                "X-WKCC-Token": token,
            },
        )
        response = connection.getresponse()
        response.read(4096)
        return response.status == 200
    except (OSError, ValueError, TypeError, http.client.HTTPException):
        return False
    finally:
        if connection is not None:
            connection.close()


def stop_spawned_process(process, timeout=5, port=None, token=None):
    """Stop and reap only the child represented by this Popen instance."""
    if process.poll() is not None:
        return
    if port is not None and token and request_control_center_shutdown(port, token):
        try:
            process.wait(timeout=timeout)
            return
        except subprocess.TimeoutExpired:
            pass
    if os.name == "nt" and isinstance(getattr(process, "pid", None), int) and shutil.which("taskkill"):
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
        process.wait(timeout=timeout)
        return
    try:
        process.terminate()
    except ProcessLookupError:
        process.wait(timeout=timeout)
        return
    try:
        process.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        pass
    if process.poll() is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    process.wait(timeout=timeout)


def validated_port(value, label="WKCC_PORT/start"):
    if isinstance(value, bool):
        raise ValueError("{} must be an integer from 1 through 65535".format(label))
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise ValueError("{} must be an integer from 1 through 65535".format(label))
    if isinstance(value, float) or not 1 <= port <= 65535:
        raise ValueError("{} must be an integer from 1 through 65535".format(label))
    return port


def free_port(start=8790):
    start = validated_port(start)
    for port in range(start, min(start + 100, 65536)):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", port))
            return port
        except OSError:
            continue
        finally:
            sock.close()
    raise RuntimeError("No free Control Center port was found.")


def ready_runtime(state_dir=None, expected_version=None):
    try:
        state_dir = validated_state_dir_path(STATE_DIR if state_dir is None else state_dir)
        runtime = json.loads(
            _read_private_regular(state_dir / RUNTIME_FILE_NAME, 65536)
        )
        return runtime if runtime_ready(runtime, expected_version) else None
    except (OSError, RuntimeError, ValueError, TypeError):
        return None


def installed_kit_version():
    value = _read_private_regular(HERE.parent / "webkit" / "VERSION", 64).strip()
    parts = value.split(".")
    if len(parts) != 3 or any(not part.isdigit() for part in parts):
        raise RuntimeError("Installed Webkit version is invalid")
    return value


def stop_outdated_runtime(runtime, state_dir, timeout=15.0):
    """Gracefully stop only the authenticated server represented by runtime."""
    if not runtime_ready(runtime):
        return True
    if not request_control_center_shutdown(runtime.get("port"), runtime.get("token")):
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not runtime_ready(runtime) and not instance_lock_held(state_dir):
            return True
        time.sleep(0.1)
    return not runtime_ready(runtime) and not instance_lock_held(state_dir)


def open_runtime(runtime):
    prefix = "http://127.0.0.1:{}/".format(runtime["port"])
    url = "{}?token={}".format(
        prefix, urllib.parse.quote(runtime["token"])
    )
    if platform.system().lower() == "darwin":
        browser_app = os.environ.get("WKCC_BROWSER_APP", "Google Chrome")
        if browser_app == "Google Chrome":
            reload_url = "{}&launch={}".format(url, secrets.token_hex(8))
            try:
                result = subprocess.run(
                    ["osascript", "-e", CHROME_REUSE_SCRIPT, reload_url, prefix],
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    capture_output=True,
                    check=False,
                    timeout=3.0,
                )
                output = result.stdout if isinstance(result.stdout, str) else ""
                if result.returncode == 0 and output.strip() == CHROME_REUSE_RESULT:
                    return
            except (OSError, subprocess.TimeoutExpired):
                pass
        try:
            result = subprocess.run(
                ["open", "-a", browser_app, url],
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
            )
            if result.returncode == 0:
                return
        except OSError:
            pass
    try:
        if webbrowser.open(url, new=2):
            return
    except (OSError, webbrowser.Error):
        pass
    print("Open AWESOME WEBKIT in your browser: {}".format(url))


def main():
    state_dir = secure_state_dir()
    expected_version = installed_kit_version()
    runtime = ready_runtime(state_dir, expected_version)
    if runtime is None:
        with startup_lock(state_dir=state_dir):
            # Another launcher may have completed while this invocation waited.
            runtime = ready_runtime(state_dir, expected_version)
            if runtime is None:
                outdated = ready_runtime(state_dir)
                if outdated is not None and not stop_outdated_runtime(
                    outdated, state_dir
                ):
                    raise RuntimeError(
                        "The installed Control Center changed, but its previous "
                        "process could not be stopped safely. Close it and retry."
                    )
                if instance_lock_held(state_dir):
                    raise RuntimeError(
                        "A Control Center already owns this state directory, but its "
                        "runtime metadata is missing or invalid. Stop that process before retrying."
                    )
                requested_port = validated_port(
                    os.environ.get("WKCC_PORT", "8790"), "WKCC_PORT"
                )
                port = free_port(requested_port)
                token = secrets.token_urlsafe(32)
                env = os.environ.copy()
                env["WKCC_TOKEN"] = token
                env["WKCC_STATE_DIR"] = str(state_dir)
                popen_kwargs = detached_process_kwargs()
                startup_timeout = startup_timeout_seconds(state_dir)
                with private_log_file(state_dir) as log:
                    process = subprocess.Popen(
                        [
                            sys.executable,
                            str(HERE / "server.py"),
                            "--port",
                            str(port),
                            "--state-dir",
                            str(state_dir),
                            "--no-browser",
                        ],
                        cwd=str(HERE),
                        env=env,
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=log,
                        **popen_kwargs
                    )
                deadline = time.monotonic() + startup_timeout
                try:
                    while time.monotonic() < deadline and process.poll() is None:
                        if control_center_ready(
                            port, token, process.pid, expected_version
                        ):
                            runtime = {
                                "pid": process.pid,
                                "port": port,
                                "token": token,
                                "kitVersion": expected_version,
                            }
                            write_runtime(runtime, state_dir)
                            break
                        time.sleep(0.15)
                except BaseException:
                    stop_spawned_process(process, port=port, token=token)
                    raise
                if runtime is None:
                    stop_spawned_process(process, port=port, token=token)
                    raise RuntimeError(
                        "Control Center failed to start. See {}".format(
                            state_dir / LOG_FILE_NAME
                        )
                    )
    open_runtime(runtime)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
