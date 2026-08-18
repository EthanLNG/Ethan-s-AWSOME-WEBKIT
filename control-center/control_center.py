#!/usr/bin/env python3
"""Core services for the AWESOME WEBKIT local control center.

The module intentionally uses only the Python standard library.  It owns the
filesystem and process boundary; the browser UI only calls the narrow methods
exposed by ``ControlCenter`` through ``server.py``.
"""

import json
import os
import platform
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


WEBKIT_POINTER = """## Webkit

This project uses Ethan's AWESOME WEBKIT for design iteration. At the start of
every website-working session, and whenever the user asks to launch, open,
preview, show, or test the website in a browser, read and follow
`webkit/SETUP.md` exactly. Use its claimed color, stamping preview server, and
`webkit/scripts/open-preview.sh`; never substitute a generic HTTP server, a
manually opened browser tab, the Codex/ChatGPT in-app browser, a Claude preview
pane, an IDE webview, or another embedded browser. The configured external
desktop browser is the only preview surface unless the user explicitly
overrides it. If the Webkit session is already active, reuse it instead of
claiming again. Read `webkit/LOOP.md` for browser feedback rounds.

"""


CONTROL_CENTER_POINTER = """## Webkit Control Center

When `WK_CONTROL_CENTER=1`, the local Control Center already owns the color,
worktree, preview process, browser tab, and waiting. Read
`webkit/CONTROL-CENTER.md`, process one finite feedback or chat transition, and
exit; never merge or discard the controller-owned branch yourself.
"""


CONTROL_CENTER_PROMPT = """You are the {emoji} ({color}) AWESOME WEBKIT background agent.
You are already running inside this color's isolated Git worktree. The local
Control Center owns the preview process, color lock, browser tab, and waiting.

Read webkit/CONTROL-CENTER.md and webkit/LOOP.md, then process exactly the
current Webkit state transition in .webkit/feedback/{color}/. If feedback.json
is awaiting the agent, apply the points, commit one point at a time, publish an
atomic review.json, and then exit. If verdicts.json is present, process the
verdicts, archive the round correctly, and then exit. Do not run wait-for-file,
do not merge to main, do not discard the worktree, and do not open another
agent app. The controller will invoke you again for the next transition.
"""


DEFAULT_SETTINGS = {
    "dictationMode": "speech",
    "interactionMode": "browse-default",
    "toggleHotkey": "KeyC",
    "dictateHotkey": "KeyV",
}

HOTKEY_CODE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,31}$")
MODIFIER_HOTKEYS = {
    "AltLeft", "AltRight", "ControlLeft", "ControlRight",
    "MetaLeft", "MetaRight", "ShiftLeft", "ShiftRight", "Escape",
}


def valid_hotkey(value):
    return (
        isinstance(value, str)
        and HOTKEY_CODE.fullmatch(value) is not None
        and value not in MODIFIER_HOTKEYS
    )


def normalized_settings(value):
    value = value if isinstance(value, dict) else {}
    dictation_mode = value.get("dictationMode", DEFAULT_SETTINGS["dictationMode"])
    interaction_mode = value.get("interactionMode", DEFAULT_SETTINGS["interactionMode"])
    toggle_hotkey = value.get("toggleHotkey", DEFAULT_SETTINGS["toggleHotkey"])
    dictate_hotkey = value.get("dictateHotkey", DEFAULT_SETTINGS["dictateHotkey"])
    toggle_hotkey = toggle_hotkey if valid_hotkey(toggle_hotkey) else "KeyC"
    dictate_hotkey = dictate_hotkey if valid_hotkey(dictate_hotkey) else "KeyV"
    if dictate_hotkey == toggle_hotkey:
        dictate_hotkey = "KeyC" if toggle_hotkey == "KeyV" else "KeyV"
    return {
        "dictationMode": dictation_mode if dictation_mode in ("speech", "voice-note") else "speech",
        "interactionMode": interaction_mode
        if interaction_mode in ("browse-default", "draw-default")
        else "browse-default",
        "toggleHotkey": toggle_hotkey,
        "dictateHotkey": dictate_hotkey,
    }


class ControlCenterError(RuntimeError):
    def __init__(self, message, status=400, details=None):
        super().__init__(message)
        self.status = status
        self.details = details or {}


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def slugify(value):
    value = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return value or "website"


def run_command(args, cwd=None, env=None, check=True, timeout=120):
    try:
        result = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise ControlCenterError("Required command is not installed: {}".format(args[0]), 409)
    except subprocess.TimeoutExpired:
        raise ControlCenterError("Command timed out: {}".format(" ".join(args)), 500)
    if check and result.returncode != 0:
        message = (result.stderr or result.stdout or "Command failed").strip()
        raise ControlCenterError(message, 409, {"command": args, "exitCode": result.returncode})
    return result


def atomic_write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(str(tmp), str(path))


def append_section(path, heading, section):
    path = Path(path)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if heading in existing:
        return False
    separator = "" if not existing else ("\n" if existing.endswith("\n") else "\n\n")
    path.write_text(existing + separator + section.rstrip() + "\n", encoding="utf-8")
    return True


class StateStore:
    def __init__(self, state_dir):
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.path = self.state_dir / "state.json"
        self.lock = threading.RLock()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            atomic_write_json(self.path, {
                "version": 1, "providers": [], "projects": [], "sessions": [],
                "settings": dict(DEFAULT_SETTINGS),
            })

    def read(self):
        with self.lock:
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except (ValueError, OSError) as exc:
                raise ControlCenterError("Control Center state is unreadable: {}".format(exc), 500)

    def update(self, mutator):
        with self.lock:
            state = self.read()
            result = mutator(state)
            atomic_write_json(self.path, state)
            return result


class EventLog:
    def __init__(self, state_dir, session_id):
        self.path = Path(state_dir) / "logs" / (session_id + ".jsonl")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    def append(self, role, text, kind="message", meta=None):
        event = {
            "time": utc_now(),
            "role": role,
            "kind": kind,
            "text": str(text),
        }
        if meta:
            event["meta"] = meta
        with self.lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event

    def read_after(self, after=0):
        if not self.path.exists():
            return {"events": [], "next": 0}
        lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
        events = []
        for index, line in enumerate(lines[after:], start=after):
            try:
                event = json.loads(line)
            except ValueError:
                event = {"time": utc_now(), "role": "system", "kind": "error", "text": line}
            event["index"] = index
            events.append(event)
        return {"events": events, "next": len(lines)}


class ProjectManager:
    def __init__(self, kit_root, store):
        self.kit_root = Path(kit_root).resolve()
        self.store = store

    def system_status(self):
        return {
            "git": self._tool_status("git", ["git", "--version"]),
            "codex": self._tool_status("codex", ["codex", "--version"]),
            "claude": self._tool_status("claude", ["claude", "--version"]),
            "platform": platform.system().lower(),
            "voiceTranscription": self._voice_engine_status(),
        }

    @staticmethod
    def _voice_engine_status():
        if shutil.which("whisper"):
            return {"available": True, "engine": "local Whisper"}
        if shutil.which("whisper-cli") and os.environ.get("WHISPER_MODEL"):
            return {"available": True, "engine": "local whisper.cpp"}
        return {
            "available": False,
            "engine": None,
            "help": "Install local Whisper before using agent voice notes.",
        }

    @staticmethod
    def _tool_status(name, command):
        executable = shutil.which(name)
        if not executable:
            return {"installed": False, "path": None, "version": None}
        result = run_command(command, check=False, timeout=15)
        version = (result.stdout or result.stderr).strip().splitlines()
        return {"installed": result.returncode == 0, "path": executable, "version": version[0] if version else None}

    def save_providers(self, providers):
        allowed = {"codex", "claude"}
        normalized = []
        for provider in providers or []:
            if provider in allowed and provider not in normalized:
                normalized.append(provider)
        if not normalized:
            raise ControlCenterError("Choose Codex, Claude Code, or both.")
        status = self.system_status()
        missing = [p for p in normalized if not status[p]["installed"]]
        if missing:
            raise ControlCenterError("Install the selected CLI first: {}".format(", ".join(missing)), 409)
        self.store.update(lambda state: state.update({"providers": normalized}))
        return normalized

    def list_projects(self):
        state = self.store.read()
        projects = []
        for project in state.get("projects", []):
            item = dict(project)
            item["exists"] = Path(project["path"]).is_dir()
            item["sessions"] = [
                s for s in state.get("sessions", [])
                if s.get("projectId") == project["id"] and s.get("status") in ("active", "busy", "error")
            ]
            projects.append(item)
        return projects

    def get_project(self, project_id):
        for project in self.store.read().get("projects", []):
            if project["id"] == project_id:
                return dict(project)
        raise ControlCenterError("Unknown project.", 404)

    def create_project(self, name, parent, provider):
        self._validate_provider(provider)
        self._require_git()
        if not (name or "").strip():
            raise ControlCenterError("Enter a website name.")
        if not (parent or "").strip():
            raise ControlCenterError("Choose a parent folder for the new website.")
        safe_name = slugify(name)
        parent_path = Path(parent).expanduser().resolve()
        project_path = parent_path / safe_name
        if project_path.exists() and any(project_path.iterdir()):
            raise ControlCenterError("That project folder already exists and is not empty.", 409)
        project_path.mkdir(parents=True, exist_ok=True)
        self._git_init(project_path)
        self._ensure_git_identity(project_path)
        index = project_path / "index.html"
        if not index.exists():
            index.write_text(self._starter_html(name.strip() or safe_name), encoding="utf-8")
        self._install_kit(project_path, provider)
        run_command(["git", "add", "-A"], cwd=project_path)
        run_command(["git", "commit", "-m", "Create website with AWESOME WEBKIT"], cwd=project_path)
        return self._register(project_path, name.strip() or safe_name, provider)

    def add_existing(self, path, provider):
        self._validate_provider(provider)
        self._require_git()
        if not (path or "").strip():
            raise ControlCenterError("Choose an existing project folder.")
        project_path = Path(path).expanduser().resolve()
        if not project_path.is_dir():
            raise ControlCenterError("Project folder does not exist.", 404)
        git_root = self._git_root(project_path)
        if git_root and git_root != project_path:
            raise ControlCenterError(
                "Choose the Git repository root, not a folder inside it: {}".format(git_root), 409
            )
        was_git = git_root is not None
        if was_git:
            dirty = run_command(["git", "status", "--porcelain"], cwd=project_path).stdout.strip()
            if dirty:
                raise ControlCenterError("Commit or stash the project's existing changes before adding it.", 409)
        else:
            self._git_init(project_path)
        self._ensure_main_branch(project_path)
        self._ensure_git_identity(project_path)
        changed = self._install_kit(project_path, provider)
        if changed or not was_git:
            run_command(["git", "add", "-A"], cwd=project_path)
            staged = run_command(["git", "diff", "--cached", "--quiet"], cwd=project_path, check=False)
            if staged.returncode != 0:
                message = "Initialize website and AWESOME WEBKIT" if not was_git else "Install AWESOME WEBKIT"
                run_command(["git", "commit", "-m", message], cwd=project_path)
        return self._register(project_path, project_path.name, provider)

    def _register(self, project_path, name, provider):
        project_path = str(Path(project_path).resolve())

        def mutate(state):
            for project in state.get("projects", []):
                if project["path"] == project_path:
                    project["provider"] = provider
                    project["name"] = name
                    return dict(project)
            project = {
                "id": uuid.uuid4().hex[:12],
                "name": name,
                "slug": slugify(Path(project_path).name),
                "path": project_path,
                "provider": provider,
                "createdAt": utc_now(),
            }
            state.setdefault("projects", []).append(project)
            return dict(project)

        return self.store.update(mutate)

    def _validate_provider(self, provider):
        enabled = self.store.read().get("providers", [])
        if provider not in ("codex", "claude") or provider not in enabled:
            raise ControlCenterError("Choose one enabled provider for this project.")

    @staticmethod
    def _require_git():
        if not shutil.which("git"):
            raise ControlCenterError("Git is required. Use the Install Git button first.", 409)

    @staticmethod
    def _git_root(path):
        result = run_command(["git", "rev-parse", "--show-toplevel"], cwd=path, check=False)
        if result.returncode != 0:
            return None
        return Path(result.stdout.strip()).resolve()

    @staticmethod
    def _git_init(path):
        result = run_command(["git", "init", "-b", "main"], cwd=path, check=False)
        if result.returncode != 0:
            run_command(["git", "init"], cwd=path)
            run_command(["git", "branch", "-M", "main"], cwd=path)

    @staticmethod
    def _ensure_main_branch(path):
        current = run_command(["git", "branch", "--show-current"], cwd=path).stdout.strip()
        if current == "main":
            return
        has_main = run_command(["git", "show-ref", "--verify", "--quiet", "refs/heads/main"], cwd=path, check=False)
        if has_main.returncode == 0:
            raise ControlCenterError("Switch this repository to its main branch before adding it.", 409)
        run_command(["git", "branch", "-M", "main"], cwd=path)

    @staticmethod
    def _ensure_git_identity(path):
        name = run_command(["git", "config", "user.name"], cwd=path, check=False).stdout.strip()
        email = run_command(["git", "config", "user.email"], cwd=path, check=False).stdout.strip()
        if not name:
            fallback = os.environ.get("USER") or os.environ.get("USERNAME") or "AWESOME WEBKIT User"
            run_command(["git", "config", "user.name", fallback], cwd=path)
        if not email:
            fallback = slugify(os.environ.get("USER") or os.environ.get("USERNAME") or "webkit")
            run_command(["git", "config", "user.email", fallback + "@localhost"], cwd=path)

    def _install_kit(self, project_path, provider):
        changed = False
        target = project_path / "webkit"
        if not target.exists():
            shutil.copytree(
                str(self.kit_root / "webkit"),
                str(target),
                ignore=shutil.ignore_patterns("__pycache__", ".DS_Store", "webkit.config.json"),
            )
            changed = True
        control_center_source = self.kit_root / "webkit" / "CONTROL-CENTER.md"
        control_center_target = target / "CONTROL-CENTER.md"
        if not control_center_target.exists():
            shutil.copy2(str(control_center_source), str(control_center_target))
            changed = True
        config_path = target / "webkit.config.json"
        if not config_path.exists():
            config = self._make_config(project_path)
            atomic_write_json(config_path, config)
            changed = True
        changed = append_section(project_path / "AGENTS.md", "## Webkit", WEBKIT_POINTER) or changed
        changed = append_section(
            project_path / "AGENTS.md", "## Webkit Control Center", CONTROL_CENTER_POINTER
        ) or changed
        if provider == "claude":
            changed = append_section(project_path / "CLAUDE.md", "## Webkit", WEBKIT_POINTER) or changed
            changed = append_section(
                project_path / "CLAUDE.md", "## Webkit Control Center", CONTROL_CENTER_POINTER
            ) or changed
            changed = self._copy_claude_skills(project_path) or changed
        for entry in (".webkit/", "__pycache__/", ".DS_Store"):
            changed = self._append_gitignore(project_path / ".gitignore", entry) or changed
        return changed

    def _copy_claude_skills(self, project_path):
        changed = False
        target_root = project_path / ".claude" / "skills"
        source_root = self.kit_root / "webkit" / "skills"
        for source in source_root.iterdir():
            if not source.is_dir():
                continue
            target = target_root / source.name
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(str(source), str(target), ignore=shutil.ignore_patterns("__pycache__", ".DS_Store"))
            changed = True
        return changed

    @staticmethod
    def _append_gitignore(path, entry):
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        lines = existing.splitlines()
        if entry in lines:
            return False
        separator = "" if not existing or existing.endswith("\n") else "\n"
        path.write_text(existing + separator + entry + "\n", encoding="utf-8")
        return True

    def _make_config(self, project_path):
        default_page = self._find_default_page(project_path)
        port_start = self._find_port_block(5311)
        slug = slugify(project_path.name)
        settings = normalized_settings(self.store.read().get("settings"))
        palette = []
        colors = [
            ("blue", "🔵"), ("red", "🔴"), ("green", "🟢"),
            ("orange", "🟠"), ("purple", "🟣"),
        ]
        for index, (color_slug, emoji) in enumerate(colors):
            palette.append({"slug": color_slug, "emoji": emoji, "port": port_start + index})
        return {
            "project_name": slug,
            "site_root": ".",
            "default_page": default_page,
            "lock_dir": "/tmp/{}-agent-colors".format(slug),
            "grace_seconds": 180,
            "palette": palette,
            "browser": {"mode": "auto", "app_name": "Google Chrome"},
            "feedback_dir": ".webkit/feedback",
            "hotkeys": {
                "toggle": settings["toggleHotkey"],
                "dictate": settings["dictateHotkey"],
            },
            "dictation": {"mode": settings["dictationMode"]},
            "interaction": {"mode": settings["interactionMode"]},
        }

    @staticmethod
    def _find_default_page(project_path):
        root_index = project_path / "index.html"
        if root_index.exists():
            return "index.html"
        candidates = []
        ignored = {".git", "node_modules", ".webkit", ".claude"}
        for root, dirs, files in os.walk(str(project_path)):
            dirs[:] = [d for d in dirs if d not in ignored]
            rel_parts = Path(root).relative_to(project_path).parts
            if len(rel_parts) > 5:
                dirs[:] = []
                continue
            if "index.html" in files:
                rel = Path(root).relative_to(project_path) / "index.html"
                candidates.append(rel)
        if not candidates:
            raise ControlCenterError("No index.html was found in this website project.", 409)
        candidates.sort(key=lambda p: (len(p.parts), str(p)))
        return candidates[0].as_posix()

    @staticmethod
    def _find_port_block(start):
        candidate = start
        while candidate < 64000:
            sockets = []
            try:
                for port in range(candidate, candidate + 5):
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    try:
                        sock.bind(("127.0.0.1", port))
                    except OSError:
                        sock.close()
                        raise
                    sockets.append(sock)
                return candidate
            except OSError:
                candidate += 10
            finally:
                for sock in sockets:
                    sock.close()
        raise ControlCenterError("Could not find five free local preview ports.", 500)

    @staticmethod
    def _starter_html(name):
        title = name.replace("<", "&lt;").replace(">", "&gt;")
        return """<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>{0}</title>
  <style>
    :root {{ color-scheme: light; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; background: #f5f1e8; color: #171717; }}
    main {{ width: min(920px, calc(100% - 40px)); padding: 72px; border: 2px solid #171717; border-radius: 28px; background: #fffdf8; box-shadow: 12px 12px 0 #3294e2; }}
    p {{ max-width: 42rem; font-size: 1.2rem; line-height: 1.6; }}
    h1 {{ margin: 0 0 20px; font-size: clamp(3rem, 9vw, 7rem); line-height: .9; letter-spacing: -.06em; }}
  </style>
</head>
<body><main><h1>{0}</h1><p>Your new AWESOME WEBKIT website is ready. Open a color session and describe what you want to build.</p></main></body>
</html>
""".format(title)

    @staticmethod
    def git_install_action():
        system = platform.system().lower()
        if shutil.which("git"):
            return {"started": False, "message": "Git is already installed."}
        if system == "darwin":
            result = run_command(["xcode-select", "--install"], check=False, timeout=30)
            message = (result.stderr or result.stdout or "Apple's Git installer was opened.").strip()
            return {"started": result.returncode == 0, "message": message}
        if system == "windows" and shutil.which("winget"):
            subprocess.Popen(["winget", "install", "--id", "Git.Git", "-e", "--source", "winget"])
            return {"started": True, "message": "The Git for Windows installer was started."}
        installers = [
            ("apt-get", "pkexec apt-get install -y git"),
            ("dnf", "pkexec dnf install -y git"),
            ("pacman", "pkexec pacman -S --needed git"),
        ]
        for binary, command in installers:
            if shutil.which(binary):
                return {"started": False, "message": "Run this command in a terminal: {}".format(command)}
        return {"started": False, "message": "Install Git from https://git-scm.com/downloads and reopen the Control Center."}

    def install_shortcut(self):
        script = self.kit_root / "control-center" / "install-shortcut.py"
        result = run_command([sys.executable, str(script)], timeout=30)
        message = result.stdout.strip() or "Installed the AWESOME WEBKIT desktop shortcut."
        return {"installed": True, "message": message}


class ProviderRunner:
    def __init__(self, session, event_log, persist_thread, set_process):
        self.session = session
        self.event_log = event_log
        self.persist_thread = persist_thread
        self.set_process = set_process

    def run(self, prompt):
        provider = self.session["provider"]
        worktree = self.session["worktree"]
        thread_id = self.session.get("threadId")
        env = os.environ.copy()
        env["WK_CONTROL_CENTER"] = "1"
        env["WK_SESSION_COLOR"] = self.session["color"]
        if provider == "codex":
            executable = shutil.which("codex")
            if not executable:
                raise ControlCenterError("Codex CLI is not installed.", 409)
            if thread_id:
                command = [executable, "exec", "resume", "--json", thread_id, prompt]
            else:
                command = [executable, "exec", "--json", "--sandbox", "workspace-write", prompt]
        else:
            executable = shutil.which("claude")
            if not executable:
                raise ControlCenterError("Claude Code CLI is not installed.", 409)
            if not thread_id:
                thread_id = str(uuid.uuid4())
                self.session["threadId"] = thread_id
                self.persist_thread(thread_id)
            command = [
                executable, "-p", "--output-format", "stream-json", "--verbose",
                "--permission-mode", "auto",
            ]
            if self.session.get("hasRun"):
                command.extend(["--resume", thread_id])
            else:
                command.extend(["--session-id", thread_id])
            command.append(prompt)

        process = subprocess.Popen(
            command,
            cwd=worktree,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.set_process(process)
        stderr_lines = []
        stderr_thread = threading.Thread(
            target=self._read_stderr, args=(process, stderr_lines), daemon=True
        )
        stderr_thread.start()
        for raw in iter(process.stdout.readline, ""):
            line = raw.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                self.event_log.append("agent", line, "progress")
                continue
            if provider == "codex":
                self._codex_event(event)
            else:
                self._claude_event(event)
        code = process.wait()
        stderr_thread.join(timeout=1)
        self.set_process(None)
        if code != 0:
            if stderr_lines:
                self.event_log.append("system", "\n".join(stderr_lines[-20:]), "error")
            raise ControlCenterError("{} exited with code {}.".format(provider, code), 500)
        self.session["hasRun"] = True

    @staticmethod
    def _read_stderr(process, sink):
        for raw in iter(process.stderr.readline, ""):
            line = raw.strip()
            if line:
                sink.append(line)

    def _codex_event(self, event):
        kind = event.get("type", "")
        if kind == "thread.started" and event.get("thread_id"):
            self.session["threadId"] = event["thread_id"]
            self.persist_thread(event["thread_id"])
            return
        item = event.get("item") or {}
        item_type = item.get("type", "")
        if kind == "item.completed" and item_type == "agent_message":
            self.event_log.append("agent", item.get("text", ""), "message")
        elif kind in ("item.started", "item.completed") and item_type in (
            "command_execution", "file_change", "mcp_tool_call", "web_search", "plan_update"
        ):
            text = item.get("command") or item.get("name") or item.get("text") or item_type.replace("_", " ")
            self.event_log.append("agent", text, "activity", {"status": item.get("status"), "type": item_type})
        elif kind in ("error", "turn.failed"):
            self.event_log.append("system", event.get("message") or json.dumps(event), "error")

    def _claude_event(self, event):
        if event.get("session_id") and event.get("session_id") != self.session.get("threadId"):
            self.session["threadId"] = event["session_id"]
            self.persist_thread(event["session_id"])
        kind = event.get("type")
        if kind == "assistant":
            content = ((event.get("message") or {}).get("content") or [])
            for block in content:
                if block.get("type") == "text" and block.get("text"):
                    self.event_log.append("agent", block["text"], "message")
                elif block.get("type") == "tool_use":
                    self.event_log.append("agent", block.get("name", "tool"), "activity")
        elif kind == "result":
            if event.get("is_error"):
                errors = event.get("errors") or [event.get("result") or "Claude Code failed."]
                self.event_log.append("system", "\n".join(str(x) for x in errors), "error")
            elif event.get("result"):
                self.event_log.append("agent", event["result"], "message")


class SessionRuntime:
    def __init__(self, manager, session):
        self.manager = manager
        self.session = session
        self.log = EventLog(manager.store.state_dir, session["id"])
        self.jobs = queue.Queue()
        self.stop_event = threading.Event()
        self.process_lock = threading.Lock()
        self.current_process = None
        self.preview_process = None
        self.last_phase_key = None
        self.worker = threading.Thread(target=self._work_loop, name="wk-agent-" + session["id"], daemon=True)
        self.watcher = threading.Thread(target=self._watch_loop, name="wk-watch-" + session["id"], daemon=True)

    def start(self):
        self.worker.start()
        self.watcher.start()

    def enqueue(self, prompt, source):
        self.jobs.put({"prompt": prompt, "source": source})

    def _work_loop(self):
        while not self.stop_event.is_set():
            try:
                job = self.jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            if job is None:
                break
            self.manager._set_session_status(self.session["id"], "busy")
            self.log.append("user" if job["source"] == "chat" else "system", job["prompt"], job["source"])
            runner = ProviderRunner(
                self.session,
                self.log,
                lambda thread_id: self.manager._set_thread(self.session["id"], thread_id),
                self._set_process,
            )
            try:
                runner.run(job["prompt"])
                self.manager._set_session_status(self.session["id"], "active")
            except Exception as exc:
                self.log.append("system", str(exc), "error")
                self.manager._set_session_status(self.session["id"], "error", str(exc))
            finally:
                self.jobs.task_done()

    def _watch_loop(self):
        inbox = Path(self.session["worktree"]) / self.session.get("feedbackDir", ".webkit/feedback") / self.session["color"]
        while not self.stop_event.wait(1):
            verdicts = inbox / "verdicts.json"
            feedback = inbox / "feedback.json"
            review = inbox / "review.json"
            key = None
            if verdicts.exists():
                key = ("verdicts", verdicts.stat().st_mtime_ns)
            elif feedback.exists() and not review.exists():
                key = ("feedback", feedback.stat().st_mtime_ns)
            if key and key != self.last_phase_key:
                self.last_phase_key = key
                prompt = CONTROL_CENTER_PROMPT.format(
                    emoji=self.session["emoji"], color=self.session["color"]
                ).replace(".webkit/feedback/{}/".format(self.session["color"]), str(inbox) + "/")
                self.enqueue(prompt, "feedback")
            elif not key:
                self.last_phase_key = None

    def _set_process(self, process):
        with self.process_lock:
            self.current_process = process

    def stop(self):
        self.stop_event.set()
        self.jobs.put(None)
        with self.process_lock:
            if self.current_process and self.current_process.poll() is None:
                self.current_process.terminate()
        if self.preview_process and self.preview_process.poll() is None:
            self.preview_process.terminate()
            try:
                self.preview_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.preview_process.kill()


class SessionManager:
    def __init__(self, store, projects):
        self.store = store
        self.projects = projects
        self.runtimes = {}
        self.lock = threading.RLock()

    def list_sessions(self, project_id=None):
        sessions = self.store.read().get("sessions", [])
        if project_id:
            sessions = [s for s in sessions if s.get("projectId") == project_id]
        return sessions

    def start_session(self, project_id, color):
        project = self.projects.get_project(project_id)
        project_path = Path(project["path"])
        if not project_path.is_dir():
            raise ControlCenterError("Project folder is missing.", 404)
        with self.lock:
            for session in self.list_sessions(project_id):
                if session.get("color") == color and session.get("status") in ("active", "busy", "error"):
                    return session
            dirty = run_command(["git", "status", "--porcelain"], cwd=project_path).stdout.strip()
            if dirty:
                raise ControlCenterError("The main project has uncommitted changes. Commit or stash them first.", 409)
            current_branch = run_command(["git", "branch", "--show-current"], cwd=project_path).stdout.strip()
            if current_branch != "main":
                raise ControlCenterError("Open the project repository on its main branch before starting color sessions.", 409)
            config = json.loads((project_path / "webkit" / "webkit.config.json").read_text(encoding="utf-8"))
            palette = {entry["slug"]: entry for entry in config.get("palette", [])}
            if color not in palette:
                raise ControlCenterError("That color is not configured for this project.", 404)
            provider = project["provider"]
            if not self.projects.system_status()[provider]["installed"]:
                raise ControlCenterError("{} CLI is not installed.".format(provider), 409)

            session_id = uuid.uuid4().hex[:12]
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            branch = "webkit/{}/{}-{}".format(color, stamp, session_id[:6])
            worktree = self.store.state_dir / "worktrees" / project["slug"] / (color + "-" + session_id[:6])
            worktree.parent.mkdir(parents=True, exist_ok=True)
            run_command(["git", "worktree", "add", "-b", branch, str(worktree), "main"], cwd=project_path)
            entry = palette[color]
            default_page = str(config.get("default_page", "index.html")).lstrip("/")
            preview_url = "http://127.0.0.1:{}/{}".format(entry["port"], default_page)
            session = {
                "id": session_id,
                "projectId": project_id,
                "projectName": project["name"],
                "provider": provider,
                "color": color,
                "emoji": entry["emoji"],
                "port": entry["port"],
                "branch": branch,
                "worktree": str(worktree),
                "previewUrl": preview_url,
                "feedbackDir": config.get("feedback_dir", ".webkit/feedback"),
                "status": "active",
                "threadId": None,
                "hasRun": False,
                "createdAt": utc_now(),
            }
            runtime = SessionRuntime(self, session)
            try:
                self._claim_and_preview(runtime, config)
            except Exception:
                run_command(["git", "worktree", "remove", "--force", str(worktree)], cwd=project_path, check=False)
                run_command(["git", "branch", "-D", branch], cwd=project_path, check=False)
                raise
            self.store.update(lambda state: state.setdefault("sessions", []).append(dict(session)))
            self.runtimes[session_id] = runtime
            runtime.log.append("system", "{} {} session started in an isolated worktree.".format(entry["emoji"], provider), "status")
            runtime.start()
            return dict(session)

    def _claim_and_preview(self, runtime, config):
        session = runtime.session
        worktree = Path(session["worktree"])
        config_path = worktree / "webkit" / "webkit.config.json"
        site_root = (worktree / config.get("site_root", ".")).resolve()
        env = os.environ.copy()
        env["WK_CONFIG"] = str(config_path)
        env["WK_COLOR_OWNER"] = str(site_root)
        settings = normalized_settings(self.store.read().get("settings"))
        env["WK_DICTATION_MODE"] = settings["dictationMode"]
        env["WK_INTERACTION_MODE"] = settings["interactionMode"]
        env["WK_HOTKEY_TOGGLE"] = settings["toggleHotkey"]
        env["WK_HOTKEY_DICTATE"] = settings["dictateHotkey"]
        self._claim_lock(config, session["color"], site_root)
        server = worktree / "webkit" / "server" / "preview-server.py"
        runtime.preview_process = subprocess.Popen(
            [shutil.which("python3") or "python3", str(server), session["emoji"]],
            cwd=str(worktree), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        deadline = time.time() + 8
        while time.time() < deadline:
            if runtime.preview_process.poll() is not None:
                output = runtime.preview_process.stdout.read() if runtime.preview_process.stdout else ""
                raise ControlCenterError("Preview server failed: {}".format(output.strip()), 409)
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(0.2)
            try:
                if sock.connect_ex(("127.0.0.1", int(session["port"]))) == 0:
                    return
            finally:
                sock.close()
            time.sleep(0.15)
        raise ControlCenterError("Preview server did not start on port {}.".format(session["port"]), 500)

    @staticmethod
    def _claim_lock(config, color, owner):
        lock_dir = Path(config.get("lock_dir", "/tmp/webkit-agent-colors"))
        if not lock_dir.is_absolute():
            raise ControlCenterError("Webkit lock_dir must be absolute.", 409)
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock = lock_dir / (color + ".lock")
        try:
            lock.mkdir()
        except FileExistsError:
            owner_file = lock / "owner"
            existing = owner_file.read_text(encoding="utf-8").strip() if owner_file.exists() else ""
            if Path(existing).resolve() == Path(owner).resolve():
                shutil.rmtree(str(lock))
                lock.mkdir()
            else:
                raise ControlCenterError("{} is already in use by another worktree.".format(color.capitalize()), 409)
        (lock / "owner").write_text(str(Path(owner).resolve()) + "\n", encoding="utf-8")

    def send_message(self, session_id, message):
        message = (message or "").strip()
        if not message:
            raise ControlCenterError("Message cannot be empty.")
        runtime = self._runtime(session_id)
        runtime.enqueue(message, "chat")
        return {"queued": True}

    def events(self, session_id, after=0):
        self._get_session(session_id)
        return EventLog(self.store.state_dir, session_id).read_after(max(0, int(after)))

    def merge(self, session_id):
        session = self._get_session(session_id)
        project = self.projects.get_project(session["projectId"])
        runtime = self.runtimes.get(session_id)
        if session.get("status") == "busy":
            raise ControlCenterError("Wait for the agent to finish before merging.", 409)
        worktree = Path(session["worktree"])
        if worktree.exists() and run_command(["git", "status", "--porcelain"], cwd=worktree).stdout.strip():
            raise ControlCenterError("The color worktree has uncommitted changes. Ask the agent to commit them first.", 409)
        project_path = Path(project["path"])
        if run_command(["git", "status", "--porcelain"], cwd=project_path).stdout.strip():
            raise ControlCenterError("Main has uncommitted changes. Commit or stash them before merging.", 409)
        if run_command(["git", "branch", "--show-current"], cwd=project_path).stdout.strip() != "main":
            raise ControlCenterError("The project repository must be on main to merge.", 409)
        self._set_session_status(session_id, "merging")
        if runtime:
            runtime.stop()
        try:
            run_command(
                ["git", "merge", "--no-ff", session["branch"], "-m", "Merge {} Webkit session".format(session["emoji"])],
                cwd=project_path,
            )
        except Exception as exc:
            run_command(["git", "merge", "--abort"], cwd=project_path, check=False)
            if runtime:
                self.runtimes.pop(session_id, None)
                try:
                    self._restart_runtime(session)
                except Exception as restart_error:
                    message = "{}; preview restart failed: {}".format(exc, restart_error)
                    self._set_session_status(session_id, "error", message)
                    raise ControlCenterError(message, 409)
            self._set_session_status(session_id, "error", str(exc))
            raise
        if runtime:
            self._release(runtime)
        if worktree.exists():
            run_command(["git", "worktree", "remove", str(worktree)], cwd=project_path)
        run_command(["git", "branch", "-d", session["branch"]], cwd=project_path)
        self._set_session_status(session_id, "merged")
        return {"merged": True, "branch": "main"}

    def _restart_runtime(self, session):
        worktree = Path(session["worktree"])
        config_path = worktree / "webkit" / "webkit.config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        restarted = SessionRuntime(self, dict(session))
        self._claim_and_preview(restarted, config)
        self.runtimes[session["id"]] = restarted
        restarted.start()
        restarted.log.append(
            "system", "Merge did not complete; the color session is still available.", "error"
        )

    def discard(self, session_id, confirmation):
        session = self._get_session(session_id)
        if confirmation != session_id:
            raise ControlCenterError("Discard confirmation did not match the session.", 409)
        project = self.projects.get_project(session["projectId"])
        runtime = self.runtimes.get(session_id)
        if runtime:
            self._release(runtime)
        project_path = Path(project["path"])
        worktree = Path(session["worktree"])
        if worktree.exists():
            run_command(["git", "worktree", "remove", "--force", str(worktree)], cwd=project_path)
        run_command(["git", "branch", "-D", session["branch"]], cwd=project_path, check=False)
        self._set_session_status(session_id, "discarded")
        return {"discarded": True}

    def _release(self, runtime):
        runtime.stop()
        session = runtime.session
        worktree = Path(session["worktree"])
        config_path = worktree / "webkit" / "webkit.config.json"
        if config_path.exists():
            config = json.loads(config_path.read_text(encoding="utf-8"))
            owner = (worktree / config.get("site_root", ".")).resolve()
            lock = Path(config.get("lock_dir", "/tmp/webkit-agent-colors")) / (session["color"] + ".lock")
            owner_file = lock / "owner"
            existing = owner_file.read_text(encoding="utf-8").strip() if owner_file.exists() else ""
            if lock.is_dir() and existing and Path(existing).resolve() == owner:
                shutil.rmtree(str(lock))
        self.runtimes.pop(session["id"], None)

    def recover(self):
        for session in self.store.read().get("sessions", []):
            if session.get("status") not in ("active", "busy", "error"):
                continue
            worktree = Path(session.get("worktree", ""))
            config_path = worktree / "webkit" / "webkit.config.json"
            if not worktree.is_dir() or not config_path.exists():
                self._set_session_status(session["id"], "error", "Session worktree is missing.")
                continue
            runtime = SessionRuntime(self, dict(session))
            try:
                config = json.loads(config_path.read_text(encoding="utf-8"))
                self._claim_and_preview(runtime, config)
                self.runtimes[session["id"]] = runtime
                self._set_session_status(session["id"], "active")
                runtime.start()
                runtime.log.append("system", "Session recovered after Control Center restart.", "status")
            except Exception as exc:
                self._set_session_status(session["id"], "error", str(exc))

    def shutdown(self):
        for runtime in list(self.runtimes.values()):
            self._release(runtime)

    def refresh_previews_for_settings(self):
        restarted = 0
        deferred = 0
        for session_id, runtime in list(self.runtimes.items()):
            session = self._get_session(session_id)
            if session.get("status") == "busy":
                deferred += 1
                continue
            runtime.stop()
            self.runtimes.pop(session_id, None)
            self._restart_runtime(session)
            restarted += 1
        return {"restarted": restarted, "deferred": deferred}

    def _runtime(self, session_id):
        runtime = self.runtimes.get(session_id)
        if not runtime:
            raise ControlCenterError("That session is not running in this Control Center process.", 409)
        return runtime

    def _get_session(self, session_id):
        for session in self.store.read().get("sessions", []):
            if session["id"] == session_id:
                return dict(session)
        raise ControlCenterError("Unknown session.", 404)

    def _set_thread(self, session_id, thread_id):
        def mutate(state):
            for session in state.get("sessions", []):
                if session["id"] == session_id:
                    session["threadId"] = thread_id
                    session["hasRun"] = True
        self.store.update(mutate)
        if session_id in self.runtimes:
            self.runtimes[session_id].session["threadId"] = thread_id
            self.runtimes[session_id].session["hasRun"] = True

    def _set_session_status(self, session_id, status, error=None):
        def mutate(state):
            for session in state.get("sessions", []):
                if session["id"] == session_id:
                    session["status"] = status
                    session["updatedAt"] = utc_now()
                    if error:
                        session["error"] = error
                    elif "error" in session:
                        session.pop("error")
        self.store.update(mutate)
        if session_id in self.runtimes:
            self.runtimes[session_id].session["status"] = status


class ControlCenter:
    def __init__(self, kit_root, state_dir):
        self.store = StateStore(state_dir)
        self.projects = ProjectManager(kit_root, self.store)
        self.sessions = SessionManager(self.store, self.projects)
        self.sessions.recover()

    def bootstrap(self):
        state = self.store.read()
        return {
            "providers": state.get("providers", []),
            "projects": self.projects.list_projects(),
            "sessions": state.get("sessions", []),
            "system": self.projects.system_status(),
            "settings": normalized_settings(state.get("settings")),
        }

    def save_settings(self, settings):
        submitted = settings if isinstance(settings, dict) else {}
        current = normalized_settings(self.store.read().get("settings"))
        dictation_mode = submitted.get("dictationMode", current["dictationMode"])
        interaction_mode = submitted.get("interactionMode", current["interactionMode"])
        toggle_hotkey = submitted.get("toggleHotkey", current["toggleHotkey"])
        dictate_hotkey = submitted.get("dictateHotkey", current["dictateHotkey"])
        if dictation_mode not in ("speech", "voice-note"):
            raise ControlCenterError("Choose browser speech or agent voice notes.")
        if interaction_mode not in ("browse-default", "draw-default"):
            raise ControlCenterError("Choose normal website clicks or immediate rectangle drawing.")
        if not valid_hotkey(toggle_hotkey) or not valid_hotkey(dictate_hotkey):
            raise ControlCenterError("Choose a regular key—not a modifier or Escape—for each shortcut.")
        if toggle_hotkey == dictate_hotkey:
            raise ControlCenterError("Open/close and dictation need different shortcut keys.")
        saved = {
            "dictationMode": dictation_mode,
            "interactionMode": interaction_mode,
            "toggleHotkey": toggle_hotkey,
            "dictateHotkey": dictate_hotkey,
        }
        self.store.update(lambda state: state.update({"settings": saved}))
        refresh = self.sessions.refresh_previews_for_settings()
        return {"settings": saved, "previews": refresh}
