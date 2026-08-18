#!/usr/bin/env python3
"""Token-protected localhost HTTP server for AWESOME WEBKIT Control Center."""

import argparse
import json
import mimetypes
import os
import secrets
import signal
import sys
import threading
import urllib.parse
import webbrowser
from http import cookies
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

from control_center import ControlCenter, ControlCenterError


HERE = Path(__file__).resolve().parent
KIT_ROOT = HERE.parent
STATIC_ROOT = HERE / "static"


class ControlCenterHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, app, token):
        super().__init__(address, handler)
        self.app = app
        self.token = token


class Handler(BaseHTTPRequestHandler):
    server_version = "AwesomeWebkitControlCenter/0.5"

    def log_message(self, fmt, *args):
        sys.stderr.write("[control-center] " + (fmt % args) + "\n")

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/" and query.get("token", [None])[0] == self.server.token:
            self.send_response(302)
            self.send_header("Location", "/")
            self.send_header(
                "Set-Cookie",
                "wkcc={}; HttpOnly; SameSite=Strict; Path=/".format(self.server.token),
            )
            self.end_headers()
            return
        if not self._authorized():
            self._json({"error": "Open the Control Center with its launcher."}, 401)
            return
        try:
            if parsed.path == "/api/bootstrap":
                self._json(self.server.app.bootstrap())
            elif parsed.path == "/api/projects":
                self._json({"projects": self.server.app.projects.list_projects()})
            elif parsed.path == "/api/sessions":
                project_id = query.get("projectId", [None])[0]
                self._json({"sessions": self.server.app.sessions.list_sessions(project_id)})
            elif parsed.path.startswith("/api/sessions/") and parsed.path.endswith("/events"):
                session_id = parsed.path.split("/")[3]
                after = query.get("after", ["0"])[0]
                self._json(self.server.app.sessions.events(session_id, int(after)))
            elif parsed.path.startswith("/api/"):
                self._json({"error": "API route not found."}, 404)
            else:
                self._static(parsed.path)
        except ControlCenterError as exc:
            self._json({"error": str(exc), "details": exc.details}, exc.status)
        except Exception as exc:
            self._json({"error": str(exc)}, 500)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if not self._authorized() or not self._same_origin():
            self._json({"error": "Unauthorized request."}, 401)
            return
        try:
            body = self._body()
            path = parsed.path
            if path == "/api/providers":
                result = self.server.app.projects.save_providers(body.get("providers"))
                self._json({"providers": result})
            elif path == "/api/system/install-git":
                if body.get("confirmed") is not True:
                    raise ControlCenterError("Confirm Git installation first.", 409)
                self._json(self.server.app.projects.git_install_action())
            elif path == "/api/system/install-shortcut":
                self._json(self.server.app.projects.install_shortcut())
            elif path == "/api/projects/create":
                project = self.server.app.projects.create_project(
                    body.get("name", ""), body.get("parent", ""), body.get("provider", "")
                )
                self._json({"project": project}, 201)
            elif path == "/api/projects/existing":
                project = self.server.app.projects.add_existing(
                    body.get("path", ""), body.get("provider", "")
                )
                self._json({"project": project}, 201)
            elif path == "/api/sessions/start":
                session = self.server.app.sessions.start_session(
                    body.get("projectId", ""), body.get("color", "")
                )
                self._json({"session": session}, 201)
            elif path.startswith("/api/sessions/"):
                parts = path.strip("/").split("/")
                if len(parts) != 4:
                    raise ControlCenterError("Session route not found.", 404)
                session_id, action = parts[2], parts[3]
                if action == "message":
                    self._json(self.server.app.sessions.send_message(session_id, body.get("message", "")), 202)
                elif action == "merge":
                    self._json(self.server.app.sessions.merge(session_id))
                elif action == "discard":
                    self._json(self.server.app.sessions.discard(session_id, body.get("confirmation")))
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
            self._json({"error": str(exc)}, 500)

    def _authorized(self):
        if os.environ.get("WKCC_NO_AUTH") == "1":
            return True
        jar = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        value = jar.get("wkcc")
        return bool(value and secrets.compare_digest(value.value, self.server.token))

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
        if length > 1024 * 1024:
            raise ControlCenterError("Request is too large.", 413)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ControlCenterError("Request body must be JSON.")
        if not isinstance(value, dict):
            raise ControlCenterError("Request body must be a JSON object.")
        return value

    def _static(self, request_path):
        relative = "index.html" if request_path in ("", "/") else request_path.lstrip("/")
        target = (STATIC_ROOT / relative).resolve()
        try:
            target.relative_to(STATIC_ROOT)
        except ValueError:
            self._json({"error": "Not found."}, 404)
            return
        if not target.is_file():
            self._json({"error": "Not found."}, 404)
            return
        data = target.read_bytes()
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type + ("; charset=utf-8" if content_type.startswith("text/") else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
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
        self.end_headers()
        self.wfile.write(data)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the AWESOME WEBKIT local Control Center")
    parser.add_argument("--port", type=int, default=int(os.environ.get("WKCC_PORT", "8790")))
    parser.add_argument("--state-dir", default=os.environ.get("WKCC_STATE_DIR", "~/.awesome-webkit"))
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    state_dir = Path(args.state_dir).expanduser()
    app = ControlCenter(KIT_ROOT, state_dir)
    token = os.environ.get("WKCC_TOKEN") or secrets.token_urlsafe(32)
    server = ControlCenterHTTPServer(("127.0.0.1", args.port), Handler, app, token)
    url = "http://127.0.0.1:{}/?token={}".format(args.port, urllib.parse.quote(token))

    def stop(_signum=None, _frame=None):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    print("AWESOME WEBKIT Control Center: http://127.0.0.1:{}".format(args.port), flush=True)
    if not args.no_browser:
        webbrowser.open(url, new=2)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        app.sessions.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
