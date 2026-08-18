import io
import json
import base64
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


CONTROL_DIR = Path(__file__).resolve().parents[1]
KIT_ROOT = CONTROL_DIR.parent
sys.path.insert(0, str(CONTROL_DIR))

from control_center import (  # noqa: E402
    ControlCenter,
    EventLog,
    ProviderRunner,
    SessionManager,
    choose_folder,
    slugify,
)


class FakeProcess:
    def __init__(self, stdout_lines, code=0, stderr_lines=None):
        self.stdout = io.StringIO("".join(stdout_lines))
        self.stderr = io.StringIO("".join(stderr_lines or []))
        self._code = code

    def wait(self):
        return self._code

    def poll(self):
        return self._code


class ControlCenterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state_dir = self.root / "state"
        self.app = ControlCenter(KIT_ROOT, self.state_dir)
        self.app.store.update(lambda state: state.update({"providers": ["codex", "claude"]}))

    def tearDown(self):
        self.app.sessions.shutdown()
        self.temp.cleanup()

    def test_slugify(self):
        self.assertEqual(slugify("My Cool Site!"), "my-cool-site")
        self.assertEqual(slugify("***"), "website")

    def test_macos_folder_picker_returns_selected_absolute_path(self):
        selected = self.root / "chosen"
        selected.mkdir()
        completed = mock.Mock(returncode=0, stdout=str(selected) + "/\n", stderr="")
        with mock.patch("control_center.platform.system", return_value="Darwin"), mock.patch(
            "control_center.subprocess.run", return_value=completed
        ) as run:
            self.assertEqual(choose_folder(str(self.root), "Choose a project"), str(selected.resolve()))
        command = run.call_args.args[0]
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
        self.assertEqual((project_path / "webkit" / "VERSION").read_text().strip(), "0.8.0")
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

    def test_new_project_onboarding_saves_optional_context_and_starts_seeds(self):
        parent = self.root / "projects"
        parent.mkdir()
        fake_session = {"id": "seed-session", "kind": "seeds", "status": "busy"}
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
        self.assertEqual(result["seedSession"], fake_session)
        start.assert_called_once_with(result["project"]["id"], 7, onboarding["brief"])

    def test_project_reference_folder_cannot_escape_context_directory(self):
        payload = base64.b64encode(b"private").decode("ascii")
        with self.assertRaisesRegex(Exception, "unsafe folder path"):
            self.app.projects._save_project_context(self.root, {
                "assets": [{"name": "secret.txt", "path": "../secret.txt", "data": payload}]
            })
        self.assertFalse((self.root.parent / "secret.txt").exists())

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
        subprocess.run(["git", "add", "seed-directions"], cwd=worktree, check=True)
        subprocess.run(
            ["git", "commit", "-m", "Generate design seeds"], cwd=worktree,
            check=True, capture_output=True,
        )
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
        runtime = mock.Mock()
        runtime.session = session
        self.app.sessions.runtimes[session["id"]] = runtime
        result = self.app.sessions.choose_seeds(
            session["id"], ["seed-01", "seed-02"], "Editorial type with kinetic navigation"
        )
        self.assertTrue(result["queued"])
        prompt = runtime.enqueue.call_args.args[0]
        self.assertIn("Editorial type with kinetic navigation", prompt)
        self.assertIn("seed-01", prompt)
        self.assertIn("seed-02", prompt)
        self.assertEqual(self.app.projects.get_project(project["id"])["onboarding"]["status"], "finalizing")

    def test_add_existing_initializes_git_and_claude_entrypoint(self):
        project_path = self.root / "legacy-site"
        project_path.mkdir()
        (project_path / "index.html").write_text("<title>Legacy</title>", encoding="utf-8")
        with mock.patch.object(self.app.projects, "_find_port_block", return_value=6321):
            project = self.app.projects.add_existing(str(project_path), "claude")
        self.assertEqual(project["provider"], "claude")
        self.assertTrue((project_path / ".git").is_dir())
        self.assertTrue((project_path / "CLAUDE.md").exists())
        self.assertTrue((project_path / ".claude" / "skills" / "abc" / "SKILL.md").exists())

    def test_add_existing_old_kit_gets_controller_files_without_overwrite(self):
        project_path = self.root / "old-kit"
        (project_path / "webkit").mkdir(parents=True)
        (project_path / "index.html").write_text("<title>Old</title>", encoding="utf-8")
        (project_path / "webkit" / "SETUP.md").write_text("local setup\n", encoding="utf-8")
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
        connected = {"connected": True, "remote": "origin", "url": "git@github.com:example/site.git"}
        with mock.patch.object(self.app.projects, "github_status", return_value=connected):
            status = self.app.projects.github_sync_status(project)
            self.assertTrue(status["unpushed"])
            self.assertEqual(status["ahead"], 1)
            result = self.app.projects.push_project(project["id"])
            self.assertTrue(result["pushed"])
            self.assertFalse(result["github"]["unpushed"])

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
        config = {"lock_dir": str(lock_dir)}
        SessionManager._claim_lock(config, "red", owner)
        self.assertEqual((lock_dir / "red.lock" / "owner").read_text().strip(), str(owner.resolve()))
        with self.assertRaises(Exception):
            SessionManager._claim_lock(config, "red", self.root / "other")

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

    def test_voice_note_setting_rejects_unknown_mode(self):
        with self.assertRaisesRegex(Exception, "browser speech or agent voice notes"):
            self.app.save_settings({"dictationMode": "cosmetic-only"})

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
        })

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
        session = {
            "id": "merge-session", "projectId": project["id"], "projectName": project["name"],
            "provider": "codex", "color": "blue", "emoji": "🔵", "port": 6341,
            "branch": branch, "worktree": str(worktree), "previewUrl": "http://127.0.0.1:6341/",
            "feedbackDir": ".webkit/feedback", "status": "active", "threadId": None,
            "hasRun": False, "createdAt": "2026-08-18T00:00:00Z",
        }
        self.app.store.update(lambda state: state.setdefault("sessions", []).append(session))
        runtime = mock.Mock()
        runtime.session = session
        self.app.sessions.runtimes["merge-session"] = runtime
        result = self.app.sessions.merge("merge-session")
        self.assertTrue(result["queued"])
        self.assertTrue(result["agentManaged"])
        runtime.enqueue.assert_called_once()
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
        }
        event_log = EventLog(self.state_dir, "session1")
        threads = []
        processes = []
        runner = ProviderRunner(session, event_log, threads.append, processes.append)
        fake = FakeProcess([
            '{"type":"thread.started","thread_id":"thread-123"}\n',
            '{"type":"item.completed","item":{"type":"agent_message","text":"Done"}}\n',
        ])
        with mock.patch("control_center.shutil.which", return_value="/fake/codex"), mock.patch("control_center.subprocess.Popen", return_value=fake) as popen:
            runner.run("Make it blue")
        command = popen.call_args.args[0]
        self.assertEqual(command[:3], ["/fake/codex", "exec", "--json"])
        self.assertIn("workspace-write", command)
        self.assertEqual(threads, ["thread-123"])
        self.assertEqual(event_log.read_after(0)["events"][0]["text"], "Done")

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
        ):
            runner.run("Check")
        texts = [event["text"] for event in event_log.read_after(0)["events"]]
        self.assertEqual(texts, ["Ready"])

    def test_claude_initial_and_resume_flags_do_not_conflict(self):
        session = {
            "id": "session2", "provider": "claude", "worktree": str(self.root),
            "color": "green", "threadId": None, "hasRun": False,
        }
        event_log = EventLog(self.state_dir, "session2")
        runner = ProviderRunner(session, event_log, lambda value: None, lambda value: None)
        fake = FakeProcess(['{"type":"result","is_error":false,"result":"Done"}\n'])
        with mock.patch("control_center.shutil.which", return_value="/fake/claude"), mock.patch("control_center.subprocess.Popen", return_value=fake) as popen:
            runner.run("Build it")
        first = popen.call_args.args[0]
        self.assertIn("--session-id", first)
        self.assertNotIn("--resume", first)
        session["hasRun"] = True
        with mock.patch("control_center.shutil.which", return_value="/fake/claude"), mock.patch("control_center.subprocess.Popen", return_value=fake) as popen:
            runner.run("Continue")
        resumed = popen.call_args.args[0]
        self.assertIn("--resume", resumed)
        self.assertNotIn("--session-id", resumed)


if __name__ == "__main__":
    unittest.main()
