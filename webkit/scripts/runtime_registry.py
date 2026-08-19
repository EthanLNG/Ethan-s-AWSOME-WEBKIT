#!/usr/bin/env python3
"""Atomic machine-local preview port reservations for AWESOME WEBKIT.

Color locks are project-scoped. This registry is deliberately user-global so
two projects cannot both finish claiming the same TCP port before either
preview process binds it. The module uses only Python 3.7 standard-library
features and is shared by the shell flow, preview server, and Control Center.
"""

import argparse
import contextlib
import errno
import http.client
import json
import math
import os
import re
import secrets
import socket
import stat
import sys
import tempfile
import threading
import time
import unicodedata
from pathlib import Path


VERSION = 1
MAX_RECORD_BYTES = 16 * 1024
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
INSTANCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
PORT_ENTRY_RE = re.compile(r"^[1-9][0-9]{0,4}\.lock$")
QUARANTINE_RE = re.compile(r"^\.wk-(?:reap|release)-[A-Za-z0-9_-]{8,128}$")
UPDATE_RE = re.compile(r"^\.wk-update-[A-Za-z0-9_-]{8,128}$")
MUTEX_ENTRY_NAME = ".wk-registry.lock"
COLOR_ENTRY_RE = re.compile(
    r"^(?:[A-Za-z0-9][A-Za-z0-9_-]{0,63}\.lock|"
    r"\.wk-(?:reap|release)-[A-Za-z0-9_-]{8,128})$"
)
COLOR_TEMP_RE = re.compile(
    r"^\.wk-write-(owner|reservation)-[A-Za-z0-9_-]{8,128}$"
)
RECORD_KEYS = {
    "version", "port", "owner", "colorLock", "color", "token",
    "instance", "pid",
}
INCOMPLETE_RECORD_GRACE_SECONDS = 5.0
MIN_GRACE_SECONDS = 30.0
MAX_GRACE_SECONDS = 86400.0
_PROCESS_REGISTRY_LOCK = threading.Lock()


class RegistryError(RuntimeError):
    pass


class ReservationBusy(RegistryError):
    def __init__(self, message, status="busy"):
        super().__init__(message)
        self.status = status


def _reject_constant(value):
    raise ValueError("nonstandard JSON constant {}".format(value))


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


def _safe_text(value, maximum=4096):
    return (
        isinstance(value, str)
        and value
        and len(value.encode("utf-8")) <= maximum
        and all(unicodedata.category(char) != "Cc" for char in value)
    )


def _signature(value):
    return (
        value.st_dev, value.st_ino, value.st_mode, value.st_size,
        getattr(value, "st_mtime_ns", int(value.st_mtime * 1000000000)),
    )


def _path_exists(path):
    return os.path.lexists(str(path))


def _normalized_binding_path(path):
    path = os.path.abspath(str(path))
    if os.name == "nt":
        missing = []
        existing = path
        while not os.path.lexists(existing):
            parent, name = os.path.split(existing)
            if parent == existing:
                break
            missing.append(name)
            existing = parent
        try:
            import ctypes
            from ctypes import wintypes

            get_long_path = ctypes.WinDLL(
                "kernel32", use_last_error=True
            ).GetLongPathNameW
            get_long_path.argtypes = [
                wintypes.LPCWSTR,
                wintypes.LPWSTR,
                wintypes.DWORD,
            ]
            get_long_path.restype = wintypes.DWORD
            required = get_long_path(existing, None, 0)
            if required:
                buffer = ctypes.create_unicode_buffer(required + 1)
                written = get_long_path(existing, buffer, len(buffer))
                if written and written < len(buffer):
                    path = buffer.value
                    for name in reversed(missing):
                        path = os.path.join(path, name)
        except (AttributeError, OSError, ValueError):
            pass
    return os.path.normcase(os.path.normpath(path))


def _normalized_owner_path(path):
    path = str(path)
    if not os.path.isabs(path):
        return path
    if os.name == "nt":
        return _normalized_binding_path(path)
    return os.path.realpath(path)


def _owners_match(first, second):
    return _normalized_owner_path(first) == _normalized_owner_path(second)


def touch_path_nofollow(path):
    """Refresh one real path without accepting a replacement or link."""
    path = str(path)
    try:
        before = os.lstat(path)
        if stat.S_ISLNK(before.st_mode):
            return False
        try:
            os.utime(path, None, follow_symlinks=False)
        except (NotImplementedError, TypeError):
            os.utime(path, None)
        after = os.lstat(path)
    except OSError:
        return False
    return (
        not stat.S_ISLNK(after.st_mode)
        and (before.st_dev, before.st_ino, stat.S_IFMT(before.st_mode))
        == (after.st_dev, after.st_ino, stat.S_IFMT(after.st_mode))
    )


def _validate_port(port):
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise RegistryError("preview port must be an integer between 1 and 65535")
    return port


def _validate_grace_seconds(value):
    if isinstance(value, bool):
        raise RegistryError(
            "claim grace must be a number from 30 through 86400 seconds"
        )
    try:
        grace = float(value)
    except (TypeError, ValueError):
        raise RegistryError(
            "claim grace must be a number from 30 through 86400 seconds"
        )
    if (
        not math.isfinite(grace)
        or grace < MIN_GRACE_SECONDS
        or grace > MAX_GRACE_SECONDS
    ):
        raise RegistryError(
            "claim grace must be a number from 30 through 86400 seconds"
        )
    return grace


def _validate_binding(port, owner, color_lock, color, token=None):
    port = _validate_port(port)
    if not _safe_text(owner):
        raise RegistryError("owner must be one nonempty line of at most 4096 bytes")
    color_lock = str(color_lock)
    if not _safe_text(color_lock) or not os.path.isabs(color_lock):
        raise RegistryError("color lock path must be absolute")
    color_lock = _normalized_binding_path(color_lock)
    if not isinstance(color, str) or SLUG_RE.fullmatch(color) is None:
        raise RegistryError("color must be a safe palette slug")
    if token is not None and (
        not isinstance(token, str) or TOKEN_RE.fullmatch(token) is None
    ):
        raise RegistryError("reservation token is invalid")
    return port, owner, color_lock, color


def runtime_registry_path(environ=None, system_name=None, temp_root=None):
    """Return the deterministic same-user port registry path."""
    environ = os.environ if environ is None else environ
    override = environ.get("WK_PORT_LOCKDIR", "")
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            raise RegistryError("WK_PORT_LOCKDIR must be an absolute path")
        return path
    system_name = (system_name or os.name).lower()
    if system_name in ("nt", "windows"):
        base = Path(temp_root or tempfile.gettempdir())
        identity = environ.get("USERNAME") or environ.get("USER") or "user"
        safe_identity = re.sub(r"[^A-Za-z0-9_.-]+", "_", identity)[:64] or "user"
    else:
        base = Path(temp_root or "/tmp")
        try:
            safe_identity = str(os.getuid())
        except AttributeError:
            safe_identity = re.sub(
                r"[^A-Za-z0-9_.-]+", "_", os.environ.get("USER", "user")
            )[:64] or "user"
    return base / ("awesome-webkit-preview-ports-" + safe_identity)


def _validate_registry(path, create=False, recover=False):
    path = Path(path).expanduser()
    if not path.is_absolute():
        raise RegistryError("runtime port registry must be an absolute non-root path")
    path = Path(os.path.abspath(str(path)))
    if path == Path(path.anchor):
        raise RegistryError("runtime port registry must be an absolute non-root path")
    try:
        listed = os.lstat(str(path))
    except FileNotFoundError:
        try:
            parent = path.parent.resolve(strict=True)
            parent_stat = os.lstat(str(parent))
        except (OSError, RuntimeError) as exc:
            raise RegistryError(
                "runtime port registry parent is unavailable: {}".format(exc)
            )
        if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(
            parent_stat.st_mode
        ):
            raise RegistryError(
                "runtime port registry parent must be a real directory"
            )
        path = parent / path.name
        if not create:
            return path
        try:
            os.mkdir(str(path), 0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise RegistryError("could not create runtime port registry: {}".format(exc))
    except OSError as exc:
        raise RegistryError("could not inspect runtime port registry: {}".format(exc))
    else:
        if stat.S_ISLNK(listed.st_mode) or not stat.S_ISDIR(listed.st_mode):
            raise RegistryError(
                "runtime port registry must be a real directory, not a link or file"
            )
        try:
            path = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise RegistryError(
                "could not resolve runtime port registry: {}".format(exc)
            )
    if path == Path(path.anchor):
        raise RegistryError("runtime port registry must be an absolute non-root path")
    try:
        before = os.lstat(str(path))
    except OSError as exc:
        raise RegistryError("runtime port registry disappeared: {}".format(exc))
    if not stat.S_ISDIR(before.st_mode):
        raise RegistryError("runtime port registry must be a real directory, not a link or file")
    if os.name == "posix":
        if before.st_uid != os.getuid():
            raise RegistryError("runtime port registry must be owned by the current user")
        if stat.S_IMODE(before.st_mode) & 0o077:
            raise RegistryError("runtime port registry must be private with mode 0700")
    try:
        names = os.listdir(str(path))
        after = os.lstat(str(path))
    except OSError as exc:
        raise RegistryError("could not inspect runtime port registry contents: {}".format(exc))
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise RegistryError("runtime port registry changed while it was inspected")
    unexpected = [
        name for name in names
        if (
            PORT_ENTRY_RE.fullmatch(name) is None
            and QUARANTINE_RE.fullmatch(name) is None
            and UPDATE_RE.fullmatch(name) is None
            and name != MUTEX_ENTRY_NAME
        )
    ]
    if unexpected:
        raise RegistryError("runtime port registry contains unexpected entries")
    for name in names:
        entry = path / name
        try:
            entry_stat = os.lstat(str(entry))
        except OSError as exc:
            raise RegistryError("runtime registry entry is unreadable: {}".format(exc))
        if name == MUTEX_ENTRY_NAME:
            if not stat.S_ISREG(entry_stat.st_mode) or entry_stat.st_size > 1:
                raise RegistryError("runtime registry mutex must be a small regular file")
            if os.name == "posix" and (
                entry_stat.st_uid != os.getuid()
                or stat.S_IMODE(entry_stat.st_mode) & 0o077
            ):
                raise RegistryError("runtime registry mutex must be private and user-owned")
            continue
        if UPDATE_RE.fullmatch(name) is not None:
            if (
                not stat.S_ISREG(entry_stat.st_mode)
                or entry_stat.st_size > MAX_RECORD_BYTES
            ):
                raise RegistryError("runtime update entries must be small regular files")
            if os.name == "posix" and (
                entry_stat.st_uid != os.getuid()
                or stat.S_IMODE(entry_stat.st_mode) & 0o077
            ):
                raise RegistryError("runtime update entries must be private and user-owned")
            if (
                recover
                and time.time() - entry_stat.st_mtime
                > INCOMPLETE_RECORD_GRACE_SECONDS
            ):
                try:
                    current = os.lstat(str(entry))
                    if _signature(current) != _signature(entry_stat):
                        raise RegistryError("runtime update entry changed during cleanup")
                    os.unlink(str(entry))
                except OSError as exc:
                    raise RegistryError(
                        "stale runtime update entry could not be removed: {}".format(exc)
                    )
            continue
        if stat.S_ISLNK(entry_stat.st_mode) or not stat.S_ISDIR(entry_stat.st_mode):
            raise RegistryError("runtime registry entries must be real directories")
        if os.name == "posix" and (
            entry_stat.st_uid != os.getuid()
            or stat.S_IMODE(entry_stat.st_mode) & 0o077
        ):
            raise RegistryError("runtime registry entries must be private and user-owned")
        try:
            _read_record(entry)
        except RegistryError:
            if _reservation_write_in_progress(entry):
                raise ReservationBusy(
                    "a preview port reservation is still being recorded",
                    status="starting",
                )
            if (
                not recover
                and PORT_ENTRY_RE.fullmatch(name) is not None
                and _reservation_write_in_progress(entry, maximum_age=None)
            ):
                continue
            if (
                recover
                and PORT_ENTRY_RE.fullmatch(name) is not None
                and _reap_incomplete_reservation(entry, int(name[:-5]))
            ):
                continue
            raise
    return path


@contextlib.contextmanager
def _registry_mutation_lock(registry):
    """Serialize cross-process mutations of one validated runtime registry."""
    registry = Path(registry)
    mutex = registry / MUTEX_ENTRY_NAME
    descriptor = None
    handle = None
    with _PROCESS_REGISTRY_LOCK:
        try:
            flags = (
                os.O_RDWR
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            created = False
            try:
                descriptor = os.open(
                    str(mutex), flags | os.O_CREAT | os.O_EXCL, 0o600
                )
                created = True
            except FileExistsError:
                descriptor = os.open(str(mutex), flags)
            os.set_inheritable(descriptor, False)
            opened = os.fstat(descriptor)
            current = os.lstat(str(mutex))
            if (
                not stat.S_ISREG(opened.st_mode)
                or not stat.S_ISREG(current.st_mode)
                or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
                or opened.st_size > 1
                or (
                    os.name == "posix"
                    and (
                        opened.st_uid != os.getuid()
                        or stat.S_IMODE(opened.st_mode) & 0o077
                    )
                )
            ):
                raise RegistryError("runtime registry mutex is unsafe")
            if created and os.name == "posix":
                os.fchmod(descriptor, 0o600)
                opened = os.fstat(descriptor)
                current = os.lstat(str(mutex))
                if (
                    (opened.st_dev, opened.st_ino)
                    != (current.st_dev, current.st_ino)
                    or opened.st_uid != os.getuid()
                    or stat.S_IMODE(opened.st_mode) & 0o077
                ):
                    raise RegistryError("runtime registry mutex is unsafe")
            if opened.st_size == 0:
                os.write(descriptor, b"\0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            handle = os.fdopen(descriptor, "r+b", buffering=0)
            descriptor = None
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
        except RegistryError:
            raise
        except OSError as exc:
            raise RegistryError("runtime registry mutex is unavailable: {}".format(exc))
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if handle is not None:
                handle.close()


def _record_path(registry, port):
    return Path(registry) / ("{}.lock".format(_validate_port(port)))


def _validate_record(value):
    if not isinstance(value, dict) or set(value) != RECORD_KEYS:
        raise RegistryError("port reservation record has an invalid schema")
    _validate_binding(
        value.get("port"), value.get("owner"), value.get("colorLock"),
        value.get("color"), value.get("token"),
    )
    if value.get("version") != VERSION:
        raise RegistryError("port reservation record version is unsupported")
    instance = value.get("instance")
    if instance is not None and (
        not isinstance(instance, str) or INSTANCE_RE.fullmatch(instance) is None
    ):
        raise RegistryError("port reservation instance identity is invalid")
    pid = value.get("pid")
    if pid is not None and (
        isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0
    ):
        raise RegistryError("port reservation process identity is invalid")
    if (instance is None) != (pid is None):
        raise RegistryError("port reservation instance and process identities must appear together")
    return value


def _read_record(lock):
    lock = Path(lock)
    try:
        lock_stat = os.lstat(str(lock))
        names = os.listdir(str(lock))
        record_path = lock / "claim.json"
        record_stat = os.lstat(str(record_path))
    except OSError as exc:
        raise RegistryError("port reservation is unreadable: {}".format(exc))
    if not stat.S_ISDIR(lock_stat.st_mode) or names != ["claim.json"]:
        raise RegistryError("port reservation must contain exactly one claim.json file")
    if not stat.S_ISREG(record_stat.st_mode) or record_stat.st_size > MAX_RECORD_BYTES:
        raise RegistryError("port reservation record must be a small regular file")
    if os.name == "posix":
        if (
            lock_stat.st_uid != os.getuid()
            or record_stat.st_uid != os.getuid()
            or stat.S_IMODE(lock_stat.st_mode) & 0o077
            or stat.S_IMODE(record_stat.st_mode) & 0o077
        ):
            raise RegistryError("port reservation files must be private and user-owned")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = None
    try:
        descriptor = os.open(str(record_path), flags)
        opened = os.fstat(descriptor)
        data = os.read(descriptor, MAX_RECORD_BYTES + 1)
        after_read = os.fstat(descriptor)
        current = os.lstat(str(record_path))
        lock_after = os.lstat(str(lock))
    except OSError as exc:
        raise RegistryError("port reservation record could not be read safely: {}".format(exc))
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if (
        _signature(opened) != _signature(record_stat)
        or _signature(opened) != _signature(after_read)
        or _signature(opened) != _signature(current)
        or (lock_stat.st_dev, lock_stat.st_ino) != (lock_after.st_dev, lock_after.st_ino)
        or len(data) > MAX_RECORD_BYTES
    ):
        raise RegistryError("port reservation changed while it was read")
    try:
        value = json.loads(data.decode("utf-8"), parse_constant=_reject_constant)
        _reject_json_surrogates(value)
    except (UnicodeDecodeError, ValueError) as exc:
        raise RegistryError("port reservation record is invalid JSON: {}".format(exc))
    return _validate_record(value), lock_stat


def _reservation_write_in_progress(
    lock, maximum_age=INCOMPLETE_RECORD_GRACE_SECONDS
):
    """Recognize only the short private interval before claim.json is complete."""
    lock = Path(lock)
    try:
        lock_stat = os.lstat(str(lock))
        names = os.listdir(str(lock))
    except OSError:
        return False
    if stat.S_ISLNK(lock_stat.st_mode) or not stat.S_ISDIR(lock_stat.st_mode):
        return False
    if os.name == "posix" and (
        lock_stat.st_uid != os.getuid()
        or stat.S_IMODE(lock_stat.st_mode) & 0o077
    ):
        return False
    if names not in ([], ["claim.json"]):
        return False
    if names == ["claim.json"]:
        try:
            record_stat = os.lstat(str(lock / "claim.json"))
        except OSError:
            return False
        if (
            not stat.S_ISREG(record_stat.st_mode)
            or record_stat.st_size > MAX_RECORD_BYTES
        ):
            return False
        if os.name == "posix" and (
            record_stat.st_uid != os.getuid()
            or stat.S_IMODE(record_stat.st_mode) & 0o077
        ):
            return False
    try:
        age = max(0.0, time.time() - lock_stat.st_mtime)
    except (AttributeError, TypeError, ValueError):
        return False
    return maximum_age is None or age <= maximum_age


def _reap_incomplete_reservation(lock, port):
    """Remove an aged private incomplete claim only while its port is free."""
    lock = Path(lock)
    try:
        before = os.lstat(str(lock))
        names = os.listdir(str(lock))
    except OSError:
        return False
    if (
        not stat.S_ISDIR(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or names not in ([], ["claim.json"])
        or time.time() - before.st_mtime <= INCOMPLETE_RECORD_GRACE_SECONDS
    ):
        return False
    record_stat = None
    if names == ["claim.json"]:
        try:
            record_stat = os.lstat(str(lock / "claim.json"))
        except OSError:
            return False
        if (
            not stat.S_ISREG(record_stat.st_mode)
            or record_stat.st_size > MAX_RECORD_BYTES
        ):
            return False
    if os.name == "posix" and (
        before.st_uid != os.getuid()
        or stat.S_IMODE(before.st_mode) & 0o077
        or (
            record_stat is not None
            and (
                record_stat.st_uid != os.getuid()
                or stat.S_IMODE(record_stat.st_mode) & 0o077
            )
        )
    ):
        return False
    if not _port_available(port):
        raise ReservationBusy(
            "incomplete preview port {} claim cannot be reaped while occupied".format(port),
            status="occupied",
        )
    isolated = _quarantine_name(lock.parent, "reap")
    try:
        os.rename(str(lock), str(isolated))
    except FileNotFoundError:
        return True
    except OSError as exc:
        raise RegistryError("could not isolate incomplete port reservation: {}".format(exc))
    try:
        current_dir = os.lstat(str(isolated))
        if (before.st_dev, before.st_ino) != (current_dir.st_dev, current_dir.st_ino):
            raise RegistryError("incomplete port reservation changed during cleanup")
        current_names = os.listdir(str(isolated))
        if current_names != names:
            raise RegistryError("incomplete port reservation contents changed")
        if record_stat is not None:
            current_record = os.lstat(str(isolated / "claim.json"))
            if _signature(current_record) != _signature(record_stat):
                raise RegistryError("incomplete port record changed during cleanup")
            os.unlink(str(isolated / "claim.json"))
        os.rmdir(str(isolated))
    except Exception:
        _restore_quarantine(isolated, lock)
        raise
    return True


def _write_all(descriptor, payload):
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if not isinstance(written, int) or written <= 0:
            raise OSError(errno.ENOSPC, "could not complete runtime metadata write")
        remaining = remaining[written:]


def _write_new_record(lock, value):
    lock = Path(lock)
    path = lock / "claim.json"
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    descriptor = None
    try:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(str(path), flags, 0o600)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _replace_record(lock, expected, updated):
    registry = _validate_registry(Path(lock).parent, create=False)
    with _registry_mutation_lock(registry):
        _validate_registry(registry, create=False, recover=True)
        return _replace_record_locked(lock, expected, updated)


def _replace_record_locked(lock, expected, updated):
    lock = Path(lock)
    current, lock_stat = _read_record(lock)
    if current != expected:
        raise RegistryError("port reservation changed before its record update")
    descriptor = None
    temporary = None
    try:
        temporary = lock.parent / ".wk-update-{}".format(
            secrets.token_urlsafe(18)
        )
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(str(temporary), flags, 0o600)
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        payload = (
            json.dumps(updated, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        current_again, current_lock_stat = _read_record(lock)
        if current_again != expected or (
            lock_stat.st_dev, lock_stat.st_ino
        ) != (current_lock_stat.st_dev, current_lock_stat.st_ino):
            raise RegistryError("port reservation changed during its record update")
        os.replace(str(temporary), str(lock / "claim.json"))
        temporary = None
        final, final_lock_stat = _read_record(lock)
        if final != updated or (lock_stat.st_dev, lock_stat.st_ino) != (
            final_lock_stat.st_dev,
            final_lock_stat.st_ino,
        ):
            raise RegistryError("port reservation changed after its record update")
        if not touch_path_nofollow(lock):
            raise RegistryError(
                "port reservation changed while its lease was refreshed"
            )
        touched, touched_lock_stat = _read_record(lock)
        if touched != updated or (lock_stat.st_dev, lock_stat.st_ino) != (
            touched_lock_stat.st_dev,
            touched_lock_stat.st_ino,
        ):
            raise RegistryError("port reservation changed while its lease was refreshed")
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try:
                os.unlink(str(temporary))
            except OSError:
                pass


def _binding_matches(record, port, owner, color_lock, color, token=None):
    expected = _validate_binding(port, owner, color_lock, color, token)
    try:
        actual = _validate_binding(
            record.get("port"),
            record.get("owner"),
            record.get("colorLock"),
            record.get("color"),
        )
    except RegistryError:
        return False
    if (
        actual[0] != expected[0]
        or _normalized_owner_path(actual[1])
        != _normalized_owner_path(expected[1])
        or actual[2:] != expected[2:]
    ):
        return False
    return token is None or record.get("token") == token


def _probe_instance(port, instance, timeout=0.35):
    if not instance:
        return False
    connection = http.client.HTTPConnection("127.0.0.1", int(port), timeout=timeout)
    try:
        connection.request("GET", "/__wk/state")
        response = connection.getresponse()
        response.read(1024)
        return response.getheader("X-WK-Preview-Instance") == instance
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()


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


def reservation_status(record, lock_stat, grace_seconds):
    grace = _validate_grace_seconds(grace_seconds)
    if record.get("instance") and _probe_instance(record["port"], record["instance"]):
        return "active"
    age = max(0.0, time.time() - lock_stat.st_mtime)
    if age <= grace:
        return "starting"
    return "stale" if _port_available(record["port"]) else "occupied"


def inspect_reservation(port, owner, color_lock, color, token, grace_seconds=180):
    grace_seconds = _validate_grace_seconds(grace_seconds)
    registry = _validate_registry(runtime_registry_path(), create=False)
    lock = _record_path(registry, port)
    if not _path_exists(lock):
        return "missing", None
    record, lock_stat = _read_record(lock)
    if not _binding_matches(record, port, owner, color_lock, color, token):
        return "foreign", record
    return reservation_status(record, lock_stat, grace_seconds), record


def find_reservation(color_lock, token):
    """Find one reservation by its color-lock path and random token."""
    if not isinstance(token, str) or TOKEN_RE.fullmatch(token) is None:
        raise RegistryError("reservation token is invalid")
    color_lock = _normalized_binding_path(color_lock)
    registry = _validate_registry(runtime_registry_path(), create=False)
    if not _path_exists(registry):
        return None, None
    matches = []
    for name in os.listdir(str(registry)):
        if PORT_ENTRY_RE.fullmatch(name) is None:
            continue
        record, lock_stat = _read_record(registry / name)
        if (
            record.get("token") == token
            and _normalized_binding_path(record.get("colorLock")) == color_lock
        ):
            matches.append((record, lock_stat))
    if len(matches) > 1:
        raise RegistryError("reservation token is duplicated in the runtime registry")
    return matches[0] if matches else (None, None)


def find_owner_reservations(owner, color=None):
    """Return validated global reservations for one exact worktree owner."""
    if not _safe_text(owner):
        raise RegistryError("owner must be one nonempty line of at most 4096 bytes")
    if color is not None and (
        not isinstance(color, str) or SLUG_RE.fullmatch(color) is None
    ):
        raise RegistryError("color must be a safe palette slug")
    registry = _validate_registry(runtime_registry_path(), create=False)
    if not _path_exists(registry):
        return []
    matches = []
    for name in os.listdir(str(registry)):
        if PORT_ENTRY_RE.fullmatch(name) is None:
            continue
        record, lock_stat = _read_record(registry / name)
        if _owners_match(record.get("owner"), owner) and (
            color is None or record.get("color") == color
        ):
            matches.append((record, lock_stat))
    return matches


def _quarantine_name(registry, purpose):
    return Path(registry) / ".wk-{}-{}".format(purpose, secrets.token_urlsafe(18))


def _restore_quarantine(isolated, target):
    if not _path_exists(target) and _path_exists(isolated):
        try:
            os.rename(str(isolated), str(target))
        except OSError:
            pass
    if not _path_exists(target):
        try:
            os.mkdir(str(target), 0o700)
        except OSError:
            pass


def _remove_quarantined_reservation(isolated, expected):
    current, _unused = _read_record(isolated)
    if current != expected:
        raise RegistryError("port reservation changed during cleanup")
    os.unlink(str(Path(isolated) / "claim.json"))
    os.rmdir(str(isolated))


def _isolate_reservation(lock, expected, purpose):
    registry = lock.parent
    isolated = _quarantine_name(registry, purpose)
    try:
        os.rename(str(lock), str(isolated))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RegistryError("could not isolate port reservation: {}".format(exc))
    try:
        current, _unused = _read_record(isolated)
        if current != expected:
            raise RegistryError("port reservation changed while it was isolated")
    except Exception:
        _restore_quarantine(isolated, lock)
        raise
    return isolated


def reserve_port(port, owner, color_lock, color, grace_seconds=180):
    grace_seconds = _validate_grace_seconds(grace_seconds)
    port, owner, color_lock, color = _validate_binding(
        port, owner, color_lock, color
    )
    registry = _validate_registry(runtime_registry_path(), create=True)
    with _registry_mutation_lock(registry):
        _validate_registry(registry, create=False, recover=True)
        return _reserve_port_locked(
            registry, port, owner, color_lock, color, grace_seconds
        )


def _reserve_port_locked(registry, port, owner, color_lock, color, grace_seconds):
    lock = _record_path(registry, port)
    for _attempt in range(3):
        try:
            os.mkdir(str(lock), 0o700)
        except FileExistsError:
            try:
                record, lock_stat = _read_record(lock)
            except RegistryError:
                if _reservation_write_in_progress(lock):
                    raise ReservationBusy(
                        "preview port {} is still being reserved".format(port),
                        status="starting",
                    )
                raise
            status = reservation_status(record, lock_stat, grace_seconds)
            if status != "stale":
                raise ReservationBusy(
                    "preview port {} is reserved by another Webkit session".format(port),
                    status=status,
                )
            isolated = _isolate_reservation(lock, record, "reap")
            if isolated is None:
                continue
            try:
                _remove_quarantined_reservation(isolated, record)
            except Exception:
                _restore_quarantine(isolated, lock)
                raise
            continue
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                continue
            raise RegistryError("could not create port reservation: {}".format(exc))
        token = secrets.token_urlsafe(32)
        value = {
            "version": VERSION,
            "port": port,
            "owner": owner,
            "colorLock": color_lock,
            "color": color,
            "token": token,
            "instance": None,
            "pid": None,
        }
        try:
            _write_new_record(lock, value)
            return value
        except Exception:
            try:
                if (lock / "claim.json").is_file() and not (lock / "claim.json").is_symlink():
                    os.unlink(str(lock / "claim.json"))
                os.rmdir(str(lock))
            except OSError:
                pass
            raise
    raise ReservationBusy("preview port {} was claimed concurrently".format(port))


def register_instance(port, owner, color_lock, color, token, instance, pid):
    port, owner, color_lock, color = _validate_binding(
        port, owner, color_lock, color, token
    )
    if not isinstance(instance, str) or INSTANCE_RE.fullmatch(instance) is None:
        raise RegistryError("preview instance identity is invalid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise RegistryError("preview process identity is invalid")
    registry = _validate_registry(runtime_registry_path(), create=False)
    with _registry_mutation_lock(registry):
        _validate_registry(registry, create=False, recover=True)
        lock = _record_path(registry, port)
        record, _unused = _read_record(lock)
        if not _binding_matches(record, port, owner, color_lock, color, token):
            raise RegistryError("port reservation belongs to another claim")
        updated = dict(record, instance=instance, pid=pid)
        _replace_record_locked(lock, record, updated)
        return updated


def touch_reservation(port, owner, color_lock, color, token, instance=None):
    port, owner, color_lock, color = _validate_binding(
        port, owner, color_lock, color, token
    )
    registry = _validate_registry(runtime_registry_path(), create=False)
    if not _path_exists(registry):
        return False
    with _registry_mutation_lock(registry):
        _validate_registry(registry, create=False, recover=True)
        lock = _record_path(registry, port)
        record, before = _read_record(lock)
        if not _binding_matches(record, port, owner, color_lock, color, token):
            return False
        if instance is not None and record.get("instance") != instance:
            return False
        current = os.lstat(str(lock))
        if (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
            return False
        if not touch_path_nofollow(lock):
            return False
        after_record, after = _read_record(lock)
        return (
            after_record == record
            and (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
        )


def release_port(port, owner, color_lock, color, token, force=False):
    port, owner, color_lock, color = _validate_binding(
        port, owner, color_lock, color, token
    )
    registry = _validate_registry(runtime_registry_path(), create=False)
    if not _path_exists(registry):
        return False
    with _registry_mutation_lock(registry):
        _validate_registry(registry, create=False, recover=True)
        return _release_port_locked(
            registry, port, owner, color_lock, color, token, force
        )


def _release_port_locked(registry, port, owner, color_lock, color, token, force):
    lock = _record_path(registry, port)
    if not _path_exists(lock):
        return False
    record, _unused = _read_record(lock)
    if record.get("token") != token or record.get("colorLock") != color_lock:
        raise RegistryError("port reservation belongs to another claim")
    if not force and not _binding_matches(record, port, owner, color_lock, color, token):
        raise RegistryError("port reservation belongs to another owner")
    isolated = _isolate_reservation(lock, record, "release")
    if isolated is None:
        return False
    try:
        _remove_quarantined_reservation(isolated, record)
    except Exception:
        _restore_quarantine(isolated, lock)
        raise
    return True


def read_color_lock(lock):
    """Return (owner, token or None, stat) for an exact safe color lock."""
    lock = Path(lock)
    try:
        lock_stat = os.lstat(str(lock))
    except OSError as exc:
        raise RegistryError("color lock is unreadable: {}".format(exc))
    if stat.S_ISLNK(lock_stat.st_mode):
        raise RegistryError("refusing symbolic-link lock target")
    if not stat.S_ISDIR(lock_stat.st_mode):
        raise RegistryError("refusing non-directory lock target")
    if os.name == "posix" and (
        lock_stat.st_uid != os.getuid()
        or stat.S_IMODE(lock_stat.st_mode) & 0o077
    ):
        raise RegistryError("color lock must be private and user-owned")
    try:
        names = sorted(os.listdir(str(lock)))
    except OSError as exc:
        raise RegistryError("color lock contents are unreadable: {}".format(exc))
    if names not in (
        ["owner"], ["owner", "reservation"]
    ):
        raise RegistryError("color lock contains unexpected entries")

    def read_line(name, maximum):
        path = lock / name
        try:
            before = os.lstat(str(path))
        except OSError as exc:
            raise RegistryError("color lock {} is unreadable: {}".format(name, exc))
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
            raise RegistryError("color lock {} must be a small regular file".format(name))
        if os.name == "posix" and (
            before.st_uid != os.getuid()
            or stat.S_IMODE(before.st_mode) & 0o077
        ):
            raise RegistryError(
                "color lock {} must be private and user-owned".format(name)
            )
        flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = None
        try:
            descriptor = os.open(str(path), flags)
            opened = os.fstat(descriptor)
            data = os.read(descriptor, maximum + 1)
            after = os.fstat(descriptor)
            current = os.lstat(str(path))
        except OSError as exc:
            raise RegistryError("color lock {} could not be read safely: {}".format(name, exc))
        finally:
            if descriptor is not None:
                os.close(descriptor)
        if (
            _signature(opened) != _signature(before)
            or _signature(opened) != _signature(after)
            or _signature(opened) != _signature(current)
            or len(data) > maximum
        ):
            raise RegistryError("color lock {} changed while it was read".format(name))
        try:
            value = data.decode("utf-8")
        except UnicodeDecodeError:
            raise RegistryError("color lock {} is not UTF-8".format(name))
        if value.endswith("\n"):
            value = value[:-1]
        if "\n" in value or not _safe_text(value, maximum):
            raise RegistryError("color lock {} is not one safe line".format(name))
        return value

    owner = read_line("owner", 4096)
    token = read_line("reservation", 256) if "reservation" in names else None
    if token is not None and TOKEN_RE.fullmatch(token) is None:
        raise RegistryError("color lock reservation token is invalid")
    lock_after = os.lstat(str(lock))
    if (lock_stat.st_dev, lock_stat.st_ino) != (lock_after.st_dev, lock_after.st_ino):
        raise RegistryError("color lock changed while it was read")
    return owner, token, lock_stat


def _write_color_line(lock, name, value):
    lock = Path(lock)
    path = lock / name
    temporary = lock / ".wk-write-{}-{}".format(
        name, secrets.token_urlsafe(18)
    )
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(str(temporary), flags, 0o600)
    try:
        _write_all(descriptor, (value + "\n").encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        if _path_exists(path):
            raise RegistryError("color metadata target already exists")
        os.replace(str(temporary), str(path))
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(str(temporary))
            except OSError:
                pass


def _incomplete_color_shape(lock):
    lock = Path(lock)
    try:
        lock_stat = os.lstat(str(lock))
        names = sorted(os.listdir(str(lock)))
    except OSError:
        return None
    permanent_names = [name for name in names if name in ("owner", "reservation")]
    temporary_names = [name for name in names if COLOR_TEMP_RE.fullmatch(name)]
    if (
        not stat.S_ISDIR(lock_stat.st_mode)
        or stat.S_ISLNK(lock_stat.st_mode)
        or permanent_names not in (
            [], ["owner"], ["reservation"], ["owner", "reservation"]
        )
        or len(temporary_names) > 1
        or len(permanent_names) + len(temporary_names) != len(names)
    ):
        return None
    if os.name == "posix" and (
        lock_stat.st_uid != os.getuid()
        or stat.S_IMODE(lock_stat.st_mode) & 0o077
    ):
        return None
    files = {}
    for name in names:
        try:
            value = os.lstat(str(lock / name))
        except OSError:
            return None
        match = COLOR_TEMP_RE.fullmatch(name)
        target_name = match.group(1) if match else name
        maximum = 4096 if target_name == "owner" else 256
        if not stat.S_ISREG(value.st_mode) or value.st_size > maximum:
            return None
        if os.name == "posix" and (
            value.st_uid != os.getuid()
            or stat.S_IMODE(value.st_mode) & 0o077
        ):
            return None
        files[name] = value
    return lock_stat, names, files


def _color_write_in_progress(lock):
    shape = _incomplete_color_shape(lock)
    return bool(
        shape
        and time.time() - shape[0].st_mtime <= INCOMPLETE_RECORD_GRACE_SECONDS
    )


def _reservations_for_color_lock(color_lock):
    registry = _validate_registry(runtime_registry_path(), create=False)
    if not _path_exists(registry):
        return []
    result = []
    expected_lock = _normalized_binding_path(color_lock)
    for name in os.listdir(str(registry)):
        if PORT_ENTRY_RE.fullmatch(name) is None:
            continue
        record, lock_stat = _read_record(registry / name)
        if _normalized_binding_path(record.get("colorLock")) == expected_lock:
            result.append((record, lock_stat))
    return result


def _reap_incomplete_color_lock(lock):
    shape = _incomplete_color_shape(lock)
    if shape is None:
        return False
    lock_stat, names, file_stats = shape
    if time.time() - lock_stat.st_mtime <= INCOMPLETE_RECORD_GRACE_SECONDS:
        return False
    linked = _reservations_for_color_lock(lock)
    for record, record_stat in linked:
        status = reservation_status(record, record_stat, 180)
        if status != "stale":
            raise ReservationBusy(
                "incomplete color claim is linked to a {} port reservation".format(status),
                status=status,
            )
    for record, _record_stat in linked:
        release_port(
            record["port"], record["owner"], record["colorLock"],
            record["color"], record["token"], force=True,
        )
    isolated = lock.parent / ".wk-reap-{}".format(secrets.token_urlsafe(18))
    try:
        os.rename(str(lock), str(isolated))
    except FileNotFoundError:
        return True
    except OSError as exc:
        raise RegistryError("could not isolate incomplete color lock: {}".format(exc))
    try:
        current_dir = os.lstat(str(isolated))
        if (lock_stat.st_dev, lock_stat.st_ino) != (
            current_dir.st_dev, current_dir.st_ino
        ):
            raise RegistryError("incomplete color lock changed during cleanup")
        if sorted(os.listdir(str(isolated))) != names:
            raise RegistryError("incomplete color lock contents changed")
        for name in names:
            current = os.lstat(str(isolated / name))
            if _signature(current) != _signature(file_stats[name]):
                raise RegistryError("incomplete color metadata changed during cleanup")
            os.unlink(str(isolated / name))
        os.rmdir(str(isolated))
    except Exception:
        _restore_quarantine(isolated, lock)
        raise
    return True


def _validate_color_registry(lock_dir, create=False):
    lock_dir = Path(lock_dir).expanduser()
    if not lock_dir.is_absolute():
        raise RegistryError("color lock registry must be an absolute path")
    if lock_dir.resolve() == Path(lock_dir.anchor):
        raise RegistryError("color lock registry must not resolve to the filesystem root")
    try:
        value = os.lstat(str(lock_dir))
    except FileNotFoundError:
        if not create:
            return lock_dir
        try:
            os.mkdir(str(lock_dir), 0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise RegistryError("could not create color lock registry: {}".format(exc))
        value = os.lstat(str(lock_dir))
    except OSError as exc:
        raise RegistryError("could not inspect color lock registry: {}".format(exc))
    if stat.S_ISLNK(value.st_mode):
        raise RegistryError("color lock registry must not be a symbolic link")
    if not stat.S_ISDIR(value.st_mode):
        raise RegistryError("color lock registry exists but is not a directory")
    if os.name == "posix":
        if value.st_uid != os.getuid():
            raise RegistryError("color lock registry must be owned by the current user")
        if stat.S_IMODE(value.st_mode) & 0o077:
            raise RegistryError("color lock registry must be private with mode 0700")
    try:
        names = os.listdir(str(lock_dir))
    except OSError as exc:
        raise RegistryError("color lock registry contents are unreadable: {}".format(exc))
    unexpected = [name for name in names if COLOR_ENTRY_RE.fullmatch(name) is None]
    if unexpected:
        raise RegistryError("color lock registry contains unexpected entries")
    for name in names:
        entry = lock_dir / name
        try:
            read_color_lock(entry)
        except RegistryError:
            if _color_write_in_progress(entry):
                raise ReservationBusy(
                    "a color claim is still being recorded", status="starting"
                )
            if _reap_incomplete_color_lock(entry):
                continue
            raise
    return lock_dir


def _remove_color_quarantine(isolated, expected_owner, expected_token):
    owner, token, _unused = read_color_lock(isolated)
    if owner != expected_owner or token != expected_token:
        raise RegistryError("color lock changed during cleanup")
    if token is not None:
        os.unlink(str(Path(isolated) / "reservation"))
    os.unlink(str(Path(isolated) / "owner"))
    os.rmdir(str(isolated))


def _isolate_color_lock(lock, expected_owner, expected_token, purpose):
    quarantine = lock.parent / ".wk-{}-{}".format(purpose, secrets.token_urlsafe(18))
    try:
        os.rename(str(lock), str(quarantine))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RegistryError("could not isolate color lock: {}".format(exc))
    try:
        owner, token, _unused = read_color_lock(quarantine)
        if owner != expected_owner or token != expected_token:
            raise RegistryError("color lock changed while it was isolated")
    except Exception:
        _restore_quarantine(quarantine, lock)
        raise
    return quarantine


def color_session_status(lock, port, color, expected_owner, grace_seconds=180):
    _validate_binding(
        port, expected_owner, os.path.abspath(str(lock)), color
    )
    owner, token, color_stat = read_color_lock(lock)
    if not _owners_match(owner, expected_owner):
        return "foreign", None
    grace = _validate_grace_seconds(grace_seconds)
    if token is None:
        age = max(0.0, time.time() - color_stat.st_mtime)
        return ("legacy" if age <= max(0.0, grace) else "stale-legacy"), None
    record, record_stat = find_reservation(str(Path(lock).absolute()), token)
    if record is None:
        return "missing", None
    if not _owners_match(record.get("owner"), owner) or record.get("color") != color:
        return "foreign", record
    status = reservation_status(record, record_stat, grace_seconds)
    return status, record


def _cleanup_existing_color(lock, owner, token, port, color, force=False):
    isolated = _isolate_color_lock(lock, owner, token, "reap")
    if isolated is None:
        return False
    try:
        if token is not None:
            record, _unused = find_reservation(str(lock.absolute()), token)
            if record is not None:
                release_port(
                    record["port"], owner, str(lock.absolute()), record["color"],
                    token, force=force,
                )
        _remove_color_quarantine(isolated, owner, token)
    except Exception:
        _restore_quarantine(isolated, lock)
        raise
    return True


def claim_color(lock_dir, color, port, owner, grace_seconds=180):
    grace_seconds = _validate_grace_seconds(grace_seconds)
    if not isinstance(color, str) or SLUG_RE.fullmatch(color) is None:
        raise RegistryError("color must be a safe palette slug")
    _validate_binding(port, owner, os.path.abspath(str(Path(lock_dir) / (color + ".lock"))), color)
    lock_dir = _validate_color_registry(lock_dir, create=True)
    lock = lock_dir / (color + ".lock")
    if lock.parent != lock_dir:
        raise RegistryError("color lock path is unsafe")
    for _attempt in range(3):
        try:
            os.mkdir(str(lock), 0o700)
        except FileExistsError:
            owner_now, token_now, color_stat = read_color_lock(lock)
            if token_now is None:
                age = max(0.0, time.time() - color_stat.st_mtime)
                same_owner_stale = _owners_match(owner_now, owner) and age > float(
                    grace_seconds
                )
                if not same_owner_stale:
                    raise ReservationBusy("{} is already claimed".format(color), status="legacy")
            else:
                status, _record = color_session_status(
                    lock, port, color, owner_now, grace_seconds
                )
                if status not in ("stale", "missing"):
                    raise ReservationBusy(
                        "{} is already claimed".format(color), status=status
                    )
            _cleanup_existing_color(
                lock, owner_now, token_now, port, color,
                force=(not _owners_match(owner_now, owner)),
            )
            continue
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                continue
            raise RegistryError("could not create color lock: {}".format(exc))
        try:
            _write_color_line(lock, "owner", owner)
            reservation = reserve_port(
                port, owner, str(lock.absolute()), color, grace_seconds
            )
            try:
                _write_color_line(lock, "reservation", reservation["token"])
            except Exception:
                release_port(
                    port, owner, str(lock.absolute()), color,
                    reservation["token"], force=False,
                )
                raise
            return lock, reservation
        except Exception:
            try:
                owner_file = lock / "owner"
                reservation_file = lock / "reservation"
                if reservation_file.is_file() and not reservation_file.is_symlink():
                    os.unlink(str(reservation_file))
                if owner_file.is_file() and not owner_file.is_symlink():
                    os.unlink(str(owner_file))
                os.rmdir(str(lock))
            except OSError:
                pass
            raise
    raise ReservationBusy("{} was claimed concurrently".format(color))


def release_color(lock_dir, color, port, owner, force=False):
    if not isinstance(color, str) or SLUG_RE.fullmatch(color) is None:
        raise RegistryError("color must be a safe palette slug")
    _validate_binding(
        port,
        owner,
        os.path.abspath(str(Path(lock_dir) / (color + ".lock"))),
        color,
    )
    lock_dir = _validate_color_registry(lock_dir, create=False)
    lock = lock_dir / (color + ".lock")
    if not _path_exists(lock):
        matches = find_owner_reservations(owner, color)
        if not matches:
            return False
        if len(matches) != 1:
            raise RegistryError(
                "more than one global reservation matches this owner and color"
            )
        record, _unused = matches[0]
        actual_lock = Path(record["colorLock"])
        actual_lock_dir = _validate_color_registry(actual_lock.parent, create=False)
        if actual_lock != actual_lock_dir / (color + ".lock") or not _path_exists(actual_lock):
            raise RegistryError("global reservation points to a missing color lock")
        actual_owner, actual_token, _unused = read_color_lock(actual_lock)
        if not _owners_match(actual_owner, owner) or actual_token != record["token"]:
            raise RegistryError("global reservation and color lock do not match")
        return release_color(
            actual_lock_dir, color, record["port"], owner, force=force
        )
    existing_owner, token, _unused = read_color_lock(lock)
    if not _owners_match(existing_owner, owner) and not force:
        raise ReservationBusy(
            "{} is claimed by another owner".format(color), status="foreign"
        )
    isolated = _isolate_color_lock(lock, existing_owner, token, "release")
    if isolated is None:
        return False
    try:
        if token is not None:
            record, _unused = find_reservation(str(lock.absolute()), token)
            if record is not None:
                release_port(
                    record["port"], existing_owner, str(lock.absolute()),
                    record["color"], token, force=force,
                )
        _remove_color_quarantine(isolated, existing_owner, token)
    except Exception:
        _restore_quarantine(isolated, lock)
        raise
    return True


def discover_active(palette, lock_dir, owner, grace_seconds=180):
    grace_seconds = _validate_grace_seconds(grace_seconds)
    lock_dir = _validate_color_registry(lock_dir, create=False)
    active = []
    blockers = []
    configured = set()
    entries_by_color = {}
    seen = set()
    for entry in palette:
        configured.add(entry["slug"])
        entries_by_color[entry["slug"]] = entry
    if _path_exists(lock_dir):
        for entry in palette:
            color = entry["slug"]
            port = entry["port"]
            lock = lock_dir / (color + ".lock")
            if not _path_exists(lock):
                continue
            lock_owner, token, _unused = read_color_lock(lock)
            if not _owners_match(lock_owner, owner):
                continue
            if token is not None:
                seen.add((_normalized_binding_path(lock), token))
            status, record = color_session_status(
                lock, port, color, owner, grace_seconds
            )
            if status == "active":
                active.append((entry, record))
            elif status in ("starting", "occupied", "legacy", "missing", "foreign"):
                blockers.append((entry, status))
        for name in os.listdir(str(lock_dir)):
            if not name.endswith(".lock"):
                continue
            color = name[:-5]
            if color in configured:
                continue
            lock = lock_dir / name
            lock_owner, token, _unused = read_color_lock(lock)
            if not _owners_match(lock_owner, owner):
                continue
            if token is not None:
                seen.add((_normalized_binding_path(lock), token))
            status, _record = color_session_status(
                lock, 1, color, owner, grace_seconds
            )
            if status in (
                "active", "starting", "occupied", "legacy", "missing", "foreign"
            ):
                blockers.append(({"slug": color}, "unconfigured-" + status))
    for record, lock_stat in find_owner_reservations(owner):
        key = (_normalized_binding_path(record["colorLock"]), record["token"])
        if key in seen:
            continue
        color = record["color"]
        entry = entries_by_color.get(color, {"slug": color})
        status = reservation_status(record, lock_stat, grace_seconds)
        if status == "stale":
            continue
        try:
            old_lock = Path(record["colorLock"])
            old_lock_dir = _validate_color_registry(old_lock.parent, create=False)
            old_owner, old_token, _unused = read_color_lock(old_lock)
            linked = (
                _normalized_binding_path(old_lock)
                == _normalized_binding_path(old_lock_dir / (color + ".lock"))
                and _owners_match(old_owner, owner)
                and old_token == record["token"]
            )
        except RegistryError:
            linked = False
        if not linked:
            blockers.append((entry, "detached-" + status))
        elif status == "active" and color in entries_by_color:
            active.append((entry, record))
        elif status in ("active", "starting", "occupied"):
            prefix = "unconfigured-" if color not in entries_by_color else "relocated-"
            blockers.append((entry, prefix + status))
    if len(active) > 1 or (active and blockers):
        raise RegistryError("more than one live or starting claim belongs to this worktree")
    if active:
        return active
    if blockers:
        entry, status = blockers[0]
        raise ReservationBusy(
            "{} has an existing same-owner session whose identity is {}".format(
                entry["slug"], status
            ),
            status=status,
        )
    return []


def _parse_palette(value):
    try:
        palette = json.loads(value, parse_constant=_reject_constant)
        _reject_json_surrogates(palette)
    except ValueError as exc:
        raise RegistryError("palette JSON is invalid: {}".format(exc))
    if not isinstance(palette, list) or not palette:
        raise RegistryError("palette must be a nonempty JSON list")
    result = []
    slugs = set()
    ports = set()
    for entry in palette:
        if not isinstance(entry, dict):
            raise RegistryError("palette entries must be objects")
        slug = entry.get("slug")
        port = entry.get("port")
        emoji = entry.get("emoji")
        _validate_binding(
            port, "palette", os.path.abspath("/palette/" + str(slug) + ".lock"), slug
        )
        if not _safe_text(emoji, 256):
            raise RegistryError("palette labels must be safe nonempty text")
        if slug in slugs:
            raise RegistryError("palette slugs must be unique")
        if port in ports:
            raise RegistryError("palette ports must be unique")
        slugs.add(slug)
        ports.add(port)
        result.append({"slug": slug, "port": port, "emoji": emoji})
    return result


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command")
    sub.required = True

    def binding(command, include_token=False):
        item = sub.add_parser(command)
        item.add_argument("--port", type=int, required=True)
        item.add_argument("--owner", required=True)
        item.add_argument("--color-lock", required=True)
        item.add_argument("--color", required=True)
        if include_token:
            item.add_argument("--token", required=True)
        return item

    reserve = binding("reserve")
    reserve.add_argument("--grace", default="180")
    release = binding("release", include_token=True)
    release.add_argument("--force", action="store_true")
    status = binding("status", include_token=True)
    status.add_argument("--grace", default="180")
    register = binding("register", include_token=True)
    register.add_argument("--instance", required=True)
    register.add_argument("--pid", type=int, required=True)
    touch = binding("touch", include_token=True)
    touch.add_argument("--instance")
    claim = sub.add_parser("claim-color")
    claim.add_argument("--lock-dir", required=True)
    claim.add_argument("--color", required=True)
    claim.add_argument("--port", type=int, required=True)
    claim.add_argument("--owner", required=True)
    claim.add_argument("--grace", default="180")
    drop = sub.add_parser("release-color")
    drop.add_argument("--lock-dir", required=True)
    drop.add_argument("--color", required=True)
    drop.add_argument("--port", type=int, required=True)
    drop.add_argument("--owner", required=True)
    drop.add_argument("--force", action="store_true")
    discover = sub.add_parser("discover")
    discover.add_argument("--lock-dir", required=True)
    discover.add_argument("--owner", required=True)
    discover.add_argument("--palette", required=True)
    discover.add_argument("--grace", default="180")
    sub.add_parser("registry-path")
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        if args.command == "registry-path":
            print(runtime_registry_path())
        elif args.command == "reserve":
            record = reserve_port(
                args.port, args.owner, args.color_lock, args.color, args.grace
            )
            print(record["token"])
        elif args.command == "release":
            release_port(
                args.port, args.owner, args.color_lock, args.color, args.token,
                force=args.force,
            )
        elif args.command == "status":
            status, record = inspect_reservation(
                args.port, args.owner, args.color_lock, args.color, args.token,
                args.grace,
            )
            if record:
                print("{}\t{}\t{}".format(
                    status, record.get("pid") or "", record.get("instance") or ""
                ))
            else:
                print(status)
        elif args.command == "register":
            register_instance(
                args.port, args.owner, args.color_lock, args.color, args.token,
                args.instance, args.pid,
            )
        elif args.command == "touch":
            if not touch_reservation(
                args.port, args.owner, args.color_lock, args.color, args.token,
                args.instance,
            ):
                raise RegistryError("port reservation no longer belongs to this server")
        elif args.command == "claim-color":
            _lock, record = claim_color(
                args.lock_dir, args.color, args.port, args.owner, args.grace
            )
            print(record["token"])
        elif args.command == "release-color":
            release_color(
                args.lock_dir, args.color, args.port, args.owner,
                force=args.force,
            )
        elif args.command == "discover":
            palette = _parse_palette(args.palette)
            active = discover_active(
                palette, args.lock_dir, args.owner, args.grace
            )
            if active:
                entry, record = active[0]
                print("{}\t{}\t{}\t{}\t{}".format(
                    entry["emoji"], entry["slug"], record["port"],
                    record["pid"], record["instance"],
                ))
                return 0
            return 1
    except ReservationBusy as exc:
        print("runtime_registry.py: {}".format(exc), file=sys.stderr)
        return 3
    except (RegistryError, OSError, ValueError) as exc:
        print("runtime_registry.py: {}".format(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
