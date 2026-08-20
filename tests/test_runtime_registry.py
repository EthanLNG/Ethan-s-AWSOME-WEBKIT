import http.server
import json
import os
import socket
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "webkit" / "scripts"
import sys
sys.path.insert(0, str(SCRIPTS))
import runtime_registry  # noqa: E402

from runtime_registry import (  # noqa: E402
    RegistryError,
    ReservationBusy,
    claim_color,
    discover_active,
    read_color_lock,
    register_instance,
    release_color,
)


class _IdentityHandler(http.server.BaseHTTPRequestHandler):
    instance = ""

    def do_GET(self):
        body = b'{"error":"token required"}'
        self.send_response(403)
        self.send_header("X-WK-Preview-Instance", self.instance)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        return


class RuntimeRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.environment = mock.patch.dict(
            os.environ,
            {"WK_PORT_LOCKDIR": str(self.root / "global-ports")},
            clear=False,
        )
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    @staticmethod
    def free_port():
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        return port

    def test_touch_path_falls_back_when_nofollow_utime_is_unavailable(self):
        target = self.root / "legacy-utime.lock"
        target.mkdir()
        real_utime = os.utime
        calls = []

        def legacy_utime(path, times=None, **options):
            calls.append(dict(options))
            if "follow_symlinks" in options:
                raise NotImplementedError("nofollow utime unavailable")
            return real_utime(path, times)

        with mock.patch(
            "runtime_registry.os.utime", side_effect=legacy_utime
        ):
            self.assertTrue(runtime_registry.touch_path_nofollow(target))

        self.assertEqual(calls, [{"follow_symlinks": False}, {}])

    def test_binding_canonicalizes_equivalent_color_lock_paths(self):
        colors = self.root / "colors"
        (colors / "nested").mkdir(parents=True)
        direct = colors / "blue.lock"
        alias = colors / "nested" / ".." / "blue.lock"
        expected = runtime_registry._validate_binding(
            5311, "owner", str(direct), "blue"
        )
        actual = runtime_registry._validate_binding(
            5311, "owner", str(alias), "blue"
        )
        self.assertEqual(actual, expected)

    def test_binding_matches_equivalent_absolute_owner_paths(self):
        owners = self.root / "owners"
        (owners / "nested").mkdir(parents=True)
        direct = str(owners.absolute())
        alias = str((owners / "nested" / "..").absolute())
        record = {
            "version": 1,
            "port": 5311,
            "owner": alias,
            "colorLock": str((self.root / "colors" / "blue.lock").absolute()),
            "color": "blue",
            "token": "a" * 32,
            "instance": None,
            "pid": None,
        }
        self.assertTrue(
            runtime_registry._binding_matches(
                record,
                5311,
                direct,
                record["colorLock"],
                "blue",
                record["token"],
            )
        )

    def test_generated_reservation_tokens_are_cli_option_safe(self):
        port = self.free_port()
        color_lock = self.root / "colors" / "blue.lock"
        with mock.patch.object(
            runtime_registry.secrets,
            "token_urlsafe",
            return_value="-" + ("a" * 42),
        ):
            reservation = runtime_registry.reserve_port(
                port, "owner", str(color_lock.absolute()), "blue", 180
            )
        try:
            self.assertTrue(reservation["token"].startswith("wk_"))
            self.assertFalse(reservation["token"].startswith("-"))
        finally:
            runtime_registry.release_port(
                port,
                "owner",
                str(color_lock.absolute()),
                "blue",
                reservation["token"],
            )

    def test_atomic_port_claim_rolls_back_losing_project_color(self):
        port = self.free_port()
        barrier = threading.Barrier(2)
        results = []

        def contender(name):
            lock_dir = self.root / (name + "-colors")
            barrier.wait()
            try:
                lock, _record = claim_color(lock_dir, "blue", port, name, 180)
                results.append(("won", name, lock))
            except ReservationBusy:
                results.append(("busy", name, lock_dir / "blue.lock"))

        threads = [
            threading.Thread(target=contender, args=("owner-a",)),
            threading.Thread(target=contender, args=("owner-b",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

        winners = [item for item in results if item[0] == "won"]
        losers = [item for item in results if item[0] == "busy"]
        self.assertEqual(len(winners), 1, results)
        self.assertEqual(len(losers), 1, results)
        self.assertFalse(losers[0][2].exists())
        release_color(
            winners[0][2].parent, "blue", port, winners[0][1]
        )

    def test_active_discovery_requires_matching_private_server_identity(self):
        port = self.free_port()
        lock_dir = self.root / "colors"
        owner = str(self.root / "project")
        lock, reservation = claim_color(lock_dir, "blue", port, owner, 180)
        instance = "active-instance-1234567890"
        handler = type("IdentityHandler", (_IdentityHandler,), {"instance": instance})
        server = http.server.HTTPServer(("127.0.0.1", port), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            register_instance(
                port, owner, str(lock.absolute()), "blue",
                reservation["token"], instance, os.getpid(),
            )
            palette = [{"slug": "blue", "emoji": "🔵", "port": port}]
            active = discover_active(palette, lock_dir, owner, 180)
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0][1]["port"], port)
            self.assertEqual(active[0][1]["pid"], os.getpid())
            self.assertEqual(active[0][1]["instance"], instance)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            release_color(lock_dir, "blue", port, owner)

    def test_instance_identity_probe_is_fixed_to_ipv4_loopback(self):
        response = mock.Mock()
        response.getheader.return_value = "instance-token-123456"
        response.read.return_value = b"{}"
        connection = mock.Mock()
        connection.getresponse.return_value = response
        with mock.patch.object(
            runtime_registry.http.client,
            "HTTPConnection",
            return_value=connection,
        ) as connection_factory:
            self.assertTrue(
                runtime_registry._probe_instance(
                    5311, "instance-token-123456"
                )
            )
        connection_factory.assert_called_once_with(
            "127.0.0.1", 5311, timeout=0.35
        )

    def test_stale_free_reservation_recovers_without_deleting_old_color_owner(self):
        port = self.free_port()
        old_dir = self.root / "old-colors"
        old_lock, _record = claim_color(old_dir, "blue", port, "owner-a", 30)
        global_lock = self.root / "global-ports" / (str(port) + ".lock")
        os.utime(str(old_lock), (1, 1))
        os.utime(str(global_lock), (1, 1))

        new_dir = self.root / "new-colors"
        new_lock, _new_record = claim_color(
            new_dir, "green", port, "owner-b", 30
        )

        self.assertTrue(old_lock.is_dir())
        self.assertEqual(read_color_lock(old_lock)[0], "owner-a")
        self.assertTrue(new_lock.is_dir())
        release_color(new_dir, "green", port, "owner-b")
        release_color(old_dir, "blue", port, "owner-a")

    def test_release_uses_token_when_configured_port_changed(self):
        port = self.free_port()
        changed_port = self.free_port()
        lock_dir = self.root / "colors"
        claim_color(lock_dir, "blue", port, "owner-a", 180)

        release_color(lock_dir, "blue", changed_port, "owner-a")

        self.assertFalse((lock_dir / "blue.lock").exists())
        self.assertFalse(
            (self.root / "global-ports" / (str(port) + ".lock")).exists()
        )

    def test_release_never_deletes_another_owner(self):
        port = self.free_port()
        lock_dir = self.root / "colors"
        lock, _record = claim_color(lock_dir, "blue", port, "owner-a", 180)

        with self.assertRaises(ReservationBusy):
            release_color(lock_dir, "blue", port, "owner-b")

        self.assertTrue(lock.is_dir())
        self.assertTrue(
            (self.root / "global-ports" / (str(port) + ".lock")).is_dir()
        )
        release_color(lock_dir, "blue", port, "owner-a")

    def test_release_and_status_reject_unsafe_color_paths(self):
        port = self.free_port()
        lock_dir = self.root / "colors"
        lock, _record = claim_color(
            lock_dir, "blue", port, "owner-a", 180
        )
        outside = self.root / "escaped.lock"
        for color in ("../escaped", "/absolute", "x" * 65):
            with self.subTest(color=color):
                with self.assertRaisesRegex(
                    RegistryError, "safe palette slug"
                ):
                    release_color(
                        lock_dir, color, port, "owner-a", force=True
                    )
                with self.assertRaisesRegex(
                    RegistryError, "safe palette slug"
                ):
                    runtime_registry.color_session_status(
                        lock, port, color, "owner-a", 180
                    )
                self.assertTrue(lock.is_dir())
                self.assertFalse(outside.exists())
        release_color(lock_dir, "blue", port, "owner-a")

    def test_unconfigured_active_same_owner_lock_blocks_a_second_session(self):
        port = self.free_port()
        lock_dir = self.root / "colors"
        owner = str(self.root / "project")
        lock, reservation = claim_color(lock_dir, "blue", port, owner, 180)
        instance = "active-instance-abcdefghij"
        handler = type("IdentityHandler", (_IdentityHandler,), {"instance": instance})
        server = http.server.HTTPServer(("127.0.0.1", port), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            register_instance(
                port, owner, str(lock.absolute()), "blue",
                reservation["token"], instance, os.getpid(),
            )
            palette = [{"slug": "green", "emoji": "🟢", "port": self.free_port()}]
            with self.assertRaisesRegex(ReservationBusy, "unconfigured-active"):
                discover_active(palette, lock_dir, owner, 180)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            release_color(lock_dir, "blue", port, owner)

    def test_active_session_is_reused_after_lock_directory_and_port_change(self):
        old_port = self.free_port()
        new_port = self.free_port()
        old_dir = self.root / "old-colors"
        new_dir = self.root / "new-colors"
        owner = str(self.root / "project")
        lock, reservation = claim_color(old_dir, "blue", old_port, owner, 180)
        instance = "relocated-instance-123456"
        handler = type("IdentityHandler", (_IdentityHandler,), {"instance": instance})
        server = http.server.HTTPServer(("127.0.0.1", old_port), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            register_instance(
                old_port, owner, str(lock.absolute()), "blue",
                reservation["token"], instance, os.getpid(),
            )
            active = discover_active(
                [{"slug": "blue", "emoji": "🔵", "port": new_port}],
                new_dir, owner, 180,
            )
            self.assertEqual(active[0][1]["port"], old_port)
            release_color(new_dir, "blue", new_port, owner)
            self.assertFalse(lock.exists())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            if lock.exists():
                release_color(old_dir, "blue", old_port, owner)

    def test_active_old_namespace_blocks_when_the_color_is_no_longer_configured(self):
        old_port = self.free_port()
        new_port = self.free_port()
        old_dir = self.root / "old-colors"
        owner = str(self.root / "project")
        lock, reservation = claim_color(old_dir, "blue", old_port, owner, 180)
        instance = "unconfigured-instance-1234"
        handler = type("IdentityHandler", (_IdentityHandler,), {"instance": instance})
        server = http.server.HTTPServer(("127.0.0.1", old_port), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            register_instance(
                old_port, owner, str(lock.absolute()), "blue",
                reservation["token"], instance, os.getpid(),
            )
            with self.assertRaisesRegex(ReservationBusy, "unconfigured-active"):
                discover_active(
                    [{"slug": "green", "emoji": "🟢", "port": new_port}],
                    self.root / "new-colors", owner, 180,
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            release_color(old_dir, "blue", old_port, owner)

    def test_claim_grace_rejects_values_that_can_disable_the_startup_lease(self):
        port = self.free_port()
        for value in (0, 29, -1, float("nan"), True, "invalid", 86401):
            with self.subTest(value=value):
                with self.assertRaisesRegex(RegistryError, "30 through 86400"):
                    claim_color(
                        self.root / "invalid-grace", "blue", port,
                        "owner-a", value,
                    )

    def test_registry_json_rejects_unpaired_surrogates_and_accepts_pairs(self):
        for unsafe in (r'\ud800', r'\udfff'):
            with self.subTest(unsafe=unsafe):
                payload = (
                    '[{"slug":"blue","emoji":"%s","port":5311}]'
                    % unsafe
                )
                with self.assertRaisesRegex(
                    RegistryError, "unpaired Unicode surrogate"
                ):
                    runtime_registry._parse_palette(payload)

        self.assertEqual(
            runtime_registry._parse_palette(
                r'[{"slug":"blue","emoji":"\ud83d\ude00","port":5311}]'
            )[0]["emoji"],
            "😀",
        )

        port = self.free_port()
        lock_dir = self.root / "surrogate-colors"
        lock, reservation = claim_color(
            lock_dir, "blue", port, "owner-a", 180
        )
        record_path = (
            self.root / "global-ports" / (str(port) + ".lock") / "claim.json"
        )
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["owner"] = "\ud800"
        record_path.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaisesRegex(RegistryError, "unpaired Unicode surrogate"):
            runtime_registry._read_record(record_path.parent)

    def test_registry_metadata_writes_complete_after_short_writes(self):
        port = self.free_port()
        lock_dir = self.root / "colors"
        owner = "owner-a"
        real_write = os.write

        def short_write(descriptor, payload):
            data = bytes(payload)
            return real_write(descriptor, data[:max(1, min(7, len(data)))])

        with mock.patch("runtime_registry.os.write", side_effect=short_write):
            lock, reservation = claim_color(
                lock_dir, "blue", port, owner, 180
            )
            registered = register_instance(
                port, owner, str(lock.absolute()), "blue",
                reservation["token"], "short-write-instance-1234", os.getpid(),
            )

        self.assertEqual((lock / "owner").read_text(encoding="utf-8"), "owner-a\n")
        self.assertEqual(
            (lock / "reservation").read_text(encoding="utf-8").strip(),
            reservation["token"],
        )
        global_record = json.loads(
            (
                self.root / "global-ports" / (str(port) + ".lock") / "claim.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(global_record, registered)
        release_color(lock_dir, "blue", port, owner)

    def test_registration_temp_is_safe_during_concurrent_registry_validation(self):
        port = self.free_port()
        lock_dir = self.root / "colors"
        owner = "owner-a"
        lock, reservation = claim_color(lock_dir, "blue", port, owner, 180)
        temporary_ready = threading.Event()
        allow_replace = threading.Event()
        errors = []
        real_replace = os.replace

        def paused_replace(source, target):
            if Path(source).name.startswith(".wk-update-"):
                temporary_ready.set()
                if not allow_replace.wait(3):
                    raise RuntimeError("test replacement barrier timed out")
            return real_replace(source, target)

        def register():
            try:
                register_instance(
                    port, owner, str(lock.absolute()), "blue",
                    reservation["token"], "concurrent-instance-1234", os.getpid(),
                )
            except Exception as exc:
                errors.append(exc)

        with mock.patch("runtime_registry.os.replace", side_effect=paused_replace):
            thread = threading.Thread(target=register)
            thread.start()
            self.assertTrue(temporary_ready.wait(3))
            matches = __import__("runtime_registry").find_owner_reservations(owner)
            self.assertEqual(len(matches), 1)
            allow_replace.set()
            thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(list((self.root / "global-ports").glob(".wk-update-*")), [])
        release_color(lock_dir, "blue", port, owner)

    def test_register_is_serialized_against_release_and_reclaim(self):
        port = self.free_port()
        lock_dir = self.root / "colors"
        owner = "owner-old"
        lock, reservation = claim_color(lock_dir, "blue", port, owner, 180)
        replacement_ready = threading.Event()
        allow_replace = threading.Event()
        reclaim_started = threading.Event()
        reclaim_done = threading.Event()
        errors = []
        new_record = {}
        real_replace = os.replace

        def paused_replace(source, target):
            if Path(source).name.startswith(".wk-update-"):
                replacement_ready.set()
                if not allow_replace.wait(3):
                    raise RuntimeError("test replacement barrier timed out")
            return real_replace(source, target)

        def register():
            try:
                register_instance(
                    port, owner, str(lock.absolute()), "blue",
                    reservation["token"], "serialized-instance-1234", os.getpid(),
                )
            except Exception as exc:
                errors.append(exc)

        def reclaim():
            reclaim_started.set()
            try:
                runtime_registry.release_port(
                    port, owner, str(lock.absolute()), "blue", reservation["token"]
                )
                new_record.update(runtime_registry.reserve_port(
                    port,
                    "owner-new",
                    str((self.root / "new-colors" / "red.lock").absolute()),
                    "red",
                    180,
                ))
            except Exception as exc:
                errors.append(exc)
            finally:
                reclaim_done.set()

        with mock.patch("runtime_registry.os.replace", side_effect=paused_replace):
            register_thread = threading.Thread(target=register)
            reclaim_thread = threading.Thread(target=reclaim)
            register_thread.start()
            self.assertTrue(replacement_ready.wait(3))
            reclaim_thread.start()
            self.assertTrue(reclaim_started.wait(3))
            time.sleep(0.1)
            self.assertFalse(reclaim_done.is_set())
            allow_replace.set()
            register_thread.join(timeout=3)
            reclaim_thread.join(timeout=3)

        self.assertFalse(register_thread.is_alive())
        self.assertFalse(reclaim_thread.is_alive())
        self.assertEqual(errors, [])
        final, _unused = runtime_registry._read_record(
            self.root / "global-ports" / (str(port) + ".lock")
        )
        self.assertEqual(final["owner"], "owner-new")
        self.assertEqual(final["token"], new_record["token"])
        runtime_registry.release_port(
            port,
            "owner-new",
            str((self.root / "new-colors" / "red.lock").absolute()),
            "red",
            new_record["token"],
        )
        release_color(lock_dir, "blue", port, owner)

    def test_registry_mutation_lock_serializes_separate_processes(self):
        port = self.free_port()
        lock_dir = self.root / "colors"
        owner = "owner-a"
        lock, reservation = claim_color(lock_dir, "blue", port, owner, 180)
        registry = self.root / "global-ports"
        command = [
            sys.executable,
            "-B",
            str(SCRIPTS / "runtime_registry.py"),
            "touch",
            "--port",
            str(port),
            "--owner",
            owner,
            "--color-lock",
            str(lock.absolute()),
            "--color",
            "blue",
            "--token",
            reservation["token"],
        ]
        environment = os.environ.copy()
        environment["WK_PORT_LOCKDIR"] = str(registry)
        process = None
        try:
            with runtime_registry._registry_mutation_lock(registry):
                process = subprocess.Popen(
                    command,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                time.sleep(0.25)
                if process.poll() is not None:
                    stdout, stderr = process.communicate(timeout=5)
                    self.fail(
                        "registry subprocess exited before waiting for the lock: {}"
                        .format(stdout + stderr)
                    )
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, stdout + stderr)
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        release_color(lock_dir, "blue", port, owner)

    def test_incomplete_claim_recovery_waits_for_registry_mutation_lock(self):
        port = self.free_port()
        registry = self.root / "global-ports"
        registry.mkdir(mode=0o700)
        incomplete = registry / (str(port) + ".lock")
        incomplete.mkdir(mode=0o700)
        os.utime(str(incomplete), (1, 1))
        prevalidation_done = threading.Event()
        reservation_done = threading.Event()
        errors = []
        result = {}
        real_validate = runtime_registry._validate_registry

        def observed_validate(path, create=False, recover=False):
            value = real_validate(path, create=create, recover=recover)
            if not recover and threading.current_thread().name == "reclaimer":
                prevalidation_done.set()
            return value

        def reserve():
            try:
                result.update(runtime_registry.reserve_port(
                    port,
                    "owner-new",
                    str((self.root / "colors" / "blue.lock").absolute()),
                    "blue",
                    180,
                ))
            except Exception as error:
                errors.append(error)
            finally:
                reservation_done.set()

        with mock.patch(
            "runtime_registry._validate_registry", side_effect=observed_validate
        ):
            with runtime_registry._registry_mutation_lock(registry):
                thread = threading.Thread(target=reserve, name="reclaimer")
                thread.start()
                self.assertTrue(prevalidation_done.wait(3))
                self.assertTrue(incomplete.is_dir())
                self.assertFalse(reservation_done.is_set())
            thread.join(timeout=3)

        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(result["owner"], "owner-new")
        runtime_registry.release_port(
            port,
            "owner-new",
            str((self.root / "colors" / "blue.lock").absolute()),
            "blue",
            result["token"],
        )

    def test_registry_uses_canonical_parent_after_symlink_retarget(self):
        first_parent = self.root / "first-parent"
        second_parent = self.root / "second-parent"
        first_parent.mkdir()
        second_parent.mkdir()
        parent_link = self.root / "parent-link"
        try:
            parent_link.symlink_to(first_parent, target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            self.skipTest("symbolic links unavailable: {}".format(error))
        configured = parent_link / "global-ports"
        port = self.free_port()
        color_lock = str((self.root / "colors" / "blue.lock").absolute())
        retargeted = {"done": False}
        real_validate = runtime_registry._validate_registry

        def retarget_after_validation(path, create=False, recover=False):
            result = real_validate(path, create=create, recover=recover)
            if not retargeted["done"] and Path(path) == configured:
                retargeted["done"] = True
                parent_link.unlink()
                parent_link.symlink_to(second_parent, target_is_directory=True)
            return result

        with mock.patch.dict(
            os.environ, {"WK_PORT_LOCKDIR": str(configured)}, clear=False
        ), mock.patch(
            "runtime_registry._validate_registry",
            side_effect=retarget_after_validation,
        ):
            reservation = runtime_registry.reserve_port(
                port, "owner-a", color_lock, "blue", 180
            )

        canonical = first_parent.resolve() / "global-ports"
        self.assertTrue((canonical / (str(port) + ".lock")).is_dir())
        self.assertFalse((second_parent / "global-ports").exists())

        parent_link.unlink()
        parent_link.symlink_to(first_parent, target_is_directory=True)
        with mock.patch.dict(
            os.environ, {"WK_PORT_LOCKDIR": str(configured)}, clear=False
        ):
            runtime_registry.release_port(
                port,
                "owner-a",
                color_lock,
                "blue",
                reservation["token"],
            )

    def test_restart_recovers_aged_incomplete_port_and_color_writes(self):
        owner = "owner-a"

        for partial_record in (False, True):
            with self.subTest(port_partial_record=partial_record):
                port = self.free_port()
                global_root = self.root / (
                    "crash-ports-file" if partial_record else "crash-ports-empty"
                )
                with mock.patch.dict(
                    os.environ, {"WK_PORT_LOCKDIR": str(global_root)}, clear=False
                ):
                    global_root.mkdir(mode=0o700)
                    crashed = global_root / (str(port) + ".lock")
                    crashed.mkdir(mode=0o700)
                    if partial_record:
                        record = crashed / "claim.json"
                        record.write_text("{", encoding="utf-8")
                        if os.name == "posix":
                            record.chmod(0o600)
                    os.utime(crashed, (1, 1))
                    color_dir = self.root / ("port-color-" + str(port))
                    claim_color(color_dir, "blue", port, owner, 30)
                    release_color(color_dir, "blue", port, owner)

        partial_colors = (
            (None, None),
            ("owner", ""),
            ("reservation", "x"),
            (".wk-write-owner-crash123", "owner"),
            (".wk-write-reservation-crash123", "token"),
        )
        for partial_name, partial_content in partial_colors:
            with self.subTest(color_partial=partial_name):
                port = self.free_port()
                color_dir = self.root / ("crash-color-" + str(port))
                color_dir.mkdir(mode=0o700)
                crashed = color_dir / "blue.lock"
                crashed.mkdir(mode=0o700)
                if partial_name is not None:
                    partial = crashed / partial_name
                    partial.write_text(partial_content, encoding="utf-8")
                    if os.name == "posix":
                        partial.chmod(0o600)
                os.utime(crashed, (1, 1))
                claim_color(color_dir, "blue", port, owner, 30)
                release_color(color_dir, "blue", port, owner)

        port = self.free_port()
        color_dir = self.root / "crash-after-port"
        lock, _reservation = claim_color(color_dir, "blue", port, owner, 30)
        (lock / "reservation").write_text("partial", encoding="utf-8")
        if os.name == "posix":
            (lock / "reservation").chmod(0o600)
        os.utime(lock, (1, 1))
        os.utime(self.root / "global-ports" / (str(port) + ".lock"), (1, 1))
        claim_color(color_dir, "blue", port, owner, 30)
        release_color(color_dir, "blue", port, owner)

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits required")
    def test_claim_records_are_private(self):
        port = self.free_port()
        lock_dir = self.root / "colors"
        lock, _record = claim_color(lock_dir, "blue", port, "owner-a", 180)
        global_root = self.root / "global-ports"
        global_lock = global_root / (str(port) + ".lock")
        self.assertEqual(stat.S_IMODE(lock_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((lock / "owner").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((lock / "reservation").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(global_root.stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE((global_root / ".wk-registry.lock").stat().st_mode),
            0o600,
        )
        self.assertEqual(stat.S_IMODE(global_lock.stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE((global_lock / "claim.json").stat().st_mode), 0o600
        )
        release_color(lock_dir, "blue", port, "owner-a")

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits required")
    def test_owned_legacy_color_registry_is_migrated_to_private_modes(self):
        lock_dir = self.root / "legacy-colors"
        lock_dir.mkdir(mode=0o700)
        lock = lock_dir / "blue.lock"
        lock.mkdir(mode=0o700)
        owner_path = lock / "owner"
        owner_path.write_text("owner-a\n", encoding="utf-8")
        lock_dir.chmod(0o1777)
        lock.chmod(0o755)
        owner_path.chmod(0o644)

        validated = runtime_registry._validate_color_registry(
            lock_dir, create=False
        )

        self.assertEqual(validated, lock_dir)
        self.assertEqual(read_color_lock(lock)[0], "owner-a")
        self.assertEqual(stat.S_IMODE(lock_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(owner_path.stat().st_mode), 0o600)

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits required")
    def test_legacy_color_migration_never_follows_a_registry_symlink(self):
        target = self.root / "legacy-color-target"
        target.mkdir(mode=0o755)
        link = self.root / "legacy-color-link"
        link.symlink_to(target, target_is_directory=True)

        with self.assertRaisesRegex(RegistryError, "symbolic link"):
            runtime_registry._validate_color_registry(link, create=False)

        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)

    def test_symlink_or_unexpected_registry_content_fails_closed(self):
        target = self.root / "target"
        target.mkdir(mode=0o700)
        link = self.root / "linked-ports"
        try:
            link.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest("symbolic links unavailable: {}".format(exc))
        port = self.free_port()
        with mock.patch.dict(
            os.environ, {"WK_PORT_LOCKDIR": str(link)}, clear=False
        ):
            with self.assertRaises(RegistryError):
                claim_color(
                    self.root / "colors-a", "blue", port, "owner-a", 180
                )

        global_root = self.root / "global-ports"
        global_root.mkdir(mode=0o700)
        (global_root / "unexpected").write_text("keep", encoding="utf-8")
        with self.assertRaises(RegistryError):
            claim_color(
                self.root / "colors-b", "blue", port, "owner-b", 180
            )
        self.assertEqual(
            (global_root / "unexpected").read_text(encoding="utf-8"), "keep"
        )


if __name__ == "__main__":
    unittest.main()
