import io
import json
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

    def test_create_project_installs_kit_and_initial_commit(self):
        parent = self.root / "projects"
        parent.mkdir()
        with mock.patch.object(self.app.projects, "_find_port_block", return_value=6311):
            project = self.app.projects.create_project("Demo Site", str(parent), "codex")
        project_path = Path(project["path"])
        self.assertTrue((project_path / "index.html").exists())
        self.assertTrue((project_path / "webkit" / "CONTROL-CENTER.md").exists())
        self.assertEqual((project_path / "webkit" / "VERSION").read_text().strip(), "0.6.1")
        self.assertIn("WK_CONTROL_CENTER=1", (project_path / "AGENTS.md").read_text())
        config = json.loads((project_path / "webkit" / "webkit.config.json").read_text())
        self.assertEqual(config["project_name"], "demo-site")
        self.assertEqual(config["default_page"], "index.html")
        self.assertEqual(config["dictation"]["mode"], "speech")
        self.assertEqual(config["interaction"]["mode"], "browse-default")
        log = subprocess.run(["git", "log", "-1", "--pretty=%s"], cwd=project_path, text=True, capture_output=True, check=True)
        self.assertEqual(log.stdout.strip(), "Create website with AWESOME WEBKIT")
        self.assertFalse(subprocess.run(["git", "status", "--porcelain"], cwd=project_path, text=True, capture_output=True, check=True).stdout)

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
            self.app.projects.add_existing(str(project_path), "codex")
        self.assertEqual((project_path / "webkit" / "SETUP.md").read_text(), "local setup\n")
        self.assertTrue((project_path / "webkit" / "CONTROL-CENTER.md").exists())
        agents = (project_path / "AGENTS.md").read_text()
        self.assertIn("Older pointer.", agents)
        self.assertIn("## Webkit Control Center", agents)

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
            })
        self.assertEqual(result["settings"]["dictationMode"], "voice-note")
        self.assertEqual(result["settings"]["interactionMode"], "draw-default")
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
        })

    def test_voice_transcription_status_requires_local_engine(self):
        with mock.patch("control_center.shutil.which", return_value=None):
            status = self.app.projects._voice_engine_status()
        self.assertFalse(status["available"])
        self.assertIn("local Whisper", status["help"])

    def test_merge_moves_committed_color_work_to_main_and_closes_worktree(self):
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
        result = self.app.sessions.merge("merge-session")
        self.assertTrue(result["merged"])
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
