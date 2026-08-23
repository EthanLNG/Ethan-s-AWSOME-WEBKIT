import io
import json
import base64
import hashlib
import http.client
import os
import re
import signal
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import uuid
from pathlib import Path
from unittest import mock


CONTROL_DIR = Path(__file__).resolve().parents[1]
KIT_ROOT = CONTROL_DIR.parent
sys.path.insert(0, str(CONTROL_DIR))

import control_center as control_center_module  # noqa: E402
from control_center import (  # noqa: E402
    ControlCenterError,
    ControlCenter,
    ProjectMutationJournal,
    EventLog,
    ProviderRunner,
    SessionRuntime,
    SessionManager,
    attach_windows_kill_job,
    atomic_write_json,
    choose_folder,
    configured_lock_dir,
    load_webkit_config,
    read_stable_regular_text,
    sanitize_remote_url,
    slugify,
    stop_process_tree,
    strict_json_loads,
    exclusive_copy_file,
)
import server as control_center_server  # noqa: E402
from server import ControlCenterHTTPServer, Handler as ControlCenterHandler  # noqa: E402


class FakeProcess:
    def __init__(self, stdout_lines, code=0, stderr_lines=None):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO("".join(stdout_lines))
        self.stderr = io.StringIO("".join(stderr_lines or []))
        self._code = code

    def wait(self):
        return self._code

    def poll(self):
        return self._code


def assemble_test_bytes(*parts):
    return b"".join(parts)


class ControlCenterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runtime_env = mock.patch.dict(
            os.environ,
            {"WK_PORT_LOCKDIR": str(self.root / "runtime-port-locks")},
            clear=False,
        )
        self.runtime_env.start()
        self.state_dir = self.root / "state"
        self.app = ControlCenter(KIT_ROOT, self.state_dir)
        self.app.store.update(lambda state: state.update({"providers": ["codex", "claude"]}))

    def tearDown(self):
        try:
            self.app.sessions.shutdown()
        finally:
            self.runtime_env.stop()
            if os.name == "nt":
                if os.path.exists(self.temp.name):
                    shutil.rmtree(
                        self.temp.name,
                        onerror=self._remove_windows_readonly_path,
                    )
                self.temp._finalizer.detach()
            else:
                self.temp.cleanup()

    @staticmethod
    def _remove_windows_readonly_path(function, path, _error):
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        function(path)

    def _make_discard_session(self, suffix):
        repository = self.root / ("discard-repository-" + suffix)
        repository.mkdir()
        subprocess.run(
            ["git", "init", "-b", "main"], cwd=repository,
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"], cwd=repository, check=True
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.invalid"],
            cwd=repository, check=True,
        )
        (repository / "index.html").write_text("safe\n", encoding="utf-8")
        subprocess.run(["git", "add", "index.html"], cwd=repository, check=True)
        subprocess.run(
            ["git", "commit", "-m", "base"], cwd=repository,
            check=True, capture_output=True,
        )
        base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repository,
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        branch = "webkit/blue/discard-{}".format(suffix)
        worktree = self.root / ("discard-worktree-" + suffix)
        ownership = self.app.projects._create_owned_worktree(
            repository, worktree, branch, base_sha
        )
        project_id = "discard-project-" + suffix
        session_id = "discard-session-" + suffix
        project = {"id": project_id, "path": str(repository)}
        session = {
            "id": session_id,
            "projectId": project_id,
            "branch": branch,
            "worktree": str(worktree),
            "baseSha": base_sha,
            "worktreeDev": ownership["dev"],
            "worktreeIno": ownership["ino"],
            "status": "active",
        }
        self.app.store.update(lambda state: (
            state.setdefault("projects", []).append(project),
            state.setdefault("sessions", []).append(session),
        ))
        return repository, worktree, branch, base_sha, session_id

    def test_slugify(self):
        self.assertEqual(slugify("My Cool Site!"), "my-cool-site")
        self.assertEqual(slugify("***"), "website")
        self.assertEqual(slugify("CON"), "website-con")
        self.assertEqual(slugify("LPT1"), "website-lpt1")

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits required")
    def test_state_directory_and_file_are_private(self):
        self.assertEqual(stat.S_IMODE(self.state_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.app.store.path.stat().st_mode), 0o600)
        self.app.store.update(lambda state: state.update({"permissionCheck": True}))
        self.assertEqual(stat.S_IMODE(self.app.store.path.stat().st_mode), 0o600)

    def test_terminal_sessions_and_logs_are_pruned_to_a_fixed_retention(self):
        sessions = []
        for index in range(55):
            session_id = "terminal-{:02d}".format(index)
            sessions.append({
                "id": session_id,
                "status": "merged" if index % 2 else "discarded",
                "updatedAt": "2026-08-19T00:{:02d}:00Z".format(index),
            })
            EventLog(self.state_dir, session_id).append("system", "finished")
        sessions.append({"id": "active-one", "status": "active"})
        self.app.store.update(
            lambda state: state.update({"sessions": sessions})
        )

        retained = self.app.store.read()["sessions"]
        terminal = [
            session for session in retained
            if session.get("status") in ("merged", "discarded")
        ]
        self.assertEqual(len(terminal), 50)
        self.assertIn("active-one", {session["id"] for session in retained})
        for index in range(5):
            self.assertFalse(
                (self.state_dir / "logs" / "terminal-{:02d}.jsonl".format(index)).exists()
            )
        self.assertTrue((self.state_dir / "logs" / "terminal-54.jsonl").is_file())

    def test_state_size_limit_rejects_growth_without_overwriting_state(self):
        before = self.app.store.path.read_bytes()
        with mock.patch("control_center.MAX_STATE_BYTES", 1024):
            with self.assertRaisesRegex(ControlCenterError, "safety limit") as raised:
                self.app.store.update(
                    lambda state: state.update({"oversized": "x" * 2000})
                )
        self.assertEqual(raised.exception.status, 507)
        self.assertEqual(self.app.store.path.read_bytes(), before)

    def test_bounded_regular_reader_rejects_symlink_oversize_and_replacement(self):
        target = self.root / "agent-result.json"
        target.write_text('{"status":"ready"}\n', encoding="utf-8")
        linked = self.root / "linked-result.json"
        try:
            linked.symlink_to(target)
        except (OSError, NotImplementedError) as exc:
            self.skipTest("symbolic links unavailable: {}".format(exc))
        with self.assertRaisesRegex(ControlCenterError, "regular file"):
            read_stable_regular_text(linked, 1024, "Agent result")

        oversized = self.root / "oversized-result.json"
        oversized.write_bytes(b"x" * 1025)
        with self.assertRaisesRegex(ControlCenterError, "larger than") as raised:
            read_stable_regular_text(oversized, 1024, "Agent result")
        self.assertEqual(raised.exception.status, 413)

        if os.name != "nt":
            replacement = self.root / "replacement.json"
            replacement.write_text(
                '{"status":"conflict"}\n', encoding="utf-8"
            )
            real_read = os.read
            replaced = []

            def replace_after_read(descriptor, size):
                data = real_read(descriptor, size)
                if not replaced:
                    replaced.append(True)
                    os.replace(str(replacement), str(target))
                return data

            with mock.patch(
                "control_center.os.read", side_effect=replace_after_read
            ):
                with self.assertRaisesRegex(
                    ControlCenterError, "changed while it was read"
                ):
                    read_stable_regular_text(target, 1024, "Agent result")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO files unavailable")
    def test_bounded_regular_reader_rejects_fifo_without_opening_it(self):
        fifo = self.root / "agent-result.fifo"
        os.mkfifo(str(fifo))
        started = time.monotonic()
        with self.assertRaisesRegex(ControlCenterError, "regular file"):
            read_stable_regular_text(fifo, 1024, "Agent result")
        self.assertLess(time.monotonic() - started, 1)

    def test_public_session_payloads_redact_preview_mutation_tokens(self):
        project = {
            "id": "project-1", "name": "Project", "slug": "project",
            "path": str(self.root), "provider": "codex",
            "onboarding": {"sessionId": "session-1", "seedCount": 10},
        }
        session = {
            "id": "session-1", "projectId": "project-1", "status": "active",
            "color": "blue", "mutationToken": "private-preview-token",
        }
        self.app.store.update(lambda state: (
            state.setdefault("projects", []).append(project),
            state.setdefault("sessions", []).append(session),
        ))
        with mock.patch.object(self.app.projects, "github_sync_status", return_value={}):
            bootstrap = self.app.bootstrap()
        self.assertIn("mutationToken", self.app.store.read()["sessions"][0])
        self.assertNotIn("mutationToken", self.app.sessions.list_sessions()[0])
        self.assertNotIn("mutationToken", bootstrap["sessions"][0])
        self.assertNotIn("mutationToken", bootstrap["projects"][0]["sessions"][0])
        self.assertNotIn(
            "mutationToken", self.app.start_project_seeds("project-1")["seedSession"]
        )

    def test_macos_folder_picker_returns_selected_absolute_path(self):
        selected = self.root / "chosen"
        selected.mkdir()
        completed = mock.Mock(returncode=0, stdout=str(selected) + "/\n", stderr="")
        with mock.patch("control_center.platform.system", return_value="Darwin"), mock.patch(
            "control_center.subprocess.run", return_value=completed
        ) as run:
            self.assertEqual(choose_folder(str(self.root), "Choose a project"), str(selected.resolve()))
        command = run.call_args[0][0]
        self.assertEqual(command[0], "osascript")
        self.assertEqual(command[-2:], ["Choose a project", str(self.root)])

    def test_folder_picker_cancel_is_not_an_error(self):
        completed = mock.Mock(returncode=0, stdout="__WK_CANCELLED__\n", stderr="")
        with mock.patch("control_center.platform.system", return_value="Darwin"), mock.patch(
            "control_center.subprocess.run", return_value=completed
        ):
            self.assertIsNone(choose_folder(str(self.root)))

    def test_create_project_installs_kit_and_initial_commit(self):
        parent = self.root / "projects"
        parent.mkdir()
        with mock.patch.object(self.app.projects, "_find_port_block", return_value=6311):
            project = self.app.projects.create_project("Demo Site", str(parent), "codex")
        project_path = Path(project["path"])
        self.assertTrue((project_path / "index.html").exists())
        self.assertTrue((project_path / "webkit" / "CONTROL-CENTER.md").exists())
        self.assertEqual((project_path / "webkit" / "VERSION").read_text().strip(), "0.8.16")
        self.assertIn("WK_CONTROL_CENTER=1", (project_path / "AGENTS.md").read_text())
        config = json.loads((project_path / "webkit" / "webkit.config.json").read_text())
        self.assertEqual(config["project_name"], "demo-site")
        self.assertEqual(config["default_page"], "index.html")
        self.assertEqual(config["dictation"]["mode"], "speech")
        self.assertEqual(config["interaction"]["mode"], "browse-default")
        self.assertEqual(config["hotkeys"], {"toggle": "KeyC", "dictate": "KeyV"})
        log = subprocess.run(["git", "log", "-1", "--pretty=%s"], cwd=project_path, text=True, capture_output=True, check=True)
        self.assertEqual(log.stdout.strip(), "Create website with AWESOME WEBKIT")
        self.assertFalse(subprocess.run(["git", "status", "--porcelain"], cwd=project_path, text=True, capture_output=True, check=True).stdout)

    def test_same_basename_repositories_get_distinct_color_lock_namespaces(self):
        repositories = []
        for parent_name in ("one", "two"):
            repository = self.root / parent_name / "site"
            repository.mkdir(parents=True)
            (repository / "index.html").write_text("<title>Site</title>\n", encoding="utf-8")
            subprocess.run(
                ["git", "init", "-b", "main"], cwd=repository,
                check=True, capture_output=True,
            )
            repositories.append(repository)
        with mock.patch.object(
            self.app.projects, "_find_port_block", side_effect=[6401, 6411]
        ):
            first = self.app.projects._make_config(repositories[0])
            second = self.app.projects._make_config(repositories[1])
        self.assertEqual(first["project_name"], "site")
        self.assertEqual(second["project_name"], "site")
        self.assertTrue(first["lock_dir"].startswith("/tmp/"))
        self.assertTrue(second["lock_dir"].startswith("/tmp/"))
        self.assertNotEqual(first["lock_dir"], second["lock_dir"])

    def test_existing_config_rejects_another_projects_color_lock_namespace(self):
        shared_lock = self.root / "shared-color-locks"

        def make_project(name, port):
            project = self.root / name
            (project / "webkit").mkdir(parents=True)
            (project / "index.html").write_text("<title>Site</title>\n", encoding="utf-8")
            config = {
                "project_name": name,
                "site_root": ".",
                "default_page": "index.html",
                "lock_dir": str(shared_lock),
                "grace_seconds": 180,
                "palette": [{"slug": "blue", "emoji": "🔵", "port": port}],
            }
            path = project / "webkit" / "webkit.config.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            return project, path

        registered, _registered_config = make_project("registered", 6421)
        candidate, candidate_config = make_project("candidate", 6431)
        self.app.store.update(lambda state: state.setdefault("projects", []).append({
            "id": "registered", "name": "Registered", "slug": "registered",
            "path": str(registered), "provider": "codex",
        }))

        with self.assertRaisesRegex(ControlCenterError, "color locks"):
            self.app.projects._reserve_config_ports(candidate_config)
        self.assertTrue(candidate.is_dir())

    def test_new_project_onboarding_saves_optional_context_and_starts_seeds(self):
        parent = self.root / "projects"
        parent.mkdir()
        fake_session = {
            "id": "seed-session", "kind": "seeds", "status": "busy",
            "mutationToken": "private-preview-token",
        }
        onboarding = {
            "brief": "Warm, editorial, confident. Avoid generic startup gradients.",
            "seedCount": 7,
            "assets": [{
                "name": "brand mark.svg",
                "path": "brand-kit/logos/brand mark.svg",
                "type": "image/svg+xml",
                "data": base64.b64encode(b"<svg></svg>").decode("ascii"),
            }],
        }
        with mock.patch.object(self.app.projects, "_find_port_block", return_value=6316), mock.patch.object(
            self.app.projects, "_ensure_github_repo"
        ), mock.patch.object(
            self.app.sessions, "start_seed_session", return_value=fake_session
        ) as start:
            result = self.app.create_project("Seeded Site", str(parent), "codex", onboarding)
        project_path = Path(result["project"]["path"])
        self.assertIn("Warm, editorial", (project_path / "project-context" / "BRAND-AND-DESIGN.md").read_text())
        self.assertEqual(
            (project_path / "project-context" / "assets" / "brand-kit" / "logos" / "brand-mark.svg").read_bytes(),
            b"<svg></svg>",
        )
        self.assertEqual(
            result["seedSession"],
            {"id": "seed-session", "kind": "seeds", "status": "busy"},
        )
        start.assert_called_once_with(result["project"]["id"], 7, onboarding["brief"])

    def test_multibyte_brand_brief_is_preserved_and_seed_prompt_references_it(self):
        parent = self.root / "multibyte-projects"
        parent.mkdir()
        brief = "界" * 20000
        with mock.patch.object(
            self.app.projects, "_find_port_block", return_value=6325
        ), mock.patch.object(self.app.projects, "_ensure_github_repo"):
            project = self.app.projects.create_project(
                "Long Brief", str(parent), "codex", {"brief": brief}
            )
        context = Path(project["path"]) / "project-context" / "BRAND-AND-DESIGN.md"
        saved = context.read_text(encoding="utf-8")
        self.assertIn(brief, saved)
        self.assertGreater(len(brief.encode("utf-8")), 16 * 1024)

        runtime = mock.Mock()
        session = {
            "id": "long-brief-seeds", "projectId": project["id"],
            "projectName": project["name"], "provider": "codex", "color": "blue",
            "emoji": "🔵", "port": 6325, "branch": "webkit/blue/long-brief",
            "worktree": project["path"], "status": "active", "threadId": None,
            "hasRun": False, "createdAt": "2026-08-19T00:00:00Z",
        }
        runtime.session = session

        def start_session(_project_id, _color, _reasoning):
            self.app.store.update(
                lambda state: state.setdefault("sessions", []).append(dict(session))
            )
            self.app.sessions.runtimes[session["id"]] = runtime
            return dict(session)

        with mock.patch.object(
            self.app.sessions, "start_session", side_effect=start_session
        ) as start:
            self.app.sessions._start_seed_session(project["id"], 2, brief)
        prompt = runtime.enqueue.call_args[0][0]
        self.assertLessEqual(len(prompt.encode("utf-8")), 16 * 1024)
        self.assertIn("project-context/BRAND-AND-DESIGN.md", prompt)
        self.assertNotIn(brief, prompt)
        start.assert_called_once()

        too_long_path = self.root / "too-long-context"
        too_long_path.mkdir()
        with self.assertRaises(ControlCenterError) as raised:
            self.app.projects._save_project_context(
                too_long_path, {"brief": "界" * 20001}
            )
        self.assertEqual(raised.exception.status, 413)
        self.assertFalse((too_long_path / "project-context").exists())

    def test_project_reference_folder_cannot_escape_context_directory(self):
        payload = base64.b64encode(b"private").decode("ascii")
        with self.assertRaisesRegex(Exception, "unsafe folder path"):
            self.app.projects._save_project_context(self.root, {
                "assets": [{"name": "secret.txt", "path": "../secret.txt", "data": payload}]
            })
        self.assertFalse((self.root.parent / "secret.txt").exists())

    def test_project_context_rejects_windows_reserved_path_components(self):
        payload = base64.b64encode(b"safe reference").decode("ascii")
        paths = (
            "CON.txt",
            "brand/aux.notes",
            "logos/COM1.svg/mark.txt",
            "nested/lPt9/manual.txt",
        )
        for index, reserved_path in enumerate(paths):
            with self.subTest(path=reserved_path):
                project_path = self.root / "reserved-context-{}".format(index)
                project_path.mkdir()
                with self.assertRaisesRegex(ControlCenterError, "Windows reserved"):
                    self.app.projects._save_project_context(project_path, {
                        "assets": [{
                            "name": "reference.txt",
                            "path": reserved_path,
                            "data": payload,
                        }]
                    })
                self.assertFalse((project_path / "project-context").exists())

    def test_new_project_rejects_secret_references_before_commit_or_remote_creation(self):
        parent = self.root / "secret-reference-projects"
        parent.mkdir()
        private_key_marker = assemble_test_bytes(b"-----BEGIN ", b"PRIVATE KEY-----")
        cases = (
            ("Environment", ".env", b"CUSTOM_SECRET=value"),
            (
                "Private Key",
                "design-notes.txt",
                private_key_marker + b"\nnot-a-real-key\n-----END PRIVATE KEY-----",
            ),
        )
        for project_name, file_name, content in cases:
            with self.subTest(file=file_name), mock.patch.object(
                self.app.projects, "_find_port_block", return_value=6316
            ), mock.patch.object(self.app.projects, "_ensure_github_repo") as create_remote:
                with self.assertRaisesRegex(ControlCenterError, "credentials|key material|secrets file"):
                    self.app.projects.create_project(
                        project_name,
                        str(parent),
                        "codex",
                        {
                            "assets": [{
                                "name": file_name,
                                "path": file_name,
                                "data": base64.b64encode(content).decode("ascii"),
                            }]
                        },
                    )
                create_remote.assert_not_called()
                self.assertFalse((parent / slugify(project_name)).exists())

    def test_seed_manifest_can_be_reviewed_and_combination_is_queued(self):
        parent = self.root / "projects"
        parent.mkdir()
        with mock.patch.object(self.app.projects, "_find_port_block", return_value=6317), mock.patch.object(
            self.app.projects, "_ensure_github_repo"
        ):
            project = self.app.projects.create_project("Directions", str(parent), "codex")
        worktree = Path(project["path"])
        seed_root = worktree / "seed-directions"
        for seed_id in ("seed-01", "seed-02"):
            folder = seed_root / seed_id
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "index.html").write_text("<title>{}</title>".format(seed_id), encoding="utf-8")
        (seed_root / "manifest.json").write_text(json.dumps({"version": 1, "seeds": [
            {"id": "seed-01", "title": "Editorial", "direction": "Type-led", "summary": "Quiet", "path": "seed-directions/seed-01/index.html"},
            {"id": "seed-02", "title": "Kinetic", "direction": "Motion-led", "summary": "Bold", "path": "seed-directions/seed-02/index.html"},
        ]}), encoding="utf-8")
        session = {
            "id": "seed-review", "projectId": project["id"], "projectName": project["name"],
            "provider": "codex", "color": "blue", "emoji": "🔵", "port": 6317,
            "branch": "webkit/blue/seed-review", "worktree": str(worktree),
            "previewUrl": "http://127.0.0.1:6317/index.html", "feedbackDir": ".webkit/feedback",
            "status": "active", "threadId": None, "hasRun": True, "kind": "seeds",
            "seedCount": 2, "createdAt": "2026-08-18T00:00:00Z",
        }
        self.app.store.update(lambda state: (
            state.setdefault("sessions", []).append(session),
            next(item for item in state["projects"] if item["id"] == project["id"]).update({
                "onboarding": {"status": "review", "sessionId": session["id"], "seedCount": 2}
            }),
        ))
        status = self.app.sessions.seed_status(session["id"])
        self.assertTrue(status["ready"])
        self.assertEqual([item["id"] for item in status["seeds"]], ["seed-01", "seed-02"])
        self.assertEqual(
            subprocess.run(
                ["git", "status", "--porcelain"], cwd=worktree,
                text=True, capture_output=True, check=True,
            ).stdout,
            "",
        )
        self.assertEqual(
            subprocess.run(
                ["git", "log", "-1", "--pretty=%s"], cwd=worktree,
                text=True, capture_output=True, check=True,
            ).stdout.strip(),
            "Generate 2 design seeds",
        )
        runtime = mock.Mock()
        runtime.session = session
        self.app.sessions.runtimes[session["id"]] = runtime
        result = self.app.sessions.choose_seeds(
            session["id"], ["seed-01", "seed-02"], "Editorial type with kinetic navigation"
        )
        self.assertTrue(result["queued"])
        prompt = runtime.enqueue.call_args[0][0]
        self.assertNotIn("Editorial type with kinetic navigation", prompt)
        self.assertIn(".webkit/seed-combination-notes.md", prompt)
        self.assertEqual(
            (worktree / ".webkit" / "seed-combination-notes.md").read_text(encoding="utf-8"),
            "Editorial type with kinetic navigation\n",
        )
        self.assertIn("seed-01", prompt)
        self.assertIn("seed-02", prompt)
        self.assertIn("Control Center will commit", prompt)
        self.assertNotIn("Commit all intended work", prompt)
        self.assertEqual(self.app.projects.get_project(project["id"])["onboarding"]["status"], "finalizing")

    def test_multibyte_seed_notes_are_file_backed_and_prompt_failures_do_not_mutate_state(self):
        parent = self.root / "seed-note-projects"
        parent.mkdir()
        with mock.patch.object(
            self.app.projects, "_find_port_block", return_value=6326
        ), mock.patch.object(self.app.projects, "_ensure_github_repo"):
            project = self.app.projects.create_project(
                "Seed Notes", str(parent), "codex"
            )
        worktree = Path(project["path"])
        session = {
            "id": "seed-note-review", "projectId": project["id"],
            "projectName": project["name"], "provider": "codex", "color": "blue",
            "emoji": "🔵", "port": 6326, "branch": "webkit/blue/seed-notes",
            "worktree": str(worktree), "status": "active", "threadId": None,
            "hasRun": True, "kind": "seeds", "seedCount": 2,
            "seedStage": "review", "createdAt": "2026-08-19T00:00:00Z",
        }
        self.app.store.update(lambda state: (
            state.setdefault("sessions", []).append(dict(session)),
            next(item for item in state["projects"] if item["id"] == project["id"]).update({
                "onboarding": {"status": "review", "sessionId": session["id"], "seedCount": 2}
            }),
        ))
        runtime = mock.Mock()
        runtime.session = dict(session)
        self.app.sessions.runtimes[session["id"]] = runtime
        ready = {"ready": True, "seeds": [{
            "id": "seed-01", "title": "Editorial", "direction": "Type-led",
            "summary": "Quiet", "path": "seed-directions/seed-01/index.html",
        }]}
        marker = worktree / ".webkit" / "seed-selection.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("keep until a prompt is accepted", encoding="utf-8")
        notes_path = worktree / ".webkit" / "seed-combination-notes.md"

        with mock.patch.object(self.app.sessions, "seed_status", return_value=ready):
            with self.assertRaises(ControlCenterError) as raised:
                self.app.sessions.choose_seeds(
                    session["id"], ["seed-01"], "界" * 12001
                )
            self.assertEqual(raised.exception.status, 413)
            with mock.patch("control_center.MAX_PROVIDER_PROMPT_BYTES", 128):
                with self.assertRaises(ControlCenterError) as prompt_error:
                    self.app.sessions.choose_seeds(
                        session["id"], ["seed-01"], "界" * 12000
                    )
            self.assertEqual(prompt_error.exception.status, 413)
            self.assertEqual(
                self.app.sessions._get_session(session["id"])["status"], "active"
            )
            self.assertEqual(
                self.app.projects.get_project(project["id"])["onboarding"]["status"],
                "review",
            )
            self.assertTrue(marker.is_file())
            self.assertFalse(notes_path.exists())
            runtime.enqueue.assert_not_called()

            result = self.app.sessions.choose_seeds(
                session["id"], ["seed-01"], "界" * 12000
            )
        self.assertTrue(result["queued"])
        prompt = runtime.enqueue.call_args[0][0]
        self.assertLessEqual(len(prompt.encode("utf-8")), 16 * 1024)
        self.assertNotIn("界" * 20, prompt)
        self.assertIn(".webkit/seed-combination-notes.md", prompt)
        self.assertEqual(
            notes_path.read_text(encoding="utf-8"), "界" * 12000 + "\n"
        )
        self.assertFalse(marker.exists())

    def test_seed_workflow_honors_a_public_document_root(self):
        parent = self.root / "projects"
        parent.mkdir()
        with mock.patch.object(self.app.projects, "_find_port_block", return_value=6319), mock.patch.object(
            self.app.projects, "_ensure_github_repo"
        ):
            project = self.app.projects.create_project("Public Directions", str(parent), "codex")
        worktree = Path(project["path"])
        public = worktree / "public"
        public.mkdir()
        (worktree / "index.html").replace(public / "index.html")
        config_path = worktree / "webkit" / "webkit.config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config.update({"site_root": "public", "default_page": "index.html"})
        config_path.write_text(json.dumps(config), encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=worktree, check=True)
        subprocess.run(
            ["git", "commit", "-m", "Serve the public document root"], cwd=worktree,
            check=True, capture_output=True,
        )
        seed_root = public / "seed-directions"
        for seed_id in ("seed-01", "seed-02"):
            folder = seed_root / seed_id
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "index.html").write_text("<title>{}</title>".format(seed_id), encoding="utf-8")
        (seed_root / "manifest.json").write_text(json.dumps({"version": 1, "seeds": [
            {"id": "seed-01", "title": "One", "direction": "Editorial", "summary": "Quiet", "path": "seed-directions/seed-01/index.html"},
            {"id": "seed-02", "title": "Two", "direction": "Kinetic", "summary": "Bold", "path": "seed-directions/seed-02/index.html"},
        ]}), encoding="utf-8")
        session = {
            "id": "public-seed-review", "projectId": project["id"], "projectName": project["name"],
            "provider": "codex", "color": "blue", "emoji": "🔵", "port": 6319,
            "branch": "webkit/blue/public-seeds", "worktree": str(worktree),
            "previewUrl": "http://127.0.0.1:6319/index.html", "feedbackDir": ".webkit/feedback",
            "status": "active", "threadId": None, "hasRun": True, "kind": "seeds",
            "seedCount": 2, "createdAt": "2026-08-19T00:00:00Z",
        }
        self.app.store.update(lambda state: (
            state.setdefault("sessions", []).append(session),
            next(item for item in state["projects"] if item["id"] == project["id"]).update({
                "onboarding": {"status": "review", "sessionId": session["id"], "seedCount": 2}
            }),
        ))

        status = self.app.sessions.seed_status(session["id"])

        self.assertTrue(status["ready"])
        self.assertTrue(all("/seed-directions/" in seed["previewUrl"] for seed in status["seeds"]))
        self.assertFalse((worktree / "seed-directions").exists())
        self.assertTrue((public / "seed-directions" / "manifest.json").is_file())
        self.assertEqual(
            subprocess.run(
                ["git", "log", "-1", "--pretty=%s"], cwd=worktree,
                text=True, capture_output=True, check=True,
            ).stdout.strip(),
            "Generate 2 design seeds",
        )
        runtime = mock.Mock()
        runtime.session = session
        self.app.sessions.runtimes[session["id"]] = runtime
        self.app.sessions.choose_seeds(session["id"], ["seed-01"])
        prompt = runtime.enqueue.call_args[0][0]
        self.assertIn("production site starting at `public/index.html`", prompt)
        self.assertIn("`public/seed-directions/` exploration folder", prompt)

    def test_seed_finalize_is_committed_and_merged_by_controller(self):
        parent = self.root / "projects"
        parent.mkdir()
        with mock.patch.object(self.app.projects, "_find_port_block", return_value=6318), mock.patch.object(
            self.app.projects, "_ensure_github_repo"
        ):
            project = self.app.projects.create_project("Finished Seeds", str(parent), "codex")
        project_path = Path(project["path"])
        branch = "webkit/red/seed-finalize"
        worktree = self.root / "seed-finalize-worktree"
        subprocess.run(
            ["git", "worktree", "add", "-b", branch, str(worktree), "main"],
            cwd=project_path, check=True, capture_output=True,
        )
        seed_page = worktree / "seed-directions" / "seed-01" / "index.html"
        seed_page.parent.mkdir(parents=True)
        seed_page.write_text("<title>Seed</title>\n", encoding="utf-8")
        subprocess.run(["git", "add", "seed-directions"], cwd=worktree, check=True)
        subprocess.run(
            ["git", "commit", "-m", "Generate 1 design seed"], cwd=worktree,
            check=True, capture_output=True,
        )
        (worktree / "index.html").write_text("<title>Chosen direction</title>\n", encoding="utf-8")
        shutil.rmtree(worktree / "seed-directions")
        marker = worktree / ".webkit" / "seed-selection.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text('{"status":"ready","message":"ready to finish onboarding"}\n', encoding="utf-8")
        base_sha = subprocess.run(
            ["git", "rev-parse", "main"], cwd=project_path,
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        worktree_details = os.lstat(str(worktree))
        session = {
            "id": "seed-finalize", "projectId": project["id"], "projectName": project["name"],
            "provider": "codex", "color": "red", "emoji": "🔴", "port": 6318,
            "branch": branch, "worktree": str(worktree),
            "previewUrl": "http://127.0.0.1:6318/index.html", "feedbackDir": ".webkit/feedback",
            "status": "merging", "threadId": None, "hasRun": True, "kind": "seeds",
            "seedCount": 1, "seedStage": "finalizing", "createdAt": "2026-08-18T00:00:00Z",
            "baseSha": base_sha, "worktreeDev": worktree_details.st_dev,
            "worktreeIno": worktree_details.st_ino,
        }
        self.app.store.update(lambda state: (
            state.setdefault("sessions", []).append(session),
            next(item for item in state["projects"] if item["id"] == project["id"]).update({
                "onboarding": {"status": "finalizing", "sessionId": session["id"], "selected": ["seed-01"]}
            }),
        ))

        self.app.sessions._complete_seed_onboarding(session["id"])

        self.assertEqual((project_path / "index.html").read_text(), "<title>Chosen direction</title>\n")
        self.assertFalse(worktree.exists())
        self.assertEqual(
            subprocess.run(
                ["git", "log", "-1", "--pretty=%s"], cwd=project_path,
                text=True, capture_output=True, check=True,
            ).stdout.strip(),
            "Build website from selected design seeds",
        )
        self.assertEqual(self.app.sessions._get_session(session["id"])["status"], "merged")

    def test_seed_finalize_chat_can_recover_after_a_conflict(self):
        parent = self.root / "projects"
        parent.mkdir()
        with mock.patch.object(self.app.projects, "_find_port_block", return_value=6320), mock.patch.object(
            self.app.projects, "_ensure_github_repo"
        ):
            project = self.app.projects.create_project("Recovered Seeds", str(parent), "codex")
        project_path = Path(project["path"])
        branch = "webkit/yellow/seed-recovery"
        worktree = self.root / "seed-recovery-worktree"
        subprocess.run(
            ["git", "worktree", "add", "-b", branch, str(worktree), "main"],
            cwd=project_path, check=True, capture_output=True,
        )
        seed_page = worktree / "seed-directions" / "seed-01" / "index.html"
        seed_page.parent.mkdir(parents=True)
        seed_page.write_text("<title>Seed</title>\n", encoding="utf-8")
        subprocess.run(["git", "add", "seed-directions"], cwd=worktree, check=True)
        subprocess.run(
            ["git", "commit", "-m", "Generate 1 design seed"], cwd=worktree,
            check=True, capture_output=True,
        )
        (worktree / "index.html").write_text(
            "<title>Recovered direction</title>\n", encoding="utf-8"
        )
        shutil.rmtree(worktree / "seed-directions")
        marker = worktree / ".webkit" / "seed-selection.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            '{"status":"conflict","message":"Choose the final heading."}\n',
            encoding="utf-8",
        )
        base_sha = subprocess.run(
            ["git", "rev-parse", "main"], cwd=project_path,
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        worktree_details = os.lstat(str(worktree))
        session = {
            "id": "seed-recovery", "projectId": project["id"], "projectName": project["name"],
            "provider": "codex", "color": "yellow", "emoji": "🟡", "port": 6320,
            "branch": branch, "worktree": str(worktree),
            "previewUrl": "http://127.0.0.1:6320/index.html", "feedbackDir": ".webkit/feedback",
            "status": "merging", "threadId": None, "hasRun": True, "kind": "seeds",
            "seedCount": 1, "seedStage": "finalizing", "createdAt": "2026-08-19T00:00:00Z",
            "baseSha": base_sha, "worktreeDev": worktree_details.st_dev,
            "worktreeIno": worktree_details.st_ino,
        }
        self.app.store.update(lambda state: (
            state.setdefault("sessions", []).append(session),
            next(item for item in state["projects"] if item["id"] == project["id"]).update({
                "onboarding": {
                    "status": "finalizing", "sessionId": session["id"], "selected": ["seed-01"]
                }
            }),
        ))

        with self.assertRaisesRegex(ControlCenterError, "Choose the final heading"):
            self.app.sessions._complete_seed_onboarding(session["id"])
        self.app.sessions._set_session_status(
            session["id"], "error", "Choose the final heading."
        )
        runtime = SessionRuntime(self.app.sessions, self.app.sessions._get_session(session["id"]))
        self.app.sessions.runtimes[session["id"]] = runtime

        def answer_in_chat(_prompt):
            marker.write_text(
                '{"status":"ready","message":"ready to finish onboarding"}\n',
                encoding="utf-8",
            )

        with mock.patch.object(ProviderRunner, "run", side_effect=answer_in_chat):
            runtime.start()
            runtime.enqueue("Use the shorter heading.", "chat")
            deadline = time.monotonic() + 60
            turn_finished = False
            while time.monotonic() < deadline:
                events = runtime.log.read_after(0)["events"]
                if any(event.get("kind") == "turn_complete" for event in events):
                    turn_finished = True
                    break
                time.sleep(0.02)
            if not turn_finished:
                runtime.stop()
                self.fail("Seed recovery did not finish within 60 seconds.")

        self.assertEqual(self.app.sessions._get_session(session["id"])["status"], "merged")
        self.assertEqual(
            (project_path / "index.html").read_text(encoding="utf-8"),
            "<title>Recovered direction</title>\n",
        )
        self.assertFalse(worktree.exists())

    def test_add_existing_git_repository_installs_claude_entrypoint(self):
        project_path = self.root / "legacy-site"
        project_path.mkdir()
        (project_path / "index.html").write_text("<title>Legacy</title>", encoding="utf-8")
        subprocess.run(["git", "init", "-b", "main"], cwd=project_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=project_path, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=project_path, check=True)
        subprocess.run(["git", "add", "index.html"], cwd=project_path, check=True)
        subprocess.run(["git", "commit", "-m", "Initial"], cwd=project_path, check=True, capture_output=True)
        with mock.patch.object(self.app.projects, "_find_port_block", return_value=6321):
            project = self.app.projects.add_existing(str(project_path), "claude")
        managed_path = Path(project["path"])
        self.assertEqual(project["provider"], "claude")
        self.assertTrue((project_path / ".git").is_dir())
        self.assertTrue(project["managedCheckout"])
        self.assertTrue((managed_path / "CLAUDE.md").exists())
        self.assertTrue((managed_path / ".claude" / "skills" / "abc" / "SKILL.md").exists())
        self.assertTrue((project_path / "CLAUDE.md").exists())
        self.assertTrue((project_path / "webkit" / "webkit.config.json").exists())

    def test_non_git_add_existing_is_rejected_without_mutation(self):
        def tree_snapshot(root):
            values = {}
            for path in sorted(root.rglob("*")):
                relative = path.relative_to(root).as_posix()
                details = path.lstat()
                if path.is_symlink():
                    values[relative] = ("link", os.readlink(str(path)), stat.S_IMODE(details.st_mode))
                elif path.is_dir():
                    values[relative] = ("dir", stat.S_IMODE(details.st_mode))
                else:
                    values[relative] = ("file", path.read_bytes(), stat.S_IMODE(details.st_mode))
            return values

        project_path = self.root / "non-git-existing"
        project_path.mkdir()
        (project_path / "index.html").write_bytes(b"<title>Original</title>\n")
        (project_path / "AGENTS.md").write_bytes(b"original agents\n")
        before = tree_snapshot(project_path)

        with self.assertRaisesRegex(ControlCenterError, "Initialize Git"):
            self.app.projects.add_existing(str(project_path), "codex")

        self.assertEqual(tree_snapshot(project_path), before)
        self.assertFalse((project_path / ".git").exists())

    def test_git_journal_rejects_concurrent_index_and_hook_metadata(self):
        for mutation in ("index", "hook"):
            with self.subTest(mutation=mutation):
                repo = self.root / ("journal-" + mutation)
                repo.mkdir()
                subprocess.run(
                    ["git", "init", "-b", "main"], cwd=repo,
                    check=True, capture_output=True,
                )
                subprocess.run(
                    ["git", "config", "user.name", "Test"], cwd=repo, check=True
                )
                subprocess.run(
                    ["git", "config", "user.email", "test@example.invalid"],
                    cwd=repo, check=True,
                )
                journal = ProjectMutationJournal(repo)
                journal.record_git_directory()
                if mutation == "index":
                    (repo / "index.html").write_text("concurrent\n", encoding="utf-8")
                    subprocess.run(["git", "add", "index.html"], cwd=repo, check=True)
                else:
                    (repo / ".git" / "hooks" / "concurrent-hook").write_text(
                        "keep\n", encoding="utf-8"
                    )
                self.assertFalse(journal.owns_removable_git_directory())
                self.assertTrue((repo / ".git").is_dir())

    def test_installer_journal_rejects_replacement_and_in_place_snapshot_races(self):
        mutations = (
            ("in-place",)
            if os.name == "nt"
            else ("replacement", "in-place")
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                repo = (self.root / ("support-snapshot-" + mutation)).resolve()
                repo.mkdir()
                target = repo / "AGENTS.md"
                target.write_bytes(b"original support bytes\n")
                replacement = repo / "replacement.txt"
                replacement.write_bytes(b"concurrent replacement bytes\n")
                journal = ProjectMutationJournal(repo)
                real_read = os.read
                injected = []

                def mutate_after_read(descriptor, size):
                    data = real_read(descriptor, size)
                    if data and not injected:
                        injected.append(True)
                        if mutation == "replacement":
                            os.replace(str(replacement), str(target))
                        else:
                            target.write_bytes(b"concurrent in-place bytes changed\n")
                    return data

                with mock.patch("control_center.os.read", side_effect=mutate_after_read):
                    with self.assertRaisesRegex(
                        ControlCenterError, "changed while it was read"
                    ):
                        journal.watch_support_file(target)

                self.assertEqual(journal.support, {})
                self.assertTrue(target.read_bytes().startswith(b"concurrent"))

    def test_installer_journal_accepts_a_canonical_alias_of_its_input_root(self):
        repo = self.root / "support-canonical-root-alias"
        repo.mkdir()
        target = repo / "AGENTS.md"
        target.write_bytes(b"original bytes\n")
        journal = ProjectMutationJournal(repo)

        journal.watch_support_file(target)

        self.assertEqual(len(journal.support), 1)
        self.assertEqual(
            next(iter(journal.support.values()))["data"], b"original bytes\n"
        )

    @unittest.skipUnless(hasattr(os, "link"), "hard links are required")
    def test_installer_journal_rejects_hardlinked_support_snapshot(self):
        repo = (self.root / "support-hardlink-snapshot").resolve()
        repo.mkdir()
        source = repo / "source.txt"
        target = repo / "AGENTS.md"
        source.write_bytes(b"shared bytes\n")
        os.link(str(source), str(target))

        journal = ProjectMutationJournal(repo)
        with self.assertRaisesRegex(ControlCenterError, "exactly one hard link"):
            journal.watch_support_file(target)

        self.assertEqual(source.read_bytes(), b"shared bytes\n")
        self.assertEqual(target.read_bytes(), b"shared bytes\n")

    def test_installer_journal_rejects_symlinked_support_snapshot(self):
        repo = (self.root / "support-symlink-snapshot").resolve()
        repo.mkdir()
        sentinel = self.root / "support-symlink-sentinel"
        sentinel.write_bytes(b"outside bytes\n")
        target = repo / "AGENTS.md"
        try:
            target.symlink_to(sentinel)
        except (OSError, NotImplementedError) as exc:
            self.skipTest("symbolic links unavailable: {}".format(exc))

        journal = ProjectMutationJournal(repo)
        with self.assertRaisesRegex(ControlCenterError, "symbolic link"):
            journal.watch_support_file(target)

        self.assertEqual(sentinel.read_bytes(), b"outside bytes\n")

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits required")
    def test_installer_journal_restores_support_bytes_and_mode_without_artifacts(self):
        repo = (self.root / "support-mode-rollback").resolve()
        repo.mkdir()
        target = repo / "AGENTS.md"
        target.write_bytes(b"original bytes\n")
        target.chmod(0o640)
        journal = ProjectMutationJournal(repo)
        journal.watch_support_file(target)

        target.write_bytes(b"installer bytes\n")
        target.chmod(0o600)
        journal.mark_support_written(target)
        journal.rollback()

        self.assertEqual(target.read_bytes(), b"original bytes\n")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)
        self.assertEqual(list(repo.glob(".wk-rollback-*")), [])
        self.assertEqual(list(repo.glob(".wk-restore-*")), [])

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits required")
    def test_installer_journal_preserves_concurrent_mode_change(self):
        repo = (self.root / "support-concurrent-mode").resolve()
        repo.mkdir()
        target = repo / "AGENTS.md"
        target.write_bytes(b"original bytes\n")
        target.chmod(0o640)
        journal = ProjectMutationJournal(repo)
        journal.watch_support_file(target)
        target.write_bytes(b"installer bytes\n")
        journal.mark_support_written(target)

        target.chmod(0o600)
        with self.assertRaisesRegex(ControlCenterError, "changed after installation"):
            journal.rollback()

        self.assertEqual(target.read_bytes(), b"installer bytes\n")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_installer_journal_claim_preserves_replacement_during_rollback(self):
        repo = (self.root / "support-replacement-rollback").resolve()
        repo.mkdir()
        target = repo / "AGENTS.md"
        target.write_bytes(b"original bytes\n")
        journal = ProjectMutationJournal(repo)
        journal.watch_support_file(target)
        target.write_bytes(b"installer bytes\n")
        journal.mark_support_written(target)
        replacement = repo / "replacement.txt"
        replacement.write_bytes(b"concurrent replacement\n")
        real_rename = control_center_module.rename_directory_noreplace
        injected = []

        def replace_before_claim(source, destination):
            if Path(source) == target and not injected:
                injected.append(True)
                os.replace(str(replacement), str(target))
            return real_rename(source, destination)

        with mock.patch(
            "control_center.rename_directory_noreplace",
            side_effect=replace_before_claim,
        ):
            with self.assertRaisesRegex(ControlCenterError, "changed after installation"):
                journal.rollback()

        self.assertEqual(target.read_bytes(), b"concurrent replacement\n")
        self.assertEqual(list(repo.glob(".wk-rollback-*")), [])
        self.assertEqual(list(repo.glob(".wk-restore-*")), [])

    def test_installer_journal_claim_preserves_in_place_write_during_rollback(self):
        repo = (self.root / "support-in-place-rollback").resolve()
        repo.mkdir()
        target = repo / "AGENTS.md"
        target.write_bytes(b"original bytes\n")
        journal = ProjectMutationJournal(repo)
        journal.watch_support_file(target)
        target.write_bytes(b"installer bytes\n")
        journal.mark_support_written(target)
        real_rename = control_center_module.rename_directory_noreplace
        injected = []

        def write_before_claim(source, destination):
            if Path(source) == target and not injected:
                injected.append(True)
                target.write_bytes(b"concurrent in-place write\n")
            return real_rename(source, destination)

        with mock.patch(
            "control_center.rename_directory_noreplace",
            side_effect=write_before_claim,
        ):
            with self.assertRaisesRegex(ControlCenterError, "changed after installation"):
                journal.rollback()

        self.assertEqual(target.read_bytes(), b"concurrent in-place write\n")
        self.assertEqual(list(repo.glob(".wk-rollback-*")), [])
        self.assertEqual(list(repo.glob(".wk-restore-*")), [])

    def test_installer_journal_preserves_symlink_replacement_during_rollback(self):
        repo = (self.root / "support-symlink-rollback").resolve()
        repo.mkdir()
        target = repo / "AGENTS.md"
        target.write_bytes(b"original bytes\n")
        journal = ProjectMutationJournal(repo)
        journal.watch_support_file(target)
        target.write_bytes(b"installer bytes\n")
        journal.mark_support_written(target)
        sentinel = self.root / "support-rollback-sentinel"
        sentinel.write_bytes(b"outside bytes\n")
        target.unlink()
        try:
            target.symlink_to(sentinel)
        except (OSError, NotImplementedError) as exc:
            self.skipTest("symbolic links unavailable: {}".format(exc))

        with self.assertRaisesRegex(ControlCenterError, "changed after installation"):
            journal.rollback()

        self.assertTrue(target.is_symlink())
        self.assertEqual(sentinel.read_bytes(), b"outside bytes\n")

    def test_journal_aware_support_mutation_rejects_changes_after_snapshot(self):
        for started_existing in (False, True):
            with self.subTest(started_existing=started_existing):
                repo = (
                    self.root / "support-gap-{}".format(started_existing)
                ).resolve()
                repo.mkdir()
                target = repo / "AGENTS.md"
                if started_existing:
                    target.write_bytes(b"original bytes\n")
                journal = ProjectMutationJournal(repo)
                journal.watch_support_file(target)
                target.write_bytes(b"concurrent bytes\n")

                with self.assertRaisesRegex(ControlCenterError, "concurrently"):
                    control_center_module.append_section(
                        target,
                        "## Webkit",
                        "## Webkit\n\ninstaller section\n",
                        repo,
                        journal=journal,
                    )

                self.assertEqual(target.read_bytes(), b"concurrent bytes\n")

    def test_windows_path_and_descriptor_identity_domains_are_normalized(self):
        repo = (self.root / "windows-stat-domains").resolve()
        repo.mkdir()
        target = repo / "AGENTS.md"
        target.write_bytes(b"stable bytes\n")
        real_fstat = os.fstat

        def windows_descriptor_stat(descriptor):
            details = real_fstat(descriptor)
            return types.SimpleNamespace(
                st_dev=details.st_dev + 1000,
                st_ino=details.st_ino + 1000,
                st_mode=details.st_mode,
                st_size=details.st_size,
                st_nlink=details.st_nlink,
                st_mtime_ns=details.st_mtime_ns,
                st_ctime_ns=details.st_ctime_ns,
            )

        listed = os.lstat(str(target))
        with mock.patch(
            "control_center._WINDOWS_SPLIT_STAT_IDENTITIES", True
        ), mock.patch(
            "control_center.os.fstat", side_effect=windows_descriptor_stat
        ):
            self.assertEqual(
                read_stable_regular_text(target, 1024), "stable bytes\n"
            )
            snapshot = ProjectMutationJournal._snapshot(
                target, require_single_link=True, include_data=False
            )

        self.assertEqual(
            snapshot["fingerprint"][:2], (listed.st_dev, listed.st_ino)
        )
        self.assertEqual(
            snapshot["fingerprint"][-1],
            hashlib.sha256(b"stable bytes\n").hexdigest(),
        )

    def test_windows_descriptor_identity_changes_do_not_break_owned_writes(self):
        repo = (self.root / "windows-changing-descriptor-identity").resolve()
        repo.mkdir()
        source = repo / "source.txt"
        source.write_bytes(b"copied bytes\n")
        copied = repo / "copied.txt"
        created = repo / "created.txt"
        real_fstat = os.fstat
        real_lstat = os.lstat

        def changing_descriptor_stat(descriptor):
            details = real_fstat(descriptor)
            identity_shift = 1000 if details.st_size else 0
            return types.SimpleNamespace(
                st_dev=details.st_dev + identity_shift,
                st_ino=details.st_ino + identity_shift,
                st_mode=details.st_mode,
                st_size=details.st_size,
                st_nlink=details.st_nlink,
                st_mtime_ns=details.st_mtime_ns,
                st_ctime_ns=details.st_ctime_ns,
            )

        def changing_path_stat(path):
            details = real_lstat(path)
            identity_shift = 2000 if details.st_size else 0
            return types.SimpleNamespace(
                st_dev=details.st_dev + identity_shift,
                st_ino=details.st_ino + identity_shift,
                st_mode=details.st_mode,
                st_size=details.st_size,
                st_nlink=details.st_nlink,
                st_mtime_ns=details.st_mtime_ns,
                st_ctime_ns=details.st_ctime_ns,
            )

        def stable_descriptor_details(descriptor):
            details = real_fstat(descriptor)
            return (
                (
                    details.st_dev,
                    details.st_ino,
                    stat.S_IFMT(details.st_mode),
                ),
                details.st_size,
            )

        journal = ProjectMutationJournal(repo)
        journal.watch_support_file(created)
        with mock.patch(
            "control_center._WINDOWS_SPLIT_STAT_IDENTITIES", True
        ), mock.patch(
            "control_center.os.fstat", side_effect=changing_descriptor_stat
        ), mock.patch(
            "control_center.os.lstat", side_effect=changing_path_stat
        ), mock.patch(
            "control_center._descriptor_file_details",
            side_effect=stable_descriptor_details,
        ):
            journal.write_new_support_file(created, b"created bytes\n")
            exclusive_copy_file(source, copied)

        self.assertEqual(created.read_bytes(), b"created bytes\n")
        self.assertEqual(copied.read_bytes(), b"copied bytes\n")

    @unittest.skipUnless(os.name == "nt", "native Windows file handles required")
    def test_windows_native_file_identity_is_stable_while_a_file_grows(self):
        target = self.root / "windows-native-identity.txt"
        descriptor = os.open(
            str(target),
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0),
            0o600,
        )
        try:
            opened_identity, opened_size = (
                control_center_module._descriptor_file_details(descriptor)
            )
            self.assertEqual(opened_size, 0)
            contents = b"native Windows identity\n"
            self.assertEqual(os.write(descriptor, contents), len(contents))
            os.fsync(descriptor)
            written_identity, written_size = (
                control_center_module._descriptor_file_details(descriptor)
            )
            self.assertEqual(written_identity, opened_identity)
            self.assertEqual(written_size, len(contents))
            probe = os.open(
                str(target), os.O_RDONLY | getattr(os, "O_BINARY", 0)
            )
            try:
                probe_identity, probe_size = (
                    control_center_module._descriptor_file_details(probe)
                )
            finally:
                os.close(probe)
            self.assertEqual(probe_identity, opened_identity)
            self.assertEqual(probe_size, len(contents))
        finally:
            os.close(descriptor)

    def test_journal_aware_support_mutation_claim_preserves_late_replacement(self):
        repo = (self.root / "support-mutation-claim-race").resolve()
        repo.mkdir()
        target = repo / "AGENTS.md"
        target.write_bytes(b"original bytes\n")
        journal = ProjectMutationJournal(repo)
        journal.watch_support_file(target)
        replacement = repo / "replacement.txt"
        replacement.write_bytes(b"concurrent replacement\n")
        real_rename = control_center_module.rename_directory_noreplace
        injected = []

        def replace_before_claim(source, destination):
            if Path(source) == target and not injected:
                injected.append(True)
                os.replace(str(replacement), str(target))
            return real_rename(source, destination)

        with mock.patch(
            "control_center.rename_directory_noreplace",
            side_effect=replace_before_claim,
        ):
            with self.assertRaisesRegex(ControlCenterError, "concurrently"):
                control_center_module.append_section(
                    target,
                    "## Webkit",
                    "## Webkit\n\ninstaller section\n",
                    repo,
                    journal=journal,
                )

        self.assertEqual(target.read_bytes(), b"concurrent replacement\n")
        self.assertEqual(list(repo.glob(".wk-rollback-*")), [])
        self.assertEqual(list(repo.glob(".wk-restore-*")), [])

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits required")
    def test_journal_aware_support_mutation_records_exact_rollback_output(self):
        repo = (self.root / "support-mutation-success").resolve()
        repo.mkdir()
        target = repo / "AGENTS.md"
        target.write_bytes(b"original bytes\n")
        target.chmod(0o640)
        journal = ProjectMutationJournal(repo)
        journal.watch_support_file(target)

        changed = control_center_module.append_section(
            target,
            "## Webkit",
            "## Webkit\n\ninstaller section\n",
            repo,
            journal=journal,
        )
        self.assertTrue(changed)
        journal.mark_support_written(target)
        self.assertIn(b"installer section", target.read_bytes())

        journal.rollback()

        self.assertEqual(target.read_bytes(), b"original bytes\n")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)

    def test_journal_exclusive_support_creation_preserves_concurrent_creator(self):
        repo = (self.root / "support-exclusive-create-race").resolve()
        repo.mkdir()
        target = repo / "webkit.config.json"
        journal = ProjectMutationJournal(repo)
        journal.watch_support_file(target)
        target.write_bytes(b"concurrent config\n")

        with self.assertRaisesRegex(ControlCenterError, "created concurrently"):
            journal.write_new_support_file(target, b"installer config\n")

        self.assertEqual(target.read_bytes(), b"concurrent config\n")

    def test_journal_exclusive_support_creation_cleans_only_its_partial(self):
        replace_cases = (False,) if os.name == "nt" else (False, True)
        for replace_partial in replace_cases:
            with self.subTest(replace_partial=replace_partial):
                repo = (
                    self.root / "support-partial-{}".format(replace_partial)
                ).resolve()
                repo.mkdir()
                target = repo / "webkit.config.json"
                journal = ProjectMutationJournal(repo)
                journal.watch_support_file(target)
                replacement = repo / "replacement.txt"
                replacement.write_bytes(b"concurrent config\n")
                real_write = os.write
                calls = []

                def fail_after_partial(descriptor, data):
                    if not calls:
                        calls.append(True)
                        written = real_write(descriptor, data[:4])
                        if replace_partial:
                            os.replace(str(replacement), str(target))
                        return written
                    raise OSError("injected support write failure")

                with mock.patch(
                    "control_center.os.write", side_effect=fail_after_partial
                ):
                    with self.assertRaisesRegex(OSError, "injected support write"):
                        journal.write_new_support_file(
                            target, b"installer config bytes\n"
                        )

                if replace_partial:
                    self.assertEqual(target.read_bytes(), b"concurrent config\n")
                else:
                    self.assertFalse(target.exists())
                self.assertEqual(list(repo.glob(".wk-rollback-*")), [])

    @unittest.skipUnless(hasattr(os, "link"), "hard links are required")
    def test_installer_journal_preserves_support_file_hardlinked_after_write(self):
        repo = (self.root / "support-hardlink-rollback").resolve()
        repo.mkdir()
        target = repo / "AGENTS.md"
        target.write_bytes(b"original bytes\n")
        journal = ProjectMutationJournal(repo)
        journal.watch_support_file(target)
        target.write_bytes(b"installer bytes\n")
        journal.mark_support_written(target)
        alias = repo / "concurrent-alias.txt"
        os.link(str(target), str(alias))

        with self.assertRaisesRegex(ControlCenterError, "changed after installation"):
            journal.rollback()

        self.assertEqual(target.read_bytes(), b"installer bytes\n")
        self.assertEqual(alias.read_bytes(), b"installer bytes\n")

    def test_installer_journal_created_file_cleanup_preserves_replacement(self):
        repo = (self.root / "created-file-cleanup").resolve()
        repo.mkdir()
        source = repo / "source.txt"
        target = repo / "installed.txt"
        source.write_bytes(b"installer bytes\n")
        journal = ProjectMutationJournal(repo)
        exclusive_copy_file(source, target, journal=journal)
        replacement = repo / "replacement.txt"
        replacement.write_bytes(b"concurrent bytes\n")
        real_rename = control_center_module.rename_directory_noreplace
        injected = []

        def replace_before_claim(path, destination):
            if Path(path) == target and not injected:
                injected.append(True)
                os.replace(str(replacement), str(target))
            return real_rename(path, destination)

        with mock.patch(
            "control_center.rename_directory_noreplace",
            side_effect=replace_before_claim,
        ):
            with self.assertRaisesRegex(ControlCenterError, "changed after installation"):
                journal.rollback()

        self.assertEqual(target.read_bytes(), b"concurrent bytes\n")

    def test_installer_journal_created_directory_cleanup_preserves_replacement(self):
        repo = (self.root / "created-directory-cleanup").resolve()
        repo.mkdir()
        target = repo / "new-directory"
        journal = ProjectMutationJournal(repo)
        journal.ensure_directory(target)
        real_rename = control_center_module.rename_directory_noreplace
        injected = []

        def replace_before_claim(path, destination):
            if Path(path) == target and not injected:
                injected.append(True)
                target.rmdir()
                target.mkdir()
            return real_rename(path, destination)

        with mock.patch(
            "control_center.rename_directory_noreplace",
            side_effect=replace_before_claim,
        ):
            with self.assertRaisesRegex(ControlCenterError, "changed after installation"):
                journal.rollback()

        self.assertTrue(target.is_dir())

    def test_installer_journal_directory_creation_never_adopts_concurrent_path(self):
        repo = (self.root / "created-directory-publish-race").resolve()
        repo.mkdir()
        target = repo / "new-directory"
        journal = ProjectMutationJournal(repo)
        real_rename = control_center_module.rename_directory_noreplace
        injected = []

        def create_before_publish(path, destination):
            if Path(destination) == target and not injected:
                injected.append(True)
                target.mkdir()
            return real_rename(path, destination)

        with mock.patch(
            "control_center.rename_directory_noreplace",
            side_effect=create_before_publish,
        ):
            with self.assertRaisesRegex(ControlCenterError, "created concurrently"):
                journal.ensure_directory(target)

        self.assertTrue(target.is_dir())
        self.assertEqual(journal.created_dirs, [])
        self.assertEqual(list(repo.glob(".wk-mkdir-*")), [])

    def test_installer_journal_removes_only_owned_created_paths(self):
        repo = (self.root / "created-path-cleanup-success").resolve()
        repo.mkdir()
        journal = ProjectMutationJournal(repo)
        directory = repo / "webkit" / "nested"
        journal.ensure_directory(directory)
        source = repo / "source.txt"
        target = directory / "installed.txt"
        source.write_bytes(b"installer bytes\n")
        exclusive_copy_file(source, target, journal=journal)
        created_support = repo / "AGENTS.md"
        journal.watch_support_file(created_support)
        created_support.write_bytes(b"installer support\n")
        journal.mark_support_written(created_support)

        journal.rollback()

        self.assertFalse(target.exists())
        self.assertFalse(created_support.exists())
        self.assertFalse((repo / "webkit").exists())
        self.assertTrue(source.is_file())
        self.assertEqual(list(repo.rglob(".wk-rollback-*")), [])
        self.assertEqual(list(repo.rglob(".wk-restore-*")), [])

    def test_installer_journal_covers_complete_kit_install_and_rollback(self):
        repo = (self.root / "journal-complete-install").resolve()
        repo.mkdir()
        index = repo / "index.html"
        index.write_bytes(b"<title>Existing site</title>\n")
        journal = ProjectMutationJournal(repo)

        with mock.patch.object(self.app.projects, "_find_port_block", return_value=6711):
            changed = self.app.projects._install_kit(
                repo, "codex", journal=journal
            )

        self.assertTrue(changed)
        self.assertTrue((repo / "webkit" / "webkit.config.json").is_file())
        self.assertTrue((repo / "AGENTS.md").is_file())
        self.assertTrue((repo / ".gitignore").is_file())

        journal.rollback()

        self.assertEqual(index.read_bytes(), b"<title>Existing site</title>\n")
        self.assertEqual(sorted(path.name for path in repo.iterdir()), ["index.html"])

    def test_missing_tree_copy_never_overwrites_a_concurrent_creator(self):
        source_root = self.root / "exclusive-source"
        target_root = self.root / "exclusive-target"
        source_root.mkdir()
        (source_root / "file.txt").write_text("installer bytes\n", encoding="utf-8")
        real_copy = control_center_module.exclusive_copy_file

        def create_then_copy(source, destination, journal=None):
            Path(destination).write_text("concurrent bytes\n", encoding="utf-8")
            return real_copy(source, destination, journal=journal)

        with mock.patch(
            "control_center.exclusive_copy_file", side_effect=create_then_copy
        ), self.assertRaisesRegex(ControlCenterError, "created concurrently"):
            self.app.projects._copy_missing_tree(source_root, target_root)

        self.assertEqual(
            (target_root / "file.txt").read_text(encoding="utf-8"),
            "concurrent bytes\n",
        )

    def test_exclusive_copy_removes_its_owned_partial_after_write_failure(self):
        source = self.root / "partial-source.txt"
        destination = self.root / "partial-destination.txt"
        source.write_bytes(b"installer bytes that must not remain")
        real_write = os.write
        write_calls = 0

        def fail_after_partial(descriptor, data):
            nonlocal write_calls
            write_calls += 1
            if write_calls == 1:
                return real_write(descriptor, data[:4])
            raise OSError("injected disk failure")

        with mock.patch("control_center.os.write", side_effect=fail_after_partial):
            with self.assertRaisesRegex(OSError, "injected disk failure"):
                exclusive_copy_file(source, destination)

        self.assertFalse(destination.exists())

    def test_exclusive_copy_cleanup_preserves_a_racing_replacement(self):
        source = self.root / "partial-race-source.txt"
        destination = self.root / "partial-race-destination.txt"
        replacement = self.root / "partial-race-replacement.txt"
        source.write_bytes(b"installer bytes that must not remain")
        replacement.write_bytes(b"concurrent replacement\n")
        real_write = os.write
        write_calls = []
        real_rename = control_center_module.rename_directory_noreplace
        replaced = []

        def fail_after_partial(descriptor, data):
            if not write_calls:
                write_calls.append(True)
                return real_write(descriptor, data[:4])
            raise OSError("injected disk failure")

        def replace_before_cleanup_claim(path, quarantine):
            if Path(path) == destination and not replaced:
                replaced.append(True)
                os.replace(str(replacement), str(destination))
            return real_rename(path, quarantine)

        with mock.patch(
            "control_center.os.write", side_effect=fail_after_partial
        ), mock.patch(
            "control_center.rename_directory_noreplace",
            side_effect=replace_before_cleanup_claim,
        ):
            with self.assertRaisesRegex(
                ControlCenterError, "changed"
            ):
                exclusive_copy_file(source, destination)

        self.assertEqual(destination.read_bytes(), b"concurrent replacement\n")
        self.assertEqual(list(self.root.glob(".wk-rollback-*")), [])

    def test_add_existing_rejects_an_unborn_git_repository_without_mutation(self):
        repo = self.root / "unborn-site"
        repo.mkdir()
        (repo / "index.html").write_text("<title>Unborn</title>\n", encoding="utf-8")
        subprocess.run(
            ["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True
        )

        before = (repo / "index.html").read_bytes()
        with self.assertRaisesRegex(ControlCenterError, "no commits"):
            self.app.projects.add_existing(str(repo), "codex")

        source_status = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=repo, text=True, capture_output=True, check=True,
        ).stdout
        source_head = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"], cwd=repo,
            text=True, capture_output=True, check=False,
        )
        self.assertNotEqual(source_head.returncode, 0)
        self.assertEqual(source_status, "?? index.html\n")
        self.assertEqual((repo / "index.html").read_bytes(), before)
        self.assertFalse((repo / "webkit").exists())
        self.assertFalse((repo / ".gitignore").exists())

    def test_add_existing_isolates_dirty_tracked_repository_without_changes(self):
        repo = self.root / "dirty-tracked"
        repo.mkdir()
        index = repo / "index.html"
        index.write_text("<title>Committed</title>\n", encoding="utf-8")
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
        subprocess.run(["git", "add", "index.html"], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-m", "Initial"], cwd=repo, check=True, capture_output=True
        )
        index.write_text("<title>Uncommitted</title>\n", encoding="utf-8")
        before_status = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=repo, text=True, capture_output=True, check=True,
        ).stdout
        before_head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo,
            text=True, capture_output=True, check=True,
        ).stdout

        with mock.patch.object(
            self.app.projects, "_find_port_block", return_value=6430
        ):
            project = self.app.projects.add_existing(str(repo), "codex")

        after_status = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=repo, text=True, capture_output=True, check=True,
        ).stdout
        after_head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo,
            text=True, capture_output=True, check=True,
        ).stdout
        self.assertEqual(after_status, before_status)
        self.assertEqual(after_head, before_head)
        self.assertEqual(index.read_text(encoding="utf-8"), "<title>Uncommitted</title>\n")
        self.assertFalse((repo / "webkit").exists())
        self.assertFalse((repo / "AGENTS.md").exists())
        managed_path = Path(project["path"])
        self.assertNotEqual(managed_path, repo)
        self.assertTrue(project["managedCheckout"])
        self.assertTrue(project["sourceIntegrationPending"])
        self.assertTrue((managed_path / "webkit" / "webkit.config.json").is_file())
        self.assertEqual(
            subprocess.run(
                ["git", "status", "--porcelain=v1", "--untracked-files=all"],
                cwd=managed_path, text=True, capture_output=True, check=True,
            ).stdout,
            "",
        )

        subprocess.run(["git", "restore", "index.html"], cwd=repo, check=True)
        integrated = self.app.projects.integrate_managed_target(project)
        self.assertTrue(integrated["integrated"])
        self.assertFalse(integrated["pending"])
        self.assertFalse(
            self.app.projects.get_project(project["id"])["sourceIntegrationPending"]
        )
        self.assertTrue((repo / "webkit" / "webkit.config.json").is_file())

    def test_add_existing_isolates_untracked_repository_file_without_changes(self):
        repo = self.root / "dirty-untracked"
        repo.mkdir()
        (repo / "index.html").write_text("<title>Committed</title>\n", encoding="utf-8")
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
        subprocess.run(["git", "add", "index.html"], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-m", "Initial"], cwd=repo, check=True, capture_output=True
        )
        notes = repo / "private-notes.txt"
        notes.write_bytes(b"untracked user data\n")
        before_status = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=repo, text=True, capture_output=True, check=True,
        ).stdout

        with mock.patch.object(
            self.app.projects, "_find_port_block", return_value=6431
        ):
            project = self.app.projects.add_existing(str(repo), "codex")

        after_status = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=repo, text=True, capture_output=True, check=True,
        ).stdout
        self.assertEqual(after_status, before_status)
        self.assertEqual(notes.read_bytes(), b"untracked user data\n")
        self.assertFalse((repo / "webkit").exists())
        self.assertFalse((repo / "AGENTS.md").exists())
        managed_path = Path(project["path"])
        self.assertNotEqual(managed_path, repo)
        self.assertTrue(project["managedCheckout"])
        self.assertTrue(project["sourceIntegrationPending"])
        self.assertTrue((managed_path / "webkit" / "webkit.config.json").is_file())
        self.assertEqual(
            subprocess.run(
                ["git", "status", "--porcelain=v1", "--untracked-files=all"],
                cwd=managed_path, text=True, capture_output=True, check=True,
            ).stdout,
            "",
        )

    def test_add_existing_non_git_no_entry_failure_is_read_only(self):
        project_path = self.root / "not-a-website"
        nested = project_path / "notes"
        nested.mkdir(parents=True)
        (project_path / "README.txt").write_bytes(b"keep this exact content\n")
        (nested / "plan.txt").write_bytes(b"keep this too\x00\xff")

        def snapshot():
            result = []
            for item in sorted(project_path.rglob("*")):
                relative = item.relative_to(project_path).as_posix()
                if item.is_dir():
                    result.append((relative, "directory", None))
                else:
                    result.append((relative, "file", item.read_bytes()))
            return result

        before = snapshot()
        with self.assertRaisesRegex(ControlCenterError, "Initialize Git"):
            self.app.projects.add_existing(str(project_path), "codex")

        self.assertEqual(snapshot(), before)
        self.assertFalse((project_path / ".git").exists())
        self.assertFalse((project_path / "webkit").exists())
        self.assertFalse((project_path / "AGENTS.md").exists())

    def test_add_existing_non_git_invalid_support_path_is_read_only(self):
        project_path = self.root / "invalid-support-path"
        agents_path = project_path / "AGENTS.md"
        agents_path.mkdir(parents=True)
        (project_path / "index.html").write_bytes(b"<title>Keep</title>\n")
        sentinel = agents_path / "keep.txt"
        sentinel.write_bytes(b"user-owned directory content\n")

        with self.assertRaisesRegex(ControlCenterError, "Initialize Git"):
            self.app.projects.add_existing(str(project_path), "codex")

        self.assertEqual(
            (project_path / "index.html").read_bytes(), b"<title>Keep</title>\n"
        )
        self.assertEqual(sentinel.read_bytes(), b"user-owned directory content\n")
        self.assertTrue(agents_path.is_dir())
        self.assertFalse((project_path / ".git").exists())
        self.assertFalse((project_path / "webkit").exists())

    def test_add_existing_old_kit_gets_controller_files_without_overwrite(self):
        project_path = self.root / "old-kit"
        (project_path / "webkit").mkdir(parents=True)
        (project_path / "index.html").write_text("<title>Old</title>", encoding="utf-8")
        (project_path / "webkit" / "SETUP.md").write_text("local setup\n", encoding="utf-8")
        (project_path / "webkit" / "VERSION").write_text(
            (KIT_ROOT / "webkit" / "VERSION").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (project_path / "AGENTS.md").write_text("## Webkit\n\nOlder pointer.\n", encoding="utf-8")
        subprocess.run(["git", "init", "-b", "main"], cwd=project_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=project_path, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=project_path, check=True)
        subprocess.run(["git", "add", "-A"], cwd=project_path, check=True)
        subprocess.run(["git", "commit", "-m", "Old project"], cwd=project_path, check=True, capture_output=True)
        with mock.patch.object(self.app.projects, "_find_port_block", return_value=6331):
            project = self.app.projects.add_existing(str(project_path), "codex")
        managed_path = Path(project["path"])
        self.assertTrue(project["managedCheckout"])
        self.assertNotEqual(managed_path, project_path)
        self.assertEqual((managed_path / "webkit" / "SETUP.md").read_text(), "local setup\n")
        self.assertTrue((managed_path / "webkit" / "CONTROL-CENTER.md").exists())
        agents = (managed_path / "AGENTS.md").read_text()
        self.assertIn("Older pointer.", agents)
        self.assertIn("## Webkit Control Center", agents)

    def test_add_existing_accepts_an_already_checked_out_feature_worktree(self):
        repo = self.root / "checked-out-repo"
        repo.mkdir()
        (repo / "index.html").write_text("<title>Main</title>", encoding="utf-8")
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "Initial"], cwd=repo, check=True, capture_output=True)
        feature_checkout = self.root / "feature-checkout"
        subprocess.run(
            ["git", "worktree", "add", "-b", "feature/test", str(feature_checkout), "main"],
            cwd=repo, check=True, capture_output=True,
        )
        with mock.patch.object(self.app.projects, "_find_port_block", return_value=6336):
            project = self.app.projects.add_existing(str(feature_checkout), "codex")
        managed_path = Path(project["path"])
        self.assertTrue(project["managedCheckout"])
        self.assertEqual(Path(project["sourcePath"]), feature_checkout.resolve())
        self.assertTrue(project["baseBranch"].startswith("webkit/control-center/"))
        self.assertEqual(
            subprocess.run(
                ["git", "branch", "--show-current"], cwd=managed_path,
                text=True, capture_output=True, check=True,
            ).stdout.strip(),
            project["baseBranch"],
        )

    def test_managed_target_integration_refuses_dirty_or_diverged_source(self):
        for scenario in ("dirty", "diverged"):
            with self.subTest(scenario=scenario):
                repo = self.root / ("managed-source-" + scenario)
                repo.mkdir()
                (repo / "index.html").write_text("<title>Initial</title>\n", encoding="utf-8")
                subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
                subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
                subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
                subprocess.run(["git", "add", "index.html"], cwd=repo, check=True)
                subprocess.run(["git", "commit", "-m", "Initial"], cwd=repo, check=True, capture_output=True)
                with mock.patch.object(
                    self.app.projects, "_find_port_block", return_value=6420
                ):
                    project = self.app.projects.add_existing(str(repo), "codex")
                managed = Path(project["path"])
                (managed / "index.html").write_text(
                    "<title>Managed next</title>\n", encoding="utf-8"
                )
                subprocess.run(["git", "add", "index.html"], cwd=managed, check=True)
                subprocess.run(
                    ["git", "commit", "-m", "Managed next"],
                    cwd=managed, check=True, capture_output=True,
                )
                source_before = subprocess.run(
                    ["git", "rev-parse", "main"], cwd=repo,
                    text=True, capture_output=True, check=True,
                ).stdout.strip()
                if scenario == "dirty":
                    (repo / "local-notes.txt").write_text(
                        "preserve me\n", encoding="utf-8"
                    )
                    expression = "Commit or stash"
                else:
                    (repo / "source-only.txt").write_text(
                        "source advance\n", encoding="utf-8"
                    )
                    subprocess.run(["git", "add", "source-only.txt"], cwd=repo, check=True)
                    subprocess.run(
                        ["git", "commit", "-m", "Source advance"],
                        cwd=repo, check=True, capture_output=True,
                    )
                    source_before = subprocess.run(
                        ["git", "rev-parse", "main"], cwd=repo,
                        text=True, capture_output=True, check=True,
                    ).stdout.strip()
                    expression = "advanced or diverged"
                with self.assertRaisesRegex(ControlCenterError, expression):
                    self.app.projects.integrate_managed_target(project)
                source_after = subprocess.run(
                    ["git", "rev-parse", "main"], cwd=repo,
                    text=True, capture_output=True, check=True,
                ).stdout.strip()
                self.assertEqual(source_after, source_before)
                if scenario == "dirty":
                    self.assertEqual(
                        (repo / "local-notes.txt").read_text(encoding="utf-8"),
                        "preserve me\n",
                    )

    def test_managed_target_is_integrated_before_a_push_failure(self):
        repo = self.root / "managed-push-failure"
        repo.mkdir()
        (repo / "index.html").write_text("<title>Initial</title>\n", encoding="utf-8")
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
        subprocess.run(["git", "add", "index.html"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "Initial"], cwd=repo, check=True, capture_output=True)
        with mock.patch.object(self.app.projects, "_find_port_block", return_value=6430):
            project = self.app.projects.add_existing(str(repo), "codex")
        managed = Path(project["path"])
        (managed / "index.html").write_text("<title>Integrated</title>\n", encoding="utf-8")
        subprocess.run(["git", "add", "index.html"], cwd=managed, check=True)
        subprocess.run(
            ["git", "commit", "-m", "Integrated change"],
            cwd=managed, check=True, capture_output=True,
        )
        expected = subprocess.run(
            ["git", "rev-parse", project["baseBranch"]], cwd=managed,
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        self.app.projects.integrate_managed_target(project)
        connected = {
            "connected": True, "remote": "origin",
            "url": "https://github.com/example/site.git",
            "fetchUrl": "https://github.com/example/site.git",
            "pushUrl": "https://github.com/example/site.git",
        }
        with mock.patch.object(
            self.app.projects, "github_status", return_value=connected
        ), mock.patch.object(
            self.app.projects, "_validated_push_boundary",
            return_value={"pushed": False, "verified": False, "error": "push failed"},
        ):
            result = self.app.projects.push_to_github(project)
        self.assertFalse(result["pushed"])
        source_head = subprocess.run(
            ["git", "rev-parse", "main"], cwd=repo,
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        self.assertEqual(source_head, expected)
        self.assertEqual(
            (repo / "index.html").read_text(encoding="utf-8"),
            "<title>Integrated</title>\n",
        )

    def test_github_remote_is_detected_without_network_access(self):
        repo = self.root / "github-repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "remote", "add", "origin", "git@github.com:example/site.git"],
            cwd=repo, check=True,
        )
        status = self.app.projects.github_status(repo)
        self.assertTrue(status["connected"])
        self.assertEqual(status["remote"], "origin")

    def test_github_status_skips_option_like_remote_names_and_uses_terminator(self):
        calls = []

        def command_result(command, **_kwargs):
            calls.append(command)
            if command == ["git", "remote"]:
                return subprocess.CompletedProcess(command, 0, "-upload-pack\norigin\n", "")
            if command == ["git", "remote", "get-url", "--", "origin"]:
                return subprocess.CompletedProcess(
                    command, 0, "https://github.com/example/site.git\n", ""
                )
            if command == ["git", "remote", "get-url", "--push", "--", "origin"]:
                return subprocess.CompletedProcess(
                    command, 0, "https://github.com/example/site.git\n", ""
                )
            if command[:3] == ["git", "rev-parse", "--abbrev-ref"]:
                return subprocess.CompletedProcess(command, 1, "", "")
            raise AssertionError(command)

        with mock.patch("control_center.run_command", side_effect=command_result):
            status = self.app.projects.github_status(self.root)
        self.assertTrue(status["connected"])
        self.assertEqual(status["remote"], "origin")
        self.assertFalse(any("-upload-pack" in command for command in calls[1:]))

    def test_github_commands_reject_unsafe_remote_and_redact_credentials(self):
        with mock.patch("control_center.run_command") as run:
            unsafe = self.app.projects._verified_push(
                self.root, "-upload-pack", "main", "main"
            )
        self.assertFalse(unsafe["pushed"])
        self.assertIn("unsafe", unsafe["error"])
        run.assert_not_called()

        secret = "private-token-value"
        failure = subprocess.CompletedProcess(
            ["git", "push"], 1, "",
            "fatal: unable to access 'https://user:{}@github.com/example/site.git?token={}': denied".format(
                secret, secret
            ),
        )
        destination_url = "https://github.com/example/site.git"
        destination = {
            "remote": "origin", "url": destination_url,
            "pushUrl": destination_url, "repository": "example/site",
        }
        with mock.patch("control_center.run_command", return_value=failure) as run, mock.patch.object(
            control_center_module.ProjectManager,
            "_validated_github_remote", return_value=destination
        ):
            result = self.app.projects._verified_push(
                self.root, "origin", "main", "main",
                validated_sha="a" * 40,
                validated_push_url=destination_url,
            )
        self.assertFalse(result["pushed"])
        self.assertNotIn(secret, result["error"])
        self.assertNotIn("?token=", result["error"])
        self.assertEqual(
            run.call_args[0][0], [
                "git", "push", "--force-with-lease=refs/heads/main:", "--",
                destination_url,
                "{}:refs/heads/main".format("a" * 40),
            ]
        )

    def test_github_push_is_not_reported_until_remote_sha_is_verified(self):
        local_sha = "a" * 40
        remote_sha = "b" * 40

        def command_result(command, **_kwargs):
            if command[:2] == ["git", "push"]:
                return subprocess.CompletedProcess(command, 0, "", "")
            if command[:3] == ["git", "rev-parse", "--verify"]:
                return subprocess.CompletedProcess(command, 0, local_sha + "\n", "")
            if command[:3] == ["git", "ls-remote", "--exit-code"]:
                return subprocess.CompletedProcess(
                    command, 0, remote_sha + "\trefs/heads/main\n", ""
                )
            raise AssertionError(command)

        destination = {
            "remote": "origin",
            "url": "https://github.com/example/site.git",
            "pushUrl": "https://github.com/example/site.git",
            "repository": "example/site",
        }
        with mock.patch("control_center.run_command", side_effect=command_result), mock.patch.object(
            control_center_module.ProjectManager,
            "_validated_github_remote", return_value=destination
        ):
            result = self.app.projects._verified_push(
                self.root, "origin", "main", "main"
            )

        self.assertFalse(result["pushed"])
        self.assertFalse(result["verified"])
        self.assertIn("could not be verified", result["error"])

    def test_existing_github_remote_push_failure_is_surfaced(self):
        connected = {
            "connected": True, "remote": "origin", "url": "https://github.com/example/site"
        }
        failure = {"pushed": False, "verified": False, "error": "push rejected"}
        with mock.patch.object(
            self.app.projects, "github_status", return_value=connected
        ), mock.patch.object(
            self.app.projects, "_validated_push_boundary", return_value=failure
        ):
            result = self.app.projects._ensure_github_repo(
                self.root, "site", "main", "main"
            )
        self.assertTrue(result["attempted"])
        self.assertFalse(result["pushed"])
        self.assertEqual(result["error"], "push rejected")

    def test_created_github_repo_push_failure_is_surfaced(self):
        disconnected = {"connected": False, "remote": None, "url": None}
        connected = {
            "connected": True, "remote": "origin", "url": "https://github.com/example/site"
        }
        success = subprocess.CompletedProcess(["gh"], 0, "", "")
        with mock.patch.object(
            self.app.projects, "github_status", side_effect=[disconnected, connected]
        ), mock.patch.object(
            self.app.projects, "_github_cli_status",
            return_value={"installed": True, "authenticated": True, "path": "/fake/gh"},
        ), mock.patch(
            "control_center.run_command", return_value=success
        ), mock.patch.object(
            self.app.projects, "_validated_push_boundary",
            return_value={"pushed": False, "verified": False, "error": "push failed"},
        ):
            result = self.app.projects._ensure_github_repo(
                self.root, "site", "main", "main"
            )
        self.assertTrue(result["connected"])
        self.assertTrue(result["attempted"])
        self.assertFalse(result["verified"])
        self.assertEqual(result["error"], "push failed")

    def test_optional_github_exception_never_deletes_the_local_project(self):
        parent = self.root / "github-timeout-projects"
        parent.mkdir()
        timeout = subprocess.TimeoutExpired(["gh", "repo", "create"], 120)
        with mock.patch.object(
            self.app.projects, "_find_port_block", return_value=6339
        ), mock.patch.object(
            self.app.projects, "_ensure_github_repo", side_effect=timeout
        ):
            project = self.app.projects.create_project(
                "Local Survives", str(parent), "codex"
            )
        project_path = Path(project["path"])
        self.assertTrue(project_path.is_dir())
        self.assertTrue((project_path / ".git").is_dir())
        self.assertEqual(self.app.projects.get_project(project["id"])["path"], str(project_path))
        self.assertTrue(project["githubSetup"]["attempted"])
        self.assertFalse(project["githubSetup"]["verified"])

    def test_unpushed_main_commit_is_detected_and_can_be_pushed(self):
        parent = self.root / "projects"
        parent.mkdir()
        with mock.patch.object(self.app.projects, "_find_port_block", return_value=6338), mock.patch.object(
            self.app.projects, "_ensure_github_repo"
        ):
            project = self.app.projects.create_project("Push Site", str(parent), "codex")
        project_path = Path(project["path"])
        remote = self.root / "remote.git"
        subprocess.run(
            ["git", "init", "--bare", "--initial-branch=main", str(remote)],
            check=True, capture_output=True,
        )
        subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=project_path, check=True)
        subprocess.run(["git", "push", "-u", "origin", "main"], cwd=project_path, check=True, capture_output=True)
        (project_path / "index.html").write_text("<title>Ready to push</title>\n", encoding="utf-8")
        subprocess.run(["git", "add", "index.html"], cwd=project_path, check=True)
        subprocess.run(["git", "commit", "-m", "Ready to push"], cwd=project_path, check=True, capture_output=True)
        connected = {
            "connected": True, "remote": "origin",
            "url": "git@github.com:example/site.git", "pushUrl": str(remote),
        }
        local_destination = {
            "remote": "origin", "url": str(remote), "pushUrl": str(remote),
            "repository": "example/site",
        }
        with mock.patch.object(
            self.app.projects, "github_status", return_value=connected
        ), mock.patch.object(
            control_center_module.ProjectManager,
            "_validated_github_remote", return_value=local_destination
        ), mock.patch(
            "control_center.safe_pinned_remote_url", side_effect=lambda value: value
        ):
            status = self.app.projects.github_sync_status(project)
            self.assertTrue(status["unpushed"])
            self.assertEqual(status["ahead"], 1)
            result = self.app.projects.push_project(project["id"])
            self.assertTrue(result["pushed"])
            self.assertFalse(result["github"]["unpushed"])

    def test_diverged_push_records_persistent_agent_action(self):
        project = {
            "id": "diverged-project",
            "name": "Diverged Project",
            "slug": "diverged-project",
            "path": str(self.root),
            "provider": "codex",
            "baseBranch": "main",
            "targetBranch": "main",
        }
        self.app.store.update(
            lambda state: state.setdefault("projects", []).append(project)
        )
        failure = ControlCenterError(
            "The GitHub target is not an ancestor of the validated local commit. Sync before pushing.",
            409,
            {"code": control_center_module.GITHUB_TARGET_DIVERGED},
        )
        with mock.patch.object(
            self.app.projects, "integrate_managed_target"
        ), mock.patch.object(
            self.app.projects,
            "github_sync_status",
            return_value={"connected": True, "unpushed": True},
        ), mock.patch.object(
            self.app.projects, "_push_to_github", side_effect=failure
        ):
            with self.assertRaises(ControlCenterError) as raised:
                self.app.projects.push_project(project["id"])

        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(
            raised.exception.details["code"],
            control_center_module.GITHUB_TARGET_DIVERGED,
        )
        issue = raised.exception.details["issue"]
        self.assertEqual(issue["action"], "handle_with_agent")
        stored = self.app.projects.get_project(project["id"])["issue"]
        self.assertEqual(stored, issue)

    def test_successful_push_clears_divergence_issue(self):
        project = {
            "id": "resolved-project",
            "name": "Resolved Project",
            "slug": "resolved-project",
            "path": str(self.root),
            "provider": "codex",
            "baseBranch": "main",
            "targetBranch": "main",
            "issue": {
                "code": control_center_module.GITHUB_TARGET_DIVERGED,
                "message": "Resolve me",
                "action": "handle_with_agent",
                "createdAt": "2026-08-20T00:00:00Z",
            },
        }
        self.app.store.update(
            lambda state: state.setdefault("projects", []).append(project)
        )
        statuses = [
            {"connected": True, "unpushed": True},
            {"connected": True, "unpushed": False},
        ]
        with mock.patch.object(
            self.app.projects, "integrate_managed_target"
        ), mock.patch.object(
            self.app.projects, "github_sync_status", side_effect=statuses
        ), mock.patch.object(
            self.app.projects, "push_to_github", return_value={"pushed": True}
        ):
            result = self.app.projects.push_project(project["id"])

        self.assertTrue(result["pushed"])
        self.assertNotIn("issue", self.app.projects.get_project(project["id"]))

    def test_issue_agent_uses_an_uncolored_isolated_worktree(self):
        parent = self.root / "issue-agent-projects"
        parent.mkdir()
        with mock.patch.object(
            self.app.projects, "_find_port_block", return_value=6344
        ), mock.patch.object(self.app.projects, "_ensure_github_repo"):
            project = self.app.projects.create_project(
                "Issue Agent Site", str(parent), "codex"
            )
        issue = self.app.projects.record_project_issue(
            project["id"],
            control_center_module.GITHUB_TARGET_DIVERGED,
            "GitHub history diverged.",
        )
        tool_status = {
            "codex": {"installed": True},
            "claude": {"installed": True},
        }
        snapshot = {
            "ref": "refs/awesome-webkit/support/123456789abc",
            "sha": "1" * 40,
            "target": "main",
        }
        with mock.patch.object(
            self.app.projects, "system_status", return_value=tool_status
        ), mock.patch.object(
            self.app.projects,
            "snapshot_github_target_for_agent",
            return_value=snapshot,
        ), mock.patch.object(SessionRuntime, "start"):
            session = self.app.sessions.start_issue_session(
                project["id"], issue["code"], "high"
            )

        self.assertEqual(session["kind"], "support")
        self.assertEqual(session["color"], "agent")
        self.assertNotIn("previewUrl", session)
        self.assertTrue(session["branch"].startswith("webkit/agent/"))
        self.assertEqual(session["supportRef"], snapshot["ref"])
        self.assertTrue(Path(session["worktree"]).is_dir())
        self.assertEqual(session["reasoningEffort"], "high")
        runtime = self.app.sessions.runtimes[session["id"]]
        queued = runtime.jobs.get_nowait()
        self.assertEqual(queued["source"], "chat")
        self.assertIn(
            "without losing either side", queued["prompt"].replace("\n", " ")
        )
        stored = self.app.projects.get_project(project["id"])["issue"]
        self.assertEqual(stored["supportSessionId"], session["id"])

        with mock.patch.object(SessionRuntime, "start") as start_again:
            reused = self.app.sessions.start_issue_session(
                project["id"], issue["code"], "medium"
            )
        self.assertEqual(reused["id"], session["id"])
        start_again.assert_not_called()

    def test_issue_agent_gets_an_immutable_validated_remote_snapshot(self):
        repo = self.root / "snapshot-project"
        remote = self.root / "snapshot-remote.git"
        repo.mkdir()
        subprocess.run(
            ["git", "init", "-b", "main"], cwd=repo,
            check=True, capture_output=True,
        )
        (repo / "index.html").write_text("<title>Snapshot</title>\n", encoding="utf-8")
        subprocess.run(["git", "add", "index.html"], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-m", "Initial"], cwd=repo,
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "init", "--bare", "--initial-branch=main", str(remote)],
            check=True, capture_output=True,
        )
        subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True)
        subprocess.run(
            ["git", "push", "origin", "main"], cwd=repo,
            check=True, capture_output=True,
        )
        project = {
            "path": str(repo),
            "targetBranch": "main",
        }
        support_ref = "refs/awesome-webkit/support/123456789abc"
        destination = {
            "remote": "origin",
            "url": str(remote),
            "fetchUrl": str(remote),
            "pushUrl": str(remote),
            "repository": "example/snapshot",
        }
        connected = {"connected": True, "remote": "origin"}
        with mock.patch.object(
            self.app.projects, "github_status", return_value=connected
        ), mock.patch.object(
            self.app.projects, "_validated_github_remote", return_value=destination
        ), mock.patch.object(
            self.app.projects, "_require_remote_tree_private"
        ), mock.patch(
            "control_center.load_webkit_config", return_value={"feedback_dir": ".webkit/feedback"}
        ), mock.patch(
            "control_center.require_private_runtime_paths_safe"
        ):
            snapshot = self.app.projects.snapshot_github_target_for_agent(
                project, support_ref
            )

        expected = subprocess.run(
            ["git", "rev-parse", "main"], cwd=repo,
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        self.assertEqual(snapshot["sha"], expected)
        self.assertEqual(snapshot["ref"], support_ref)
        self.assertTrue(
            self.app.projects.delete_issue_agent_snapshot(
                repo, support_ref, expected
            )
        )
        missing = subprocess.run(
            ["git", "show-ref", "--verify", support_ref], cwd=repo,
            check=False, capture_output=True,
        )
        self.assertNotEqual(missing.returncode, 0)

    def test_push_project_rejects_add_delete_history_for_historical_feedback_root(self):
        parent = self.root / "private-push-projects"
        parent.mkdir()
        with mock.patch.object(
            self.app.projects, "_find_port_block", return_value=6343
        ), mock.patch.object(self.app.projects, "_ensure_github_repo"):
            project = self.app.projects.create_project(
                "Private Push Site", str(parent), "codex"
            )
        project_path = Path(project["path"])
        remote = self.root / "private-push-remote.git"
        subprocess.run(
            ["git", "init", "--bare", "--initial-branch=main", str(remote)],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "remote", "add", "origin", str(remote)],
            cwd=project_path, check=True,
        )
        subprocess.run(
            ["git", "push", "-u", "origin", "main"],
            cwd=project_path, check=True, capture_output=True,
        )
        config_path = project_path / "webkit" / "webkit.config.json"
        original_config = json.loads(config_path.read_text(encoding="utf-8"))
        custom_config = dict(original_config)
        custom_config["feedback_dir"] = "private-feedback"
        config_path.write_text(json.dumps(custom_config, indent=2) + "\n", encoding="utf-8")
        with (project_path / ".gitignore").open("a", encoding="utf-8") as handle:
            handle.write("private-feedback/\n")
        private_file = project_path / "private-feedback" / "leak.txt"
        private_file.parent.mkdir()
        private_file.write_text("must never be pushed\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "webkit/webkit.config.json", ".gitignore"],
            cwd=project_path, check=True,
        )
        subprocess.run(
            ["git", "add", "-f", "private-feedback/leak.txt"],
            cwd=project_path, check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Transient private data"],
            cwd=project_path, check=True, capture_output=True,
        )
        private_file.unlink()
        config_path.write_text(json.dumps(original_config, indent=2) + "\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=project_path, check=True)
        subprocess.run(
            ["git", "commit", "-m", "Remove transient data"],
            cwd=project_path, check=True, capture_output=True,
        )
        connected = {
            "connected": True,
            "remote": "origin",
            "url": "https://github.com/example/private-push.git",
            "pushUrl": str(remote),
        }
        local_destination = {
            "remote": "origin", "url": str(remote), "fetchUrl": str(remote),
            "pushUrl": str(remote), "repository": "example/private-push",
        }
        with mock.patch.object(
            self.app.projects, "github_status", return_value=connected
        ), mock.patch(
            "control_center.safe_pinned_remote_url", side_effect=lambda value: value
        ), mock.patch.object(
            self.app.projects, "_validated_github_remote",
            return_value=local_destination,
        ), mock.patch.object(self.app.projects, "_verified_push") as push:
            with self.assertRaisesRegex(ControlCenterError, "private Webkit runtime"):
                self.app.projects.push_project(project["id"])
        push.assert_not_called()

    def test_private_feedback_ignore_must_cover_normal_protocol_files(self):
        repo = self.root / "partial-feedback-ignore"
        repo.mkdir()
        subprocess.run(
            ["git", "init", "-b", "main"], cwd=repo,
            check=True, capture_output=True,
        )
        (repo / ".gitignore").write_text(
            ".webkit/\ncustom-feedback/.*\n", encoding="utf-8"
        )
        config = {"feedback_dir": "custom-feedback"}

        with self.assertRaisesRegex(ControlCenterError, "normal feedback protocol"):
            control_center_module.require_private_runtime_paths_safe(repo, config)

    def test_private_runtime_roots_reject_portable_absolute_and_git_paths(self):
        for value in (
            "/tmp/feedback", "C:\\temp\\feedback", ".GIT/feedback",
            "nested/.Git/feedback",
        ):
            with self.subTest(value=value), self.assertRaisesRegex(
                ControlCenterError, "unsafe"
            ):
                control_center_module.private_runtime_roots(
                    {"feedback_dir": value}
                )

    def test_add_existing_rejects_a_subfolder_of_another_repository(self):
        parent_repo = self.root / "parent-repo"
        project_path = parent_repo / "website"
        project_path.mkdir(parents=True)
        (project_path / "index.html").write_text("<title>Nested</title>", encoding="utf-8")
        subprocess.run(["git", "init", "-b", "main"], cwd=parent_repo, check=True, capture_output=True)
        with self.assertRaisesRegex(Exception, "repository root"):
            self.app.projects.add_existing(str(project_path), "codex")
        self.assertFalse((project_path / "webkit").exists())

    def test_exact_color_lock_is_owner_guarded(self):
        lock_dir = self.root / "locks"
        owner = self.root / "worktree"
        owner.mkdir()
        config = {
            "lock_dir": str(lock_dir),
            "palette": [{"slug": "red", "emoji": "🔴", "port": 6520}],
        }
        SessionManager._claim_lock(config, "red", owner)
        self.assertEqual((lock_dir / "red.lock" / "owner").read_text().strip(), str(owner.resolve()))
        with self.assertRaises(Exception):
            SessionManager._claim_lock(config, "red", self.root / "other")

    def test_windows_maps_portable_tmp_lock_to_native_temp_directory(self):
        native_temp = self.root / "windows-temp"
        native_temp.mkdir()
        owner = self.root / "windows-worktree"
        owner.mkdir()
        config = {
            "project_name": "portable-site",
            "lock_dir": "/tmp/portable-site-agent-colors",
            "palette": [{"slug": "red", "emoji": "🔴", "port": 6521}],
        }
        with mock.patch("control_center.platform.system", return_value="Windows"), mock.patch(
            "control_center.tempfile.gettempdir", return_value=str(native_temp)
        ):
            resolved = configured_lock_dir(config)
            self.assertEqual(resolved, native_temp / "portable-site-agent-colors")
            self.assertTrue(resolved.is_absolute())
            SessionManager._claim_lock(config, "red", owner)
        self.assertEqual(
            (resolved / "red.lock" / "owner").read_text(encoding="utf-8").strip(),
            str(owner.resolve()),
        )

    def test_settings_persist_and_refresh_active_previews(self):
        with mock.patch.object(
            self.app.sessions, "refresh_previews_for_settings",
            return_value={"restarted": 2, "deferred": 1},
        ) as refresh:
            result = self.app.save_settings({
                "dictationMode": "voice-note",
                "interactionMode": "draw-default",
                "toggleHotkey": "Backquote",
                "dictateHotkey": "KeyD",
            })
        self.assertEqual(result["settings"]["dictationMode"], "voice-note")
        self.assertEqual(result["settings"]["interactionMode"], "draw-default")
        self.assertEqual(result["settings"]["toggleHotkey"], "Backquote")
        self.assertEqual(result["settings"]["dictateHotkey"], "KeyD")
        self.assertEqual(self.app.bootstrap()["settings"]["dictationMode"], "voice-note")
        self.assertEqual(self.app.bootstrap()["settings"]["interactionMode"], "draw-default")
        self.assertEqual(result["previews"], {"restarted": 2, "deferred": 1})
        refresh.assert_called_once_with()

    def test_preferences_persist_providers_and_settings_in_one_update(self):
        with mock.patch.object(
            self.app.projects, "validated_providers", return_value=["codex"]
        ) as validate, mock.patch.object(
            self.app.sessions, "refresh_previews_for_settings",
            return_value={"restarted": 1, "deferred": 0},
        ) as refresh:
            result = self.app.save_preferences({
                "providers": ["codex"],
                "dictationMode": "voice-note",
                "interactionMode": "draw-default",
                "toggleHotkey": "Backquote",
                "dictateHotkey": "KeyD",
            })
        state = self.app.store.read()
        self.assertEqual(state["providers"], ["codex"])
        self.assertEqual(state["settings"], result["settings"])
        self.assertEqual(result["previews"], {"restarted": 1, "deferred": 0})
        validate.assert_called_once_with(["codex"])
        refresh.assert_called_once_with()

    def test_invalid_preferences_leave_providers_and_settings_unchanged(self):
        before = self.app.store.read()
        with mock.patch.object(
            self.app.projects, "validated_providers", return_value=["codex"]
        ), self.assertRaisesRegex(
            ControlCenterError, "browser speech, local agent voice notes"
        ):
            self.app.save_preferences({
                "providers": ["codex"],
                "dictationMode": "invalid",
            })
        after = self.app.store.read()
        self.assertEqual(after.get("providers"), before.get("providers"))
        self.assertEqual(after.get("settings"), before.get("settings"))

    def test_voice_note_setting_rejects_unknown_mode(self):
        with self.assertRaisesRegex(Exception, "browser speech, local agent voice notes"):
            self.app.save_settings({"dictationMode": "cosmetic-only"})

    def test_cloud_voice_note_setting_requires_and_detects_openai_key(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False):
            self.assertFalse(
                self.app.projects.system_status()["cloudVoiceTranscription"]["available"]
            )
            with self.assertRaisesRegex(ControlCenterError, "OPENAI_API_KEY"):
                self.app.save_settings({"dictationMode": "cloud-voice-note"})
        with mock.patch.dict(
            os.environ, {"OPENAI_API_KEY": "test-key-not-real"}, clear=False
        ), mock.patch.object(
            self.app.sessions, "refresh_previews_for_settings",
            return_value={"restarted": 0, "deferred": 0},
        ):
            result = self.app.save_settings({"dictationMode": "cloud-voice-note"})
        self.assertEqual(result["settings"]["dictationMode"], "cloud-voice-note")

    def test_merged_session_can_be_dismissed_but_active_session_cannot(self):
        self.app.store.update(lambda state: state["sessions"].extend([
            {"id": "merged-card", "projectId": "project", "status": "merged"},
            {"id": "active-card", "projectId": "project", "status": "active"},
        ]))
        EventLog(self.state_dir, "merged-card").append("system", "merged")
        with self.assertRaisesRegex(ControlCenterError, "explicitly confirmed"):
            self.app.sessions.dismiss_merged("merged-card", None)
        self.assertIn(
            "merged-card", {item["id"] for item in self.app.store.read()["sessions"]}
        )
        result = self.app.sessions.dismiss_merged("merged-card", "merged-card")
        self.assertEqual(result, {"dismissed": True})
        self.assertNotIn(
            "merged-card", {item["id"] for item in self.app.store.read()["sessions"]}
        )
        self.assertFalse((self.state_dir / "logs" / "merged-card.jsonl").exists())
        with self.assertRaisesRegex(ControlCenterError, "Only a merged session"):
            self.app.sessions.dismiss_merged("active-card", "active-card")

    def test_merged_session_survives_startup_recovery_until_dismissed(self):
        session = {
            "id": "merged-after-restart",
            "projectId": "project",
            "provider": "codex",
            "color": "blue",
            "status": "merged",
            "updatedAt": "2026-08-23T10:16:04Z",
        }
        self.app.store.update(
            lambda state: state.setdefault("sessions", []).append(session)
        )

        self.assertTrue(self.app.sessions.recover())
        recovered = self.app.sessions._get_session("merged-after-restart")
        self.assertEqual(recovered["status"], "merged")

    def test_interaction_setting_rejects_unknown_mode(self):
        with self.assertRaisesRegex(Exception, "normal website clicks or immediate rectangle drawing"):
            self.app.save_settings({"interactionMode": "sometimes"})

    def test_old_settings_state_gets_click_first_default(self):
        self.app.store.update(lambda state: state.update({"settings": {"dictationMode": "voice-note"}}))
        self.assertEqual(self.app.bootstrap()["settings"], {
            "dictationMode": "voice-note",
            "interactionMode": "browse-default",
            "toggleHotkey": "KeyC",
            "dictateHotkey": "KeyV",
            "fastModeNoticeSeen": False,
        })

    def test_fast_mode_notice_acknowledgement_persists_without_preview_restart(self):
        with mock.patch.object(
            self.app.sessions, "refresh_previews_for_settings"
        ) as refresh:
            result = self.app.acknowledge_fast_mode_notice()
        self.assertEqual(result, {"fastModeNoticeSeen": True})
        self.assertTrue(self.app.bootstrap()["settings"]["fastModeNoticeSeen"])
        refresh.assert_not_called()

    def test_session_speed_updates_persist_and_reject_unknown_values(self):
        session = {
            "id": "speed-session", "projectId": "project", "provider": "codex",
            "color": "blue", "status": "active", "speedMode": "normal",
        }
        self.app.store.update(
            lambda state: state.setdefault("sessions", []).append(dict(session))
        )
        result = self.app.sessions.set_speed("speed-session", "fast")
        self.assertEqual(result, {"speedMode": "fast"})
        self.assertEqual(
            self.app.sessions._get_session("speed-session")["speedMode"], "fast"
        )
        with self.assertRaisesRegex(ControlCenterError, "normal or fast"):
            self.app.sessions.set_speed("speed-session", "turbo")

    def test_hotkey_settings_reject_reserved_or_duplicate_keys(self):
        with self.assertRaisesRegex(Exception, "not a modifier or Escape"):
            self.app.save_settings({"toggleHotkey": "AltLeft"})
        with self.assertRaisesRegex(Exception, "different shortcut keys"):
            self.app.save_settings({"toggleHotkey": "KeyV", "dictateHotkey": "KeyV"})

    def test_custom_hotkeys_flow_into_new_project_config(self):
        (self.root / "index.html").write_text("<title>Hotkeys</title>", encoding="utf-8")
        self.app.store.update(lambda state: state.update({"settings": {
            "toggleHotkey": "Slash",
            "dictateHotkey": "Space",
        }}))
        with mock.patch.object(self.app.projects, "_find_port_block", return_value=6351):
            config = self.app.projects._make_config(self.root)
        self.assertEqual(config["hotkeys"], {"toggle": "Slash", "dictate": "Space"})

    def test_voice_transcription_status_requires_local_engine(self):
        with mock.patch("control_center.shutil.which", return_value=None):
            status = self.app.projects._voice_engine_status()
        self.assertFalse(status["available"])
        self.assertIn("local Whisper", status["help"])

    def test_voice_transcription_requires_ffmpeg_and_a_real_cpp_model(self):
        with mock.patch(
            "control_center.shutil.which",
            side_effect=lambda name: "/fake/whisper" if name == "whisper" else None,
        ):
            status = self.app.projects._voice_engine_status()
        self.assertFalse(status["available"])
        self.assertIn("ffmpeg", status["help"])

        with mock.patch(
            "control_center.shutil.which",
            side_effect=lambda name: "/fake/" + name if name in ("whisper-cli", "ffmpeg") else None,
        ), mock.patch.dict(os.environ, {"WHISPER_MODEL": str(self.root / "missing-model.bin")}):
            status = self.app.projects._voice_engine_status()
        self.assertFalse(status["available"])
        self.assertIn("existing", status["help"])

    def test_subdirectory_document_root_is_inferred(self):
        project = self.root / "public-site"
        (project / "public").mkdir(parents=True)
        (project / "public" / "index.html").write_text("<title>Public</title>", encoding="utf-8")
        with mock.patch.object(self.app.projects, "_find_port_block", return_value=6361):
            config = self.app.projects._make_config(project)
        self.assertEqual(config["site_root"], "public")
        self.assertEqual(config["default_page"], "index.html")

    def test_inactive_projects_receive_disjoint_port_palettes(self):
        first = self.root / "first"
        second = self.root / "second"
        first.mkdir()
        second.mkdir()
        (first / "index.html").write_text("<title>First</title>", encoding="utf-8")
        (second / "index.html").write_text("<title>Second</title>", encoding="utf-8")
        config_one = self.app.projects._make_config(first)
        config_path = first / "webkit" / "webkit.config.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(json.dumps(config_one), encoding="utf-8")
        self.app.projects._register(first, "First", "codex")
        config_two = self.app.projects._make_config(second)
        ports_one = {entry["port"] for entry in config_one["palette"]}
        ports_two = {entry["port"] for entry in config_two["palette"]}
        self.assertFalse(ports_one & ports_two)

    def test_existing_trunk_repository_keeps_its_target_branch(self):
        repo = self.root / "trunk-repo"
        repo.mkdir()
        (repo / "index.html").write_text("<title>Trunk</title>", encoding="utf-8")
        subprocess.run(["git", "init", "-b", "trunk"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "Initial"], cwd=repo, check=True, capture_output=True)
        with mock.patch.object(self.app.projects, "_ensure_github_repo"):
            project = self.app.projects.add_existing(str(repo), "codex")
        self.assertEqual(project["targetBranch"], "trunk")
        self.assertTrue(project["baseBranch"].startswith("webkit/control-center/"))

    def test_added_remote_project_without_webkit_can_start_a_session(self):
        remote = self.root / "plain-remote.git"
        repo = self.root / "plain-remote-source"
        subprocess.run(
            ["git", "init", "--bare", str(remote)], check=True, capture_output=True
        )
        repo.mkdir()
        (repo / "index.html").write_text("<title>Plain</title>\n", encoding="utf-8")
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
        subprocess.run(["git", "add", "index.html"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "Initial"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True)
        subprocess.run(["git", "push", "-u", "origin", "main"], cwd=repo, check=True, capture_output=True)

        with mock.patch.object(
            self.app.projects, "_find_port_block", return_value=6400
        ):
            project = self.app.projects.add_existing(str(repo), "codex")
        (repo / "remote-note.txt").write_text(
            "collaborator update\n", encoding="utf-8"
        )
        subprocess.run(["git", "add", "remote-note.txt"], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-m", "Collaborator update"],
            cwd=repo, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=repo, check=True, capture_output=True,
        )
        config = load_webkit_config(
            Path(project["path"]) / "webkit" / "webkit.config.json",
            project["path"], require_default_page=True,
        )
        color = config["palette"][0]["slug"]
        connected = {
            "connected": True,
            "remote": "origin",
            "url": "https://github.com/example/plain.git",
            "fetchUrl": str(remote),
            "pushUrl": str(remote),
        }
        tool_status = {
            "codex": {"installed": True},
            "claude": {"installed": True},
        }
        with mock.patch.object(
            self.app.projects, "github_status", return_value=connected
        ), mock.patch.object(
            self.app.projects, "system_status", return_value=tool_status
        ), mock.patch.object(
            self.app.sessions, "_claim_and_preview"
        ), mock.patch.object(SessionRuntime, "start"):
            session = self.app.sessions.start_session(
                project["id"], color, "medium", "fast"
            )

        self.assertEqual(session["status"], "active")
        self.assertEqual(session["speedMode"], "fast")
        runtime = self.app.sessions.runtimes[session["id"]]
        bootstrap_job = runtime.jobs.get_nowait()
        self.assertEqual(bootstrap_job["source"], "bootstrap")
        self.assertFalse(bootstrap_job["show_prompt"])
        self.assertFalse(bootstrap_job["show_turn"])
        self.assertFalse(bootstrap_job["show_agent_output"])
        self.assertIn("visual design system", bootstrap_job["prompt"])
        self.assertIn("frame-aware", bootstrap_job["prompt"])
        self.assertEqual(
            (Path(project["path"]) / "remote-note.txt").read_text(encoding="utf-8"),
            "collaborator update\n",
        )
        remote_config = subprocess.run(
            ["git", "cat-file", "-e", "origin/main:webkit/webkit.config.json"],
            cwd=project["path"], check=False, capture_output=True,
        )
        self.assertEqual(remote_config.returncode, 0)

    def test_settings_restart_only_the_preview_process(self):
        worktree = self.root / "settings-worktree"
        (worktree / "webkit").mkdir(parents=True)
        (worktree / "index.html").write_text("<title>Settings</title>", encoding="utf-8")
        config = {
            "project_name": "settings",
            "site_root": ".",
            "default_page": "index.html",
            "feedback_dir": ".webkit/feedback",
            "lock_dir": str(self.root / "settings-locks"),
            "palette": [{"slug": "blue", "emoji": "🔵", "port": 5311}],
        }
        (worktree / "webkit" / "webkit.config.json").write_text(json.dumps(config), encoding="utf-8")
        session = {
            "id": "settings-session", "worktree": str(worktree), "status": "merging",
            "color": "blue", "emoji": "🔵",
        }
        runtime = mock.Mock()
        runtime.session = session
        runtime.log = mock.Mock()
        self.app.sessions.runtimes[session["id"]] = runtime
        with mock.patch.object(self.app.sessions, "_claim_and_preview") as restart:
            result = self.app.sessions.refresh_previews_for_settings()
        runtime.stop_preview.assert_called_once_with()
        runtime.stop.assert_not_called()
        expected_config = dict(config)
        expected_config["lock_dir"] = str(Path(config["lock_dir"]).resolve())
        expected_config["grace_seconds"] = 180
        restart.assert_called_once_with(runtime, expected_config)
        self.assertIs(self.app.sessions.runtimes[session["id"]], runtime)
        self.assertEqual(result, {"restarted": 1, "deferred": 0, "failed": []})

    def test_settings_do_not_start_a_preview_for_issue_agents(self):
        runtime = mock.Mock()
        runtime.session = {
            "id": "support-session",
            "kind": "support",
            "worktree": str(self.root),
        }
        self.app.sessions.runtimes[runtime.session["id"]] = runtime
        with mock.patch.object(self.app.sessions, "_restart_preview") as restart:
            result = self.app.sessions.refresh_previews_for_settings()
        restart.assert_not_called()
        self.assertEqual(result, {"restarted": 0, "deferred": 0, "failed": []})

    def test_feedback_phase_probe_tolerates_atomic_file_removal(self):
        inbox = self.root / "feedback"
        inbox.mkdir()
        feedback = inbox / "feedback.json"
        feedback.write_text(
            json.dumps(
                {
                    "version": 1,
                    "kind": "feedback",
                    "batchId": "batch-1",
                    "round": 1,
                }
            ),
            encoding="utf-8",
        )
        key = SessionRuntime._feedback_phase_key(inbox)
        self.assertEqual(key[0], "feedback")

    def test_preview_revision_is_persisted_for_frontend_tab_refresh(self):
        session = {
            "id": "preview-revision-session",
            "status": "active",
            "previewRevision": 0,
        }
        self.app.store.update(
            lambda state: state.setdefault("sessions", []).append(dict(session))
        )
        runtime = mock.Mock()
        runtime.session = dict(session)
        self.app.sessions.runtimes[session["id"]] = runtime

        self.assertEqual(self.app.sessions._bump_preview_revision(session["id"]), 1)
        self.assertEqual(self.app.sessions._bump_preview_revision(session["id"]), 2)

        saved = self.app.sessions._get_session(session["id"])
        self.assertEqual(saved["previewRevision"], 2)
        self.assertEqual(runtime.session["previewRevision"], 2)

    def test_unchanged_feedback_phase_becomes_visible_error_instead_of_false_waiting(self):
        worktree = self.root / "unchanged-feedback-worktree"
        inbox = worktree / ".webkit" / "feedback" / "blue"
        inbox.mkdir(parents=True)
        (inbox / "feedback.json").write_text(json.dumps({
            "version": 1,
            "kind": "feedback",
            "batchId": "batch-1",
            "round": 1,
        }), encoding="utf-8")
        session = {
            "id": "unchanged-feedback-session",
            "worktree": str(worktree),
            "feedbackDir": ".webkit/feedback",
            "color": "blue",
            "emoji": "🔵",
            "status": "active",
        }
        self.app.store.update(
            lambda state: state.setdefault("sessions", []).append(dict(session))
        )
        runtime = SessionRuntime(self.app.sessions, session)
        phase = runtime._feedback_phase_key(inbox)
        runtime._observe_feedback_phase(inbox, phase)
        runtime.jobs.put(None)
        runner = mock.Mock()
        runner.run.return_value = False

        with mock.patch("control_center.ProviderRunner", return_value=runner):
            runtime._work_loop()

        saved = self.app.sessions._get_session(session["id"])
        self.assertEqual(saved["status"], "error")
        self.assertIn("without advancing", saved["error"])
        events = runtime.log.read_after(0)["events"]
        self.assertFalse(any("feedback.json" in event.get("text", "") for event in events))
        self.assertEqual(events[-1]["kind"], "turn_complete")
        self.assertEqual(events[-1]["meta"]["outcome"], "failed")

    def test_feedback_transition_is_forwarded_before_agent_turn_completes(self):
        worktree = self.root / "immediate-transition-worktree"
        inbox = worktree / ".webkit" / "feedback" / "blue"
        inbox.mkdir(parents=True)
        session = {
            "id": "immediate-transition-session",
            "worktree": str(worktree),
            "feedbackDir": ".webkit/feedback",
            "color": "blue",
            "emoji": "🔵",
            "status": "active",
        }
        self.app.store.update(
            lambda state: state.setdefault("sessions", []).append(dict(session))
        )
        runtime = SessionRuntime(self.app.sessions, session)
        phase = ("verdicts", "same-revision")
        runtime._observe_feedback_phase(inbox, phase)
        runtime.jobs.put(None)
        runner = mock.Mock()
        runner.run.return_value = False

        with mock.patch("control_center.ProviderRunner", return_value=runner), mock.patch.object(
            runtime, "_feedback_phase_key", side_effect=[phase, None]
        ), mock.patch.object(
            runtime, "_forward_transition_request", return_value=True
        ) as forward:
            runtime._work_loop()

        forward.assert_called_once_with(inbox)
        self.assertEqual(
            self.app.sessions._get_session(session["id"])["status"], "active"
        )
        complete = runtime.log.read_after(0)["events"][-1]
        self.assertEqual(complete["kind"], "turn_complete")
        self.assertEqual(complete["meta"]["outcome"], "completed")

    def test_control_center_forwards_a_queued_transition_and_removes_the_request(self):
        worktree = self.root / "transition-worktree"
        inbox = worktree / ".webkit" / "feedback" / "blue"
        inbox.mkdir(parents=True)
        request = {
            "version": 1,
            "mode": "complete",
            "batchId": "batch-1",
            "round": 1,
        }
        (inbox / "transition-request.json").write_text(
            json.dumps(request), encoding="utf-8"
        )
        token = inbox / "transition-token"
        token.write_text("safe-token-1234567890\n", encoding="utf-8")
        if os.name == "posix":
            token.chmod(0o600)
        session = {
            "id": "transition-session",
            "worktree": str(worktree),
            "feedbackDir": ".webkit/feedback",
            "color": "blue",
            "emoji": "🔵",
            "port": 5311,
        }
        runtime = SessionRuntime(self.app.sessions, session)
        response = mock.Mock(status=200)
        response.read.return_value = json.dumps({
            "ok": True,
            "mode": "complete",
            "batchId": "batch-1",
            "round": 1,
        }).encode("utf-8")
        connection = mock.Mock()
        connection.getresponse.return_value = response

        with mock.patch(
            "control_center.http.client.HTTPConnection", return_value=connection
        ) as factory:
            self.assertTrue(runtime._forward_transition_request(inbox))

        factory.assert_called_once_with("127.0.0.1", 5311, timeout=10)
        sent = connection.request.call_args
        self.assertEqual(sent[0][:2], ("POST", "/__wk/transition"))
        self.assertEqual(json.loads(sent[1]["body"].decode("utf-8")), request)
        self.assertEqual(
            sent[1]["headers"]["X-WK-Transition-Token"],
            "safe-token-1234567890",
        )
        connection.close.assert_called_once_with()
        self.assertFalse((inbox / "transition-request.json").exists())
        self.assertFalse((inbox / ".transition-request.processing").exists())
        events = runtime.log.read_after(0)["events"]
        self.assertIn("archived round 1", events[-1]["text"])

    def test_control_center_preserves_a_rejected_transition_for_diagnosis(self):
        worktree = self.root / "rejected-transition-worktree"
        inbox = worktree / ".webkit" / "feedback" / "blue"
        inbox.mkdir(parents=True)
        request = {
            "version": 1,
            "mode": "complete",
            "batchId": "batch-1",
            "round": 1,
        }
        (inbox / "transition-request.json").write_text(
            json.dumps(request), encoding="utf-8"
        )
        token = inbox / "transition-token"
        token.write_text("safe-token-1234567890\n", encoding="utf-8")
        if os.name == "posix":
            token.chmod(0o600)
        runtime = SessionRuntime(self.app.sessions, {
            "id": "rejected-transition-session",
            "worktree": str(worktree),
            "feedbackDir": ".webkit/feedback",
            "color": "blue",
            "emoji": "🔵",
            "port": 5311,
        })
        response = mock.Mock(status=409)
        response.read.return_value = json.dumps({
            "error": "transition_conflict",
            "reason": "live round does not match",
        }).encode("utf-8")
        connection = mock.Mock()
        connection.getresponse.return_value = response

        with mock.patch(
            "control_center.http.client.HTTPConnection", return_value=connection
        ), self.assertRaisesRegex(ControlCenterError, "live round does not match"):
            runtime._forward_transition_request(inbox)

        self.assertFalse((inbox / "transition-request.json").exists())
        self.assertFalse((inbox / ".transition-request.processing").exists())
        preserved = inbox / "transition-request-error.json"
        self.assertEqual(json.loads(preserved.read_text(encoding="utf-8")), request)

    def test_feedback_phase_rejects_verdicts_without_live_feedback(self):
        inbox = self.root / "verdicts-only-feedback"
        inbox.mkdir()
        (inbox / "verdicts.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "kind": "verdicts",
                    "batchId": "batch-1",
                    "round": 1,
                }
            ),
            encoding="utf-8",
        )
        self.assertIsNone(SessionRuntime._feedback_phase_key(inbox))

    def test_feedback_phase_rejects_mismatched_live_documents(self):
        inbox = self.root / "mismatched-feedback"
        inbox.mkdir()
        documents = {
            "feedback.json": {
                "version": 1, "kind": "feedback", "batchId": "batch-1", "round": 1,
            },
            "review.json": {
                "version": 1, "kind": "review", "batchId": "batch-1", "round": 2,
            },
            "verdicts.json": {
                "version": 1, "kind": "verdicts", "batchId": "batch-2", "round": 1,
            },
        }
        for name, value in documents.items():
            (inbox / name).write_text(json.dumps(value), encoding="utf-8")
        self.assertIsNone(SessionRuntime._feedback_phase_key(inbox))

        documents["verdicts.json"]["batchId"] = "batch-1"
        (inbox / "verdicts.json").write_text(
            json.dumps(documents["verdicts.json"]), encoding="utf-8"
        )
        self.assertIsNone(SessionRuntime._feedback_phase_key(inbox))

        documents["verdicts.json"]["round"] = 2
        documents["feedback.json"]["round"] = 2
        (inbox / "feedback.json").write_text(
            json.dumps(documents["feedback.json"]), encoding="utf-8"
        )
        (inbox / "verdicts.json").write_text(
            json.dumps(documents["verdicts.json"]), encoding="utf-8"
        )
        self.assertEqual(SessionRuntime._feedback_phase_key(inbox)[0], "verdicts")

    def test_feedback_phase_rejects_malformed_json(self):
        inbox = self.root / "malformed-feedback"
        inbox.mkdir()
        (inbox / "feedback.json").write_text("{", encoding="utf-8")
        self.assertIsNone(SessionRuntime._feedback_phase_key(inbox))

    def test_feedback_phase_accepts_feedback_update_without_review(self):
        inbox = self.root / "feedback-update"
        inbox.mkdir()
        (inbox / "feedback.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "kind": "feedback",
                    "batchId": "batch-1",
                    "round": 1,
                }
            ),
            encoding="utf-8",
        )
        (inbox / "verdicts.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "kind": "feedback_update",
                    "batchId": "batch-1",
                    "round": 1,
                    "addedPointIds": ["point-2"],
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(SessionRuntime._feedback_phase_key(inbox)[0], "verdicts")

    def test_filesystem_feedback_jobs_are_single_flight_and_coalesce_latest_state(self):
        worktree = self.root / "single-flight-worktree"
        inbox = worktree / ".webkit" / "feedback" / "blue"
        inbox.mkdir(parents=True)
        session = {
            "id": "single-flight-session",
            "worktree": str(worktree),
            "feedbackDir": ".webkit/feedback",
            "color": "blue",
            "emoji": "🔵",
        }
        runtime = SessionRuntime(self.app.sessions, session)
        first = ("feedback", 1)
        second = ("feedback", 2)
        third = ("verdicts", 3)

        runtime._observe_feedback_phase(inbox, first)
        runtime._observe_feedback_phase(inbox, second)
        runtime._observe_feedback_phase(inbox, third)
        self.assertEqual(runtime.jobs.qsize(), 1)

        first_job = runtime.jobs.get_nowait()
        with mock.patch.object(runtime, "_feedback_phase_key", return_value=third):
            self.assertFalse(runtime._feedback_job_is_current(first_job))
        runtime._finish_feedback_job()
        runtime.jobs.task_done()
        self.assertEqual(runtime.jobs.qsize(), 1)

        follow_up = runtime.jobs.get_nowait()
        self.assertEqual(follow_up["feedback_key"], third)
        with mock.patch.object(runtime, "_feedback_phase_key", return_value=third):
            self.assertTrue(runtime._feedback_job_is_current(follow_up))

        fourth = ("feedback", 4)
        latest = ("verdicts", 5)
        runtime._observe_feedback_phase(inbox, fourth)
        runtime._observe_feedback_phase(inbox, latest)
        self.assertTrue(runtime.jobs.empty())
        runtime._finish_feedback_job()
        runtime.jobs.task_done()

        final_follow_up = runtime.jobs.get_nowait()
        self.assertEqual(final_follow_up["feedback_key"], latest)
        self.assertTrue(runtime.jobs.empty())
        runtime._finish_feedback_job()
        runtime.jobs.task_done()

    def test_stale_archive_feedback_job_does_not_invoke_provider(self):
        worktree = self.root / "stale-archive-worktree"
        inbox = worktree / ".webkit" / "feedback" / "blue"
        inbox.mkdir(parents=True)
        session = {
            "id": "stale-archive-session",
            "worktree": str(worktree),
            "feedbackDir": ".webkit/feedback",
            "color": "blue",
            "emoji": "🔵",
        }
        runtime = SessionRuntime(self.app.sessions, session)
        runtime._observe_feedback_phase(inbox, ("verdicts", 1))
        runtime.jobs.put(None)

        with mock.patch.object(runtime, "_feedback_phase_key", return_value=None), mock.patch(
            "control_center.ProviderRunner"
        ) as provider, mock.patch.object(self.app.sessions, "_set_session_status") as set_status:
            runtime._work_loop()

        provider.assert_not_called()
        set_status.assert_not_called()
        self.assertFalse(runtime.feedback_job_active)
        self.assertTrue(runtime.jobs.empty())

    def test_chat_jobs_remain_independent_queue_entries(self):
        session = {
            "id": "chat-queue-session",
            "worktree": str(self.root),
            "color": "blue",
            "emoji": "🔵",
            "status": "active",
            "previewRevision": 0,
        }
        self.app.store.update(
            lambda state: state.setdefault("sessions", []).append(dict(session))
        )
        runtime = SessionRuntime(self.app.sessions, session)
        self.app.sessions.runtimes[session["id"]] = runtime
        runtime.enqueue("first message", "chat")
        runtime.enqueue("second message", "chat")
        runtime.jobs.put(None)
        runner = mock.Mock()

        with mock.patch("control_center.ProviderRunner", return_value=runner):
            runtime._work_loop()

        self.assertEqual(
            runner.run.call_args_list,
            [mock.call("first message"), mock.call("second message")],
        )
        self.assertEqual(
            self.app.sessions._get_session(session["id"])["previewRevision"], 2
        )

    def test_bootstrap_context_runs_without_appearing_in_chat(self):
        session = {
            "id": "silent-bootstrap-session",
            "worktree": str(self.root),
            "color": "blue",
            "emoji": "🔵",
            "status": "active",
        }
        self.app.store.update(
            lambda state: state.setdefault("sessions", []).append(dict(session))
        )
        runtime = SessionRuntime(self.app.sessions, session)
        runtime.enqueue(
            "private initialization context",
            "bootstrap",
            show_prompt=False,
            show_turn=False,
            show_agent_output=False,
        )
        runtime.jobs.put(None)
        runner = mock.Mock()
        with mock.patch("control_center.ProviderRunner", return_value=runner) as provider:
            runtime._work_loop()
        runner.run.assert_called_once_with("private initialization context")
        _provider_args, provider_kwargs = provider.call_args
        self.assertTrue(provider_kwargs["read_only"])
        self.assertEqual(runtime.log.read_after(0)["events"], [])

    def test_preview_uses_a_bounded_pipe_drain_and_instance_identity(self):
        worktree = self.root / "preview-worktree"
        (worktree / "webkit" / "server").mkdir(parents=True)
        (worktree / "index.html").write_text("<title>Preview</title>", encoding="utf-8")
        config = {
            "site_root": ".", "lock_dir": str(self.root / "preview-locks"),
            "palette": [{"slug": "blue", "emoji": "🔵", "port": 0}],
        }
        (worktree / "webkit" / "webkit.config.json").write_text(json.dumps(config), encoding="utf-8")
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        config["palette"][0]["port"] = port
        (worktree / "webkit" / "webkit.config.json").write_text(
            json.dumps(config), encoding="utf-8"
        )
        session = {
            "id": "preview-session", "worktree": str(worktree), "port": port,
            "color": "blue", "emoji": "🔵",
        }
        runtime = SessionRuntime(self.app.sessions, session)
        processes = [mock.Mock(), mock.Mock()]
        for process in processes:
            process.poll.return_value = None
            process.stdout = io.StringIO("")
        inherited = {
            "WKCC_TOKEN": "inherited-controller-token",
            "WKCC_NO_AUTH": "1",
            "WKCC_STATE_DIR": "/private/inherited-state",
            "WK_CONFIG": "/private/inherited-config",
            "WK_COLOR_OWNER": "/private/inherited-owner",
            "WK_COLOR_LOCKDIR": "/private/inherited-locks",
            "WK_MUTATION_TOKEN": "inherited-mutation-token",
            "WK_PREVIEW_INSTANCE_TOKEN": "inherited-instance-token",
            "WK_TRANSITION_TOKEN": "inherited-transition-token",
            "WK_CONTROL_CENTER": "inherited-controller-mode",
            "WK_SESSION_COLOR": "inherited-color",
            "WK_ENABLE_API_PROXY": "1",
            "WK_COLOR_FORCE": "purple",
        }
        with mock.patch.dict(os.environ, inherited, clear=False), mock.patch(
            "control_center.subprocess.Popen", side_effect=processes
        ) as popen, mock.patch.object(
            self.app.sessions, "_preview_instance_ready", return_value=True
        ):
            self.app.sessions._claim_and_preview(runtime, config)
            first_env = popen.call_args[1]["env"]
            runtime.stop_preview()
            self.app.sessions._claim_and_preview(runtime, config)
            second_env = popen.call_args[1]["env"]
        self.assertIs(popen.call_args[1]["stdout"], subprocess.PIPE)
        self.assertEqual(popen.call_args[0][0][0], sys.executable)
        self.assertIsNotNone(runtime.preview_log)
        self.assertIsNotNone(runtime.preview_log.thread)
        self.assertEqual(second_env["WK_COLOR_LOCKDIR"], config["lock_dir"])
        self.assertEqual(second_env["PYTHONIOENCODING"], "utf-8")
        self.assertIn("WK_PREVIEW_INSTANCE_TOKEN", second_env)
        self.assertEqual(first_env["WK_MUTATION_TOKEN"], runtime.mutation_token)
        self.assertEqual(second_env["WK_MUTATION_TOKEN"], runtime.mutation_token)
        self.assertNotEqual(
            first_env["WK_PREVIEW_INSTANCE_TOKEN"], second_env["WK_PREVIEW_INSTANCE_TOKEN"]
        )
        for key in ("WKCC_TOKEN", "WKCC_NO_AUTH", "WKCC_STATE_DIR"):
            self.assertNotIn(key, first_env)
            self.assertNotIn(key, second_env)
        for key in ("WK_TRANSITION_TOKEN", "WK_CONTROL_CENTER", "WK_SESSION_COLOR", "WK_ENABLE_API_PROXY"):
            self.assertNotIn(key, first_env)
            self.assertNotIn(key, second_env)
        self.assertNotEqual(first_env["WK_CONFIG"], inherited["WK_CONFIG"])
        self.assertNotEqual(first_env["WK_COLOR_OWNER"], inherited["WK_COLOR_OWNER"])
        self.assertNotEqual(first_env["WK_COLOR_LOCKDIR"], inherited["WK_COLOR_LOCKDIR"])
        self.assertNotEqual(first_env["WK_MUTATION_TOKEN"], inherited["WK_MUTATION_TOKEN"])
        self.assertNotEqual(
            first_env["WK_PREVIEW_INSTANCE_TOKEN"],
            inherited["WK_PREVIEW_INSTANCE_TOKEN"],
        )
        self.app.sessions._release(runtime)

    def test_preview_pipe_drain_caps_tail_and_closes_blocked_reader(self):
        retained = control_center_module.BoundedPreviewLog()
        expected_tail = "preview-tail"
        retained.start(io.StringIO(
            "x" * (control_center_module.MAX_PREVIEW_LOG_CHARS + 1024)
            + expected_tail
        ))
        retained.thread.join(timeout=2)
        self.assertFalse(retained.thread.is_alive())
        self.assertLessEqual(
            len(retained.tail(control_center_module.MAX_PREVIEW_LOG_CHARS)),
            control_center_module.MAX_PREVIEW_LOG_CHARS,
        )
        self.assertEqual(retained.tail(len(expected_tail)), expected_tail)
        retained.close()

        class BlockingStream:
            def __init__(self):
                self.started = threading.Event()
                self.closed = threading.Event()

            def read(self, _size):
                self.started.set()
                self.closed.wait(timeout=2)
                return ""

            def close(self):
                self.closed.set()

        stream = BlockingStream()
        blocked = control_center_module.BoundedPreviewLog()
        blocked.start(stream)
        self.assertTrue(stream.started.wait(timeout=1))
        blocked.close()
        self.assertFalse(blocked.thread.is_alive())

    @unittest.skipUnless(os.name == "nt", "native Windows preview smoke test")
    def test_native_windows_preview_starts_with_portable_lock_config(self):
        worktree = self.root / "windows-preview-worktree"
        server_dir = worktree / "webkit" / "server"
        server_dir.mkdir(parents=True)
        shutil.copy2(
            str(KIT_ROOT / "webkit" / "server" / "preview-server.py"),
            str(server_dir / "preview-server.py"),
        )
        scripts_dir = worktree / "webkit" / "scripts"
        scripts_dir.mkdir()
        shutil.copy2(
            str(KIT_ROOT / "webkit" / "scripts" / "runtime_registry.py"),
            str(scripts_dir / "runtime_registry.py"),
        )
        (worktree / "index.html").write_text("<title>Windows Preview</title>", encoding="utf-8")
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        config = {
            "project_name": "windows-smoke-{}".format(uuid.uuid4().hex[:8]),
            "site_root": ".",
            "lock_dir": "/tmp/windows-smoke-{}-agent-colors".format(uuid.uuid4().hex[:8]),
            "feedback_dir": ".webkit/feedback",
            "palette": [{"slug": "blue", "emoji": "🔵", "port": port}],
        }
        config_path = worktree / "webkit" / "webkit.config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        runtime = SessionRuntime(self.app.sessions, {
            "id": "windows-preview-session",
            "worktree": str(worktree),
            "port": port,
            "color": "blue",
            "emoji": "🔵",
        })
        lock_dir = configured_lock_dir(config)
        try:
            self.app.sessions._claim_and_preview(runtime, config)
            self.assertIsNotNone(runtime.preview_process)
            self.assertIsNone(runtime.preview_process.poll())
        finally:
            self.app.sessions._release(runtime)
            shutil.rmtree(str(lock_dir), ignore_errors=True)

    def test_port_probe_matches_preview_server_reuse_behavior(self):
        probe = mock.Mock()
        with mock.patch("control_center.socket.socket", return_value=probe):
            self.assertTrue(self.app.sessions._port_available(5311))
        probe.setsockopt.assert_called_once_with(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind.assert_called_once_with(("127.0.0.1", 5311))
        probe.close.assert_called_once_with()

    def test_preview_start_failure_releases_lock_worktree_and_branch(self):
        parent = self.root / "projects"
        parent.mkdir()
        with mock.patch.object(self.app.projects, "_find_port_block", return_value=6371), mock.patch.object(
            self.app.projects, "_ensure_github_repo"
        ):
            project = self.app.projects.create_project("Cleanup Site", str(parent), "codex")
        project_path = Path(project["path"])
        config_path = project_path / "webkit" / "webkit.config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["lock_dir"] = str(self.root / "startup-locks")
        config_path.write_text(json.dumps(config), encoding="utf-8")
        subprocess.run(["git", "add", "webkit/webkit.config.json"], cwd=project_path, check=True)
        subprocess.run(["git", "commit", "-m", "Use test lock path"], cwd=project_path, check=True, capture_output=True)
        captured = []

        def fail_after_claim(runtime, loaded):
            captured.append(dict(runtime.session))
            owner = Path(runtime.session["worktree"]).resolve()
            runtime.claimed_lock, runtime.claimed_owner = self.app.sessions._claim_lock(
                loaded, runtime.session["color"], owner
            )
            runtime.claimed_port = runtime.session["port"]
            runtime.claimed_color = runtime.session["color"]
            runtime.preview_process = mock.Mock()
            runtime.preview_process.poll.return_value = None
            raise RuntimeError("startup failed")

        with mock.patch.object(
            self.app.projects, "system_status", return_value={"codex": {"installed": True}}
        ), mock.patch.object(self.app.sessions, "_claim_and_preview", side_effect=fail_after_claim):
            with self.assertRaisesRegex(RuntimeError, "startup failed"):
                self.app.sessions.start_session(project["id"], "blue")
        failed = captured[0]
        self.assertFalse(Path(failed["worktree"]).exists())
        self.assertFalse((Path(config["lock_dir"]) / "blue.lock").exists())
        branches = subprocess.run(
            ["git", "branch", "--list", failed["branch"]], cwd=project_path,
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        self.assertEqual(branches, "")

    def test_control_center_csp_allows_loopback_seed_frames_only(self):
        server = ControlCenterHTTPServer(
            ("127.0.0.1", 0), ControlCenterHandler, self.app,
            "test-token-1234567890",
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with mock.patch.dict(os.environ, {"WKCC_NO_AUTH": "1"}):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                connection.request("GET", "/")
                response = connection.getresponse()
                response.read()
                policy = response.getheader("Content-Security-Policy")
                connection.close()
            self.assertIn("frame-src http://127.0.0.1:* http://localhost:*", policy)
            self.assertIn("frame-ancestors 'none'", policy)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_existing_project_http_route_forwards_webkit_update_choice(self):
        token = "test-token-1234567890"
        app = mock.Mock()
        app.projects.add_existing.return_value = {"id": "updated-project"}
        server = ControlCenterHTTPServer(
            ("127.0.0.1", 0), ControlCenterHandler, app, token
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            body = json.dumps({
                "path": "/projects/old-site",
                "provider": "codex",
                "updateWebkit": True,
            })
            connection = http.client.HTTPConnection(
                "127.0.0.1", server.server_port, timeout=3
            )
            connection.request(
                "POST", "/api/projects/existing", body=body,
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(len(body.encode("utf-8"))),
                    "X-WKCC-Token": token,
                },
            )
            response = connection.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
            connection.close()
            self.assertEqual(response.status, 201)
            self.assertEqual(payload["project"]["id"], "updated-project")
            app.projects.add_existing.assert_called_once_with(
                "/projects/old-site", "codex", update_webkit=True
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_issue_agent_http_route_forwards_the_persistent_issue(self):
        token = "test-token-1234567890"
        app = mock.Mock()
        app.sessions.start_issue_session.return_value = {
            "id": "support-session",
            "kind": "support",
        }
        server = ControlCenterHTTPServer(
            ("127.0.0.1", 0), ControlCenterHandler, app, token
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            body = json.dumps({
                "issueCode": control_center_module.GITHUB_TARGET_DIVERGED,
                "reasoningEffort": "high",
            })
            connection = http.client.HTTPConnection(
                "127.0.0.1", server.server_port, timeout=3
            )
            connection.request(
                "POST", "/api/projects/project-1/agent", body=body,
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(len(body.encode("utf-8"))),
                    "X-WKCC-Token": token,
                },
            )
            response = connection.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
            connection.close()
            self.assertEqual(response.status, 201)
            self.assertEqual(payload["session"]["kind"], "support")
            app.sessions.start_issue_session.assert_called_once_with(
                "project-1",
                control_center_module.GITHUB_TARGET_DIVERGED,
                "high",
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_session_speed_http_routes_forward_the_selected_mode(self):
        token = "test-token-1234567890"
        app = mock.Mock()
        app.sessions.start_session.return_value = {
            "id": "speed-session", "speedMode": "fast",
        }
        app.sessions.set_speed.return_value = {"speedMode": "normal"}
        server = ControlCenterHTTPServer(
            ("127.0.0.1", 0), ControlCenterHandler, app, token
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            headers = {
                "Content-Type": "application/json",
                "X-WKCC-Token": token,
            }
            connection = http.client.HTTPConnection(
                "127.0.0.1", server.server_port, timeout=3
            )
            connection.request(
                "POST", "/api/sessions/start",
                body=json.dumps({
                    "projectId": "project-1", "color": "blue",
                    "reasoningEffort": "high", "speedMode": "fast",
                }),
                headers=headers,
            )
            response = connection.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(response.status, 201)
            self.assertEqual(payload["session"]["speedMode"], "fast")
            app.sessions.start_session.assert_called_once_with(
                "project-1", "blue", "high", "fast"
            )

            connection.request(
                "POST", "/api/sessions/speed-session/speed",
                body=json.dumps({"speedMode": "normal"}), headers=headers,
            )
            response = connection.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(response.status, 200)
            self.assertEqual(payload, {"speedMode": "normal"})
            app.sessions.set_speed.assert_called_once_with(
                "speed-session", "normal"
            )
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_issue_agent_http_route_defaults_to_high_reasoning(self):
        token = "test-token-1234567890"
        app = mock.Mock()
        app.sessions.start_issue_session.return_value = {
            "id": "support-session",
            "kind": "support",
        }
        server = ControlCenterHTTPServer(
            ("127.0.0.1", 0), ControlCenterHandler, app, token
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            body = json.dumps({
                "issueCode": control_center_module.GITHUB_TARGET_DIVERGED,
            })
            connection = http.client.HTTPConnection(
                "127.0.0.1", server.server_port, timeout=3
            )
            connection.request(
                "POST", "/api/projects/project-1/agent", body=body,
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(len(body.encode("utf-8"))),
                    "X-WKCC-Token": token,
                },
            )
            response = connection.getresponse()
            response.read()
            connection.close()
            self.assertEqual(response.status, 201)
            app.sessions.start_issue_session.assert_called_once_with(
                "project-1",
                control_center_module.GITHUB_TARGET_DIVERGED,
                "high",
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_control_center_close_waits_for_in_flight_mutation_handler(self):
        entered = threading.Event()
        release = threading.Event()
        closed = threading.Event()
        client_error = []
        app = mock.Mock()

        def create_project(*_args, **_kwargs):
            entered.set()
            release.wait(timeout=5)
            return {"project": {"id": "finished"}}

        app.create_project.side_effect = create_project
        server = ControlCenterHTTPServer(
            ("127.0.0.1", 0), ControlCenterHandler, app,
            "test-token-1234567890",
        )
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()

        def request_create():
            connection = http.client.HTTPConnection(
                "127.0.0.1", server.server_port, timeout=5
            )
            try:
                body = json.dumps({"name": "Site", "parent": str(self.root), "provider": "codex"})
                connection.request(
                    "POST", "/api/projects/create", body=body,
                    headers={
                        "Content-Type": "application/json",
                        "Content-Length": str(len(body.encode("utf-8"))),
                        "X-WKCC-Token": "test-token-1234567890",
                    },
                )
                response = connection.getresponse()
                response.read()
                if response.status != 201:
                    client_error.append(response.status)
            except BaseException as exc:
                client_error.append(exc)
            finally:
                connection.close()

        client = threading.Thread(target=request_create, daemon=True)
        client.start()
        self.assertTrue(entered.wait(timeout=3))
        server.shutdown()

        def close_server():
            server.server_close()
            closed.set()

        closer = threading.Thread(target=close_server, daemon=True)
        closer.start()
        self.assertFalse(closed.wait(timeout=0.1))
        release.set()
        self.assertTrue(closed.wait(timeout=3))
        client.join(timeout=3)
        serving.join(timeout=3)
        closer.join(timeout=3)
        self.assertEqual(client_error, [])

    def test_merge_delegates_to_agent_then_fast_forwards_and_closes_worktree(self):
        parent = self.root / "projects"
        parent.mkdir()
        with mock.patch.object(self.app.projects, "_find_port_block", return_value=6341):
            project = self.app.projects.create_project("Merge Site", str(parent), "codex")
        project_path = Path(project["path"])
        worktree = self.root / "merge-worktree"
        branch = "webkit/blue/test-merge"
        subprocess.run(
            ["git", "worktree", "add", "-b", branch, str(worktree), "main"],
            cwd=project_path, check=True, capture_output=True,
        )
        (worktree / "index.html").write_text("<title>Merged</title>\n", encoding="utf-8")
        subprocess.run(["git", "add", "index.html"], cwd=worktree, check=True)
        subprocess.run(
            ["git", "commit", "-m", "Change title"], cwd=worktree,
            check=True, capture_output=True,
        )
        base_sha = subprocess.run(
            ["git", "rev-parse", "main"], cwd=project_path,
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        worktree_details = os.lstat(str(worktree))
        session = {
            "id": "merge-session", "projectId": project["id"], "projectName": project["name"],
            "provider": "codex", "color": "blue", "emoji": "🔵", "port": 6341,
            "branch": branch, "worktree": str(worktree), "previewUrl": "http://127.0.0.1:6341/",
            "feedbackDir": ".webkit/feedback", "status": "active", "threadId": None,
            "hasRun": False, "createdAt": "2026-08-18T00:00:00Z",
            "baseSha": base_sha, "worktreeDev": worktree_details.st_dev,
            "worktreeIno": worktree_details.st_ino,
        }
        self.app.store.update(lambda state: state.setdefault("sessions", []).append(session))
        runtime = mock.Mock()
        runtime.session = session
        self.app.sessions.runtimes["merge-session"] = runtime
        result = self.app.sessions.merge("merge-session")
        self.assertTrue(result["queued"])
        self.assertFalse(result["agentManaged"])
        self.assertTrue(result["controllerManaged"])
        runtime.enqueue.assert_called_once()
        merge_call = runtime.enqueue.call_args
        merge_args, merge_kwargs = merge_call
        self.assertEqual(merge_args[1], "merge")
        self.assertIn("Validate our work", merge_args[0])
        self.assertEqual(
            merge_kwargs["display"],
            "Merge in progress. I will let you know when it is ready.",
        )
        self.assertEqual(merge_kwargs["display_role"], "user")
        marker = worktree / ".webkit" / "control-center-merge.json"
        marker.parent.mkdir(exist_ok=True)
        marker.write_text('{"status":"ready","message":"ready"}', encoding="utf-8")
        with mock.patch.object(self.app.sessions, "_release"):
            self.app.sessions._complete_agent_merge("merge-session")
        self.assertEqual((project_path / "index.html").read_text(), "<title>Merged</title>\n")
        self.assertFalse(worktree.exists())
        branches = subprocess.run(
            ["git", "branch", "--list", branch], cwd=project_path, text=True,
            capture_output=True, check=True,
        ).stdout.strip()
        self.assertEqual(branches, "")
        self.assertEqual(self.app.sessions._get_session("merge-session")["status"], "merged")

    def test_codex_jsonl_captures_thread_and_message(self):
        session = {
            "id": "session1", "provider": "codex", "worktree": str(self.root),
            "color": "blue", "threadId": None, "hasRun": False,
            "speedMode": "fast",
        }
        event_log = EventLog(self.state_dir, "session1")
        threads = []
        processes = []
        runner = ProviderRunner(session, event_log, threads.append, processes.append)
        fake = FakeProcess([
            '{"type":"thread.started","thread_id":"thread-123"}\n',
            '{"type":"item.completed","item":{"type":"agent_message","text":"מוכן 🎨"}}\n',
        ])
        fake.stdin = mock.Mock()
        inherited = {
            "WKCC_TOKEN": "inherited-controller-token",
            "WKCC_NO_AUTH": "1",
            "WKCC_STATE_DIR": "/private/inherited-state",
            "WK_CONFIG": "/private/inherited-config",
            "WK_COLOR_OWNER": "/private/inherited-owner",
            "WK_COLOR_LOCKDIR": "/private/inherited-locks",
            "WK_PORT_LOCKDIR": "/private/inherited-ports",
            "WK_MUTATION_TOKEN": "inherited-mutation-token",
            "WK_PREVIEW_INSTANCE_TOKEN": "inherited-instance-token",
            "WK_TRANSITION_TOKEN": "inherited-transition-token",
            "WK_CONTROL_CENTER": "inherited-controller-mode",
            "WK_SESSION_COLOR": "inherited-color",
            "WK_ENABLE_API_PROXY": "1",
        }
        with mock.patch.dict(os.environ, inherited, clear=False), mock.patch(
            "control_center.shutil.which", return_value="/fake/codex"
        ), mock.patch(
            "control_center.subprocess.Popen", return_value=fake
        ) as popen:
            runner.run("צבע אותו בכחול 🎨")
        command = popen.call_args[0][0]
        child_env = popen.call_args[1]["env"]
        self.assertEqual(command[:3], ["/fake/codex", "exec", "--json"])
        self.assertIn("workspace-write", command)
        self.assertIn('service_tier="fast"', command)
        self.assertIn("features.fast_mode=true", command)
        self.assertIn('forced_login_method="chatgpt"', command)
        self.assertEqual(popen.call_args[1]["encoding"], "utf-8")
        self.assertEqual(popen.call_args[1]["errors"], "replace")
        fake.stdin.write.assert_called_once_with("צבע אותו בכחול 🎨")
        fake.stdin.close.assert_called_once_with()
        self.assertEqual(threads, ["thread-123"])
        self.assertEqual(event_log.read_after(0)["events"][0]["text"], "מוכן 🎨")
        self.assertEqual(child_env["WK_CONTROL_CENTER"], "1")
        self.assertEqual(child_env["WK_SESSION_COLOR"], "blue")
        for key in inherited:
            if key not in ("WK_CONTROL_CENTER", "WK_SESSION_COLOR"):
                self.assertNotIn(key, child_env)

    def test_successful_provider_run_hides_cli_diagnostics(self):
        session = {
            "id": "session-noise", "provider": "codex", "worktree": str(self.root),
            "color": "blue", "threadId": None, "hasRun": False,
        }
        event_log = EventLog(self.state_dir, "session-noise")
        runner = ProviderRunner(session, event_log, lambda value: None, lambda value: None)
        fake = FakeProcess(
            ['{"type":"item.completed","item":{"type":"agent_message","text":"Ready"}}\n'],
            stderr_lines=["unrelated plugin warning\n"],
        )
        with mock.patch("control_center.shutil.which", return_value="/fake/codex"), mock.patch(
            "control_center.subprocess.Popen", return_value=fake
        ) as popen:
            runner.run("Check")
        command = popen.call_args[0][0]
        self.assertIn('service_tier="default"', command)
        self.assertIn("features.fast_mode=false", command)
        self.assertNotIn('forced_login_method="chatgpt"', command)
        texts = [event["text"] for event in event_log.read_after(0)["events"]]
        self.assertEqual(texts, ["Ready"])

    def test_provider_stream_treats_non_object_json_as_progress(self):
        session = {
            "id": "session-shapes", "provider": "codex", "worktree": str(self.root),
            "color": "blue", "threadId": None, "hasRun": False,
        }
        event_log = EventLog(self.state_dir, "session-shapes")
        runner = ProviderRunner(
            session, event_log, lambda value: None, lambda value: None
        )
        fake = FakeProcess([
            "[]\n",
            "42\n",
            '{"type":"item.completed","item":{"type":"agent_message","text":"Ready"}}\n',
        ])
        with mock.patch("control_center.shutil.which", return_value="/fake/codex"), mock.patch(
            "control_center.subprocess.Popen", return_value=fake
        ):
            runner.run("Check stream shapes")
        events = event_log.read_after(0)["events"]
        self.assertEqual([event["text"] for event in events], ["[]", "42", "Ready"])
        self.assertEqual([event["kind"] for event in events], ["progress", "progress", "message"])

    def test_claude_initial_and_resume_flags_do_not_conflict(self):
        session = {
            "id": "session2", "provider": "claude", "worktree": str(self.root),
            "color": "green", "threadId": None, "hasRun": False,
            "speedMode": "fast",
        }
        event_log = EventLog(self.state_dir, "session2")
        runner = ProviderRunner(session, event_log, lambda value: None, lambda value: None)
        first_process = FakeProcess(['{"type":"result","is_error":false,"result":"Done"}\n'])
        with mock.patch("control_center.shutil.which", return_value="/fake/claude"), mock.patch("control_center.subprocess.Popen", return_value=first_process) as popen:
            runner.run("Build it")
        first = popen.call_args[0][0]
        self.assertIn("--session-id", first)
        self.assertNotIn("--resume", first)
        self.assertNotIn("Build it", first)
        first_settings = json.loads(first[first.index("--settings") + 1])
        self.assertTrue(first_settings["fastMode"])
        self.assertEqual(first_settings["forceLoginMethod"], "claudeai")
        session["hasRun"] = True
        session["speedMode"] = "normal"
        resumed_process = FakeProcess(['{"type":"result","is_error":false,"result":"Done"}\n'])
        with mock.patch("control_center.shutil.which", return_value="/fake/claude"), mock.patch("control_center.subprocess.Popen", return_value=resumed_process) as popen:
            runner.run("Continue")
        resumed = popen.call_args[0][0]
        self.assertIn("--resume", resumed)
        self.assertNotIn("--session-id", resumed)
        self.assertNotIn("Continue", resumed)
        resumed_settings = json.loads(resumed[resumed.index("--settings") + 1])
        self.assertFalse(resumed_settings["fastMode"])
        self.assertNotIn("forceLoginMethod", resumed_settings)

    def test_atomic_json_write_does_not_follow_predictable_temp_symlink(self):
        target = self.root / "config.json"
        sentinel = self.root / "sentinel.txt"
        sentinel.write_text("keep me", encoding="utf-8")
        legacy_temp = target.with_suffix(".json.tmp")
        legacy_temp.symlink_to(sentinel)

        atomic_write_json(target, {"safe": True})

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep me")
        self.assertTrue(legacy_temp.is_symlink())
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"safe": True})

    def test_state_root_and_state_file_symlinks_are_rejected(self):
        real = self.root / "real-state"
        real.mkdir()
        linked = self.root / "linked-state"
        linked.symlink_to(real, target_is_directory=True)
        with self.assertRaisesRegex(ControlCenterError, "state directory.*symbolic link"):
            ControlCenter(KIT_ROOT, linked)

        state_root = self.root / "state-file-link"
        state_root.mkdir()
        sentinel = self.root / "outside-state.json"
        sentinel.write_text("{}", encoding="utf-8")
        (state_root / "state.json").symlink_to(sentinel)
        with self.assertRaisesRegex(ControlCenterError, "state file.*symbolic link"):
            ControlCenter(KIT_ROOT, state_root)

    def test_installer_rejects_symlinked_project_entrypoints(self):
        for relative in ("AGENTS.md", ".gitignore", ".claude"):
            with self.subTest(relative=relative):
                project = self.root / ("symlink-" + relative.replace(".", "dot").replace("/", "-"))
                project.mkdir()
                (project / "index.html").write_text("<title>Safe</title>", encoding="utf-8")
                sentinel = self.root / (project.name + "-sentinel")
                if relative == ".claude":
                    sentinel.mkdir()
                    (project / relative).symlink_to(sentinel, target_is_directory=True)
                    provider = "claude"
                else:
                    sentinel.write_text("outside", encoding="utf-8")
                    (project / relative).symlink_to(sentinel)
                    provider = "codex"
                with mock.patch.object(self.app.projects, "_find_port_block", return_value=6501):
                    with self.assertRaisesRegex(ControlCenterError, "symbolic link"):
                        self.app.projects._install_kit(project, provider)
                if sentinel.is_file():
                    self.assertEqual(sentinel.read_text(encoding="utf-8"), "outside")
                else:
                    self.assertEqual(list(sentinel.iterdir()), [])

    @unittest.skipUnless(hasattr(os, "link"), "hard links are required")
    def test_installer_rejects_hardlinked_mutable_support_files_before_copying(self):
        project = self.root / "hardlinked-support"
        project.mkdir()
        (project / "index.html").write_text("<title>Safe</title>\n", encoding="utf-8")
        sentinel = self.root / "outside-agents.md"
        sentinel.write_text("outside content\n", encoding="utf-8")
        os.link(str(sentinel), str(project / "AGENTS.md"))

        with self.assertRaisesRegex(ControlCenterError, "exactly one hard link"):
            self.app.projects._install_kit(project, "codex")

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "outside content\n")
        self.assertFalse((project / "webkit").exists())

    def test_existing_kit_version_mismatch_refuses_without_touching_repo(self):
        repo = self.root / "old-version"
        (repo / "webkit").mkdir(parents=True)
        (repo / "index.html").write_text("<title>Old</title>", encoding="utf-8")
        (repo / "webkit" / "VERSION").write_text("0.0.1\n", encoding="utf-8")
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "Old kit"], cwd=repo, check=True, capture_output=True)

        with self.assertRaises(ControlCenterError) as raised:
            self.app.projects.add_existing(str(repo), "codex")

        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(raised.exception.details, {
            "code": "webkit_update_required",
            "installedVersion": "0.0.1",
            "requiredVersion": "0.8.16",
        })

        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo, text=True, capture_output=True, check=True
        )
        self.assertEqual(status.stdout, "")

    def test_existing_kit_update_choice_must_be_boolean(self):
        with self.assertRaisesRegex(ControlCenterError, "true or false"):
            self.app.projects.add_existing(
                str(self.root), "codex", update_webkit="yes"
            )

    def test_existing_kit_update_replaces_payload_and_preserves_config(self):
        repo = self.root / "update-old-version"
        (repo / "webkit").mkdir(parents=True)
        (repo / "index.html").write_text("<title>Old</title>\n", encoding="utf-8")
        (repo / "webkit" / "VERSION").write_text("0.4.1\n", encoding="utf-8")
        (repo / "webkit" / "legacy-only.txt").write_text(
            "remove me\n", encoding="utf-8"
        )
        with mock.patch.object(
            self.app.projects, "_find_port_block", return_value=6521
        ):
            config = self.app.projects._make_config(repo)
        config_path = repo / "webkit" / "webkit.config.json"
        config_path.write_text(
            json.dumps(config, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        self.app.projects._allocated_ports.clear()
        config_before = config_path.read_bytes()
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-m", "Old kit"], cwd=repo,
            check=True, capture_output=True,
        )

        project = self.app.projects.add_existing(
            str(repo), "codex", update_webkit=True
        )

        self.assertEqual(project["webkitUpdated"]["installedVersion"], "0.4.1")
        self.assertEqual(project["webkitUpdated"]["requiredVersion"], "0.8.16")
        self.assertFalse(project["sourceIntegrationPending"])
        self.assertEqual((repo / "webkit" / "VERSION").read_text().strip(), "0.8.16")
        self.assertEqual(config_path.read_bytes(), config_before)
        self.assertFalse((repo / "webkit" / "legacy-only.txt").exists())
        self.assertTrue((repo / "webkit" / "CONTROL-CENTER.md").is_file())
        subject = subprocess.run(
            ["git", "log", "-1", "--pretty=%s"], cwd=repo,
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        self.assertEqual(subject, "Update AWESOME WEBKIT to v0.8.16")
        self.assertEqual(
            subprocess.run(
                ["git", "status", "--porcelain=v1", "--untracked-files=all"],
                cwd=repo, text=True, capture_output=True, check=True,
            ).stdout,
            "",
        )

    def test_existing_kit_update_keeps_dirty_source_checkout_untouched(self):
        repo = self.root / "update-dirty-version"
        (repo / "webkit").mkdir(parents=True)
        index = repo / "index.html"
        index.write_text("<title>Committed</title>\n", encoding="utf-8")
        (repo / "webkit" / "VERSION").write_text("0.4.1\n", encoding="utf-8")
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-m", "Old kit"], cwd=repo,
            check=True, capture_output=True,
        )
        index.write_text("<title>Uncommitted</title>\n", encoding="utf-8")
        before_head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo,
            text=True, capture_output=True, check=True,
        ).stdout
        before_status = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=repo, text=True, capture_output=True, check=True,
        ).stdout

        with mock.patch.object(
            self.app.projects, "_find_port_block", return_value=6526
        ):
            project = self.app.projects.add_existing(
                str(repo), "codex", update_webkit=True
            )

        self.assertTrue(project["sourceIntegrationPending"])
        self.assertEqual(index.read_text(encoding="utf-8"), "<title>Uncommitted</title>\n")
        self.assertEqual(
            subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo,
                text=True, capture_output=True, check=True,
            ).stdout,
            before_head,
        )
        self.assertEqual(
            subprocess.run(
                ["git", "status", "--porcelain=v1", "--untracked-files=all"],
                cwd=repo, text=True, capture_output=True, check=True,
            ).stdout,
            before_status,
        )
        managed_path = Path(project["path"])
        self.assertEqual(
            (managed_path / "webkit" / "VERSION").read_text().strip(), "0.8.16"
        )
        self.assertEqual(
            subprocess.run(
                ["git", "status", "--porcelain=v1", "--untracked-files=all"],
                cwd=managed_path, text=True, capture_output=True, check=True,
            ).stdout,
            "",
        )

    def test_registered_project_exposes_and_applies_required_webkit_update(self):
        source = self.root / "registered-update-source"
        source.mkdir()
        (source / "index.html").write_text("<title>Registered</title>\n", encoding="utf-8")
        subprocess.run(["git", "init", "-b", "main"], cwd=source, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=source, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=source, check=True)
        subprocess.run(["git", "add", "index.html"], cwd=source, check=True)
        subprocess.run(
            ["git", "commit", "-m", "Initial"], cwd=source,
            check=True, capture_output=True,
        )
        with mock.patch.object(self.app.projects, "_find_port_block", return_value=6531):
            project = self.app.projects.add_existing(str(source), "codex")
        managed = Path(project["path"])
        (managed / "webkit" / "VERSION").write_text("0.8.3\n", encoding="utf-8")
        subprocess.run(["git", "add", "webkit/VERSION"], cwd=managed, check=True)
        subprocess.run(
            ["git", "commit", "-m", "Downgrade test kit"], cwd=managed,
            check=True, capture_output=True,
        )
        old_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=managed,
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        self.app.projects.integrate_managed_target(project, validated_sha=old_sha)

        listed = next(
            item for item in self.app.projects.list_projects()
            if item["id"] == project["id"]
        )
        self.assertEqual(listed["webkitUpdate"], {
            "code": "webkit_update_required",
            "installedVersion": "0.8.3",
            "requiredVersion": "0.8.16",
        })

        active = {
            "id": "registered-update-active",
            "projectId": project["id"],
            "status": "active",
        }
        self.app.store.update(
            lambda state: state.setdefault("sessions", []).append(active)
        )
        with self.assertRaisesRegex(ControlCenterError, "active agents"):
            self.app.projects.add_existing(
                str(source), "codex", update_webkit=True
            )
        self.app.sessions._set_session_status(active["id"], "discarded")

        def copy_working_payload(target):
            return self.app.projects._copy_missing_tree(KIT_ROOT / "webkit", target)

        with mock.patch.object(
            self.app.projects,
            "_copy_current_kit_payload",
            side_effect=copy_working_payload,
        ):
            updated = self.app.projects.add_existing(
                str(source), "codex", update_webkit=True
            )

        self.assertEqual(updated["webkitUpdated"]["requiredVersion"], "0.8.16")
        self.assertEqual((managed / "webkit" / "VERSION").read_text().strip(), "0.8.16")
        self.assertEqual((source / "webkit" / "VERSION").read_text().strip(), "0.8.16")
        self.assertIsNone(
            next(
                item for item in self.app.projects.list_projects()
                if item["id"] == project["id"]
            )["webkitUpdate"]
        )

    def test_kit_update_payload_excludes_untracked_source_files(self):
        kit = self.root / "tracked-kit"
        (kit / "webkit").mkdir(parents=True)
        (kit / "webkit" / "VERSION").write_text("7.2.3\n", encoding="utf-8")
        subprocess.run(["git", "init", "-b", "main"], cwd=kit, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=kit, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=kit, check=True)
        subprocess.run(["git", "add", "webkit/VERSION"], cwd=kit, check=True)
        subprocess.run(
            ["git", "commit", "-m", "Tracked kit"], cwd=kit,
            check=True, capture_output=True,
        )
        (kit / "webkit" / "untracked-private.txt").write_text(
            "do not copy\n", encoding="utf-8"
        )
        destination = self.root / "prepared-kit"
        destination.mkdir()
        manager = control_center_module.ProjectManager(kit, self.app.store)

        manager._copy_current_kit_payload(destination)

        self.assertEqual(
            (destination / "VERSION").read_text(encoding="utf-8"), "7.2.3\n"
        )
        self.assertFalse((destination / "untracked-private.txt").exists())

    def test_unignored_secret_is_never_added_to_unborn_repository_history(self):
        repo = self.root / "secret-site"
        repo.mkdir()
        (repo / "index.html").write_text("<title>Private</title>", encoding="utf-8")
        (repo / ".env").write_text("TOKEN=sentinel-secret\n", encoding="utf-8")
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)

        before_env = (repo / ".env").read_bytes()
        before_index = (repo / "index.html").read_bytes()
        with self.assertRaisesRegex(ControlCenterError, "no commits"):
            self.app.projects.add_existing(str(repo), "codex")

        history = subprocess.run(
            ["git", "log", "--all", "--format=%H"], cwd=repo,
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(history.stdout.strip(), "")
        tracked = subprocess.run(
            ["git", "ls-files", "--", ".env"], cwd=repo,
            text=True, capture_output=True, check=True,
        )
        self.assertEqual(tracked.stdout.strip(), "")
        self.assertFalse((repo / ".gitignore").exists())
        self.assertEqual((repo / ".env").read_bytes(), before_env)
        self.assertEqual((repo / "index.html").read_bytes(), before_index)

    def test_staged_secret_scan_reads_large_blob_prefix_from_the_index(self):
        repo = self.root / "large-staged-secret"
        repo.mkdir()
        subprocess.run(
            ["git", "init", "-b", "main"], cwd=repo,
            check=True, capture_output=True,
        )
        staged_path = repo / "large.txt"
        staged_path.write_bytes(assemble_test_bytes(
            b"sk-proj-", b"A" * 40, b"\n", b"x" * (3 * 1024 * 1024),
        ))
        subprocess.run(["git", "add", "large.txt"], cwd=repo, check=True)

        with self.assertRaisesRegex(ControlCenterError, "possible secrets"):
            self.app.projects._reject_staged_secrets(repo)

    def test_tracked_source_tree_has_no_high_confidence_secret_markers(self):
        tracked = subprocess.run(
            ["git", "ls-files", "-z"], cwd=KIT_ROOT,
            capture_output=True, check=True,
        ).stdout.split(b"\0")
        violations = []
        for raw_path in tracked:
            if not raw_path:
                continue
            relative_path = Path(os.fsdecode(raw_path))
            candidate = KIT_ROOT / relative_path
            try:
                details = candidate.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(details.st_mode):
                continue
            with candidate.open("rb") as handle:
                sample = handle.read(control_center_module.MAX_SECRET_SCAN_BYTES)
            risk = control_center_module.secret_file_risk(relative_path.as_posix(), sample)
            if risk:
                violations.append("{} ({})".format(relative_path.as_posix(), risk))
        self.assertEqual(violations, [])

    def test_secret_scan_covers_common_web_source_suffixes(self):
        marker = assemble_test_bytes(b"github_pat_", b"S" * 40)
        for suffix in (
            ".astro", ".coffee", ".cts", ".htm", ".mdx", ".mts", ".xhtml",
        ):
            with self.subTest(suffix=suffix):
                self.assertEqual(
                    control_center_module.secret_file_risk("component" + suffix, marker),
                    "a GitHub token",
                )

    def test_staged_secret_scan_uses_index_blob_not_racing_worktree_bytes(self):
        repo = self.root / "exact-staged-secret"
        repo.mkdir()
        subprocess.run(
            ["git", "init", "-b", "main"], cwd=repo,
            check=True, capture_output=True,
        )
        staged_path = repo / "notes.txt"
        staged_path.write_text("safe staged text\n", encoding="utf-8")
        subprocess.run(["git", "add", "notes.txt"], cwd=repo, check=True)
        staged_path.write_text(
            "sk-proj-{}\n".format("B" * 40), encoding="utf-8"
        )

        self.app.projects._reject_staged_secrets(repo)

    def test_add_existing_never_creates_a_github_repository(self):
        repo = self.root / "local-only"
        repo.mkdir()
        (repo / "index.html").write_text("<title>Local</title>", encoding="utf-8")
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "Initial"], cwd=repo, check=True, capture_output=True)

        with mock.patch.object(self.app.projects, "_find_port_block", return_value=6511), mock.patch.object(
            self.app.projects, "_ensure_github_repo"
        ) as create_remote:
            project = self.app.projects.add_existing(str(repo), "codex")

        create_remote.assert_not_called()
        self.assertFalse(self.app.projects.github_status(project["path"])["connected"])

    def test_github_remote_display_strips_credentials(self):
        value = sanitize_remote_url("https://user:private-token@github.com/example/site.git?token=also-private")
        self.assertEqual(value, "https://github.com/example/site.git")
        self.assertNotIn("private", value)

    def test_github_status_requires_exact_remote_hostname(self):
        repo = self.root / "false-github"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "remote", "add", "origin", "https://evil.example/@github.com/example/site.git"],
            cwd=repo, check=True,
        )
        self.assertFalse(self.app.projects.github_status(repo)["connected"])

    def test_config_validator_accepts_compatible_custom_slug_and_rejects_unsafe_label(self):
        project = self.root / "custom-config"
        (project / "webkit").mkdir(parents=True)
        (project / "index.html").write_text("<title>Custom</title>", encoding="utf-8")
        config_path = project / "webkit" / "webkit.config.json"
        config = {
            "project_name": "custom",
            "site_root": ".",
            "default_page": "index.html",
            "feedback_dir": ".webkit/feedback",
            "lock_dir": str(self.root / "custom-locks"),
            "palette": [{"slug": "blue_2", "emoji": "🔷", "port": 6521}],
        }
        config_path.write_text(json.dumps(config), encoding="utf-8")
        self.assertEqual(load_webkit_config(config_path, project)["palette"][0]["slug"], "blue_2")
        config["palette"][0]["emoji"] = "<script>"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        with self.assertRaisesRegex(ControlCenterError, "without whitespace.*markup"):
            load_webkit_config(config_path, project)
        for unsafe in ("blue\u202e", "blue\u2066", "blue\u009f"):
            config["palette"][0]["emoji"] = unsafe
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaisesRegex(ControlCenterError, "control"):
                load_webkit_config(config_path, project)
        config["palette"][0]["emoji"] = "👩‍💻️"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        self.assertEqual(load_webkit_config(config_path, project)["palette"][0]["emoji"], "👩‍💻️")
        for invalid_grace in (0, 29, 86401, True, 30.5, "180"):
            with self.subTest(grace=invalid_grace):
                config["grace_seconds"] = invalid_grace
                config_path.write_text(json.dumps(config), encoding="utf-8")
                with self.assertRaisesRegex(ControlCenterError, "30 through 86400"):
                    load_webkit_config(config_path, project)

    def test_config_validator_rejects_symlink_lock_directory(self):
        project = self.root / "symlink-lock-config"
        (project / "webkit").mkdir(parents=True)
        (project / "index.html").write_text("<title>Config</title>", encoding="utf-8")
        real_lock = self.root / "real-lock"
        real_lock.mkdir()
        linked_lock = self.root / "linked-lock"
        linked_lock.symlink_to(real_lock, target_is_directory=True)
        config = {
            "site_root": ".", "default_page": "index.html",
            "feedback_dir": ".webkit/feedback", "lock_dir": str(linked_lock),
            "palette": [{"slug": "blue", "emoji": "🔵", "port": 6522}],
        }
        path = project / "webkit" / "webkit.config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        with self.assertRaisesRegex(ControlCenterError, "lock_dir.*symbolic link"):
            load_webkit_config(path, project)

        linked_lock.unlink()
        linked_lock.symlink_to(self.root / "missing-lock-target", target_is_directory=True)
        with self.assertRaisesRegex(ControlCenterError, "lock_dir.*symbolic link"):
            load_webkit_config(path, project)

    def test_config_reader_rejects_oversize_and_symlink_files(self):
        project = self.root / "unsafe-config-file"
        (project / "webkit").mkdir(parents=True)
        (project / "index.html").write_text("<title>Config</title>\n", encoding="utf-8")
        path = project / "webkit" / "webkit.config.json"
        path.write_bytes(b"x" * (256 * 1024 + 1))
        with self.assertRaisesRegex(ControlCenterError, "larger than"):
            load_webkit_config(path, project)

        outside = self.root / "outside-config.json"
        outside.write_text("{}\n", encoding="utf-8")
        path.unlink()
        try:
            path.symlink_to(outside)
        except (OSError, NotImplementedError) as exc:
            self.skipTest("symbolic links unavailable: {}".format(exc))
        with self.assertRaisesRegex(ControlCenterError, "symbolic link"):
            load_webkit_config(path, project)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO files unavailable")
    def test_config_reader_rejects_fifo_without_blocking(self):
        project = self.root / "fifo-config-file"
        (project / "webkit").mkdir(parents=True)
        (project / "index.html").write_text("<title>Config</title>\n", encoding="utf-8")
        path = project / "webkit" / "webkit.config.json"
        os.mkfifo(str(path))
        started = time.monotonic()
        with self.assertRaisesRegex(ControlCenterError, "regular file"):
            load_webkit_config(path, project)
        self.assertLess(time.monotonic() - started, 1)

    def test_event_logs_share_one_lock_across_instances(self):
        first = EventLog(self.state_dir, "concurrent")
        second = EventLog(self.state_dir, "concurrent")
        self.assertIs(first.lock, second.lock)
        threads = []
        for index in range(8):
            log = first if index % 2 else second
            thread = threading.Thread(
                target=lambda current=index, target=log: [
                    target.append("agent", "{}:{}".format(current, item)) for item in range(40)
                ]
            )
            threads.append(thread)
            thread.start()
        for thread in threads:
            thread.join()
        cursor = 0
        events = []
        while True:
            page = first.read_after(cursor)
            events.extend(page["events"])
            if page["next"] == cursor:
                break
            cursor = page["next"]
        self.assertEqual(len(events), 320)
        indexes = [event["index"] for event in events]
        self.assertEqual(len(set(indexes)), 320)
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{32}:\d+", index) for index in indexes))

    def test_event_logs_bound_each_page_and_large_event_text(self):
        log = EventLog(self.state_dir, "bounded")
        for index in range(130):
            log.append("agent", "x" * 40000 if index == 0 else str(index))
        first = log.read_after(0)
        self.assertEqual(len(first["events"]), 100)
        self.assertIn("output truncated", first["events"][0]["text"])
        second = log.read_after(first["next"])
        self.assertEqual(len(second["events"]), 30)

    def test_event_log_rotates_and_resets_a_stale_byte_cursor(self):
        with mock.patch("control_center.MAX_EVENT_LOG_BYTES", 1800), mock.patch(
            "control_center.EVENT_LOG_RETAIN_BYTES", 900
        ):
            log = EventLog(self.state_dir, "rotating")
            for index in range(6):
                log.append("agent", "before-{}-{}".format(index, "x" * 140))
            stale_cursor = log.read_after(0)["next"]
            self.assertRegex(stale_cursor, r"^[0-9a-f]{32}:\d+$")
            for index in range(24):
                log.append("agent", "after-{}-{}".format(index, "y" * 140))

            page = log.read_after(stale_cursor)

            self.assertTrue(page["reset"])
            self.assertLessEqual(log.path.stat().st_size, 1800)
            current = log.read_after(0)
            self.assertEqual(current["events"][-1]["text"], "after-23-{}".format("y" * 140))

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "No-follow file opens required")
    def test_event_log_refuses_a_symbolic_link(self):
        log = EventLog(self.state_dir, "linked")
        outside = self.root / "outside-log"
        outside.write_text('{"text":"outside"}\n', encoding="utf-8")
        log.path.symlink_to(outside)
        with self.assertRaisesRegex(ControlCenterError, "unreadable"):
            log.read_after(0)
        self.assertEqual(outside.read_text(encoding="utf-8"), '{"text":"outside"}\n')

    def test_feedback_protocol_rejects_symlinks_and_keys_content_not_mtime(self):
        inbox = self.root / "feedback-safety"
        inbox.mkdir()
        outside = self.root / "outside-feedback.json"
        outside.write_text(json.dumps({
            "version": 1, "kind": "feedback", "batchId": "outside", "round": 1,
        }), encoding="utf-8")
        (inbox / "feedback.json").symlink_to(outside)
        self.assertIsNone(SessionRuntime._feedback_phase_key(inbox))
        (inbox / "feedback.json").unlink()
        path = inbox / "feedback.json"
        first = {"version": 1, "kind": "feedback", "batchId": "batch-1", "round": 1}
        second = {"version": 1, "kind": "feedback", "batchId": "batch-2", "round": 1}
        path.write_text(json.dumps(first), encoding="utf-8")
        fixed = 1_700_000_000_000_000_000
        os.utime(path, ns=(fixed, fixed))
        first_key = SessionRuntime._feedback_phase_key(inbox)
        path.write_text(json.dumps(second), encoding="utf-8")
        os.utime(path, ns=(fixed, fixed))
        second_key = SessionRuntime._feedback_phase_key(inbox)
        self.assertNotEqual(first_key, second_key)

    def test_provider_prompt_limit_rejects_before_spawning(self):
        session = {
            "id": "prompt-limit", "provider": "codex", "worktree": str(self.root),
            "color": "blue", "branch": "webkit/blue/prompt-limit",
        }
        runner = ProviderRunner(
            session, EventLog(self.state_dir, "prompt-limit"), lambda _value: None, lambda _value: True
        )
        with mock.patch("control_center.subprocess.Popen") as popen:
            with self.assertRaisesRegex(ControlCenterError, "16 KB"):
                runner.run("x" * (16 * 1024 + 1))
        popen.assert_not_called()

    def test_codex_resume_keeps_workspace_sandbox_and_scopes_git_write_dirs(self):
        repo = self.root / "provider-repo"
        repo.mkdir()
        (repo / "index.html").write_text("<title>Provider</title>", encoding="utf-8")
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "Initial"], cwd=repo, check=True, capture_output=True)
        worktree = self.root / "provider-worktree"
        branch = "webkit/blue/provider"
        subprocess.run(
            ["git", "worktree", "add", "-b", branch, str(worktree), "main"],
            cwd=repo, check=True, capture_output=True,
        )
        session = {
            "id": "provider-resume", "provider": "codex", "worktree": str(worktree),
            "color": "blue", "branch": branch, "threadId": "thread-123", "hasRun": True,
        }
        runner = ProviderRunner(
            session, EventLog(self.state_dir, "provider-resume"),
            lambda _value: None, lambda _value: True, allow_git=True,
        )
        fake = FakeProcess([])
        real_popen = subprocess.Popen
        def provider_popen(command, *args, **kwargs):
            if command[0] == "/fake/codex":
                return fake
            return real_popen(command, *args, **kwargs)
        with mock.patch("control_center.shutil.which", return_value="/fake/codex"), mock.patch(
            "control_center.subprocess.Popen", side_effect=provider_popen
        ) as popen:
            runner.run("Continue")
        command = next(
            call[0][0] for call in popen.call_args_list if call[0][0][0] == "/fake/codex"
        )
        self.assertEqual(command[:5], [
            "/fake/codex", "exec", "--json", "--sandbox", "workspace-write",
        ])
        self.assertLess(command.index("workspace-write"), command.index("resume"))
        self.assertEqual(command[command.index("resume") + 1], "thread-123")
        self.assertEqual(command[-1], "-")
        self.assertNotIn("Continue", command)
        add_dirs = [command[index + 1] for index, value in enumerate(command) if value == "--add-dir"]
        common = Path(subprocess.run(
            ["git", "rev-parse", "--git-common-dir"], cwd=worktree,
            text=True, capture_output=True, check=True,
        ).stdout.strip()).resolve()
        self.assertIn(str((common / "objects").resolve()), add_dirs)
        self.assertIn(str((common / "refs" / "heads" / "webkit" / "blue").resolve()), add_dirs)
        self.assertNotIn(str((common / "refs" / "heads").resolve()), add_dirs)

        seed_runner = ProviderRunner(
            dict(session, threadId=None), EventLog(self.state_dir, "provider-seed"),
            lambda _value: None, lambda _value: True, allow_git=False,
        )
        seed_fake = FakeProcess([])
        def seed_provider_popen(command, *args, **kwargs):
            if command[0] == "/fake/codex":
                return seed_fake
            return real_popen(command, *args, **kwargs)
        with mock.patch("control_center.shutil.which", return_value="/fake/codex"), mock.patch(
            "control_center.subprocess.Popen", side_effect=seed_provider_popen
        ) as seed_popen:
            seed_runner.run("Generate seeds")
        seed_command = next(
            call[0][0] for call in seed_popen.call_args_list if call[0][0][0] == "/fake/codex"
        )
        self.assertNotIn("--add-dir", seed_command)
        self.assertNotIn("Generate seeds", seed_command)

    def test_project_context_rejects_invalid_batch_without_partial_writes(self):
        valid = base64.b64encode(b"valid").decode("ascii")
        with self.assertRaisesRegex(ControlCenterError, "base64"):
            self.app.projects._save_project_context(self.root, {"assets": [
                {"name": "valid.txt", "data": valid},
                {"name": "invalid.txt", "data": "%%%"},
            ]})
        self.assertFalse((self.root / "project-context").exists())

        with self.assertRaisesRegex(ControlCenterError, "limited to 500"):
            self.app.projects._save_project_context(self.root, {
                "assets": [{"name": "{}.txt".format(index), "data": valid} for index in range(501)]
            })
        self.assertFalse((self.root / "project-context").exists())

    def test_new_project_rejects_preexisting_symlink_target(self):
        parent = self.root / "new-project-parent"
        parent.mkdir()
        outside = self.root / "outside-project"
        outside.mkdir()
        (parent / "demo").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ControlCenterError, "symbolic link"):
            self.app.projects.create_project("Demo", str(parent), "codex")
        self.assertEqual(list(outside.iterdir()), [])

    def test_new_project_rejects_preexisting_file_target_without_changes(self):
        parent = self.root / "new-project-file-parent"
        parent.mkdir()
        project_path = parent / "demo"
        project_path.write_bytes(b"user-owned content\n")

        with self.assertRaisesRegex(ControlCenterError, "must be a folder"):
            self.app.projects.create_project("Demo", str(parent), "codex")

        self.assertEqual(project_path.read_bytes(), b"user-owned content\n")

    def test_new_project_failure_restores_preexisting_empty_target(self):
        parent = self.root / "existing-empty-parent"
        parent.mkdir()
        project_path = parent / "existing-empty"
        project_path.mkdir()

        with mock.patch.object(
            self.app.projects, "_find_port_block", return_value=6526
        ), mock.patch.object(
            self.app.projects,
            "_install_kit",
            side_effect=ControlCenterError("forced install failure", 409),
        ):
            with self.assertRaisesRegex(ControlCenterError, "forced install failure"):
                self.app.projects.create_project(
                    "Existing Empty", str(parent), "codex"
                )

        self.assertTrue(project_path.is_dir())
        self.assertEqual(list(project_path.iterdir()), [])
        self.assertEqual(
            list(parent.glob(".existing-empty-webkit-empty-*")), []
        )

    def test_release_uses_exact_claim_after_config_is_deleted(self):
        worktree = self.root / "claimed-worktree"
        worktree.mkdir()
        config = {
            "lock_dir": str(self.root / "exact-locks"),
            "palette": [{"slug": "blue", "emoji": "🔵", "port": 6531}],
        }
        session = {
            "id": "claimed", "worktree": str(worktree), "color": "blue",
            "emoji": "🔵", "port": 6531,
        }
        runtime = SessionRuntime(self.app.sessions, session)
        runtime.claimed_lock, runtime.claimed_owner = self.app.sessions._claim_lock(
            config, "blue", worktree
        )
        self.app.sessions.runtimes[session["id"]] = runtime
        self.assertTrue(runtime.claimed_lock.exists())
        self.app.sessions._release(runtime)
        self.assertFalse(Path(config["lock_dir"]).joinpath("blue.lock").exists())

    def test_release_closes_the_owned_external_preview_tab(self):
        worktree = self.root / "close-preview-worktree"
        script = worktree / "webkit" / "scripts" / "open-preview.sh"
        script.parent.mkdir(parents=True)
        script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        script.chmod(0o755)
        session = {
            "id": "close-preview", "worktree": str(worktree), "color": "red",
            "emoji": "🔴", "previewUrl": "http://127.0.0.1:6539/index.html",
        }
        runtime = SessionRuntime(self.app.sessions, session)
        completed = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch("control_center.platform.system", return_value="Darwin"), mock.patch(
            "control_center.run_command", return_value=completed
        ) as run:
            self.app.sessions._close_preview_tab(runtime)
        run.assert_called_once_with(
            [str(script), "--close", session["previewUrl"]],
            cwd=worktree,
            check=False,
            timeout=15,
        )

    def test_release_uses_a_posix_shell_to_close_the_preview_tab_on_windows(self):
        worktree = self.root / "close-preview-windows-worktree"
        script = worktree / "webkit" / "scripts" / "open-preview.sh"
        script.parent.mkdir(parents=True)
        script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        session = {
            "id": "close-preview-windows", "worktree": str(worktree), "color": "red",
            "emoji": "🔴", "previewUrl": "http://127.0.0.1:6540/index.html",
        }
        runtime = SessionRuntime(self.app.sessions, session)
        completed = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch("control_center.platform.system", return_value="Windows"), mock.patch(
            "control_center.shutil.which", return_value="C:/Program Files/Git/bin/bash.exe"
        ), mock.patch("control_center.run_command", return_value=completed) as run:
            self.app.sessions._close_preview_tab(runtime)
        run.assert_called_once_with(
            [
                "C:/Program Files/Git/bin/bash.exe", str(script), "--close",
                session["previewUrl"],
            ],
            cwd=worktree,
            check=False,
            timeout=15,
        )

    def test_release_continues_when_preview_tab_cleanup_cannot_start(self):
        worktree = self.root / "close-preview-error-worktree"
        script = worktree / "webkit" / "scripts" / "open-preview.sh"
        script.parent.mkdir(parents=True)
        script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        session = {
            "id": "close-preview-error", "worktree": str(worktree), "color": "red",
            "emoji": "🔴", "previewUrl": "http://127.0.0.1:6541/index.html",
        }
        runtime = SessionRuntime(self.app.sessions, session)
        with mock.patch(
            "control_center.run_command", side_effect=OSError("cannot execute")
        ):
            self.app.sessions._close_preview_tab(runtime)
        events = runtime.log.read_after(0)["events"]
        self.assertEqual(
            events[-1]["text"],
            "The finished preview tab could not be closed automatically.",
        )

    def test_generic_merge_rejects_seed_sessions(self):
        session = {
            "id": "seed-no-merge", "projectId": "project", "kind": "seeds",
            "status": "active", "worktree": str(self.root),
        }
        self.app.store.update(lambda state: state.setdefault("sessions", []).append(session))
        with self.assertRaisesRegex(ControlCenterError, "seed onboarding"):
            self.app.sessions.merge(session["id"])

    def test_interrupted_chat_recovers_as_retryable_error_without_losing_existing_error(self):
        project = self.root / "recovery-project"
        (project / "webkit").mkdir(parents=True)
        (project / "index.html").write_text("<title>Recovery</title>", encoding="utf-8")
        config = {
            "project_name": "recovery", "site_root": ".", "default_page": "index.html",
            "feedback_dir": ".webkit/feedback", "lock_dir": str(self.root / "recovery-locks"),
            "palette": [{"slug": "blue", "emoji": "🔵", "port": 6532}],
        }
        (project / "webkit" / "webkit.config.json").write_text(json.dumps(config), encoding="utf-8")
        sessions = [
            {
                "id": "busy-recovery", "projectId": "project", "provider": "codex",
                "worktree": str(project), "color": "blue", "emoji": "🔵", "port": 6532,
                "branch": "webkit/blue/recovery", "status": "busy",
                "pendingOperation": "chat",
            },
            {
                "id": "error-recovery", "projectId": "project", "provider": "codex",
                "worktree": str(project), "color": "red", "emoji": "🔴", "port": 6533,
                "branch": "webkit/red/recovery", "status": "error", "error": "keep this error",
            },
        ]
        self.app.store.update(lambda state: state.setdefault("sessions", []).extend(sessions))
        with mock.patch.object(self.app.sessions, "_claim_and_preview"), mock.patch.object(
            SessionRuntime, "start"
        ):
            self.app.sessions.recover()
        self.assertIn("interrupted", self.app.sessions._get_session("busy-recovery")["error"])
        self.assertEqual(self.app.sessions._get_session("error-recovery")["error"], "keep this error")

    def test_cancelled_startup_recovery_stops_every_runtime_it_spawned(self):
        project = self.root / "cancelled-recovery"
        (project / "webkit").mkdir(parents=True)
        (project / "index.html").write_text("<title>Recovery</title>", encoding="utf-8")
        config = {
            "project_name": "recovery", "site_root": ".", "default_page": "index.html",
            "feedback_dir": ".webkit/feedback", "lock_dir": str(self.root / "cancelled-locks"),
            "palette": [{"slug": "blue", "emoji": "🔵", "port": 6534}],
        }
        (project / "webkit" / "webkit.config.json").write_text(
            json.dumps(config), encoding="utf-8"
        )
        session = {
            "id": "cancelled-recovery", "projectId": "project", "provider": "codex",
            "worktree": str(project), "color": "blue", "emoji": "🔵", "port": 6534,
            "branch": "webkit/blue/cancelled", "status": "active",
        }
        self.app.store.update(lambda state: state.setdefault("sessions", []).append(session))
        cancellation = threading.Event()
        preview = mock.Mock()
        preview.pid = None
        preview.returncode = None
        preview.poll.return_value = None
        preview.wait.return_value = 0

        def spawn_then_cancel(runtime, _config, cancel_event=None):
            self.assertIs(cancel_event, cancellation)
            runtime.preview_process = preview
            cancellation.set()

        with mock.patch.object(
            self.app.sessions, "_claim_and_preview", side_effect=spawn_then_cancel
        ):
            self.assertFalse(self.app.sessions.recover(cancel_event=cancellation))
        self.assertEqual(self.app.sessions.runtimes, {})
        preview.terminate.assert_called_once_with()
        preview.wait.assert_called()
        self.assertEqual(
            self.app.sessions._get_session("cancelled-recovery")["status"], "active"
        )

    @unittest.skipUnless(os.name == "posix", "POSIX process groups required")
    def test_process_tree_cleanup_kills_child_after_parent_exits(self):
        heartbeat = self.root / "child-heartbeat"
        script = (
            "import subprocess,sys; "
            "child=subprocess.Popen([sys.executable,'-c',"
            "'import pathlib,sys,time; p=pathlib.Path(sys.argv[1]); "
            "[(p.write_text(str(i)), time.sleep(.02)) for i in range(3000)]',sys.argv[1]]); "
            "print(child.pid, flush=True)"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(heartbeat)], stdout=subprocess.PIPE, text=True,
            start_new_session=True,
        )
        self.assertTrue(int(process.stdout.readline().strip()) > 0)
        time.sleep(0.1)
        stop_process_tree(process, timeout=2, grace=0.05)
        process.stdout.close()
        time.sleep(0.1)
        first = heartbeat.read_text(encoding="utf-8")
        time.sleep(0.2)
        self.assertEqual(heartbeat.read_text(encoding="utf-8"), first)

    def test_windows_process_tree_does_not_force_kill_after_graceful_exit(self):
        process = mock.Mock()
        process.pid = 4172
        process.returncode = None
        process.wait.return_value = 0
        with mock.patch("control_center.os.name", "nt"), mock.patch(
            "control_center.shutil.which", return_value="C:/Windows/System32/taskkill.exe"
        ), mock.patch("control_center.subprocess.run") as run:
            self.assertEqual(stop_process_tree(process, timeout=2, grace=0.05), 0)
        self.assertEqual(run.call_count, 1)
        self.assertNotIn("/F", run.call_args[0][0])
        process.wait.assert_called_once_with(timeout=0.05)

    @unittest.skipIf(
        os.name != "nt" and sys.version_info < (3, 8),
        "ctypes.wintypes is not importable on Python 3.7 POSIX builds",
    )
    def test_windows_provider_job_closes_when_exact_leader_handle_exits(self):
        import ctypes
        from ctypes import wintypes  # noqa: F401

        kernel32 = mock.Mock()
        kernel32.CreateJobObjectW.return_value = 8123
        kernel32.SetInformationJobObject.return_value = True
        kernel32.AssignProcessToJobObject.return_value = True
        kernel32.WaitForSingleObject.return_value = 0
        process = mock.Mock()
        process._handle = 4172
        process.pid = 4172
        with mock.patch("control_center.os.name", "nt"), mock.patch.object(
            ctypes, "WinDLL", return_value=kernel32, create=True
        ):
            self.assertEqual(attach_windows_kill_job(process), 8123)
            ProviderRunner._watch_windows_provider_exit(
                process, threading.Event()
            )
        self.assertTrue(process._webkit_leader_exited)
        self.assertIsNone(process._webkit_windows_job)
        kernel32.AssignProcessToJobObject.assert_called_once()
        kernel32.CloseHandle.assert_called_once_with(8123)

    def test_chat_attachment_batch_is_all_or_nothing(self):
        worktree = self.root / "attachment-worktree"
        worktree.mkdir()
        session = {
            "id": "attachments", "projectId": "project", "provider": "codex",
            "worktree": str(worktree), "color": "blue", "emoji": "🔵",
            "branch": "webkit/blue/attachments", "status": "active",
        }
        self.app.store.update(lambda state: state.setdefault("sessions", []).append(session))
        runtime = SessionRuntime(self.app.sessions, dict(session))
        self.app.sessions.runtimes[session["id"]] = runtime
        payload = base64.b64encode(b"safe").decode("ascii")
        with self.assertRaisesRegex(ControlCenterError, "base64"):
            self.app.sessions.send_message(session["id"], "inspect", [
                {"name": "safe.txt", "data": payload},
                {"name": "bad.txt", "data": "%%%"},
            ])
        folder = worktree / ".webkit" / "chat-attachments" / session["id"]
        self.assertFalse(folder.exists())
        self.assertEqual(self.app.sessions._get_session(session["id"])["status"], "active")

    def test_chat_attachment_symlink_parent_is_rejected(self):
        worktree = self.root / "attachment-link-worktree"
        worktree.mkdir()
        outside = self.root / "outside-attachments"
        outside.mkdir()
        (worktree / ".webkit").symlink_to(outside, target_is_directory=True)
        session = {
            "id": "attachment-link", "projectId": "project", "provider": "codex",
            "worktree": str(worktree), "color": "blue", "emoji": "🔵",
            "branch": "webkit/blue/attachment-link", "status": "active",
        }
        self.app.store.update(lambda state: state.setdefault("sessions", []).append(session))
        runtime = SessionRuntime(self.app.sessions, dict(session))
        self.app.sessions.runtimes[session["id"]] = runtime
        with self.assertRaisesRegex(ControlCenterError, "symbolic link"):
            self.app.sessions.send_message(session["id"], "inspect", [{
                "name": "safe.txt", "data": base64.b64encode(b"safe").decode("ascii"),
            }])
        self.assertEqual(list(outside.iterdir()), [])

    def test_terminal_session_status_cannot_regress(self):
        session = {"id": "terminal", "status": "merged"}
        self.app.store.update(lambda state: state.setdefault("sessions", []).append(session))
        self.assertFalse(self.app.sessions._set_session_status("terminal", "active"))
        self.assertEqual(self.app.sessions._get_session("terminal")["status"], "merged")

    def test_http_body_rejects_non_json_and_nonstandard_constants(self):
        for content_type, body, expected in (
            ("text/plain", b"{}", "Content-Type"),
            ("application/json", b'{"value":NaN}', "Nonstandard JSON"),
            ("application/json", b'{"value":Infinity}', "Nonstandard JSON"),
        ):
            with self.subTest(body=body):
                request = mock.Mock()
                request.headers = {
                    "Content-Length": str(len(body)), "Content-Type": content_type,
                }
                request.rfile = io.BytesIO(body)
                with self.assertRaisesRegex(ControlCenterError, expected):
                    ControlCenterHandler._body(request)

    def test_http_body_rejects_truncated_content_length(self):
        request = mock.Mock()
        request.headers = {
            "Content-Length": "12", "Content-Type": "application/json",
        }
        request.rfile = io.BytesIO(b"{}")
        with self.assertRaisesRegex(ControlCenterError, "ended before Content-Length"):
            ControlCenterHandler._body(request)

    def test_server_rejects_unsafe_auth_tokens_before_startup(self):
        for token in ("short", "a" * 129, "unsafe token value", "line\nbreak-token-value"):
            with self.subTest(token=token):
                self.assertFalse(control_center_server.valid_auth_token(token))
                with self.assertRaisesRegex(ValueError, "16 to 128 URL-safe"):
                    ControlCenterHTTPServer(
                        ("127.0.0.1", 0), ControlCenterHandler, self.app, token
                    )
        self.assertTrue(control_center_server.valid_auth_token("safe_token-123456"))

    def test_server_reports_invalid_environment_port_cleanly(self):
        with mock.patch.dict(os.environ, {"WKCC_PORT": "not-a-port"}), mock.patch(
            "sys.stderr", new_callable=io.StringIO
        ) as stderr:
            with self.assertRaises(SystemExit) as raised:
                control_center_server.main([])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("WKCC_PORT must be an integer", stderr.getvalue())

    def test_server_reports_invalid_environment_token_cleanly(self):
        with mock.patch.dict(os.environ, {"WKCC_TOKEN": "bad token"}), mock.patch(
            "sys.stderr", new_callable=io.StringIO
        ) as stderr, mock.patch.object(control_center_server, "ControlCenter"):
            with self.assertRaises(SystemExit) as raised:
                control_center_server.main(["--state-dir", str(self.state_dir), "--no-browser"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("WKCC_TOKEN must be 16 to 128 characters", stderr.getvalue())

    def test_server_bind_failure_happens_before_recovery_and_still_shuts_down(self):
        app = mock.Mock()
        with mock.patch.object(
            control_center_server, "ControlCenter", return_value=app
        ) as create_app, mock.patch.object(
            control_center_server, "ControlCenterHTTPServer",
            side_effect=OSError("address in use"),
        ), mock.patch.dict(
            os.environ, {"WKCC_TOKEN": "safe-token-1234567890"}
        ):
            with self.assertRaisesRegex(OSError, "address in use"):
                control_center_server.main([
                    "--port", "8799", "--state-dir", str(self.state_dir), "--no-browser",
                ])
        create_app.assert_called_once_with(
            control_center_server.KIT_ROOT, self.state_dir.resolve(), recover=False
        )
        app.sessions.recover.assert_not_called()
        app.sessions.shutdown.assert_called_once_with()

    def test_server_recovery_exception_closes_bound_server_and_all_sessions(self):
        order = []
        app = mock.Mock()
        def fail_recovery(cancel_event=None):
            self.assertTrue(hasattr(cancel_event, "is_set"))
            order.append("recover")
            raise RuntimeError("recovery failed")
        app.sessions.recover.side_effect = fail_recovery
        server = mock.Mock()
        server.server_close.side_effect = lambda: order.append("server-close")
        app.sessions.shutdown.side_effect = lambda: order.append("sessions-shutdown")

        def bind_server(*_args, **_kwargs):
            order.append("bind")
            return server

        with mock.patch.object(
            control_center_server, "ControlCenter", return_value=app
        ), mock.patch.object(
            control_center_server, "ControlCenterHTTPServer", side_effect=bind_server
        ), mock.patch.object(
            control_center_server.signal, "signal"
        ), mock.patch.dict(
            os.environ, {"WKCC_TOKEN": "safe-token-1234567890"}
        ):
            with self.assertRaisesRegex(RuntimeError, "recovery failed"):
                control_center_server.main([
                    "--port", "8799", "--state-dir", str(self.state_dir), "--no-browser",
                ])
        self.assertEqual(
            order, ["bind", "recover", "server-close", "sessions-shutdown"]
        )
        server.serve_forever.assert_not_called()
        server.server_close.assert_called_once_with()
        app.sessions.shutdown.assert_called_once_with()

    def test_server_startup_signal_cancels_recovery_before_serving(self):
        handlers = {}
        app = mock.Mock()
        server = mock.Mock()

        def install_handler(signum, handler):
            handlers[signum] = handler

        def interrupt_recovery(cancel_event=None):
            self.assertIn(signal.SIGTERM, handlers)
            handlers[signal.SIGTERM]()
            self.assertTrue(cancel_event.is_set())
            return False

        app.sessions.recover.side_effect = interrupt_recovery
        with mock.patch.object(
            control_center_server, "ControlCenter", return_value=app
        ), mock.patch.object(
            control_center_server, "ControlCenterHTTPServer", return_value=server
        ), mock.patch.object(
            control_center_server.signal, "signal", side_effect=install_handler
        ), mock.patch.dict(
            os.environ, {"WKCC_TOKEN": "safe-token-1234567890"}
        ):
            self.assertEqual(control_center_server.main([
                "--port", "8799", "--state-dir", str(self.state_dir), "--no-browser",
            ]), 0)
        app.sessions.request_shutdown.assert_called_once_with()
        app.sessions.shutdown.assert_called_once_with()
        server.serve_forever.assert_not_called()
        server.server_close.assert_called_once_with()

    def test_invalid_event_cursor_is_a_client_error(self):
        session = {"id": "events", "status": "active"}
        self.app.store.update(lambda state: state.setdefault("sessions", []).append(session))
        with self.assertRaises(ControlCenterError) as raised:
            self.app.sessions.events("events", "not-a-number")
        self.assertEqual(raised.exception.status, 400)

    def test_staged_secret_scan_reads_only_the_pinned_tree(self):
        repo = self.root / "pinned-index"
        repo.mkdir()
        subprocess.run(
            ["git", "init", "-b", "main"], cwd=repo,
            check=True, capture_output=True,
        )
        (repo / "theme.css").write_text(
            "sk-proj-{}\n".format("Q" * 40), encoding="utf-8"
        )
        subprocess.run(["git", "add", "theme.css"], cwd=repo, check=True)
        real_run = control_center_module.run_command

        def reject_mutable_index_queries(command, **kwargs):
            if command[:3] == ["git", "diff", "--cached"] or command[:3] == [
                "git", "ls-files", "--stage"
            ]:
                raise AssertionError("mutable index enumeration was attempted")
            return real_run(command, **kwargs)

        with mock.patch(
            "control_center.run_command", side_effect=reject_mutable_index_queries
        ):
            with self.assertRaisesRegex(ControlCenterError, "possible secrets"):
                self.app.projects._reject_staged_secrets(repo)

    def test_history_secret_scan_rejects_add_then_delete_blob(self):
        repo = self.root / "history-secret"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
        (repo / "index.html").write_text("safe\n", encoding="utf-8")
        subprocess.run(["git", "add", "index.html"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
        base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo,
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        (repo / "component.tsx").write_text(
            "github_pat_{}\n".format("R" * 40), encoding="utf-8"
        )
        subprocess.run(["git", "add", "component.tsx"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "add transient"], cwd=repo, check=True, capture_output=True)
        (repo / "component.tsx").unlink()
        subprocess.run(["git", "add", "-u"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "delete transient"], cwd=repo, check=True, capture_output=True)
        tip_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo,
            text=True, capture_output=True, check=True,
        ).stdout.strip()

        with self.assertRaisesRegex(ControlCenterError, "possible secrets"):
            self.app.projects._reject_history_secrets(
                repo, base_sha, tip_sha, "Outgoing commit history"
            )

    def test_history_secret_scan_fails_closed_at_commit_bound(self):
        repo = self.root / "bounded-history"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
        (repo / "notes.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "notes.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
        base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo,
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        for value in ("one", "two"):
            (repo / "notes.txt").write_text(value + "\n", encoding="utf-8")
            subprocess.run(["git", "add", "notes.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", value], cwd=repo, check=True, capture_output=True)
        tip_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo,
            text=True, capture_output=True, check=True,
        ).stdout.strip()

        with mock.patch("control_center.MAX_HISTORY_SECRET_SCAN_COMMITS", 1):
            with self.assertRaisesRegex(ControlCenterError, "too many commits"):
                self.app.projects._reject_history_secrets(repo, base_sha, tip_sha)

    def test_owned_worktree_partial_add_cleans_only_its_cas_branch(self):
        repo = self.root / "partial-worktree-repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
        (repo / "index.html").write_text("safe\n", encoding="utf-8")
        subprocess.run(["git", "add", "index.html"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
        start_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo,
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        worktree = self.root / "partial-worktree"
        branch = "webkit/blue/partial"
        real_run = control_center_module.run_command

        def fail_worktree_add(command, **kwargs):
            if command[:3] == ["git", "worktree", "add"]:
                raise ControlCenterError("injected worktree failure", 409)
            return real_run(command, **kwargs)

        with mock.patch("control_center.run_command", side_effect=fail_worktree_add):
            with self.assertRaisesRegex(ControlCenterError, "injected"):
                self.app.projects._create_owned_worktree(
                    repo, worktree, branch, start_sha
                )
        self.assertFalse(worktree.exists())
        self.assertNotEqual(
            subprocess.run(
                ["git", "show-ref", "--verify", "--quiet", "refs/heads/{}".format(branch)],
                cwd=repo,
            ).returncode,
            0,
        )

    def test_owned_worktree_cas_race_preserves_concurrent_branch(self):
        repo = self.root / "worktree-race-repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
        (repo / "index.html").write_text("safe\n", encoding="utf-8")
        subprocess.run(["git", "add", "index.html"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
        start_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo,
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        branch = "webkit/blue/concurrent"
        branch_ref = "refs/heads/{}".format(branch)
        real_run = control_center_module.run_command
        raced = []

        def create_concurrent_branch(command, **kwargs):
            if command[:3] == ["git", "update-ref", branch_ref] and not raced:
                raced.append(True)
                subprocess.run(
                    ["git", "update-ref", branch_ref, start_sha], cwd=repo, check=True
                )
            return real_run(command, **kwargs)

        with mock.patch("control_center.run_command", side_effect=create_concurrent_branch):
            with self.assertRaisesRegex(ControlCenterError, "concurrently"):
                self.app.projects._create_owned_worktree(
                    repo, self.root / "never-created", branch, start_sha
                )
        self.assertEqual(
            subprocess.run(
                ["git", "rev-parse", branch_ref], cwd=repo,
                text=True, capture_output=True, check=True,
            ).stdout.strip(),
            start_sha,
        )

    def test_git_install_action_explains_required_upgrade(self):
        result = subprocess.CompletedProcess(
            ["git", "--version"], 0, "git version 2.29.9\n", ""
        )
        with mock.patch("control_center.shutil.which", return_value="/old/git"), mock.patch(
            "control_center.run_command", return_value=result
        ), mock.patch("control_center.platform.system", return_value="Darwin"):
            action = self.app.projects.git_install_action()
        self.assertFalse(action["started"])
        self.assertIn("2.30 or newer", action["message"])
        self.assertIn("Update", action["message"])

    def test_verified_push_uses_exact_remote_lease_and_requires_fast_forward(self):
        destination_url = "https://github.com/example/site.git"
        destination = {
            "remote": "origin", "url": destination_url,
            "fetchUrl": destination_url, "pushUrl": destination_url,
            "repository": "example/site",
        }
        local_sha = "a" * 40
        remote_sha = "b" * 40
        calls = []

        def reject_push(command, **_kwargs):
            calls.append(command)
            if command[:4] == ["git", "merge-base", "--is-ancestor", remote_sha]:
                return subprocess.CompletedProcess(command, 0, "", "")
            if command[:2] == ["git", "push"]:
                return subprocess.CompletedProcess(command, 1, "", "rejected")
            raise AssertionError(command)

        with mock.patch.object(
            control_center_module.ProjectManager,
            "_validated_github_remote", return_value=destination
        ), mock.patch("control_center.run_command", side_effect=reject_push):
            result = self.app.projects._verified_push(
                self.root, "origin", "main", "main",
                validated_sha=local_sha,
                validated_push_url=destination_url,
                validated_remote_sha=remote_sha,
            )
        self.assertFalse(result["pushed"])
        self.assertIn(
            "--force-with-lease=refs/heads/main:{}".format(remote_sha),
            next(command for command in calls if command[:2] == ["git", "push"]),
        )

        with mock.patch.object(
            control_center_module.ProjectManager,
            "_validated_github_remote", return_value=destination
        ), mock.patch(
            "control_center.run_command",
            return_value=subprocess.CompletedProcess(["git"], 1, "", ""),
        ) as run:
            non_fast_forward = self.app.projects._verified_push(
                self.root, "origin", "main", "main",
                validated_sha=local_sha,
                validated_push_url=destination_url,
                validated_remote_sha=remote_sha,
            )
        self.assertFalse(non_fast_forward["pushed"])
        self.assertIn("not a fast-forward", non_fast_forward["error"])
        self.assertFalse(
            any(call[0][0][:2] == ["git", "push"] for call in run.call_args_list)
        )

    def test_changed_owned_worktree_is_preserved_without_force(self):
        repo = self.root / "preserved-worktree-repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
        (repo / "index.html").write_text("safe\n", encoding="utf-8")
        subprocess.run(["git", "add", "index.html"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
        start_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo,
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        worktree = self.root / "preserved-worktree"
        branch = "webkit/red/preserve"
        ownership = self.app.projects._create_owned_worktree(
            repo, worktree, branch, start_sha
        )
        (worktree / "keep-me.txt").write_text("user work\n", encoding="utf-8")

        removed, reason = self.app.projects._remove_owned_worktree(
            repo, worktree, branch, start_sha,
            (ownership["dev"], ownership["ino"]),
        )
        self.assertFalse(removed)
        self.assertIn("changed", reason)
        self.assertEqual((worktree / "keep-me.txt").read_text(), "user work\n")
        self.assertEqual(
            subprocess.run(
                ["git", "rev-parse", "refs/heads/{}".format(branch)], cwd=repo,
                text=True, capture_output=True, check=True,
            ).stdout.strip(),
            start_sha,
        )

    def test_discard_rejects_a_replaced_worktree_path(self):
        repo, worktree, branch, _base_sha, session_id = self._make_discard_session(
            "replaced-path"
        )
        moved_owned_worktree = self.root / "moved-owned-discard-worktree"
        worktree.rename(moved_owned_worktree)
        worktree.mkdir()
        sentinel = worktree / "unrelated-user-data.txt"
        sentinel.write_text("preserve me\n", encoding="utf-8")

        with self.assertRaisesRegex(ControlCenterError, "recorded identity"):
            self.app.sessions.discard(session_id, session_id)

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve me\n")
        self.assertEqual(
            subprocess.run(
                ["git", "rev-parse", "refs/heads/{}".format(branch)], cwd=repo,
                text=True, capture_output=True, check=True,
            ).stdout.strip(),
            subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo,
                text=True, capture_output=True, check=True,
            ).stdout.strip(),
        )

    def test_discard_rejects_a_worktree_retargeted_to_another_branch(self):
        repo, worktree, branch, _base_sha, session_id = self._make_discard_session(
            "retargeted"
        )
        subprocess.run(
            ["git", "switch", "-c", "user/retargeted"], cwd=worktree,
            check=True, capture_output=True,
        )

        with self.assertRaisesRegex(ControlCenterError, "branch|worktree"):
            self.app.sessions.discard(session_id, session_id)

        self.assertTrue(worktree.is_dir())
        self.assertEqual(
            subprocess.run(
                ["git", "rev-parse", "refs/heads/{}".format(branch)], cwd=repo,
                text=True, capture_output=True, check=True,
            ).returncode,
            0,
        )

    def test_discard_rejects_a_branch_associated_with_another_worktree(self):
        repo, original, branch, base_sha, session_id = self._make_discard_session(
            "associated-elsewhere"
        )
        other = self.root / "different-discard-worktree"
        other_branch = "webkit/blue/different-discard-worktree"
        other_ownership = self.app.projects._create_owned_worktree(
            repo, other, other_branch, base_sha
        )

        def replace_recorded_worktree(state):
            for session in state.get("sessions", []):
                if session.get("id") == session_id:
                    session["worktree"] = str(other)
                    session["worktreeDev"] = other_ownership["dev"]
                    session["worktreeIno"] = other_ownership["ino"]

        self.app.store.update(replace_recorded_worktree)
        with self.assertRaisesRegex(ControlCenterError, "another worktree"):
            self.app.sessions.discard(session_id, session_id)

        self.assertTrue(original.is_dir())
        self.assertTrue(other.is_dir())
        self.assertEqual(
            subprocess.run(
                ["git", "rev-parse", "refs/heads/{}".format(branch)], cwd=repo,
                text=True, capture_output=True, check=True,
            ).returncode,
            0,
        )

    def test_discard_removes_the_exact_dirty_owned_worktree_and_branch(self):
        repo, worktree, branch, _base_sha, session_id = self._make_discard_session(
            "dirty-owned"
        )
        (worktree / "index.html").write_text("dirty tracked work\n", encoding="utf-8")
        (worktree / "untracked.txt").write_text("dirty untracked work\n", encoding="utf-8")

        result = self.app.sessions.discard(session_id, session_id)

        self.assertEqual(result, {"discarded": True})
        self.assertFalse(worktree.exists())
        self.assertNotEqual(
            subprocess.run(
                ["git", "show-ref", "--verify", "--quiet", "refs/heads/{}".format(branch)],
                cwd=repo,
            ).returncode,
            0,
        )
        self.assertEqual(
            self.app.sessions._get_session(session_id)["status"], "discarded"
        )

    def test_multi_remote_add_existing_retains_selected_upstream(self):
        repo = self.root / "multi-remote-source"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
        (repo / "index.html").write_text("safe\n", encoding="utf-8")
        subprocess.run(["git", "add", "index.html"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo,
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        for remote in ("origin", "backup"):
            subprocess.run(
                ["git", "remote", "add", remote, "git@github.com:example/{}.git".format(remote)],
                cwd=repo, check=True,
            )
            subprocess.run(
                ["git", "update-ref", "refs/remotes/{}/main".format(remote), head],
                cwd=repo, check=True,
            )
        subprocess.run(
            ["git", "branch", "--set-upstream-to=backup/main", "main"],
            cwd=repo, check=True, capture_output=True,
        )

        with mock.patch.object(self.app.projects, "_find_port_block", return_value=6551):
            project = self.app.projects.add_existing(str(repo), "codex")
        managed = Path(project["path"])
        self.assertEqual(
            subprocess.run(
                ["git", "config", "--get", "branch.{}.remote".format(project["baseBranch"])],
                cwd=managed, text=True, capture_output=True, check=True,
            ).stdout.strip(),
            "backup",
        )

    def test_detached_session_is_rejected_before_controller_fast_forward(self):
        parent = self.root / "detached-projects"
        parent.mkdir()
        with mock.patch.object(self.app.projects, "_find_port_block", return_value=6556):
            project = self.app.projects.create_project("Detached Session", str(parent), "codex")
        project_path = Path(project["path"])
        base_sha = subprocess.run(
            ["git", "rev-parse", "main"], cwd=project_path,
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        branch = "webkit/blue/detached"
        worktree = self.root / "detached-session"
        ownership = self.app.projects._create_owned_worktree(
            project_path, worktree, branch, base_sha
        )
        (worktree / "index.html").write_text("detached change\n", encoding="utf-8")
        subprocess.run(["git", "add", "index.html"], cwd=worktree, check=True)
        subprocess.run(["git", "commit", "-m", "session change"], cwd=worktree, check=True, capture_output=True)
        session_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=worktree,
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        session = {
            "id": "detached-session", "projectId": project["id"],
            "projectName": project["name"], "provider": "codex", "color": "blue",
            "emoji": "blue", "port": 6556, "branch": branch,
            "worktree": str(worktree), "previewUrl": "http://127.0.0.1:6556/",
            "feedbackDir": ".webkit/feedback", "status": "active",
            "threadId": None, "hasRun": False, "baseSha": base_sha,
            "worktreeDev": ownership["dev"], "worktreeIno": ownership["ino"],
            "createdAt": "2026-08-19T00:00:00Z",
        }
        self.app.store.update(lambda state: state.setdefault("sessions", []).append(session))
        runtime = mock.Mock()
        runtime.session = session
        self.app.sessions.runtimes[session["id"]] = runtime
        self.app.sessions.merge(session["id"])
        marker = worktree / ".webkit" / "control-center-merge.json"
        marker.parent.mkdir(exist_ok=True)
        marker.write_text('{"status":"ready","message":"ready"}', encoding="utf-8")
        subprocess.run(["git", "checkout", "--detach", session_sha], cwd=worktree, check=True, capture_output=True)

        with self.assertRaisesRegex(ControlCenterError, "detached"):
            self.app.sessions._complete_agent_merge(session["id"])
        self.assertEqual(
            subprocess.run(
                ["git", "rev-parse", "main"], cwd=project_path,
                text=True, capture_output=True, check=True,
            ).stdout.strip(),
            base_sha,
        )
        self.assertTrue(worktree.exists())

    @unittest.skipUnless(os.name == "posix", "executable Git hook required")
    def test_managed_target_rejects_post_merge_hook_head_change(self):
        repo = self.root / "hooked-source"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
        (repo / "index.html").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "index.html"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
        with mock.patch.object(self.app.projects, "_find_port_block", return_value=6561):
            project = self.app.projects.add_existing(str(repo), "codex")
        managed = Path(project["path"])
        (managed / "index.html").write_text("validated\n", encoding="utf-8")
        subprocess.run(["git", "add", "index.html"], cwd=managed, check=True)
        subprocess.run(["git", "commit", "-m", "validated"], cwd=managed, check=True, capture_output=True)
        validated_sha = subprocess.run(
            ["git", "rev-parse", project["baseBranch"]], cwd=managed,
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        hook_result = subprocess.run(
            ["git", "rev-parse", "--git-path", "hooks/post-merge"], cwd=repo,
            text=True, capture_output=True, check=True,
        )
        hook = Path(hook_result.stdout.strip())
        if not hook.is_absolute():
            hook = repo / hook
        hook.write_text(
            "#!/bin/sh\ngit commit --allow-empty -m hook-tamper >/dev/null 2>&1\n",
            encoding="utf-8",
        )
        hook.chmod(0o755)

        with self.assertRaisesRegex(ControlCenterError, "changed during integration"):
            self.app.projects.integrate_managed_target(
                project, validated_sha=validated_sha
            )
        source_sha = subprocess.run(
            ["git", "rev-parse", "main"], cwd=repo,
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        self.assertNotEqual(source_sha, validated_sha)
        self.assertEqual((repo / "index.html").read_text(), "validated\n")

    def test_sync_validation_ref_is_removed_when_validation_raises(self):
        parent = self.root / "sync-ref-projects"
        parent.mkdir()
        with mock.patch.object(self.app.projects, "_find_port_block", return_value=6566), mock.patch.object(
            self.app.projects, "_ensure_github_repo"
        ):
            project = self.app.projects.create_project("Sync Ref", str(parent), "codex")
        project_path = Path(project["path"])
        remote = self.root / "sync-ref-remote.git"
        subprocess.run(
            ["git", "init", "--bare", "--initial-branch=main", str(remote)],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "push", str(remote), "main:main"], cwd=project_path,
            check=True, capture_output=True,
        )
        status = {
            "connected": True, "remote": "origin", "url": str(remote),
            "fetchUrl": str(remote), "pushUrl": str(remote),
        }
        with mock.patch.object(
            self.app.projects, "github_status", return_value=status
        ), mock.patch.object(
            self.app.projects, "_require_remote_tree_private",
            side_effect=ControlCenterError("injected validation failure", 409),
        ):
            with self.assertRaisesRegex(ControlCenterError, "injected"):
                self.app.projects.sync_from_github(project)
        refs = subprocess.run(
            ["git", "for-each-ref", "--format=%(refname)", "refs/awesome-webkit/sync/"],
            cwd=project_path, text=True, capture_output=True, check=True,
        ).stdout.strip()
        self.assertEqual(refs, "")

    def test_detached_seed_session_is_rejected_before_controller_fast_forward(self):
        parent = self.root / "detached-seed-projects"
        parent.mkdir()
        with mock.patch.object(self.app.projects, "_find_port_block", return_value=6571):
            project = self.app.projects.create_project("Detached Seed", str(parent), "codex")
        project_path = Path(project["path"])
        base_sha = subprocess.run(
            ["git", "rev-parse", "main"], cwd=project_path,
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        branch = "webkit/red/detached-seed"
        worktree = self.root / "detached-seed-session"
        ownership = self.app.projects._create_owned_worktree(
            project_path, worktree, branch, base_sha
        )
        seed_page = worktree / "seed-directions" / "seed-01" / "index.html"
        seed_page.parent.mkdir(parents=True)
        seed_page.write_text("seed\n", encoding="utf-8")
        subprocess.run(["git", "add", "seed-directions"], cwd=worktree, check=True)
        subprocess.run(["git", "commit", "-m", "seed"], cwd=worktree, check=True, capture_output=True)
        (worktree / "index.html").write_text("final seed\n", encoding="utf-8")
        shutil.rmtree(worktree / "seed-directions")
        subprocess.run(["git", "add", "-A"], cwd=worktree, check=True)
        subprocess.run(["git", "commit", "-m", "final seed"], cwd=worktree, check=True, capture_output=True)
        session_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=worktree,
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "checkout", "--detach", session_sha], cwd=worktree,
            check=True, capture_output=True,
        )
        marker = worktree / ".webkit" / "seed-selection.json"
        marker.parent.mkdir(exist_ok=True)
        marker.write_text('{"status":"ready","message":"ready"}', encoding="utf-8")
        session = {
            "id": "detached-seed", "projectId": project["id"],
            "projectName": project["name"], "provider": "codex", "color": "red",
            "emoji": "red", "port": 6571, "branch": branch,
            "worktree": str(worktree), "previewUrl": "http://127.0.0.1:6571/",
            "feedbackDir": ".webkit/feedback", "status": "merging",
            "threadId": None, "hasRun": True, "kind": "seeds",
            "seedCount": 1, "seedStage": "finalizing", "baseSha": base_sha,
            "worktreeDev": ownership["dev"], "worktreeIno": ownership["ino"],
            "createdAt": "2026-08-19T00:00:00Z",
        }
        self.app.store.update(lambda state: (
            state.setdefault("sessions", []).append(session),
            next(item for item in state["projects"] if item["id"] == project["id"]).update({
                "onboarding": {"status": "finalizing", "sessionId": session["id"]}
            }),
        ))

        with self.assertRaisesRegex(ControlCenterError, "detached"):
            self.app.sessions._complete_seed_onboarding(session["id"])
        self.assertEqual(
            subprocess.run(
                ["git", "rev-parse", "main"], cwd=project_path,
                text=True, capture_output=True, check=True,
            ).stdout.strip(),
            base_sha,
        )
        self.assertTrue(worktree.exists())

    def test_static_ui_contains_preview_and_accessibility_boundaries(self):
        html = (CONTROL_DIR / "static" / "index.html").read_text(encoding="utf-8")
        script = (CONTROL_DIR / "static" / "app.js").read_text(encoding="utf-8")
        styles = (CONTROL_DIR / "static" / "styles.css").read_text(encoding="utf-8")
        self.assertIn('sandbox="allow-scripts allow-same-origin"', html)
        self.assertIn('aria-label="Message the coding agent"', html)
        self.assertIn('role="tabpanel"', html)
        self.assertIn("tab.opener = null", script)
        self.assertIn('"X-WKCC-Token": CONTROL_CENTER_TOKEN', script)
        self.assertIn("window.sessionStorage", script)
        self.assertNotIn("document.cookie", script)
        self.assertIn("busy-preserved-content", script)
        self.assertIn("Project added in an isolated checkout", script)
        self.assertIn("Local checkout sync pending", script)
        self.assertNotIn("button.textContent = label", script)
        self.assertNotIn(".project-rail { display: none; }", styles)
        self.assertIn("@media (prefers-reduced-motion: reduce)", styles)
        self.assertIn("animation-iteration-count: 1 !important", styles)
        self.assertNotIn("COLORS.forEach", script)


if __name__ == "__main__":
    unittest.main()
