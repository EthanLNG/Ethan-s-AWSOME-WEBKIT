import base64
import contextlib
import http.client
import importlib.util
import io
import json
import os
import plistlib
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock


CONTROL_DIR = Path(__file__).resolve().parents[1]
KIT_ROOT = CONTROL_DIR.parent
sys.path.insert(0, str(CONTROL_DIR))

import launch  # noqa: E402
from control_center import ControlCenterError  # noqa: E402
from server import ControlCenterHTTPServer, Handler as ControlCenterHandler  # noqa: E402


def load_install_shortcut():
    spec = importlib.util.spec_from_file_location(
        "install_shortcut", str(CONTROL_DIR / "install-shortcut.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


install_shortcut = load_install_shortcut()


class LauncherTests(unittest.TestCase):
    def _run_shortcut_installer(self, system, desktop):
        output = io.StringIO()
        with mock.patch.object(
            install_shortcut.platform, "system", return_value=system
        ), mock.patch.object(
            install_shortcut, "desktop_directory", return_value=desktop
        ), contextlib.redirect_stdout(output):
            self.assertEqual(install_shortcut.main(), 0)
        return output.getvalue()

    def _assert_conflicting_shortcut_is_preserved(self, system, filename):
        with tempfile.TemporaryDirectory() as raw:
            desktop = Path(raw) / "Desktop"
            desktop.mkdir()
            preferred = desktop / filename
            preferred.write_bytes(b"user-owned shortcut\n")

            output = self._run_shortcut_installer(system, desktop)

            numbered = preferred.with_name(
                "{} (2){}".format(preferred.stem, preferred.suffix)
            )
            self.assertEqual(preferred.read_bytes(), b"user-owned shortcut\n")
            self.assertTrue(numbered.is_file())
            self.assertNotEqual(numbered.read_bytes(), preferred.read_bytes())
            self.assertIn(str(numbered), output)

            repeated_output = self._run_shortcut_installer(system, desktop)
            self.assertIn("Shortcut already installed:", repeated_output)
            self.assertIn(str(numbered), repeated_output)
            self.assertEqual(sorted(path.name for path in desktop.iterdir()), sorted([
                preferred.name,
                numbered.name,
            ]))

    def test_state_root_rejects_symlink_and_non_directory_targets(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            victim = root / "victim"
            victim.mkdir()
            if os.name != "nt":
                victim.chmod(0o755)
            linked_state = root / "linked-state"
            try:
                linked_state.symlink_to(victim, target_is_directory=True)
            except (OSError, NotImplementedError) as error:
                self.skipTest("symbolic links unavailable: {}".format(error))

            with mock.patch.object(launch, "STATE_DIR", linked_state):
                with self.assertRaisesRegex(RuntimeError, "real directory"):
                    launch.secure_state_dir()
            self.assertEqual(list(victim.iterdir()), [])
            if os.name != "nt":
                self.assertEqual(victim.stat().st_mode & 0o777, 0o755)

            state_file = root / "state-file"
            state_file.write_text("keep", encoding="utf-8")
            with mock.patch.object(launch, "STATE_DIR", state_file):
                with self.assertRaisesRegex(RuntimeError, "real directory"):
                    launch.secure_state_dir()
            self.assertEqual(state_file.read_text(encoding="utf-8"), "keep")

    def test_state_root_rejects_relative_and_filesystem_root_before_chmod(self):
        with tempfile.TemporaryDirectory() as raw:
            project = Path(raw) / "project"
            project.mkdir(mode=0o755)
            previous = os.getcwd()
            try:
                os.chdir(str(project))
                with self.assertRaisesRegex(RuntimeError, "absolute path"):
                    launch.secure_state_dir(Path("."))
            finally:
                os.chdir(previous)
            if os.name == "posix":
                self.assertEqual(project.stat().st_mode & 0o777, 0o755)

        filesystem_root = Path(Path.cwd().anchor)
        with mock.patch.object(launch.os, "chmod") as chmod:
            with self.assertRaisesRegex(RuntimeError, "filesystem root"):
                launch.secure_state_dir(filesystem_root)
        chmod.assert_not_called()

    def test_state_root_rejects_unrelated_nonempty_directory_before_chmod(self):
        with tempfile.TemporaryDirectory() as raw:
            unrelated = Path(raw) / "unrelated"
            unrelated.mkdir(mode=0o755)
            sentinel = unrelated / "user-document.txt"
            sentinel.write_text("keep", encoding="utf-8")

            with mock.patch.object(launch.os, "chmod") as chmod:
                with self.assertRaisesRegex(RuntimeError, "unexpected entry"):
                    launch.secure_state_dir(unrelated)

            chmod.assert_not_called()
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
            if os.name == "posix":
                self.assertEqual(unrelated.stat().st_mode & 0o777, 0o755)

    def test_state_root_canonicalizes_symlinked_parent_before_use(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            first_parent = root / "first"
            second_parent = root / "second"
            first_parent.mkdir()
            second_parent.mkdir()
            parent_link = root / "parent-link"
            try:
                parent_link.symlink_to(first_parent, target_is_directory=True)
            except (OSError, NotImplementedError) as error:
                self.skipTest("symbolic links unavailable: {}".format(error))

            state_dir = launch.secure_state_dir(parent_link / "state")
            self.assertEqual(state_dir, first_parent.resolve() / "state")
            parent_link.unlink()
            parent_link.symlink_to(second_parent, target_is_directory=True)

            launch.write_runtime({"pid": 1, "port": 8790, "token": "safe"}, state_dir)

            self.assertTrue((first_parent / "state" / launch.RUNTIME_FILE_NAME).is_file())
            self.assertFalse((second_parent / "state").exists())

    @unittest.skipUnless(os.name == "posix", "POSIX ownership is required")
    def test_state_root_rejects_foreign_owner_before_chmod(self):
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            state_dir.mkdir(mode=0o755)
            actual = os.lstat(str(state_dir))
            foreign = types.SimpleNamespace(
                st_mode=actual.st_mode,
                st_dev=actual.st_dev,
                st_ino=actual.st_ino,
                st_uid=os.getuid() + 1,
            )
            with mock.patch.object(
                launch.os, "lstat", return_value=foreign
            ), mock.patch.object(launch.os, "chmod") as chmod:
                with self.assertRaisesRegex(RuntimeError, "owned by the current user"):
                    launch.secure_state_dir(state_dir)
            chmod.assert_not_called()

    def test_runtime_replaces_symlink_atomically_and_log_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            state_dir.mkdir(mode=0o700)
            runtime_file = state_dir / launch.RUNTIME_FILE_NAME
            log_file = state_dir / "control-center.log"
            runtime_victim = Path(raw) / "runtime-victim.txt"
            log_victim = Path(raw) / "log-victim.txt"
            runtime_victim.write_text("runtime sentinel", encoding="utf-8")
            log_victim.write_text("log sentinel", encoding="utf-8")
            try:
                runtime_file.symlink_to(runtime_victim)
                log_file.symlink_to(log_victim)
            except (OSError, NotImplementedError) as error:
                self.skipTest("symbolic links unavailable: {}".format(error))

            with mock.patch.object(launch, "STATE_DIR", state_dir), mock.patch.object(
                launch, "RUNTIME_FILE", runtime_file
            ), mock.patch.object(launch, "LOG_FILE", log_file):
                launch.write_runtime({"pid": 1, "port": 8790, "token": "safe"})
                with self.assertRaisesRegex(RuntimeError, "regular file"):
                    launch.private_log_file()

            self.assertFalse(runtime_file.is_symlink())
            self.assertEqual(
                json.loads(runtime_file.read_text(encoding="utf-8"))["token"], "safe"
            )
            self.assertEqual(runtime_victim.read_text(encoding="utf-8"), "runtime sentinel")
            self.assertEqual(log_victim.read_text(encoding="utf-8"), "log sentinel")
            if os.name != "nt":
                self.assertEqual(runtime_file.stat().st_mode & 0o777, 0o600)

    def test_startup_log_is_truncated_before_a_new_server_spawn(self):
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            state_dir.mkdir(mode=0o700)
            log_file = state_dir / "control-center.log"
            log_file.write_bytes(b"old-log-line\n" * 200000)
            if os.name != "nt":
                log_file.chmod(0o600)

            with mock.patch.object(
                launch, "STATE_DIR", state_dir
            ), mock.patch.object(launch, "LOG_FILE", log_file):
                with launch.private_log_file() as handle:
                    self.assertEqual(log_file.stat().st_size, 0)
                    handle.write("new startup\n")

            self.assertEqual(
                log_file.read_text(encoding="utf-8"), "new startup\n"
            )

    def test_open_runtime_prints_url_when_browser_launch_fails(self):
        runtime = {"port": 8790, "token": "token with spaces"}
        output = io.StringIO()
        failed = mock.Mock(returncode=1)
        with mock.patch.object(launch.platform, "system", return_value="Darwin"), mock.patch.object(
            launch.subprocess, "run", return_value=failed
        ), mock.patch.object(
            launch.webbrowser, "open", return_value=False
        ), contextlib.redirect_stdout(output):
            launch.open_runtime(runtime)

        self.assertIn(
            "http://127.0.0.1:8790/?token=token%20with%20spaces",
            output.getvalue(),
        )

    def test_linux_installer_preserves_symlink_and_uses_numbered_shortcut(self):
        with tempfile.TemporaryDirectory() as raw:
            desktop = Path(raw) / "Desktop"
            desktop.mkdir()
            victim = Path(raw) / "victim.txt"
            victim.write_text("sentinel", encoding="utf-8")
            shortcut = desktop / "awesome-webkit.desktop"
            try:
                shortcut.symlink_to(victim)
            except (OSError, NotImplementedError) as error:
                self.skipTest("symbolic links unavailable: {}".format(error))

            output = self._run_shortcut_installer("Linux", desktop)

            self.assertEqual(victim.read_text(encoding="utf-8"), "sentinel")
            self.assertTrue(shortcut.is_symlink())
            numbered = desktop / "awesome-webkit (2).desktop"
            self.assertIn("[Desktop Entry]", numbered.read_text(encoding="utf-8"))
            self.assertIn(str(numbered), output)

    def test_macos_installer_preserves_differing_existing_shortcut(self):
        with tempfile.TemporaryDirectory() as raw:
            desktop = Path(raw) / "Desktop"
            desktop.mkdir()
            preferred = desktop / "AWESOME WEBKIT.app"
            preferred.write_bytes(b"user-owned shortcut\n")

            output = self._run_shortcut_installer("Darwin", desktop)

            numbered = desktop / "AWESOME WEBKIT (2).app"
            self.assertEqual(preferred.read_bytes(), b"user-owned shortcut\n")
            self.assertTrue(numbered.is_dir())
            self.assertTrue((numbered / "Contents/Resources/AppIcon.icns").is_file())
            self.assertIn(str(numbered), output)

            repeated_output = self._run_shortcut_installer("Darwin", desktop)
            self.assertIn("Shortcut already installed:", repeated_output)
            self.assertEqual(
                sorted(path.name for path in desktop.iterdir()),
                ["AWESOME WEBKIT (2).app", "AWESOME WEBKIT.app"],
            )

    def test_windows_installer_preserves_differing_existing_shortcut(self):
        self._assert_conflicting_shortcut_is_preserved(
            "Windows", "AWESOME WEBKIT.cmd"
        )

    def test_linux_installer_preserves_differing_existing_shortcut(self):
        self._assert_conflicting_shortcut_is_preserved(
            "Linux", "awesome-webkit.desktop"
        )

    def test_shortcut_installer_reuses_identical_content_on_every_platform(self):
        cases = (
            ("Darwin", "AWESOME WEBKIT.app"),
            ("Windows", "AWESOME WEBKIT.cmd"),
            ("Linux", "awesome-webkit.desktop"),
        )
        for system, filename in cases:
            with self.subTest(system=system), tempfile.TemporaryDirectory() as raw:
                desktop = Path(raw) / "Desktop"
                desktop.mkdir()
                writer_name = (
                    "write_macos_app_exclusive"
                    if system == "Darwin" else "write_shortcut_exclusive"
                )
                writer = getattr(install_shortcut, writer_name)
                with mock.patch.object(
                    install_shortcut, writer_name, wraps=writer,
                ) as write:
                    self._run_shortcut_installer(system, desktop)
                    output = self._run_shortcut_installer(system, desktop)

                self.assertEqual(write.call_count, 1)
                self.assertEqual([path.name for path in desktop.iterdir()], [filename])
                self.assertIn("Shortcut already installed:", output)

    @unittest.skipUnless(os.name == "posix", "Executable shortcut modes are POSIX-only")
    def test_shortcut_installer_does_not_reuse_non_executable_content(self):
        for system, filename in (("Linux", "awesome-webkit.desktop"),):
            with self.subTest(system=system), tempfile.TemporaryDirectory() as raw:
                desktop = Path(raw) / "Desktop"
                desktop.mkdir()
                self._run_shortcut_installer(system, desktop)
                preferred = desktop / filename
                preferred.chmod(0o644)

                output = self._run_shortcut_installer(system, desktop)

                numbered = preferred.with_name(
                    "{} (2){}".format(preferred.stem, preferred.suffix)
                )
                self.assertFalse(preferred.stat().st_mode & stat.S_IXUSR)
                self.assertTrue(numbered.stat().st_mode & stat.S_IXUSR)
                self.assertIn(str(numbered), output)

    @unittest.skipUnless(os.name == "posix", "Executable shortcut modes are POSIX-only")
    def test_macos_installer_does_not_reuse_app_with_non_executable_launcher(self):
        with tempfile.TemporaryDirectory() as raw:
            desktop = Path(raw) / "Desktop"
            desktop.mkdir()
            self._run_shortcut_installer("Darwin", desktop)
            preferred = desktop / "AWESOME WEBKIT.app"
            launcher = preferred / "Contents/MacOS/awesome-webkit"
            launcher.chmod(0o644)

            output = self._run_shortcut_installer("Darwin", desktop)

            numbered = desktop / "AWESOME WEBKIT (2).app"
            self.assertFalse(launcher.stat().st_mode & stat.S_IXUSR)
            self.assertTrue(
                (numbered / "Contents/MacOS/awesome-webkit").stat().st_mode
                & stat.S_IXUSR
            )
            self.assertIn(str(numbered), output)

    def test_macos_app_contains_native_icon_and_plist(self):
        with tempfile.TemporaryDirectory() as raw:
            desktop = Path(raw) / "Desktop"
            desktop.mkdir()
            self._run_shortcut_installer("Darwin", desktop)
            app = desktop / "AWESOME WEBKIT.app"
            info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
            icon = (app / "Contents/Resources/AppIcon.icns").read_bytes()

            self.assertEqual(info["CFBundleIconFile"], "AppIcon.icns")
            self.assertEqual(info["CFBundleExecutable"], "awesome-webkit")
            self.assertEqual(icon[:4], b"icns")
            self.assertEqual(struct.unpack(">I", icon[4:8])[0], len(icon))
            self.assertIn(b"\x89PNG\r\n\x1a\n", icon)
            self.assertEqual(
                install_shortcut.brand_icon_png(128)[:8], b"\x89PNG\r\n\x1a\n"
            )

    def test_shortcut_exclusive_write_refuses_a_racing_existing_file(self):
        with tempfile.TemporaryDirectory() as raw:
            desktop = Path(raw)
            preferred = desktop / "awesome-webkit.desktop"
            data = b"generated shortcut\n"
            real_writer = install_shortcut.write_shortcut_exclusive
            raced = {"done": False}

            def race_once(shortcut, content, mode):
                if not raced["done"]:
                    raced["done"] = True
                    shortcut.write_bytes(b"racing user content\n")
                return real_writer(shortcut, content, mode)

            with mock.patch.object(
                install_shortcut,
                "write_shortcut_exclusive",
                side_effect=race_once,
            ):
                shortcut, created = install_shortcut.install_without_overwrite(
                    preferred, data, 0o755
                )

            self.assertTrue(created)
            self.assertEqual(preferred.read_bytes(), b"racing user content\n")
            self.assertEqual(shortcut, desktop / "awesome-webkit (2).desktop")
            self.assertEqual(shortcut.read_bytes(), data)

    def test_shortcut_concurrent_identical_create_is_reused(self):
        with tempfile.TemporaryDirectory() as raw:
            desktop = Path(raw)
            preferred = desktop / "awesome-webkit.desktop"
            data = b"generated shortcut\n"
            real_writer = install_shortcut.write_shortcut_exclusive
            raced = {"done": False}

            def race_once(shortcut, content, mode):
                if not raced["done"]:
                    raced["done"] = True
                    real_writer(shortcut, content, mode)
                    raise FileExistsError("created by a concurrent installer")
                return real_writer(shortcut, content, mode)

            with mock.patch.object(
                install_shortcut,
                "write_shortcut_exclusive",
                side_effect=race_once,
            ):
                shortcut, created = install_shortcut.install_without_overwrite(
                    preferred, data, 0o755
                )

            self.assertFalse(created)
            self.assertEqual(shortcut, preferred)
            self.assertEqual([path.name for path in desktop.iterdir()], [preferred.name])

    def test_shortcut_failed_write_removes_only_its_partial_file(self):
        with tempfile.TemporaryDirectory() as raw:
            shortcut = Path(raw) / "awesome-webkit.desktop"
            with mock.patch.object(
                install_shortcut.os, "fsync", side_effect=OSError("disk full")
            ):
                with self.assertRaisesRegex(OSError, "disk full"):
                    install_shortcut.write_shortcut_exclusive(
                        shortcut, b"partial shortcut\n", 0o755
                    )
            self.assertFalse(shortcut.exists())

    @unittest.skipUnless(os.name == "posix", "Executable shortcut modes are POSIX-only")
    def test_shortcut_failed_executable_mode_does_not_report_installation(self):
        with tempfile.TemporaryDirectory() as raw:
            shortcut = Path(raw) / "awesome-webkit.desktop"
            with mock.patch.object(
                install_shortcut.os,
                "fchmod",
                side_effect=OSError("read-only permissions"),
            ):
                with self.assertRaisesRegex(RuntimeError, "made executable"):
                    install_shortcut.write_shortcut_exclusive(
                        shortcut, b"generated shortcut\n", 0o755
                    )

            self.assertFalse(shortcut.exists())

    def test_posix_launcher_declares_python_37_or_newer_gate(self):
        launcher = KIT_ROOT / "launch-control-center.sh"
        text = launcher.read_text(encoding="utf-8")
        self.assertIn("command -v python3", text)
        self.assertIn("sys.version_info[0] != 3", text)
        self.assertIn("sys.version_info[1] < 7", text)
        self.assertIn("requires Python 3.7 or newer", text)

    @unittest.skipIf(os.name == "nt", "POSIX launcher execution requires /bin/sh")
    def test_posix_launcher_reports_missing_python(self):
        launcher = KIT_ROOT / "launch-control-center.sh"
        with tempfile.TemporaryDirectory() as raw:
            fake_dirname = Path(raw) / "dirname"
            fake_dirname.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$WK_TEST_LAUNCHER_DIR\"\n",
                encoding="utf-8",
            )
            fake_dirname.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = raw
            env["WK_TEST_LAUNCHER_DIR"] = str(KIT_ROOT)
            result = subprocess.run(
                ["/bin/sh", str(launcher)],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 1)
        self.assertIn("requires Python 3.7 or newer", result.stderr)

    @unittest.skipIf(os.name == "nt", "POSIX launcher execution requires /bin/sh")
    def test_posix_launcher_rejects_unsupported_python(self):
        launcher = KIT_ROOT / "launch-control-center.sh"
        with tempfile.TemporaryDirectory() as raw:
            fake_python = Path(raw) / "python3"
            fake_python.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = \"-c\" ]; then exit 1; fi\n"
                "exit 99\n",
                encoding="utf-8",
            )
            fake_python.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = raw + os.pathsep + env.get("PATH", "")
            result = subprocess.run(
                ["/bin/sh", str(launcher)],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 1)
        self.assertIn("requires Python 3.7 or newer", result.stderr)

    @unittest.skipIf(os.name == "nt", "POSIX launcher execution requires /bin/sh")
    def test_posix_launcher_uses_validated_interpreter_and_forwards_arguments(self):
        launcher = KIT_ROOT / "launch-control-center.sh"
        with tempfile.TemporaryDirectory() as raw:
            fake_python = Path(raw) / "python3"
            fake_python.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = \"-c\" ]; then exit 0; fi\n"
                "printf '%s\\n' \"$@\"\n"
                "exit 23\n",
                encoding="utf-8",
            )
            fake_python.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = raw + os.pathsep + env.get("PATH", "")
            result = subprocess.run(
                ["/bin/sh", str(launcher), "hello world"],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 23)
        self.assertEqual(
            result.stdout.splitlines(),
            [str(CONTROL_DIR / "launch.py"), "hello world"],
        )

    def test_startup_lock_serializes_callers_and_is_private(self):
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            holder_acquired = threading.Event()
            release_holder = threading.Event()
            contender_tried_lock = threading.Event()
            contender_acquired = threading.Event()
            failures = []
            real_try_lock = launch._try_startup_file_lock

            def observed_try_lock(handle):
                acquired = real_try_lock(handle)
                if threading.current_thread().name == "startup-lock-contender":
                    contender_tried_lock.set()
                return acquired

            def holder():
                try:
                    with launch.startup_lock(timeout=2) as path:
                        holder_acquired.set()
                        self.assertEqual(
                            path,
                            state_dir.resolve() / launch.STARTUP_LOCK_NAME,
                        )
                        if os.name != "nt":
                            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                        release_holder.wait(timeout=2)
                except BaseException as error:
                    failures.append(error)

            def contender():
                try:
                    with launch.startup_lock(timeout=2):
                        contender_acquired.set()
                except BaseException as error:
                    failures.append(error)

            with mock.patch.object(launch, "STATE_DIR", state_dir), mock.patch.object(
                launch, "_try_startup_file_lock", side_effect=observed_try_lock
            ):
                holder_thread = threading.Thread(target=holder)
                contender_thread = threading.Thread(
                    target=contender, name="startup-lock-contender"
                )
                holder_thread.start()
                self.assertTrue(holder_acquired.wait(timeout=2))
                contender_thread.start()
                self.assertTrue(contender_tried_lock.wait(timeout=2))
                self.assertFalse(contender_acquired.is_set())
                release_holder.set()
                holder_thread.join(timeout=2)
                contender_thread.join(timeout=2)

            self.assertFalse(holder_thread.is_alive())
            self.assertFalse(contender_thread.is_alive())
            self.assertEqual(failures, [])
            self.assertTrue(contender_acquired.is_set())

    def test_lifetime_instance_lock_refuses_a_second_owner(self):
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            first = launch.acquire_instance_lock(state_dir)
            self.assertIsNotNone(first)
            try:
                self.assertIsNone(launch.acquire_instance_lock(state_dir))
                self.assertTrue(launch.instance_lock_held(state_dir))
                lock_path = state_dir / launch.INSTANCE_LOCK_NAME
                if os.name != "nt":
                    self.assertEqual(lock_path.stat().st_mode & 0o777, 0o600)
            finally:
                launch.release_instance_lock(first)
            second = launch.acquire_instance_lock(state_dir)
            self.assertIsNotNone(second)
            launch.release_instance_lock(second)

    def test_missing_or_corrupt_runtime_cannot_spawn_over_a_live_instance(self):
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt), tempfile.TemporaryDirectory() as raw:
                state_dir = Path(raw) / "state"
                state_dir.mkdir(mode=0o700)
                runtime_file = state_dir / launch.RUNTIME_FILE_NAME
                state_file = state_dir / "state.json"
                state_file.write_text('{"sentinel":"unchanged"}\n', encoding="utf-8")
                if corrupt:
                    runtime_file.write_text("not json\n", encoding="utf-8")
                owner = launch.acquire_instance_lock(state_dir)
                self.assertIsNotNone(owner)
                before = {
                    path.name: path.read_bytes()
                    for path in state_dir.iterdir()
                    if (
                        path.is_file()
                        and path.name != launch.INSTANCE_LOCK_NAME
                    )
                }
                try:
                    with mock.patch.object(launch, "STATE_DIR", state_dir), mock.patch.object(
                        launch, "RUNTIME_FILE", runtime_file
                    ), mock.patch.object(
                        launch, "LOG_FILE", state_dir / "control-center.log"
                    ), mock.patch.object(
                        launch.subprocess, "Popen"
                    ) as popen, mock.patch.object(
                        launch, "open_runtime"
                    ) as opened:
                        with self.assertRaisesRegex(RuntimeError, "already owns"):
                            launch.main()
                    popen.assert_not_called()
                    opened.assert_not_called()
                    self.assertEqual(state_file.read_text(encoding="utf-8"), '{"sentinel":"unchanged"}\n')
                    if corrupt:
                        self.assertEqual(runtime_file.read_text(encoding="utf-8"), "not json\n")
                    self.assertTrue(
                        launch.instance_lock_held(state_dir)
                    )
                finally:
                    launch.release_instance_lock(owner)

    def test_waiting_launcher_rechecks_runtime_before_spawning(self):
        runtime = {"pid": 123, "port": 8790, "token": "already-running"}
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            runtime_file = state_dir / launch.RUNTIME_FILE_NAME

            @contextlib.contextmanager
            def completed_by_first_launcher(state_dir=None):
                self.assertEqual(state_dir, Path(raw).resolve())
                runtime_file.write_text(json.dumps(runtime), encoding="utf-8")
                yield runtime_file

            with mock.patch.object(launch, "STATE_DIR", state_dir), mock.patch.object(
                launch, "RUNTIME_FILE", runtime_file
            ), mock.patch.object(
                launch, "LOG_FILE", state_dir / "control-center.log"
            ), mock.patch.object(
                launch, "startup_lock", completed_by_first_launcher
            ), mock.patch.object(
                launch, "runtime_ready", return_value=True
            ) as ready, mock.patch.object(
                launch, "open_runtime"
            ) as opened, mock.patch.object(
                launch.subprocess, "Popen"
            ) as popen:
                self.assertEqual(launch.main(), 0)

        ready.assert_called_once_with(runtime, launch.installed_kit_version())
        opened.assert_called_once_with(runtime)
        popen.assert_not_called()

    def test_launcher_replaces_an_authenticated_outdated_server(self):
        process = mock.Mock(pid=321)
        process.poll.return_value = None
        outdated = {
            "pid": 123,
            "port": 8790,
            "token": "outdated-token-1234567890",
        }
        expected_version = launch.installed_kit_version()
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            with mock.patch.object(
                launch, "STATE_DIR", state_dir
            ), mock.patch.object(
                launch, "ready_runtime", side_effect=[None, None, outdated]
            ) as ready, mock.patch.object(
                launch, "stop_outdated_runtime", return_value=True
            ) as stopped, mock.patch.object(
                launch, "instance_lock_held", return_value=False
            ), mock.patch.object(
                launch, "free_port", return_value=8791
            ), mock.patch.object(
                launch, "control_center_ready", return_value=True
            ), mock.patch.object(
                launch, "open_runtime"
            ) as opened, mock.patch.object(
                launch.subprocess, "Popen", return_value=process
            ) as popen:
                self.assertEqual(launch.main(), 0)

            self.assertEqual(ready.call_args_list, [
                mock.call(state_dir.resolve(), expected_version),
                mock.call(state_dir.resolve(), expected_version),
                mock.call(state_dir.resolve()),
            ])
            stopped.assert_called_once_with(outdated, state_dir.resolve())
            self.assertEqual(popen.call_count, 1)
            runtime = json.loads(
                (state_dir / launch.RUNTIME_FILE_NAME).read_text(encoding="utf-8")
            )
            self.assertEqual(runtime["kitVersion"], expected_version)
            opened.assert_called_once_with(runtime)

    def test_invalid_port_starts_are_rejected_before_socket_creation(self):
        with mock.patch.object(launch.socket, "socket") as socket_factory:
            for value in (None, True, 0, -1, 65536, "", "abc", "1.5"):
                with self.subTest(value=value):
                    with self.assertRaisesRegex(ValueError, "1 through 65535"):
                        launch.free_port(value)
        socket_factory.assert_not_called()

    def test_main_rejects_invalid_wkcc_port_before_spawning(self):
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            with mock.patch.object(launch, "STATE_DIR", state_dir), mock.patch.object(
                launch, "RUNTIME_FILE", state_dir / launch.RUNTIME_FILE_NAME
            ), mock.patch.object(
                launch, "LOG_FILE", state_dir / "control-center.log"
            ), mock.patch.dict(
                os.environ, {"WKCC_PORT": "70000"}, clear=False
            ), mock.patch.object(
                launch.subprocess, "Popen"
            ) as popen, mock.patch.object(
                launch.socket, "socket"
            ) as socket_factory:
                with self.assertRaisesRegex(ValueError, "WKCC_PORT"):
                    launch.main()
        popen.assert_not_called()
        socket_factory.assert_not_called()

    def test_windows_liveness_uses_process_handle_probe(self):
        with mock.patch.object(launch.os, "name", "nt"), mock.patch.object(
            launch, "windows_process_alive", return_value=True
        ) as probe, mock.patch.object(launch.os, "kill") as kill:
            self.assertTrue(launch.process_alive(123))
        probe.assert_called_once_with(123)
        kill.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows process handles are only available on Windows")
    def test_windows_process_probe_recognizes_current_process(self):
        self.assertTrue(launch.windows_process_alive(os.getpid()))

    def test_windows_detachment_uses_creation_flags(self):
        with mock.patch.object(launch.subprocess, "DETACHED_PROCESS", 8, create=True), mock.patch.object(
            launch.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, create=True
        ):
            self.assertEqual(launch.detached_process_kwargs("windows"), {"creationflags": 520})
        self.assertEqual(launch.detached_process_kwargs("linux"), {"start_new_session": True})

    def test_startup_timeout_accounts_for_recoverable_sessions(self):
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            (state_dir / "state.json").write_text(
                json.dumps(
                    {
                        "sessions": [
                            {"status": "active"},
                            {"status": "busy"},
                            {"status": "completed"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(launch, "STATE_DIR", state_dir), mock.patch.dict(
                os.environ, {}, clear=True
            ):
                self.assertEqual(launch.startup_timeout_seconds(), 30.0)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO files unavailable")
    def test_startup_timeout_ignores_fifo_state_without_blocking(self):
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            os.mkfifo(str(state_dir / "state.json"))
            started = time.monotonic()
            with mock.patch.object(launch, "STATE_DIR", state_dir), mock.patch.dict(
                os.environ, {}, clear=True
            ):
                self.assertEqual(launch.startup_timeout_seconds(), 10.0)
            self.assertLess(time.monotonic() - started, 1)

    def test_failed_startup_stops_and_reaps_the_spawned_process(self):
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.return_value = 0
        with mock.patch.object(launch.os, "kill") as kill:
            launch.stop_spawned_process(process)
        process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=5)
        process.kill.assert_not_called()
        kill.assert_not_called()

    def test_failed_startup_force_kills_a_child_that_ignores_terminate(self):
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("server", 5), 0]
        launch.stop_spawned_process(process)
        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        self.assertEqual(process.wait.call_count, 2)

    def test_windows_failed_startup_terminates_the_entire_process_tree(self):
        process = mock.Mock(pid=7312)
        process.poll.return_value = None
        process.wait.return_value = 0
        with mock.patch.object(launch.os, "name", "nt"), mock.patch.object(
            launch.shutil, "which", return_value="C:/Windows/System32/taskkill.exe"
        ), mock.patch.object(launch.subprocess, "run") as run:
            launch.stop_spawned_process(process)
        run.assert_called_once()
        self.assertEqual(
            run.call_args[0][0], ["taskkill", "/PID", "7312", "/T", "/F"]
        )
        process.wait.assert_called_once_with(timeout=5)
        process.terminate.assert_not_called()

    def test_failed_startup_prefers_authenticated_graceful_shutdown(self):
        process = mock.Mock(pid=7313)
        process.poll.return_value = None
        process.wait.return_value = 0
        with mock.patch.object(
            launch, "request_control_center_shutdown", return_value=True
        ) as shutdown:
            launch.stop_spawned_process(
                process, port=8790, token="safe-token-1234567890"
            )
        shutdown.assert_called_once_with(8790, "safe-token-1234567890")
        process.wait.assert_called_once_with(timeout=5)
        process.terminate.assert_not_called()

    def test_authenticated_readiness_checks_token_and_pid(self):
        response = mock.Mock(status=200)
        response.read.return_value = json.dumps({"ok": True, "pid": 42}).encode("utf-8")
        connection = mock.Mock()
        connection.getresponse.return_value = response
        with mock.patch.object(
            launch.http.client, "HTTPConnection", return_value=connection
        ) as connection_factory:
            self.assertTrue(launch.control_center_ready(8790, "secret-token", 42))
        connection_factory.assert_called_once_with(
            "127.0.0.1", 8790, timeout=0.35
        )
        connection.request.assert_called_once_with(
            "GET", "/api/health", headers={"X-WKCC-Token": "secret-token"}
        )
        connection.close.assert_called_once_with()

    def test_authenticated_readiness_can_require_the_running_kit_version(self):
        def ready(version):
            response = mock.Mock(status=200)
            response.read.return_value = json.dumps({
                "ok": True,
                "pid": 42,
                "kitVersion": version,
            }).encode("utf-8")
            connection = mock.Mock()
            connection.getresponse.return_value = response
            return connection

        with mock.patch.object(
            launch.http.client, "HTTPConnection", return_value=ready("0.8.15")
        ):
            self.assertTrue(launch.control_center_ready(
                8790, "secret-token", 42, "0.8.15"
            ))
        with mock.patch.object(
            launch.http.client, "HTTPConnection", return_value=ready("0.8.14")
        ):
            self.assertFalse(launch.control_center_ready(
                8790, "secret-token", 42, "0.8.15"
            ))

    def test_outdated_authenticated_runtime_stops_before_replacement(self):
        runtime = {"pid": 42, "port": 8790, "token": "secret-token"}
        with mock.patch.object(
            launch, "runtime_ready", side_effect=[True, False]
        ), mock.patch.object(
            launch, "request_control_center_shutdown", return_value=True
        ) as shutdown, mock.patch.object(
            launch, "instance_lock_held", return_value=False
        ):
            self.assertTrue(launch.stop_outdated_runtime(runtime, Path("/state")))
        shutdown.assert_called_once_with(8790, "secret-token")

    def test_authenticated_readiness_rejects_wrong_pid(self):
        response = mock.Mock(status=200)
        response.read.return_value = b'{"ok": true, "pid": 41}'
        connection = mock.Mock()
        connection.getresponse.return_value = response
        with mock.patch.object(launch.http.client, "HTTPConnection", return_value=connection):
            self.assertFalse(launch.control_center_ready(8790, "secret-token", 42))

    def test_main_passes_platform_detachment_to_child(self):
        process = mock.Mock(pid=321)
        process.poll.return_value = None
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            with mock.patch.object(launch, "STATE_DIR", state_dir), mock.patch.object(
                launch, "RUNTIME_FILE", state_dir / launch.RUNTIME_FILE_NAME
            ), mock.patch.object(launch, "LOG_FILE", state_dir / "control-center.log"), mock.patch.object(
                launch, "free_port", return_value=8790
            ), mock.patch.object(
                launch, "detached_process_kwargs", return_value={"creationflags": 520}
            ), mock.patch.object(
                launch, "control_center_ready", return_value=True
            ), mock.patch.object(
                launch, "open_runtime"
            ), mock.patch.object(
                launch.subprocess, "Popen", return_value=process
            ) as popen:
                self.assertEqual(launch.main(), 0)
                if os.name != "nt":
                    self.assertEqual((state_dir.stat().st_mode & 0o777), 0o700)
                    self.assertEqual(((state_dir / launch.RUNTIME_FILE_NAME).stat().st_mode & 0o777), 0o600)
                    self.assertEqual(((state_dir / "control-center.log").stat().st_mode & 0o777), 0o600)
                    self.assertEqual(
                        ((state_dir / launch.STARTUP_LOCK_NAME).stat().st_mode & 0o777),
                        0o600,
                    )
        self.assertEqual(popen.call_args[1]["creationflags"], 520)
        self.assertNotIn("start_new_session", popen.call_args[1])

    def test_main_cleans_up_its_child_when_startup_times_out(self):
        process = mock.Mock(pid=321)
        process.poll.return_value = None
        process.wait.return_value = 0
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            with mock.patch.object(launch, "STATE_DIR", state_dir), mock.patch.object(
                launch, "RUNTIME_FILE", state_dir / launch.RUNTIME_FILE_NAME
            ), mock.patch.object(launch, "LOG_FILE", state_dir / "control-center.log"), mock.patch.object(
                launch, "free_port", return_value=8790
            ), mock.patch.object(
                launch, "startup_timeout_seconds", return_value=0
            ), mock.patch.object(
                launch.subprocess, "Popen", return_value=process
            ), mock.patch.object(
                launch.subprocess, "run"
            ) as run:
                with self.assertRaisesRegex(RuntimeError, "failed to start"):
                    launch.main()
        if os.name == "nt" and shutil.which("taskkill"):
            run.assert_called_once()
            process.terminate.assert_not_called()
        else:
            process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=5)

    def test_windows_launcher_prefers_available_python_three_runtimes(self):
        script_path = r"C:\Program Files\AWESOME WEBKIT\launch.py"
        text = install_shortcut.windows_launcher(script_path)
        self.assertLess(text.index("where py"), text.index("where python3"))
        self.assertLess(text.index("where python3"), text.index("where python >"))
        self.assertIn("sys.version_info[1] not in range(7, 100)", text)
        encoded = base64.b64encode(script_path.encode("utf-8")).decode("ascii")
        self.assertIn('py -3 -c "import base64,subprocess,sys;', text)
        self.assertIn("subprocess.call([sys.executable,path]+sys.argv[2:])", text)
        self.assertEqual(text.count('"{}" %*'.format(encoded)), 3)
        self.assertNotIn(script_path, text)
        text.encode("ascii")
        root_launcher = (KIT_ROOT / "AWESOME WEBKIT.cmd").read_text(encoding="utf-8")
        self.assertIn("where py", root_launcher)
        self.assertIn("where python3", root_launcher)
        self.assertIn("where python >nul", root_launcher)

    def test_windows_installer_writes_exact_crlf_bytes(self):
        with tempfile.TemporaryDirectory() as raw:
            desktop = Path(raw)
            with mock.patch.object(install_shortcut.platform, "system", return_value="Windows"), mock.patch.object(
                install_shortcut, "desktop_directory", return_value=desktop
            ):
                self.assertEqual(install_shortcut.main(), 0)
            data = (desktop / "AWESOME WEBKIT.cmd").read_bytes()
        self.assertTrue(data.endswith(b"\r\n"))
        self.assertNotIn(b"\r\r\n", data)
        self.assertEqual(data.count(b"\n"), data.count(b"\r\n"))
        data.decode("ascii")

    @unittest.skipUnless(os.name == "nt", "cmd.exe launcher execution requires Windows")
    def test_generated_windows_launcher_executes_with_cmd(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            script_dir = root / "Kit 100% Ready - ערכה - 工具 - 🚀"
            script_dir.mkdir()
            script = script_dir / "harmless fixture.py"
            result_file = root / "launcher-result.json"
            script.write_text(
                "import json\n"
                "import sys\n"
                "from pathlib import Path\n"
                "Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]), encoding='utf-8')\n"
                "raise SystemExit(23)\n",
                encoding="utf-8",
            )
            shortcut = root / "Launch Test.cmd"
            shortcut.write_bytes(install_shortcut.windows_launcher_bytes(script))
            result = subprocess.run(
                [
                    "cmd.exe",
                    "/d",
                    "/c",
                    "call",
                    str(shortcut),
                    str(result_file),
                    "hello world",
                    "plain",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 23, result.stdout + result.stderr)
            self.assertEqual(
                json.loads(result_file.read_text(encoding="utf-8")),
                ["hello world", "plain"],
            )

    def test_linux_desktop_exec_quotes_reserved_characters(self):
        value = install_shortcut.desktop_exec_quote('/tmp/My 100% $Kit/launch`control".sh')
        self.assertEqual(value, r'"/tmp/My 100%% \\$Kit/launch\\`control\\".sh"')
        self.assertEqual(
            install_shortcut.desktop_exec_quote(r"/tmp/Back\slash"),
            r'"/tmp/Back\\\\slash"',
        )

    def test_http_banner_tracks_release_version(self):
        version = (KIT_ROOT / "webkit" / "VERSION").read_text(encoding="utf-8").strip()
        self.assertEqual(
            ControlCenterHandler.server_version,
            "AwesomeWebkitControlCenter/{}".format(version),
        )

    def test_access_log_redacts_bootstrap_token_query_values(self):
        request = mock.Mock()
        request.server.token = "private-bootstrap-token"
        output = io.StringIO()
        with mock.patch("server.sys.stderr", output):
            ControlCenterHandler.log_message(
                request,
                '"%s" %s %s',
                "GET /?token=private-bootstrap-token&view=home HTTP/1.1",
                "302",
                "-",
            )
        logged = output.getvalue()
        self.assertNotIn("private-bootstrap-token", logged)
        self.assertIn("/?token=[REDACTED]&view=home", logged)

    def test_bootstrap_redirect_suppresses_routine_access_log(self):
        token = "private-bootstrap-token"
        server = ControlCenterHTTPServer(
            ("127.0.0.1", 0), ControlCenterHandler, object(), token
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        output = io.StringIO()
        try:
            with mock.patch("server.sys.stderr", output):
                connection = http.client.HTTPConnection(
                    "127.0.0.1", server.server_port, timeout=3
                )
                connection.request("GET", "/?token={}&view=home".format(token))
                response = connection.getresponse()
                response.read()
                connection.close()
            self.assertEqual(response.status, 302)
            self.assertEqual(response.getheader("Location"), "/#token=" + token)
            self.assertIsNone(response.getheader("Set-Cookie"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
        logged = output.getvalue()
        self.assertNotIn(token, logged)
        self.assertEqual(logged, "")

    def test_control_center_error_logs_are_bounded_and_redacted(self):
        request = object.__new__(ControlCenterHandler)
        request.server = mock.Mock(token="private-bootstrap-token")
        output = io.StringIO()
        with mock.patch("server.sys.stderr", output):
            ControlCenterHandler.log_error(
                request,
                "failure %s %s",
                "private-bootstrap-token",
                "x" * 10000,
            )
        logged = output.getvalue()
        self.assertNotIn("private-bootstrap-token", logged)
        self.assertIn("[REDACTED]", logged)
        self.assertLess(len(logged.encode("utf-8")), 2200)

    def test_partial_client_cannot_block_control_center_close_forever(self):
        accepted = threading.Event()

        class PartialHandler(ControlCenterHandler):
            def setup(handler_self):
                super().setup()
                accepted.set()

        with mock.patch("server.CLIENT_IO_TIMEOUT_SECONDS", 0.2):
            server = ControlCenterHTTPServer(
                ("127.0.0.1", 0), PartialHandler, object(),
                "test-token-1234567890",
            )
            serving = threading.Thread(target=server.serve_forever, daemon=True)
            serving.start()
            client = socket.create_connection(
                ("127.0.0.1", server.server_port), timeout=3
            )
            client.sendall(
                (
                    "POST /api/settings HTTP/1.1\r\n"
                    "Host: 127.0.0.1:{}\r\n"
                ).format(server.server_port).encode("ascii")
            )
            self.assertTrue(accepted.wait(timeout=3))

        try:
            server.shutdown()
            started = time.monotonic()
            server.server_close()
            self.assertLess(time.monotonic() - started, 2)
        finally:
            client.close()
            if serving.is_alive():
                server.shutdown()
            server.server_close()
            serving.join(timeout=3)
            self.assertFalse(serving.is_alive())

    def test_control_center_rejects_negative_content_length(self):
        request = mock.Mock()
        request.headers = {"Content-Length": "-1"}
        with self.assertRaisesRegex(ControlCenterError, "Invalid Content-Length"):
            ControlCenterHandler._body(request)

    def test_health_endpoint_is_authenticated_and_reports_server_pid(self):
        token = "test-token-1234567890"
        server = ControlCenterHTTPServer(
            ("127.0.0.1", 0), ControlCenterHandler, object(), token
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            connection.request("GET", "/api/health", headers={"X-WKCC-Token": token})
            response = connection.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
            connection.close()
            self.assertEqual(response.status, 200)
            self.assertEqual(payload, {
                "ok": True,
                "pid": os.getpid(),
                "kitVersion": server.kit_version,
            })

            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            with mock.patch.dict(os.environ, {"WKCC_NO_AUTH": "1"}, clear=False):
                connection.request("GET", "/api/health")
            response = connection.getresponse()
            response.read()
            connection.close()
            self.assertEqual(response.status, 401)

            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            connection.request("GET", "/api/health", headers={"Cookie": "wkcc=" + token})
            response = connection.getresponse()
            response.read()
            connection.close()
            self.assertEqual(response.status, 401)

            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            connection.request(
                "GET",
                "/api/health",
                headers={"X-WKCC-Token": token, "Host": "attacker.invalid"},
            )
            response = connection.getresponse()
            response.read()
            connection.close()
            self.assertEqual(response.status, 421)

            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            connection.request(
                "GET",
                "/api/health",
                headers={
                    "X-WKCC-Token": token,
                    "Origin": "http://attacker.invalid",
                },
            )
            response = connection.getresponse()
            response.read()
            connection.close()
            self.assertEqual(response.status, 401)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_static_shell_is_public_but_contains_no_control_center_state(self):
        token = "test-token-1234567890"
        server = ControlCenterHTTPServer(
            ("127.0.0.1", 0), ControlCenterHandler, object(), token
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection(
                "127.0.0.1", server.server_port, timeout=3
            )
            connection.request("GET", "/app.js")
            response = connection.getresponse()
            payload = response.read().decode("utf-8")
            connection.close()
            self.assertEqual(response.status, 200)
            self.assertIn("X-WKCC-Token", payload)
            self.assertNotIn(token, payload)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_running_server_keeps_one_compatible_static_snapshot(self):
        token = "test-token-1234567890"
        with tempfile.TemporaryDirectory() as raw:
            static_root = Path(raw)
            for name in ("index.html", "styles.css", "brand-icon.svg"):
                (static_root / name).write_text(name, encoding="utf-8")
            app_js = static_root / "app.js"
            app_js.write_text("first frontend", encoding="utf-8")
            with mock.patch("server.STATIC_ROOT", static_root):
                server = ControlCenterHTTPServer(
                    ("127.0.0.1", 0), ControlCenterHandler, object(), token
                )
            app_js.write_text("incompatible replacement", encoding="utf-8")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = http.client.HTTPConnection(
                    "127.0.0.1", server.server_port, timeout=3
                )
                connection.request("GET", "/app.js")
                response = connection.getresponse()
                payload = response.read().decode("utf-8")
                connection.close()
                self.assertEqual(response.status, 200)
                self.assertEqual(payload, "first frontend")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_nested_session_get_routes_must_match_exactly(self):
        token = "test-token-1234567890"
        app = mock.Mock()
        server = ControlCenterHTTPServer(
            ("127.0.0.1", 0), ControlCenterHandler, app, token
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection(
                "127.0.0.1", server.server_port, timeout=3
            )
            connection.request(
                "GET",
                "/api/sessions/session-1/extra/events",
                headers={"X-WKCC-Token": token},
            )
            response = connection.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
            connection.close()
            self.assertEqual(response.status, 404)
            self.assertEqual(payload, {"error": "API route not found."})
            app.sessions.events.assert_not_called()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_unexpected_http_errors_do_not_leak_exception_details(self):
        token = "test-token-1234567890"
        app = mock.Mock()
        app.bootstrap.side_effect = RuntimeError("private-path-and-token")
        server = ControlCenterHTTPServer(
            ("127.0.0.1", 0), ControlCenterHandler, app, token
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection(
                "127.0.0.1", server.server_port, timeout=3
            )
            connection.request(
                "GET", "/api/bootstrap", headers={"X-WKCC-Token": token}
            )
            response = connection.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
            connection.close()
            self.assertEqual(response.status, 500)
            self.assertEqual(payload, {"error": "Internal server error."})
            self.assertNotIn("private-path-and-token", json.dumps(payload))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_control_center_csp_does_not_depend_on_inline_styles(self):
        script = (CONTROL_DIR / "static" / "app.js").read_text(encoding="utf-8")
        styles = (CONTROL_DIR / "static" / "styles.css").read_text(encoding="utf-8")
        self.assertNotIn(".style.setProperty", script)
        for color in ("blue", "red", "green", "orange", "purple"):
            self.assertIn('.color-card[data-color="{}"]'.format(color), styles)


if __name__ == "__main__":
    unittest.main()
