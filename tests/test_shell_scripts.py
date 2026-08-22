import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONFIG_GET = ROOT / "webkit" / "scripts" / "config-get.sh"
CLAIM = ROOT / "webkit" / "scripts" / "claim-color.sh"
RELEASE = ROOT / "webkit" / "scripts" / "release-color.sh"
OPEN_PREVIEW = ROOT / "webkit" / "scripts" / "open-preview.sh"
WAIT_FOR_FILE = ROOT / "webkit" / "scripts" / "wait-for-file.sh"


class ShellScriptTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tempdir.name)
        self.lock_dir = self.base / "locks"
        self.port_lock_dir = self.base / "port-locks"
        self.config_path = self.base / "webkit.config.json"
        self.config = {
            "lock_dir": str(self.lock_dir),
            "grace_seconds": 180,
            "palette": [
                {"slug": "green", "emoji": "🟢", "port": 6123},
                {"slug": "blue_2", "emoji": "🔵", "port": 6124},
            ],
            "browser": {"mode": "print", "app_name": "Google Chrome"},
        }
        self.write_config()

    def tearDown(self):
        self.tempdir.cleanup()

    def write_config(self, config=None):
        value = self.config if config is None else config
        self.config_path.write_text(
            json.dumps(value, ensure_ascii=False), encoding="utf-8"
        )

    def run_script(self, script, *args, **kwargs):
        env = os.environ.copy()
        for key in (
            "WK_COLOR_FORCE",
            "WK_COLOR_LOCKDIR",
            "WK_COLOR_OWNER",
            "WK_COLOR_TABS",
            "WK_PORT_LOCKDIR",
        ):
            env.pop(key, None)
        env["WK_CONFIG"] = str(self.config_path)
        env["WK_PORT_LOCKDIR"] = str(self.port_lock_dir)
        env.update(kwargs.pop("env", {}))
        return subprocess.run(
            ["/bin/bash", str(script)] + list(args),
            cwd=str(kwargs.pop("cwd", self.base)),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            **kwargs
        )

    def test_valid_palette_is_available_to_all_commands(self):
        palette = self.run_script(CONFIG_GET, "palette")
        self.assertEqual(palette.returncode, 0, palette.stderr)
        self.assertEqual(
            palette.stdout.splitlines(),
            ["green 🟢 6123", "blue_2 🔵 6124"],
        )

        color = self.run_script(CONFIG_GET, "color", "🔵")
        self.assertEqual(color.returncode, 0, color.stderr)
        self.assertEqual(color.stdout.strip(), "blue_2 6124")

    def test_config_string_scalars_reject_controls_and_embedded_newlines(self):
        for value in ("Bad\nBrowser", "Bad\tBrowser", "Bad\u0085Browser"):
            with self.subTest(value=repr(value)):
                self.config["browser"]["app_name"] = value
                self.write_config()
                result = self.run_script(CONFIG_GET, "get", "browser.app_name")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("control or newline", result.stderr)

    def test_config_rejects_nonstandard_json_numbers(self):
        for value in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value):
                raw = json.dumps(self.config, ensure_ascii=False).replace("180", value, 1)
                self.config_path.write_text(raw, encoding="utf-8")
                result = self.run_script(CONFIG_GET, "get", "grace_seconds")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("non-standard JSON constant", result.stderr)

    def test_config_rejects_unpaired_surrogates_and_accepts_emoji_pairs(self):
        for unsafe in ("\ud800", "\udfff"):
            with self.subTest(unsafe=ascii(unsafe)):
                config = json.loads(json.dumps(self.config))
                config["browser"]["app_name"] = unsafe
                self.config_path.write_text(
                    json.dumps(config), encoding="utf-8"
                )
                result = self.run_script(
                    CONFIG_GET, "get", "browser.app_name"
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("unpaired Unicode surrogate", result.stderr)

        config = json.loads(json.dumps(self.config))
        config["browser"]["app_name"] = "Browser 😀"
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        result = self.run_script(CONFIG_GET, "get", "browser.app_name")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "Browser 😀")

    def test_config_rejects_unsafe_claim_grace_values(self):
        for value in (0, 29, 86401, True, 30.5, "180"):
            with self.subTest(value=value):
                self.config["grace_seconds"] = value
                self.write_config()
                result = self.run_script(CONFIG_GET, "get", "grace_seconds")
                self.assertEqual(result.returncode, 1)
                self.assertIn("integer from 30 through 86400", result.stderr)

    def test_config_reader_rejects_oversize_links_and_fifo_without_blocking(self):
        oversized = self.base / "oversized-config.json"
        with oversized.open("wb") as handle:
            handle.seek(1024 * 1024)
            handle.write(b"x")
        result = self.run_script(
            CONFIG_GET, "palette", env={"WK_CONFIG": str(oversized)}
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("exceeds 1 MB", result.stderr)

        linked = self.base / "linked-config.json"
        try:
            linked.symlink_to(self.config_path)
        except (OSError, NotImplementedError) as error:
            self.skipTest("symbolic links unavailable: {}".format(error))
        result = self.run_script(
            CONFIG_GET, "palette", env={"WK_CONFIG": str(linked)}
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("regular file", result.stderr)

        if hasattr(os, "mkfifo"):
            fifo = self.base / "config.pipe"
            os.mkfifo(str(fifo))
            result = self.run_script(
                CONFIG_GET, "palette", env={"WK_CONFIG": str(fifo)}, timeout=3
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("regular file", result.stderr)

    def test_palette_label_rejects_markup_controls_and_bidi_controls(self):
        invalid = ("<", ">", "&", '"', "'", "`", "x\x7fy", "x\u2028y", "x\u202ey", "x\u2067y")
        for value in invalid:
            with self.subTest(value=repr(value)):
                self.config["palette"][0]["emoji"] = value
                self.write_config()
                result = self.run_script(CONFIG_GET, "palette")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("palette[0].emoji", result.stderr)

        self.config["palette"][0]["emoji"] = "👩‍💻"
        self.write_config()
        result = self.run_script(CONFIG_GET, "palette")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_palette_contract_rejects_invalid_entries(self):
        invalid_cases = {
            "missing palette": dict(self.config, palette=[]),
            "entry is not object": dict(self.config, palette=["green"]),
            "entry missing field": dict(
                self.config, palette=[{"slug": "green", "emoji": "🟢"}]
            ),
            "unsafe slug": dict(
                self.config,
                palette=[{"slug": "../escaped", "emoji": "🟢", "port": 6123}],
            ),
            "empty emoji": dict(
                self.config,
                palette=[{"slug": "green", "emoji": "", "port": 6123}],
            ),
            "whitespace emoji": dict(
                self.config,
                palette=[{"slug": "green", "emoji": "x y", "port": 6123}],
            ),
            "boolean port": dict(
                self.config,
                palette=[{"slug": "green", "emoji": "🟢", "port": True}],
            ),
            "zero port": dict(
                self.config,
                palette=[{"slug": "green", "emoji": "🟢", "port": 0}],
            ),
            "high port": dict(
                self.config,
                palette=[{"slug": "green", "emoji": "🟢", "port": 65536}],
            ),
        }
        for name, config in invalid_cases.items():
            with self.subTest(name=name):
                self.write_config(config)
                result = self.run_script(CONFIG_GET, "get", "lock_dir")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("config-get.sh:", result.stderr)

    def test_palette_contract_rejects_duplicate_identity_fields(self):
        first = {"slug": "green", "emoji": "🟢", "port": 6123}
        duplicates = {
            "slug": {"slug": "green", "emoji": "🔵", "port": 6124},
            "emoji": {"slug": "blue", "emoji": "🟢", "port": 6124},
            "port": {"slug": "blue", "emoji": "🔵", "port": 6123},
        }
        for field, second in duplicates.items():
            with self.subTest(field=field):
                self.write_config(dict(self.config, palette=[first, second]))
                result = self.run_script(CONFIG_GET, "palette")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("duplicate palette %s" % field, result.stderr)

    def test_malicious_slug_is_rejected_before_claim(self):
        self.config["palette"] = [
            {"slug": "../escaped", "emoji": "🟢", "port": 6123}
        ]
        self.write_config()

        result = self.run_script(CLAIM)

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.base / "escaped.lock").exists())
        self.assertFalse(self.lock_dir.exists())

    def test_claim_rejects_root_equivalent_lock_directory(self):
        self.config["lock_dir"] = os.path.join(os.path.sep, ".")
        self.write_config()

        claim_result = self.run_script(CLAIM, "--slug", "green")
        release_result = self.run_script(RELEASE, "🟢")

        self.assertEqual(claim_result.returncode, 2)
        self.assertEqual(release_result.returncode, 2)
        self.assertIn("filesystem root", claim_result.stderr)
        self.assertIn("filesystem root", release_result.stderr)

    def test_release_rejects_relative_lock_directory(self):
        result = self.run_script(
            RELEASE,
            "🟢",
            env={"WK_COLOR_LOCKDIR": "relative/locks", "WK_COLOR_OWNER": "owner-a"},
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("absolute path", result.stderr)

    def test_normal_claim_and_release_with_custom_absolute_directory(self):
        custom_lock_dir = self.base / "custom lock registry"
        common_env = {
            "WK_COLOR_LOCKDIR": str(custom_lock_dir),
            "WK_COLOR_OWNER": "owner-a",
            "WK_COLOR_TABS": "",
        }

        claimed = self.run_script(CLAIM, "--full", "--slug", "green", env=common_env)

        self.assertEqual(claimed.returncode, 0, claimed.stderr)
        self.assertEqual(claimed.stdout.strip(), "🟢 green 6123")
        owner_path = custom_lock_dir / "green.lock" / "owner"
        reservation_path = custom_lock_dir / "green.lock" / "reservation"
        self.assertEqual(owner_path.read_text(encoding="utf-8"), "owner-a\n")
        self.assertRegex(
            reservation_path.read_text(encoding="utf-8").strip(),
            r"^[A-Za-z0-9_-]{32,128}$",
        )
        self.assertEqual(stat.S_IMODE(owner_path.stat().st_mode), 0o600)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(custom_lock_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(owner_path.parent.stat().st_mode), 0o700)

        released = self.run_script(RELEASE, "🟢", env=common_env)
        self.assertEqual(released.returncode, 0, released.stderr)
        self.assertFalse((custom_lock_dir / "green.lock").exists())

    def test_session_mode_tags_a_fresh_claim_for_safe_startup(self):
        common_env = {"WK_COLOR_OWNER": "owner-a", "WK_COLOR_TABS": ""}

        claimed = self.run_script(
            CLAIM, "--session", "--slug", "green", env=common_env
        )

        self.assertEqual(claimed.returncode, 0, claimed.stderr)
        self.assertEqual(claimed.stdout.strip(), "claimed 🟢 green 6123")
        released = self.run_script(RELEASE, "--slug", "green", env=common_env)
        self.assertEqual(released.returncode, 0, released.stderr)

    def test_session_and_active_modes_are_mutually_exclusive(self):
        result = self.run_script(CLAIM, "--session", "--active")

        self.assertEqual(result.returncode, 2)
        self.assertIn("mutually exclusive", result.stderr)

    def test_claim_does_not_chmod_preexisting_custom_registry(self):
        self.lock_dir.mkdir(mode=0o700)
        if os.name != "nt":
            self.lock_dir.chmod(0o700)

        claimed = self.run_script(
            CLAIM,
            "--slug",
            "green",
            env={"WK_COLOR_OWNER": "owner-a", "WK_COLOR_TABS": ""},
        )

        self.assertEqual(claimed.returncode, 0, claimed.stderr)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.lock_dir.stat().st_mode), 0o700)

    def test_release_finds_original_reservation_after_config_port_changes(self):
        common = {"WK_COLOR_OWNER": "owner-a", "WK_COLOR_TABS": ""}
        claimed = self.run_script(CLAIM, "--slug", "green", env=common)
        self.assertEqual(claimed.returncode, 0, claimed.stderr)
        original_port_lock = self.port_lock_dir / "6123.lock"
        self.assertTrue(original_port_lock.is_dir())

        self.config["palette"][0]["port"] = 6203
        self.write_config()
        released = self.run_script(RELEASE, "🟢", env=common)

        self.assertEqual(released.returncode, 0, released.stderr)
        self.assertFalse((self.lock_dir / "green.lock").exists())
        self.assertFalse(original_port_lock.exists())

    def test_lock_cleanup_refuses_unexpected_contents_without_deleting_them(self):
        self.lock_dir.mkdir(mode=0o700)
        lock = self.lock_dir / "green.lock"
        lock.mkdir()
        (lock / "owner").write_text("owner-a\n", encoding="utf-8")
        unexpected = lock / "unexpected.txt"
        unexpected.write_text("sentinel", encoding="utf-8")
        self.config["grace_seconds"] = 30
        self.write_config()
        os.utime(str(lock), (1, 1))

        reclaim = self.run_script(
            CLAIM,
            "--slug",
            "green",
            env={"WK_COLOR_OWNER": "owner-a", "WK_COLOR_TABS": ""},
        )
        release = self.run_script(
            RELEASE,
            "🟢",
            env={"WK_COLOR_OWNER": "owner-a", "WK_COLOR_FORCE": "1"},
        )

        self.assertEqual(reclaim.returncode, 2)
        self.assertEqual(release.returncode, 2)
        self.assertEqual(unexpected.read_text(encoding="utf-8"), "sentinel")
        self.assertEqual((lock / "owner").read_text(encoding="utf-8"), "owner-a\n")

    def test_release_enforces_ownership_and_exact_force_value(self):
        claim_env = {
            "WK_COLOR_OWNER": "owner-a",
            "WK_COLOR_TABS": "",
        }
        claimed = self.run_script(CLAIM, "--slug", "green", env=claim_env)
        self.assertEqual(claimed.returncode, 0, claimed.stderr)
        lock = self.lock_dir / "green.lock"

        wrong_owner = self.run_script(
            RELEASE, "🟢", env={"WK_COLOR_OWNER": "owner-b"}
        )
        self.assertEqual(wrong_owner.returncode, 1)
        self.assertTrue(lock.is_dir())

        wrong_force = self.run_script(
            RELEASE,
            "🟢",
            env={"WK_COLOR_OWNER": "owner-b", "WK_COLOR_FORCE": "yes"},
        )
        self.assertEqual(wrong_force.returncode, 1)
        self.assertTrue(lock.is_dir())

        forced = self.run_script(
            RELEASE,
            "🟢",
            env={"WK_COLOR_OWNER": "owner-b", "WK_COLOR_FORCE": "1"},
        )
        self.assertEqual(forced.returncode, 0, forced.stderr)
        self.assertFalse(lock.exists())

    def test_claim_and_release_reject_symlink_registry(self):
        target = self.base / "real-registry"
        target.mkdir()
        link = self.base / "linked-registry"
        try:
            link.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            self.skipTest("symbolic links unavailable: %s" % error)
        env = {"WK_COLOR_LOCKDIR": str(link), "WK_COLOR_OWNER": "owner-a"}

        claimed = self.run_script(CLAIM, env=env)
        released = self.run_script(RELEASE, "🟢", env=env)
        released_with_slash = self.run_script(
            RELEASE,
            "🟢",
            env={"WK_COLOR_LOCKDIR": str(link) + "/", "WK_COLOR_OWNER": "owner-a"},
        )

        self.assertEqual(claimed.returncode, 2)
        self.assertEqual(released.returncode, 2)
        self.assertEqual(released_with_slash.returncode, 2)
        self.assertIn("symbolic link", claimed.stderr)
        self.assertIn("symbolic link", released.stderr)
        self.assertIn("symbolic link", released_with_slash.stderr)

    def test_claim_rejects_non_directory_registry_and_lock(self):
        registry_file = self.base / "registry-file"
        registry_file.write_text("not a directory", encoding="utf-8")
        bad_registry = self.run_script(
            CLAIM, env={"WK_COLOR_LOCKDIR": str(registry_file)}
        )
        self.assertEqual(bad_registry.returncode, 2)
        self.assertIn("not a directory", bad_registry.stderr)

        self.lock_dir.mkdir(mode=0o700)
        (self.lock_dir / "green.lock").write_text("not a directory", encoding="utf-8")
        bad_lock = self.run_script(CLAIM, "--slug", "green")
        self.assertEqual(bad_lock.returncode, 2)
        self.assertIn("non-directory lock target", bad_lock.stderr)

        released = self.run_script(RELEASE, "🟢")
        self.assertEqual(released.returncode, 2)
        self.assertIn("non-directory lock target", released.stderr)

    def test_claim_safely_reclaims_its_own_stale_lock(self):
        self.config["grace_seconds"] = 30
        self.write_config()
        env = {"WK_COLOR_OWNER": "owner-a", "WK_COLOR_TABS": ""}
        first = self.run_script(CLAIM, "--slug", "green", env=env)
        self.assertEqual(first.returncode, 0, first.stderr)
        lock = self.lock_dir / "green.lock"
        os.utime(str(lock), (1, 1))
        os.utime(str(self.port_lock_dir / "6123.lock"), (1, 1))

        reclaimed = self.run_script(CLAIM, "--slug", "green", env=env)

        self.assertEqual(reclaimed.returncode, 0, reclaimed.stderr)
        self.assertEqual((lock / "owner").read_text(encoding="utf-8"), "owner-a\n")
        self.assertEqual(list(self.lock_dir.glob(".wk-reap-*")), [])

    def test_different_projects_cannot_claim_the_same_port_before_bind(self):
        first_env = {"WK_COLOR_OWNER": "owner-a", "WK_COLOR_TABS": ""}
        first = self.run_script(CLAIM, "--slug", "green", env=first_env)
        self.assertEqual(first.returncode, 0, first.stderr)

        other_lock_dir = self.base / "other-locks"
        other_config = self.base / "other-config.json"
        other_value = dict(self.config)
        other_value["lock_dir"] = str(other_lock_dir)
        other_config.write_text(
            json.dumps(other_value, ensure_ascii=False), encoding="utf-8"
        )
        second = self.run_script(
            CLAIM,
            "--slug",
            "green",
            env={
                "WK_CONFIG": str(other_config),
                "WK_COLOR_OWNER": "owner-b",
                "WK_COLOR_TABS": "",
            },
        )

        self.assertEqual(second.returncode, 1, second.stderr)
        self.assertFalse((other_lock_dir / "green.lock").exists())
        self.assertTrue((self.port_lock_dir / "6123.lock").is_dir())

    def test_malformed_port_reservation_fails_closed_on_release(self):
        common = {"WK_COLOR_OWNER": "owner-a", "WK_COLOR_TABS": ""}
        claimed = self.run_script(CLAIM, "--slug", "green", env=common)
        self.assertEqual(claimed.returncode, 0, claimed.stderr)
        lock = self.lock_dir / "green.lock"
        port_lock = self.port_lock_dir / "6123.lock"
        unexpected = port_lock / "unexpected"
        unexpected.write_text("keep", encoding="utf-8")

        refused = self.run_script(RELEASE, "🟢", env=common)

        self.assertEqual(refused.returncode, 2)
        self.assertTrue(lock.is_dir())
        self.assertTrue(port_lock.is_dir())
        self.assertEqual(unexpected.read_text(encoding="utf-8"), "keep")

    def test_release_does_not_follow_symlink_lock(self):
        self.lock_dir.mkdir(mode=0o700)
        victim = self.base / "victim"
        victim.mkdir()
        sentinel = victim / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        link = self.lock_dir / "green.lock"
        try:
            link.symlink_to(victim, target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            self.skipTest("symbolic links unavailable: %s" % error)

        result = self.run_script(
            RELEASE,
            "🟢",
            env={"WK_COLOR_OWNER": "owner-a", "WK_COLOR_FORCE": "1"},
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("symbolic-link lock target", result.stderr)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_print_preview_normalizes_scheme_and_hostname(self):
        result = self.run_script(
            OPEN_PREVIEW, "LOCALHOST:6123/path?view=review#point"
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(),
            "Open in your browser: http://localhost:6123/path?view=review#point",
        )

    def test_print_close_preview_requests_manual_cleanup(self):
        close_result = self.run_script(
            OPEN_PREVIEW, "--close", "LOCALHOST:6123/path?view=review#point"
        )
        self.assertEqual(close_result.returncode, 0, close_result.stderr)
        self.assertEqual(
            close_result.stdout.strip(),
            "Close preview in your browser: http://localhost:6123/path?view=review#point",
        )

    def test_print_preview_removes_default_http_and_https_ports(self):
        cases = (
            ("HTTP://LOCALHOST:80/path", "http://localhost/path"),
            ("https://EXAMPLE.test:443/review", "https://example.test/review"),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                result = self.run_script(OPEN_PREVIEW, value)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "Open in your browser: " + expected)

    def test_preview_rejects_unsafe_or_unsupported_urls(self):
        invalid = (
            "file:///tmp/site.html",
            "javascript:alert(1)",
            "http://user:secret@localhost:6123/",
            "http://localhost:70000/",
            "http://localhost:6123/a b",
        )
        for value in invalid:
            with self.subTest(value=value):
                result = self.run_script(OPEN_PREVIEW, value)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("open-preview.sh:", result.stderr)

    def test_applescript_preview_uses_exact_normalized_origin(self):
        self.config["browser"]["mode"] = "applescript"
        self.config["browser"]["app_name"] = 'Brave "Beta" \\ Browser'
        self.write_config()
        tools_dir = self.base / "tools"
        tools_dir.mkdir()
        capture = self.base / "osascript-capture.txt"
        fake_osascript = tools_dir / "osascript"
        fake_osascript.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$@\" > \"$WK_TEST_CAPTURE\"\n"
            "printf '%s\\n' '__SCRIPT__' >> \"$WK_TEST_CAPTURE\"\n"
            "/bin/cat >> \"$WK_TEST_CAPTURE\"\n",
            encoding="utf-8",
        )
        fake_osascript.chmod(0o755)
        env = {
            "PATH": str(tools_dir) + os.pathsep + os.environ.get("PATH", ""),
            "WK_TEST_CAPTURE": str(capture),
        }

        result = self.run_script(
            OPEN_PREVIEW, "HTTP://LOCALHOST:5311/review", env=env
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        captured = capture.read_text(encoding="utf-8")
        self.assertTrue(captured.startswith("-\nhttp://localhost:5311/review\nhttp://localhost:5311\n"))
        self.assertIn("tabURL is matchOrigin", captured)
        self.assertNotIn("contains matchKey", captured)
        self.assertIn('tell application "Brave \\"Beta\\" \\\\ Browser"', captured)

    def test_applescript_close_preview_closes_without_reopening(self):
        self.config["browser"]["mode"] = "applescript"
        self.write_config()
        tools_dir = self.base / "close-tools"
        tools_dir.mkdir()
        capture = self.base / "close-osascript-capture.txt"
        fake_osascript = tools_dir / "osascript"
        fake_osascript.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$@\" > \"$WK_TEST_CAPTURE\"\n"
            "printf '%s\\n' '__SCRIPT__' >> \"$WK_TEST_CAPTURE\"\n"
            "/bin/cat >> \"$WK_TEST_CAPTURE\"\n",
            encoding="utf-8",
        )
        fake_osascript.chmod(0o755)
        env = {
            "PATH": str(tools_dir) + os.pathsep + os.environ.get("PATH", ""),
            "WK_TEST_CAPTURE": str(capture),
        }

        result = self.run_script(
            OPEN_PREVIEW, "--close", "HTTP://LOCALHOST:5311/review", env=env
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        captured = capture.read_text(encoding="utf-8")
        self.assertTrue(
            captured.startswith(
                "-\nhttp://localhost:5311/review\nhttp://localhost:5311\nclose\n"
            )
        )
        self.assertIn('if requestedAction is not "close" then', captured)

    def test_applescript_preview_rejects_control_characters_in_app_name(self):
        self.config["browser"] = {
            "mode": "applescript",
            "app_name": "Google Chrome\nend tell",
        }
        self.write_config()

        result = self.run_script(OPEN_PREVIEW, "http://localhost:5311/")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("control or newline", result.stderr)

    def test_wait_for_file_reports_valid_missing_and_invalid_json(self):
        valid = self.base / "valid.json"
        valid.write_text('{"ok": true}', encoding="utf-8")
        arrived = self.run_script(WAIT_FOR_FILE, str(valid), "0", "1")
        self.assertEqual(arrived.returncode, 0, arrived.stderr)
        self.assertEqual(arrived.stdout.strip(), str(valid))

        missing = self.run_script(
            WAIT_FOR_FILE, str(self.base / "missing.json"), "0", "1"
        )
        self.assertEqual(missing.returncode, 124)
        self.assertIn("waiting for", missing.stderr)

        invalid = self.base / "invalid.json"
        invalid.write_text("{", encoding="utf-8")
        malformed = self.run_script(WAIT_FOR_FILE, str(invalid), "0", "1")
        self.assertEqual(malformed.returncode, 65)
        self.assertIn("does not parse as JSON", malformed.stderr)

    def test_wait_for_file_requires_a_strict_json_object(self):
        invalid_values = ("[]", '"text"', "null", "true", "NaN", "Infinity", "-Infinity")
        for index, value in enumerate(invalid_values):
            with self.subTest(value=value):
                path = self.base / ("strict-%d.json" % index)
                path.write_text(value, encoding="utf-8")
                result = self.run_script(WAIT_FOR_FILE, str(path), "0", "1")
                self.assertEqual(result.returncode, 65)
                self.assertIn("does not parse as JSON", result.stderr)

    def test_wait_for_file_rejects_unpaired_surrogates_and_accepts_pairs(self):
        for index, payload in enumerate((
            r'{"value":"\ud800"}',
            r'{"\udfff":"value"}',
        )):
            with self.subTest(payload=payload):
                path = self.base / ("surrogate-%d.json" % index)
                path.write_text(payload, encoding="utf-8")
                result = self.run_script(WAIT_FOR_FILE, str(path), "0", "1")
                self.assertEqual(result.returncode, 65)
                self.assertIn("does not parse as JSON", result.stderr)

        valid = self.base / "emoji-pair.json"
        valid.write_text(r'{"emoji":"\ud83d\ude00"}', encoding="utf-8")
        result = self.run_script(WAIT_FOR_FILE, str(valid), "0", "1")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_wait_for_file_detects_in_place_mutation_metadata(self):
        path = self.base / "changing.json"
        path.write_text('{"ok": true}', encoding="utf-8")
        modules = self.base / "modules"
        modules.mkdir()
        (modules / "sitecustomize.py").write_text(
            "import os\n"
            "import types\n"
            "_real_fstat = os.fstat\n"
            "_calls = []\n"
            "def _changed_fstat(descriptor):\n"
            "    value = _real_fstat(descriptor)\n"
            "    _calls.append(value)\n"
            "    if len(_calls) != 2:\n"
            "        return value\n"
            "    return types.SimpleNamespace(st_mode=value.st_mode, st_dev=value.st_dev, "
            "st_ino=value.st_ino, st_size=value.st_size + 1, st_mtime_ns=value.st_mtime_ns)\n"
            "os.fstat = _changed_fstat\n",
            encoding="utf-8",
        )

        result = self.run_script(
            WAIT_FOR_FILE,
            str(path),
            "30",
            "1",
            timeout=3,
            env={"PYTHONPATH": str(modules)},
        )

        self.assertEqual(result.returncode, 66)
        self.assertIn("changed while it was read", result.stderr)

    def test_wait_for_file_rejects_invalid_timing_values(self):
        for timeout, interval in (("-1", "1"), ("no", "1"), ("1", "0"), ("1", "0.5")):
            with self.subTest(timeout=timeout, interval=interval):
                result = self.run_script(
                    WAIT_FOR_FILE,
                    str(self.base / "missing.json"),
                    timeout,
                    interval,
                )
                self.assertEqual(result.returncode, 2)

    def test_wait_for_file_rejects_special_paths_without_blocking(self):
        regular = self.base / "regular.json"
        regular.write_text('{"ok": true}', encoding="utf-8")
        directory = self.base / "directory.json"
        directory.mkdir()
        symlink = self.base / "linked.json"
        try:
            symlink.symlink_to(regular)
        except (OSError, NotImplementedError) as error:
            self.skipTest("symbolic links unavailable: {}".format(error))

        paths = [directory, symlink]
        if hasattr(os, "mkfifo"):
            fifo = self.base / "pipe.json"
            os.mkfifo(str(fifo))
            paths.append(fifo)
        for path in paths:
            with self.subTest(path=path.name):
                result = self.run_script(
                    WAIT_FOR_FILE, str(path), "30", "1", timeout=3
                )
                self.assertEqual(result.returncode, 66)
                self.assertIn("unsafe protocol path", result.stderr)


if __name__ == "__main__":
    unittest.main()
