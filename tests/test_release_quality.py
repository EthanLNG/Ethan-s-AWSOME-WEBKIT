import json
import re
import shutil
import stat
import subprocess
import unittest
import warnings
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
IGNORED_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".webkit",
    "__pycache__",
}
MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
SHELL_BLOCK = re.compile(
    r"^[ \t]*```sh[ \t]*\n(.*?)^[ \t]*```[ \t]*$",
    re.DOTALL | re.MULTILINE,
)


def source_files(suffixes):
    return sorted(
        path
        for path in ROOT.rglob("*")
        if path.is_file()
        and path.suffix in suffixes
        and not IGNORED_PARTS.intersection(path.parts)
    )


class InlineScriptCollector(HTMLParser):
    def __init__(self):
        HTMLParser.__init__(self)
        self.current = None
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "script":
            return
        values = dict(attrs)
        script_type = values.get("type", "").lower()
        if "src" not in values and script_type in ("", "text/javascript"):
            self.current = []

    def handle_data(self, data):
        if self.current is not None:
            self.current.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "script" and self.current is not None:
            self.scripts.append("".join(self.current))
            self.current = None


class ReleaseQualityTests(unittest.TestCase):
    def test_runtime_and_tool_caches_are_not_release_inputs(self):
        for name in (".webkit", ".mypy_cache", ".pytest_cache", ".ruff_cache"):
            self.assertIn(name, IGNORED_PARTS)
        for path in source_files({".html", ".js", ".json", ".md", ".py"}):
            with self.subTest(path=str(path.relative_to(ROOT))):
                self.assertFalse(IGNORED_PARTS.intersection(path.parts))

    def test_every_python_source_compiles(self):
        paths = source_files({".py"})
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(path=str(path.relative_to(ROOT))):
                source = path.read_text(encoding="utf-8")
                with warnings.catch_warnings():
                    warnings.simplefilter("error", SyntaxWarning)
                    compile(source, str(path), "exec")

    def test_every_json_document_parses(self):
        paths = source_files({".json"})
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(path=str(path.relative_to(ROOT))):
                with path.open(encoding="utf-8") as handle:
                    json.load(
                        handle,
                        parse_constant=lambda value: (_ for _ in ()).throw(
                            ValueError("Nonstandard JSON constant: {}".format(value))
                        ),
                    )

    def test_local_markdown_links_resolve(self):
        for path in source_files({".md"}):
            source = path.read_text(encoding="utf-8")
            for match in MARKDOWN_LINK.finditer(source):
                target = match.group(1).strip().strip("<>")
                parsed = urlsplit(target)
                if parsed.scheme or target.startswith("#"):
                    continue
                local = unquote(parsed.path)
                if not local:
                    continue
                with self.subTest(path=str(path.relative_to(ROOT)), target=target):
                    self.assertTrue((path.parent / local).exists())

    def test_version_and_changelog_agree(self):
        version = (ROOT / "webkit" / "VERSION").read_text(encoding="utf-8").strip()
        self.assertRegex(version, r"^\d+\.\d+\.\d+$")
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        heading = re.search(r"^## v([^\s]+)", changelog, re.MULTILINE)
        self.assertIsNotNone(heading)
        self.assertEqual(heading.group(1), version)

    def test_control_center_phone_header_and_muted_text_meet_release_contract(self):
        styles = (ROOT / "control-center" / "static" / "styles.css").read_text(
            encoding="utf-8"
        )
        html = (ROOT / "control-center" / "static" / "index.html").read_text(
            encoding="utf-8"
        )
        app = (ROOT / "control-center" / "static" / "app.js").read_text(
            encoding="utf-8"
        )

        variables = dict(
            re.findall(r"--([a-z-]+):\s*(#[0-9a-fA-F]{6});", styles)
        )
        self.assertIn("muted", variables)

        def luminance(value):
            channels = [int(value[index:index + 2], 16) / 255.0 for index in (1, 3, 5)]
            linear = [
                channel / 12.92
                if channel <= 0.04045
                else ((channel + 0.055) / 1.055) ** 2.4
                for channel in channels
            ]
            return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

        def contrast(first, second):
            lighter, darker = sorted(
                (luminance(first), luminance(second)), reverse=True
            )
            return (lighter + 0.05) / (darker + 0.05)

        backgrounds = [
            variables["paper"],
            variables["panel"],
            "#ece7dd",
            "#eee9df",
            "#f7f4ed",
            "#edf7ff",
        ]
        for background in backgrounds:
            with self.subTest(background=background):
                self.assertGreaterEqual(contrast(variables["muted"], background), 4.5)

        self.assertIn("@media (max-width: 480px)", styles)
        mobile = styles.split("@media (max-width: 480px)", 1)[1]
        self.assertIn(".shortcut-guide { display: none; }", mobile)
        self.assertIn(".topbar-action-label { display: none; }", mobile)
        self.assertIn("width: 40px", mobile)
        self.assertIn('aria-label="Settings"', html)
        self.assertIn('aria-label="Add project"', html)
        self.assertIn("info.supported === false", app)
        self.assertIn("Git ${minimumGit}+ required", app)

    def test_release_docs_state_git_csp_and_integration_boundaries(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        setup = (ROOT / "webkit" / "SETUP.md").read_text(encoding="utf-8")
        loop = (ROOT / "webkit" / "LOOP.md").read_text(encoding="utf-8")
        controller = (ROOT / "webkit" / "CONTROL-CENTER.md").read_text(
            encoding="utf-8"
        )

        for label, source in (
            ("README", readme),
            ("SETUP", setup),
            ("LOOP", loop),
            ("CONTROL-CENTER", controller),
        ):
            with self.subTest(document=label):
                self.assertIn("Git 2.30 or newer", source)
                self.assertRegex(source, r"security\s+audit")

        self.assertIn("HTTP response header", readme)
        self.assertIn("HTTP response header", setup)
        self.assertIn("managed checkout", readme)
        self.assertIn("managed checkout", controller)
        self.assertIn("preserves both the managed result and session worktree", readme)
        self.assertIn("remain available", controller)
        self.assertIn("outdated Git installation", changelog)
        self.assertIn("Live and Git-backed BEFORE HTML transformation", readme)
        self.assertIn("Bounded live", changelog)
        for limit in (
            "10,000 commits",
            "20,000 changed",
            "256 MiB of aggregate scanned blob prefixes",
            "2 MiB per blob",
        ):
            with self.subTest(secret_scan_limit=limit):
                self.assertIn(limit, readme)
        self.assertIn("Staged commits and session integrations are refused", readme)

    def test_release_keeps_the_proprietary_license_notice(self):
        license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("AWESOME WEBKIT PROPRIETARY LICENSE", license_text)
        self.assertIn("All rights reserved", license_text)
        self.assertIn("NO GENERAL LICENSE GRANT", license_text)
        self.assertNotIn("Permission is hereby granted", license_text)
        self.assertIn("Proprietary. All rights reserved.", readme)

    def test_install_and_session_shell_blocks_fail_closed(self):
        documents = {
            "AGENTS.md": (ROOT / "AGENTS.md").read_text(encoding="utf-8"),
            "README.md": (ROOT / "README.md").read_text(encoding="utf-8"),
            "webkit/SETUP.md": (ROOT / "webkit" / "SETUP.md").read_text(
                encoding="utf-8"
            ),
        }
        for name, source in documents.items():
            blocks = SHELL_BLOCK.findall(source)
            self.assertTrue(blocks, name)
            for index, block in enumerate(blocks, 1):
                with self.subTest(document=name, block=index):
                    self.assertRegex(block, r"^\s*set -eu(?:\n|$)")
                    bash = shutil.which("bash")
                    if bash is not None:
                        parsed = subprocess.run(
                            [bash, "-n"],
                            input=block,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            universal_newlines=True,
                            check=False,
                        )
                        self.assertEqual(parsed.returncode, 0, parsed.stderr)

        install = documents["AGENTS.md"]
        self.assertNotIn("rsync", install)
        for command in (
            '--output="$WK_INSTALL_TMP/webkit.tar" "$WK_SOURCE_COMMIT" -- webkit || exit 1',
            'tar -xf "$WK_INSTALL_TMP/webkit.tar" -C "$WK_INSTALL_TMP/source" || exit 1',
            'cp -pR "$WK_INSTALL_TMP/source/webkit/$WK_ITEM" "$WK_PROJECT/webkit/" || exit 1',
        ):
            with self.subTest(fail_closed_command=command):
                self.assertIn(command, install)
        self.assertIn(': > "$WK_INSTALL_TMP/claude-skills-added" || exit 1', install)
        self.assertIn(
            'cp -pR "$WK_INSTALL_TMP/source/webkit/skills/$WK_SKILL"', install
        )
        self.assertIn('done < "$WK_INSTALL_TMP/claude-skills-added"', install)
        for skill in ("abc", "webkit-setup", "webkit-loop"):
            with self.subTest(claude_skill=skill):
                self.assertIn(".claude/skills/" + skill, install)
        self.assertNotIn("git -C \"$WK_PROJECT\" add -- webkit AGENTS.md", install)

        setup = documents["webkit/SETUP.md"]
        self.assertIn('session_info="$(webkit/scripts/claim-color.sh --session)" || exit 1', setup)
        self.assertIn("read -r session_state color slug port server_pid server_instance", setup)
        self.assertIn("active)", setup)
        self.assertIn("claimed)", setup)
        self.assertIn("reused_server=1", setup)
        self.assertIn("reused_server=0", setup)
        self.assertIn('tracked_runtime="$(git ls-files', setup)
        self.assertNotIn('test -z "$(git ls-files', setup)

        readme = documents["README.md"]
        self.assertIn("python3 control-center/install-shortcut.py", readme)
        self.assertIn("py -3 control-center\\install-shortcut.py", readme)

    def test_update_and_loop_docs_preserve_fail_closed_guards(self):
        update = (ROOT / "webkit" / "UPDATE-KIT.md").read_text(encoding="utf-8")
        loop = (ROOT / "webkit" / "LOOP.md").read_text(encoding="utf-8")

        self.assertIn("for WK_TOOL in python3 git tar diff cmp rsync; do", update)
        self.assertIn('command -v "$WK_TOOL"', update)
        self.assertIn('WK_UPSTREAM_STATUS="$(git -C "$WK_UPSTREAM" status', update)
        self.assertIn('WK_PROJECT_STATUS="$(git -C "$WK_PROJECT" status', update)
        self.assertNotIn('test -z "$(git -C "$WK_UPSTREAM" status', update)
        self.assertNotIn('test -z "$(git -C "$WK_PROJECT" status', update)

        self.assertIn(
            'feedback_dir="$(webkit/scripts/config-get.sh get feedback_dir)" || exit 1',
            loop,
        )
        self.assertIn('wk_round_status="$(git status', loop)
        self.assertNotIn('test -z "$(git status', loop)
        self.assertIn('beforeRef="$(git rev-parse --verify \'HEAD^{commit}\')" || exit 1', loop)
        self.assertIn('umask 077', loop)
        self.assertIn('mktemp "$inbox/.review.json.XXXXXX"', loop)
        self.assertIn("trap 'test -z", loop)
        self.assertNotIn("$inbox/review.json.tmp", loop)

    def test_runtime_and_private_project_files_are_not_tracked(self):
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            check=True,
        )
        tracked = result.stdout.splitlines()
        forbidden_names = {".DS_Store", "webkit.config.json"}
        for name in tracked:
            parts = Path(name).parts
            with self.subTest(path=name):
                self.assertFalse("__pycache__" in parts or name.endswith((".pyc", ".pyo")))
                self.assertNotIn(Path(name).name, forbidden_names)
                self.assertFalse(name == ".webkit" or name.startswith(".webkit/"))

    def test_runtime_entry_points_are_executable_in_git(self):
        result = subprocess.run(
            ["git", "ls-files", "--stage"],
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            check=True,
        )
        modes = {}
        for line in result.stdout.splitlines():
            metadata, name = line.split("\t", 1)
            modes[name] = metadata.split()[0]
        expected = {
            "AWESOME WEBKIT.command",
            "launch-control-center.sh",
            "control-center/install-shortcut.py",
            "control-center/launch.py",
            "control-center/server.py",
            "webkit/scripts/claim-color.sh",
            "webkit/scripts/config-get.sh",
            "webkit/scripts/open-preview.sh",
            "webkit/scripts/release-color.sh",
            "webkit/scripts/runtime_registry.py",
            "webkit/scripts/transcribe-voice-note.py",
            "webkit/scripts/transition-round.py",
            "webkit/scripts/wait-for-file.sh",
            "webkit/server/preview-server.py",
        }
        for name in expected:
            with self.subTest(path=name):
                if name in modes:
                    self.assertEqual(modes[name], "100755")
                else:
                    path = ROOT / name
                    self.assertTrue(path.is_file())
                    self.assertTrue(stat.S_IMODE(path.stat().st_mode) & 0o111)

    def test_repository_has_no_tracked_symlinks_or_submodules(self):
        result = subprocess.run(
            ["git", "ls-files", "--stage"],
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            check=True,
        )
        for line in result.stdout.splitlines():
            metadata, name = line.split("\t", 1)
            mode = metadata.split()[0]
            with self.subTest(path=name):
                self.assertNotIn(mode, {"120000", "160000"})

    def test_repository_artifacts_do_not_contain_em_dash_character(self):
        paths = sorted(
            path
            for path in ROOT.rglob("*")
            if path.is_file() and not IGNORED_PARTS.intersection(path.parts)
        )
        for path in paths:
            with self.subTest(path=str(path.relative_to(ROOT))):
                self.assertNotIn(b"\xe2\x80\x94", path.read_bytes())

    def test_javascript_sources_and_fixtures_parse(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is unavailable")

        sources = [
            (str(path.relative_to(ROOT)), path.read_text(encoding="utf-8"))
            for path in source_files({".js"})
        ]
        for path in source_files({".html"}):
            parser = InlineScriptCollector()
            parser.feed(path.read_text(encoding="utf-8"))
            parser.close()
            for index, script in enumerate(parser.scripts, 1):
                label = "{} inline script {}".format(path.relative_to(ROOT), index)
                sources.append((label, script))

        self.assertTrue(sources)
        for label, source in sources:
            with self.subTest(source=label):
                result = subprocess.run(
                    [node, "--check", "-"],
                    input=source,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_shell_sources_parse(self):
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("Bash is unavailable")

        paths = source_files({".command", ".sh"})
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(path=str(path.relative_to(ROOT))):
                result = subprocess.run(
                    [bash, "-n"],
                    input=path.read_text(encoding="utf-8"),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    universal_newlines=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_cross_platform_line_endings_are_declared(self):
        attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
        required = {
            "*.sh text eol=lf",
            "*.command text eol=lf",
            "*.cmd text eol=crlf",
        }
        self.assertTrue(required.issubset(set(attributes.splitlines())))

    def test_workflow_actions_use_immutable_release_commits(self):
        workflow = (ROOT / ".github" / "workflows" / "quality.yml").read_text(
            encoding="utf-8"
        )
        actions = []
        for line in workflow.splitlines():
            match = re.match(r"\s*uses:\s+([^\s#]+)", line)
            if match and not match.group(1).startswith("./"):
                actions.append(match.group(1))
        self.assertTrue(actions)
        for action in actions:
            with self.subTest(action=action):
                self.assertRegex(action, r"^[^@\s]+@[0-9a-f]{40}$")

    def test_workflow_covers_all_supported_desktop_platforms(self):
        workflow = (ROOT / ".github" / "workflows" / "quality.yml").read_text(
            encoding="utf-8"
        )
        for runner in ("ubuntu-22.04", "windows-latest", "macos-15"):
            with self.subTest(runner=runner):
                self.assertIn("runs-on: " + runner, workflow)
        self.assertIn("BASH_VERSINFO", workflow)
        self.assertIn("-exec /bin/bash -n {} +", workflow)


if __name__ == "__main__":
    unittest.main(verbosity=2)
