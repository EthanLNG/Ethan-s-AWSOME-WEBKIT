#!/usr/bin/env python3
"""Token-protected localhost HTTP server for AWESOME WEBKIT Control Center."""

import argparse
import json
import mimetypes
import os
import re
import secrets
import signal
import sys
import threading
import urllib.parse
import webbrowser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

from control_center import (
    ControlCenter,
    ControlCenterError,
    read_stable_regular_text,
    strict_json_loads,
)
from launch import acquire_instance_lock, release_instance_lock, validated_state_dir_path


HERE = Path(__file__).resolve().parent
KIT_ROOT = HERE.parent
STATIC_ROOT = HERE / "static"
STATIC_ASSET_NAMES = ("index.html", "app.js", "styles.css", "brand-icon.svg")
MAX_STATIC_ASSET_BYTES = 1024 * 1024
AUTH_TOKEN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
CLIENT_IO_TIMEOUT_SECONDS = 15
MAX_LOG_MESSAGE_BYTES = 2048


def valid_auth_token(value):
    return isinstance(value, str) and AUTH_TOKEN.fullmatch(value) is not None


def kit_version():
    try:
        return (KIT_ROOT / "webkit" / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"


def load_static_assets():
    """Snapshot one compatible frontend bundle for this server process."""
    assets = {}
    for name in STATIC_ASSET_NAMES:
        text = read_stable_regular_text(
            STATIC_ROOT / name,
            MAX_STATIC_ASSET_BYTES,
            "Control Center static asset {}".format(name),
        )
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        assets[name] = (text.encode("utf-8"), content_type)
    return assets


class ControlCenterHTTPServer(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True

    def __init__(self, address, handler, app, token):
        if not valid_auth_token(token):
            raise ValueError("Control Center auth tokens must be 16 to 128 URL-safe characters.")
        self.kit_version = kit_version()
        self.static_assets = load_static_assets()
        super().__init__(address, handler)
        self.app = app
        self.token = token

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(CLIENT_IO_TIMEOUT_SECONDS)
        return request, address


class Handler(BaseHTTPRequestHandler):
    server_version = "AwesomeWebkitControlCenter/{}".format(kit_version())

    def log_message(self, fmt, *args):
        try:
            message = fmt % args
        except (TypeError, ValueError):
            message = fmt
        token = str(getattr(self.server, "token", "") or "")
        if token:
            message = message.replace(token, "[REDACTED]")
        message = re.sub(
            r"([?&]token=)[^&\s\"]*",
            r"\1[REDACTED]",
            message,
            flags=re.IGNORECASE,
        )
        encoded = message.encode("utf-8", errors="backslashreplace")
        if len(encoded) > MAX_LOG_MESSAGE_BYTES:
            encoded = encoded[:MAX_LOG_MESSAGE_BYTES - 3] + b"..."
        message = encoded.decode("utf-8", errors="ignore")
        sys.stderr.write("[control-center] " + message + "\n")

    def log_request(self, _code="-", _size="-"):
        return

    def do_GET(self):
        if not self._guard_host():
            return
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        bootstrap_token = query.get("token", [None])[0]
        if parsed.path == "/" and bootstrap_token is not None:
            if not secrets.compare_digest(str(bootstrap_token), self.server.token):
                self._json({"error": "Invalid Control Center launch token."}, 401)
                return
            self.send_response(302)
            self.send_header(
                "Location",
                "/#token={}".format(urllib.parse.quote(self.server.token, safe="")),
            )
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            return
        if parsed.path.startswith("/api/") and (
            not self._authorized() or not self._same_origin()
        ):
            self._json({"error": "Open the Control Center with its launcher."}, 401)
            return
        try:
            if parsed.path == "/api/health":
                self._json({
                    "ok": True,
                    "pid": os.getpid(),
                    "kitVersion": self.server.kit_version,
                })
            elif parsed.path == "/api/bootstrap":
                self._json(self.server.app.bootstrap())
            elif parsed.path == "/api/projects":
                self._json({"projects": self.server.app.projects.list_projects()})
            elif parsed.path == "/api/sessions":
                project_id = query.get("projectId", [None])[0]
                self._json({"sessions": self.server.app.sessions.list_sessions(project_id)})
            elif re.fullmatch(r"/api/sessions/[^/]+/events", parsed.path):
                session_id = parsed.path.split("/")[3]
                after = query.get("after", ["0"])[0]
                self._json(self.server.app.sessions.events(session_id, after))
            elif re.fullmatch(r"/api/sessions/[^/]+/seeds", parsed.path):
                session_id = parsed.path.split("/")[3]
                self._json(self.server.app.sessions.seed_status(session_id))
            elif parsed.path.startswith("/api/"):
                self._json({"error": "API route not found."}, 404)
            else:
                self._static(parsed.path)
        except ControlCenterError as exc:
            self._json({"error": str(exc), "details": exc.details}, exc.status)
        except Exception as exc:
            self.log_error("unexpected GET failure (%s)", type(exc).__name__)
            self._json({"error": "Internal server error."}, 500)

    def do_POST(self):
        if not self._guard_host():
            return
        parsed = urllib.parse.urlparse(self.path)
        if not self._authorized() or not self._same_origin():
            self._json({"error": "Unauthorized request."}, 401)
            return
        try:
            body = self._body()
            path = parsed.path
            if path == "/api/preferences":
                self._json(self.server.app.save_preferences(body))
            elif path == "/api/notices/fast-mode":
                self._json(self.server.app.acknowledge_fast_mode_notice())
            elif path == "/api/providers":
                result = self.server.app.projects.save_providers(body.get("providers"))
                self._json({"providers": result})
            elif path == "/api/settings":
                self._json(self.server.app.save_settings(body))
            elif path == "/api/system/choose-folder":
                self._json(self.server.app.choose_folder(body.get("initial"), body.get("purpose")))
            elif path == "/api/system/install-git":
                if body.get("confirmed") is not True:
                    raise ControlCenterError("Confirm Git installation first.", 409)
                self._json(self.server.app.projects.git_install_action())
            elif path == "/api/system/install-shortcut":
                self._json(self.server.app.projects.install_shortcut())
            elif path == "/api/projects/create":
                result = self.server.app.create_project(
                    body.get("name", ""), body.get("parent", ""), body.get("provider", ""),
                    body.get("onboarding"),
                )
                self._json(result, 201)
            elif path == "/api/projects/existing":
                project = self.server.app.projects.add_existing(
                    body.get("path", ""), body.get("provider", ""),
                    update_webkit=body.get("updateWebkit", False),
                )
                self._json({"project": project}, 201)
            elif path.startswith("/api/projects/") and path.endswith("/push"):
                parts = path.strip("/").split("/")
                if len(parts) != 4:
                    raise ControlCenterError("Project route not found.", 404)
                self._json(self.server.app.projects.push_project(parts[2]))
            elif path.startswith("/api/projects/") and path.endswith("/agent"):
                parts = path.strip("/").split("/")
                if len(parts) != 4:
                    raise ControlCenterError("Project route not found.", 404)
                session = self.server.app.sessions.start_issue_session(
                    parts[2],
                    body.get("issueCode", ""),
                    body.get("reasoningEffort", "high"),
                )
                self._json({"session": session}, 201)
            elif path.startswith("/api/projects/") and path.endswith("/seeds-start"):
                parts = path.strip("/").split("/")
                if len(parts) != 4:
                    raise ControlCenterError("Project route not found.", 404)
                self._json(self.server.app.start_project_seeds(parts[2]), 201)
            elif path == "/api/sessions/start":
                session = self.server.app.sessions.start_session(
                    body.get("projectId", ""), body.get("color", ""),
                    body.get("reasoningEffort", "medium"), body.get("speedMode", "normal")
                )
                self._json({"session": session}, 201)
            elif path.startswith("/api/sessions/"):
                parts = path.strip("/").split("/")
                if len(parts) != 4:
                    raise ControlCenterError("Session route not found.", 404)
                session_id, action = parts[2], parts[3]
                if action == "message":
                    self._json(self.server.app.sessions.send_message(
                        session_id, body.get("message", ""), body.get("attachments")
                    ), 202)
                elif action == "reasoning":
                    self._json(self.server.app.sessions.set_reasoning(
                        session_id, body.get("reasoningEffort", "")
                    ))
                elif action == "speed":
                    self._json(self.server.app.sessions.set_speed(
                        session_id, body.get("speedMode", "")
                    ))
                elif action == "merge":
                    self._json(self.server.app.sessions.merge(session_id))
                elif action == "seeds-select":
                    self._json(self.server.app.sessions.choose_seeds(
                        session_id, body.get("selected"), body.get("notes", "")
                    ), 202)
                elif action == "discard":
                    self._json(self.server.app.sessions.discard(session_id, body.get("confirmation")))
                elif action == "dismiss":
                    self._json(self.server.app.sessions.dismiss_merged(session_id))
                else:
                    raise ControlCenterError("Session action not found.", 404)
            elif path == "/api/shutdown":
                self._json({"stopping": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self._json({"error": "API route not found."}, 404)
        except ControlCenterError as exc:
            self._json({"error": str(exc), "details": exc.details}, exc.status)
        except Exception as exc:
            self.log_error("unexpected POST failure (%s)", type(exc).__name__)
            self._json({"error": "Internal server error."}, 500)

    def _authorized(self):
        value = self.headers.get("X-WKCC-Token", "")
        return bool(value and secrets.compare_digest(value, self.server.token))

    def _host_allowed(self):
        host = self.headers.get("Host", "").strip().lower()
        port = int(self.server.server_port)
        allowed = {
            "127.0.0.1:{}".format(port),
            "localhost:{}".format(port),
        }
        if port == 80:
            allowed.update(("127.0.0.1", "localhost"))
        return host in allowed

    def _guard_host(self):
        if self._host_allowed():
            return True
        self._json({"error": "Request Host is not allowed for this Control Center."}, 421)
        return False

    def _same_origin(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True
        expected = "http://{}:{}".format(*self.server.server_address)
        aliases = {expected, expected.replace("127.0.0.1", "localhost")}
        return origin in aliases

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ControlCenterError("Invalid Content-Length.")
        if length < 0:
            raise ControlCenterError("Invalid Content-Length.")
        if length > 30 * 1024 * 1024:
            raise ControlCenterError("Request is too large.", 413)
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise ControlCenterError("Content-Type must be application/json.", 415)
        raw = self.rfile.read(length) if length else b"{}"
        if length and len(raw) != length:
            raise ControlCenterError("Request body ended before Content-Length bytes were received.")
        try:
            value = strict_json_loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ControlCenterError("Request body must be valid standard JSON: {}".format(exc))
        if not isinstance(value, dict):
            raise ControlCenterError("Request body must be a JSON object.")
        return value

    def _static(self, request_path):
        relative = "index.html" if request_path in ("", "/") else request_path.lstrip("/")
        asset = self.server.static_assets.get(relative)
        if asset is None:
            self._json({"error": "Not found."}, 404)
            return
        data, content_type = asset
        self.send_response(200)
        self.send_header("Content-Type", content_type + ("; charset=utf-8" if content_type.startswith("text/") else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-src http://127.0.0.1:* http://localhost:*; "
            "frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'",
        )
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, value, status=200):
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the AWESOME WEBKIT local Control Center")
    parser.add_argument("--port", default=os.environ.get("WKCC_PORT", "8790"))
    parser.add_argument("--state-dir", default=os.environ.get("WKCC_STATE_DIR", "~/.awesome-webkit"))
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    try:
        args.port = int(args.port)
    except (TypeError, ValueError):
        parser.error("--port and WKCC_PORT must be an integer between 1 and 65535")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    token = os.environ.get("WKCC_TOKEN") or secrets.token_urlsafe(32)
    if not valid_auth_token(token):
        parser.error("WKCC_TOKEN must be 16 to 128 characters using only letters, numbers, underscores, or hyphens")
    state_dir = validated_state_dir_path(args.state_dir)
    instance_lock = acquire_instance_lock(state_dir)
    if instance_lock is None:
        raise RuntimeError("Another Control Center already owns this state directory.")
    app = None
    server = None
    url = "http://127.0.0.1:{}/?token={}".format(args.port, urllib.parse.quote(token))
    try:
        app = ControlCenter(KIT_ROOT, state_dir, recover=False)
        server = ControlCenterHTTPServer(("127.0.0.1", args.port), Handler, app, token)
        stop_requested = threading.Event()
        serving = threading.Event()

        def stop(_signum=None, _frame=None):
            stop_requested.set()
            app.sessions.request_shutdown()
            if serving.is_set():
                threading.Thread(target=server.shutdown, daemon=True).start()

        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
        recovered = app.sessions.recover(cancel_event=stop_requested)
        if recovered is False or stop_requested.is_set():
            return 0
        print("AWESOME WEBKIT Control Center: http://127.0.0.1:{}".format(args.port), flush=True)
        if not args.no_browser:
            webbrowser.open(url, new=2)
        serving.set()
        if stop_requested.is_set():
            return 0
        server.serve_forever(poll_interval=0.25)
    finally:
        if server is not None:
            server.server_close()
        if app is not None:
            app.sessions.shutdown()
        release_instance_lock(instance_lock)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
