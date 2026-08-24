import http.client
import html as html_lib
import importlib.util
import io
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock


SERVER_PATH = Path(__file__).resolve().parents[1] / "webkit" / "server" / "preview-server.py"
TRANSITION_HELPER = Path(__file__).resolve().parents[1] / "webkit" / "scripts" / "transition-round.py"
RUNTIME_SCRIPTS = Path(__file__).resolve().parents[1] / "webkit" / "scripts"
sys.path.insert(0, str(RUNTIME_SCRIPTS))
from runtime_registry import claim_color, release_color  # noqa: E402


def _remove_test_tree(path):
    def repair_and_retry(operation, target, _error):
        try:
            os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
            operation(target)
        except OSError:
            raise

    last_error = None
    for _attempt in range(5):
        try:
            shutil.rmtree(path, onerror=repair_and_retry)
            return
        except PermissionError as exc:
            last_error = exc
            time.sleep(0.05)
    if last_error is not None:
        raise last_error


class _WritableTemporaryDirectory(tempfile.TemporaryDirectory):
    def cleanup(self):
        if self._finalizer.detach():
            _remove_test_tree(self.name)


class PreviewServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = _WritableTemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "webkit").mkdir()
        (self.root / "index.html").write_text("<html><head></head><body>Preview</body></html>", encoding="utf-8")
        subprocess.run(["git", "init", "-b", "main"], cwd=self.root, check=True, capture_output=True)
        (self.root / ".gitignore").write_text(
            ".webkit/feedback/\n", encoding="utf-8"
        )
        config = {
            "site_root": ".",
            "feedback_dir": ".webkit/feedback",
            "lock_dir": str(self.root / "locks"),
            "palette": [{"slug": "blue", "emoji": "🔵", "port": 5311}],
        }
        self.config_path = self.root / "webkit" / "webkit.config.json"
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        module_name = "preview_server_test_{}".format(uuid.uuid4().hex)
        spec = importlib.util.spec_from_file_location(module_name, SERVER_PATH)
        self.module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(os.environ, {
            "WK_CONFIG": str(self.config_path),
            "WK_COLOR_FORCE": "1",
            "WK_OVERLAY_THEME": "black",
            "WK_MUTATION_TOKEN": "test-mutation-token-1234567890",
            "WK_PREVIEW_INSTANCE_TOKEN": "test-instance-token-123456",
        }), mock.patch.object(sys, "argv", [str(SERVER_PATH), "🔵", "5311", str(self.root)]):
            spec.loader.exec_module(self.module)
        self.server = self.module.PreviewHTTPServer(("127.0.0.1", 0), self.module.Handler)
        self.module._publish_transition_token()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.assertFalse(self.thread.is_alive())
        self.module._remove_transition_token()
        self.temp.cleanup()

    @property
    def inbox(self):
        return Path(self.module.FEEDBACK_DIR)

    def request(self, method, path, body=b"", headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = response.read()
        result = response.status, dict(response.getheaders()), payload
        connection.close()
        return result

    def post_json(self, path, value, token=True, content_type="application/json"):
        headers = {"Content-Type": content_type}
        if token is True:
            headers["X-WK-Token"] = self.module.MUTATION_TOKEN
        elif isinstance(token, str):
            headers["X-WK-Token"] = token
        return self.request("POST", path, json.dumps(value).encode("utf-8"), headers)

    def post_transition(self, value, token=True):
        headers = {"Content-Type": "application/json"}
        if token is True:
            headers["X-WK-Transition-Token"] = self.module.TRANSITION_TOKEN
        elif isinstance(token, str):
            headers["X-WK-Transition-Token"] = token
        return self.request(
            "POST", "/__wk/transition", json.dumps(value).encode("utf-8"), headers
        )

    def test_default_dictation_hotkey_is_space(self):
        self.assertEqual(
            self.module._HOTKEY_VALUES,
            {"toggle": "KeyC", "dictate": "Space"},
        )

    def test_overlay_theme_is_bounded_and_injected_into_every_document_mode(self):
        self.assertEqual(self.module._OVERLAY_THEME, "black")
        self.assertIn(
            'data-wk-theme="black"',
            self.module.inject("<html><body></body></html>", "after"),
        )
        self.assertIn(
            'data-wk-theme="black"',
            self.module.inject(
                "<html><body></body></html>",
                "before",
                before_prefix="/__wk/before/" + "a" * 64,
            ),
        )

        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        white = self.import_server(
            config, env_overrides={"WK_OVERLAY_THEME": "white"}
        )
        self.assertEqual(white._OVERLAY_THEME, "white")
        self.assertIn(
            'data-wk-theme="white"',
            white.inject("<html><body></body></html>", "after"),
        )

        invalid = self.import_server(
            config, env_overrides={"WK_OVERLAY_THEME": 'white" data-unsafe="yes'}
        )
        self.assertEqual(invalid._OVERLAY_THEME, "black")
        injected = invalid.inject("<html><body></body></html>", "after")
        self.assertIn('data-wk-theme="black"', injected)
        self.assertNotIn("data-unsafe", injected)

    def test_legacy_control_center_v_hotkey_migrates_inside_preview(self):
        resolve = self.module._resolved_hotkeys
        self.assertEqual(resolve({}, {
            "WK_HOTKEY_TOGGLE": "KeyC",
            "WK_HOTKEY_DICTATE": "KeyV",
        }), {"toggle": "KeyC", "dictate": "Space"})
        self.assertEqual(resolve({}, {
            "WK_HOTKEY_TOGGLE": "KeyC",
            "WK_HOTKEY_DICTATE": "KeyV",
            "WK_HOTKEY_DEFAULTS_VERSION": "2",
        }), {"toggle": "KeyC", "dictate": "KeyV"})
        self.assertEqual(resolve({"dictate": "KeyV"}, {}), {
            "toggle": "KeyC", "dictate": "KeyV",
        })
        self.assertEqual(resolve({}, {
            "WK_HOTKEY_TOGGLE": "Space",
            "WK_HOTKEY_DICTATE": "KeyV",
        }), {"toggle": "Space", "dictate": "KeyV"})

    @staticmethod
    def batch(point_id="point-1"):
        return {
            "version": 1,
            "kind": "feedback",
            "batchId": "batch-1",
            "round": 1,
            "color": "blue",
            "pages": ["/index.html"],
            "points": [{"id": point_id, "number": 1, "page": "/index.html", "text": "Fix it"}],
        }

    @staticmethod
    def review(point_ids=("point-1",), round_number=1):
        return {
            "version": 1,
            "kind": "review",
            "batchId": "batch-1",
            "round": round_number,
            "beforeRef": "a" * 40,
            "points": [
                {
                    "id": point_id,
                    "handled": "done",
                    "note": "done",
                    "commit": "b" * 40,
                }
                for point_id in point_ids
            ],
        }

    @staticmethod
    def verdicts(values=(("point-1", "accept"),), round_number=1):
        return {
            "version": 1,
            "kind": "verdicts",
            "batchId": "batch-1",
            "round": round_number,
            "sentAt": "2026-08-19T10:00:00Z",
            "verdicts": [
                {"pointId": point_id, "verdict": verdict}
                for point_id, verdict in values
            ],
        }

    @staticmethod
    def transition(mode, round_number=1, next_review=None):
        value = {
            "version": 1,
            "mode": mode,
            "batchId": "batch-1",
            "round": round_number,
        }
        if next_review is not None:
            value["nextReview"] = next_review
        return value

    def write_data(self, name, value):
        (self.inbox / name).write_text(json.dumps(value), encoding="utf-8")

    def read_data(self, name):
        return json.loads((self.inbox / name).read_text(encoding="utf-8"))

    def before_path(self, path="/"):
        review = self.read_data("review.json")
        return self.module._before_prefix(review) + path

    def import_server(self, config, force="1", argv=None, env_overrides=None):
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        module_name = "preview_server_import_{}".format(uuid.uuid4().hex)
        spec = importlib.util.spec_from_file_location(module_name, SERVER_PATH)
        module = importlib.util.module_from_spec(spec)
        environment = {"WK_CONFIG": str(self.config_path)}
        environment.update(env_overrides or {})
        if force is not None:
            environment["WK_COLOR_FORCE"] = force
        with mock.patch.dict(os.environ, environment, clear=False):
            if force is None:
                os.environ.pop("WK_COLOR_FORCE", None)
            with mock.patch.object(
                sys,
                "argv",
                argv or [str(SERVER_PATH), "🔵", "5311", str(self.root)],
            ):
                spec.loader.exec_module(module)
        return module

    def state_request(self, method="GET", known="", token=True):
        headers = {}
        if token is True:
            headers["X-WK-Token"] = self.module.MUTATION_TOKEN
        elif isinstance(token, str):
            headers["X-WK-Token"] = token
        path = "/__wk/state"
        if known:
            path += "?known=" + known
        return self.request(method, path, headers=headers)

    def test_mutations_require_token_and_json_content_type(self):
        status, _, _ = self.post_json("/__wk/feedback", self.batch(), token=False)
        self.assertEqual(status, 403)
        self.assertFalse((self.inbox / "feedback.json").exists())

        status, _, _ = self.post_json("/__wk/feedback", self.batch(), token="wrong-token")
        self.assertEqual(status, 403)
        self.assertFalse((self.inbox / "feedback.json").exists())

        status, _, _ = self.post_json("/__wk/feedback", self.batch(), content_type="text/plain")
        self.assertEqual(status, 415)
        self.assertFalse((self.inbox / "feedback.json").exists())

        status, _, _ = self.post_json("/__wk/feedback", self.batch())
        self.assertEqual(status, 200)
        self.assertTrue((self.inbox / "feedback.json").is_file())
        self.assertEqual(self.module.MUTATION_TOKEN, "test-mutation-token-1234567890")
        injected = self.module.inject("<html><body></body></html>", "after")
        self.assertIn('data-wk-token="{}"'.format(self.module.MUTATION_TOKEN), injected)

    def test_mutations_enforce_origin_when_present_but_allow_native_clients(self):
        body = json.dumps(self.batch()).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "X-WK-Token": "wrong-token",
            "Origin": "https://attacker.example",
        }
        status, _, payload = self.request(
            "POST", "/__wk/feedback", body, headers
        )
        self.assertEqual(status, 403, payload)
        self.assertIn(b"cross-origin", payload)
        self.assertNotIn(b"mutation token", payload)
        self.assertFalse((self.inbox / "feedback.json").exists())

        headers.update({
            "X-WK-Token": self.module.MUTATION_TOKEN,
            "Origin": "http://127.0.0.1:{}".format(self.server.server_port),
        })
        status, _, payload = self.request(
            "POST", "/__wk/feedback", body, headers
        )
        self.assertEqual(status, 200, payload)

        headers.pop("Origin")
        status, _, payload = self.request(
            "POST", "/__wk/feedback", body, headers
        )
        self.assertEqual(status, 200, payload)

    def test_webkit_internal_errors_are_logged_but_not_returned(self):
        marker = "SENTINEL-INTERNAL-PATH-DETAIL"
        exception = RuntimeError(marker + ":/private/path/" + "x" * 5000)

        def fail(_handler, _raw):
            raise exception

        captured = io.StringIO()
        with mock.patch.object(self.module.Handler, "_wk_get", fail), mock.patch(
            "sys.stderr", captured
        ):
            status, _, payload = self.state_request()

        self.assertEqual(status, 500, payload)
        self.assertEqual(
            json.loads(payload), {"error": "internal server error"}
        )
        self.assertNotIn(marker.encode("utf-8"), payload)
        diagnostic = captured.getvalue()
        self.assertIn(marker, diagnostic)
        self.assertLess(len(diagnostic.encode("utf-8")), 2500)

    def test_routine_preview_requests_do_not_emit_access_logs(self):
        captured = io.StringIO()
        with mock.patch("sys.stderr", captured):
            status, _, payload = self.request("GET", "/")
        self.assertEqual(status, 200, payload)
        self.assertEqual(captured.getvalue(), "")

    def test_negative_content_length_is_rejected_without_reading_to_eof(self):
        status, _, _ = self.request(
            "POST",
            "/__wk/feedback",
            b"{}",
            {
                "Content-Type": "application/json",
                "Content-Length": "-1",
                "X-WK-Token": self.module.MUTATION_TOKEN,
            },
        )
        self.assertEqual(status, 400)

    def test_feedback_ids_are_safe_unique_tokens_and_retries_are_noops(self):
        duplicate = self.batch()
        duplicate["points"].append(dict(duplicate["points"][0]))
        status, _, _ = self.post_json("/__wk/feedback", duplicate)
        self.assertEqual(status, 400)

        unsafe = self.batch("../../instructions")
        status, _, _ = self.post_json("/__wk/feedback", unsafe)
        self.assertEqual(status, 400)
        self.assertFalse((self.inbox / "feedback.json").exists())

        status, _, _ = self.post_json("/__wk/feedback", self.batch())
        self.assertEqual(status, 200)
        before = (self.inbox / "feedback.json").read_bytes()
        before_mtime = (self.inbox / "feedback.json").stat().st_mtime_ns
        status, _, _ = self.post_json("/__wk/feedback", self.batch())
        self.assertEqual(status, 200)
        self.assertEqual((self.inbox / "feedback.json").read_bytes(), before)
        self.assertEqual((self.inbox / "feedback.json").stat().st_mtime_ns, before_mtime)
        self.assertFalse((self.inbox / "verdicts.json").exists())

    def test_duplicate_feedback_id_must_keep_the_same_payload(self):
        original = self.batch()
        status, _, _ = self.post_json("/__wk/feedback", original)
        self.assertEqual(status, 200)
        before = (self.inbox / "feedback.json").read_bytes()

        conflict = self.batch()
        conflict["points"][0]["text"] = "different instruction"
        status, _, payload = self.post_json("/__wk/feedback", conflict)
        self.assertEqual(status, 409, payload)
        self.assertEqual((self.inbox / "feedback.json").read_bytes(), before)

    def test_feedback_schema_rejects_unsafe_client_shapes(self):
        def changed(mutator):
            value = json.loads(json.dumps(self.batch()))
            mutator(value)
            return value

        cases = {
            "wrong color": changed(lambda value: value.update(color="red")),
            "duplicate pages": changed(
                lambda value: value.update(pages=["/index.html", "/index.html"])
            ),
            "traversal page": changed(
                lambda value: (
                    value.update(pages=["/../secret"]),
                    value["points"][0].update(page="/../secret"),
                )
            ),
            "duplicate numbers": changed(
                lambda value: value["points"].append({
                    "id": "point-2", "number": 1,
                    "page": "/index.html", "text": "Second",
                })
            ),
            "oversized text": changed(
                lambda value: value["points"][0].update(text="x" * 10001)
            ),
            "malformed rect": changed(
                lambda value: value["points"][0].update(
                    rect={"x": 0, "y": 0, "w": 0, "h": 10}
                )
            ),
            "oversized context selector": changed(
                lambda value: value["points"][0].update(context=[{
                    "selector": "#" + "x" * 2048,
                    "tag": "div",
                    "text": "target",
                    "box": {"x": 0, "y": 0, "w": 10, "h": 10},
                    "role": "primary",
                }])
            ),
            "malformed context role": changed(
                lambda value: value["points"][0].update(context=[{
                    "selector": "#target",
                    "tag": "div",
                    "text": "target",
                    "box": {"x": 0, "y": 0, "w": 10, "h": 10},
                    "role": "execute",
                }])
            ),
            "invalid ABC count": changed(
                lambda value: value["points"][0].update(
                    abcRequest={"mode": "model", "count": 11}
                )
            ),
            "oversized ABC brief": changed(
                lambda value: value["points"][0].update(
                    abcRequest={"mode": "model", "count": 2, "brief": "x" * 4001}
                )
            ),
            "unknown point shape": changed(
                lambda value: value["points"][0].update(command={"run": "anything"})
            ),
        }
        for name, value in cases.items():
            with self.subTest(name=name):
                status, _, payload = self.post_json("/__wk/feedback", value)
                self.assertEqual(status, 400, payload)
                self.assertFalse((self.inbox / "feedback.json").exists())

    def test_feedback_allows_zero_size_context_boxes_but_not_zero_drawn_rects(self):
        value = self.batch()
        value["points"][0]["rect"] = {"x": 0, "y": 0, "w": 10, "h": 10}
        value["points"][0]["context"] = [{
            "selector": "#tiny",
            "tag": "span",
            "text": "",
            "box": {"x": 2, "y": 2, "w": 0, "h": 0},
            "role": "primary",
        }]
        status, _, payload = self.post_json("/__wk/feedback", value)
        self.assertEqual(status, 200, payload)

    def test_feedback_accepts_multiline_human_text_and_rejects_controls(self):
        for unsafe in ("\x00", "\x01", "\x0b", "\x7f", "\x85"):
            with self.subTest(unsafe=repr(unsafe)):
                value = self.batch()
                value["points"][0]["text"] = "unsafe" + unsafe + "text"
                self.assertIn(
                    "feedback point text",
                    self.module._feedback_schema_error(value),
                )

        value = self.batch()
        value["points"][0].update({
            "text": "First line\nSecond line\twith a tab\rThird line",
            "context": [{
                "selector": "#target",
                "tag": "div",
                "text": "First label\nSecond label",
                "box": {"x": 0, "y": 0, "w": 10, "h": 10},
                "role": "primary",
            }],
            "abcRequest": {
                "mode": "user",
                "brief": "Shared direction\nwith detail",
                "prompts": {
                    "A": "First option\nwith detail",
                    "B": "Second option\twith detail",
                },
            },
        })
        status, _, payload = self.post_json("/__wk/feedback", value)
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.read_data("feedback.json"), value)

    def test_json_rejects_unpaired_surrogates_but_accepts_emoji_pairs(self):
        for unsafe in ("\ud800", "\udfff"):
            with self.subTest(unsafe=ascii(unsafe)):
                value = self.batch()
                value["points"][0]["text"] = unsafe
                status, _, payload = self.post_json("/__wk/feedback", value)
                self.assertEqual(status, 400, payload)
                self.assertFalse((self.inbox / "feedback.json").exists())

        value = self.batch()
        value["points"][0]["text"] = "Looks good 😀"
        status, _, payload = self.post_json("/__wk/feedback", value)
        self.assertEqual(status, 200, payload)
        self.assertEqual(
            self.read_data("feedback.json")["points"][0]["text"],
            "Looks good 😀",
        )

        unsafe_agent_data = self.inbox / "surrogate-agent-data.json"
        unsafe_agent_data.write_text(
            r'{"safe": {"\ud800": "value"}}', encoding="utf-8"
        )
        self.assertIsNone(
            self.module._load_json_object_path(str(unsafe_agent_data))
        )

        valid_agent_data = self.inbox / "emoji-agent-data.json"
        valid_agent_data.write_text(
            r'{"emoji": "\ud83d\ude00"}', encoding="utf-8"
        )
        self.assertEqual(
            self.module._load_json_object_path(str(valid_agent_data)),
            {"emoji": "😀"},
        )

    def test_http_json_rejects_non_finite_constants_and_short_bodies(self):
        for constant in (b"NaN", b"Infinity", b"-Infinity"):
            with self.subTest(constant=constant):
                body = json.dumps(self.batch()).encode("utf-8").replace(b'"Fix it"', constant)
                status, _, _ = self.request(
                    "POST", "/__wk/feedback", body,
                    {
                        "Content-Type": "application/json",
                        "X-WK-Token": self.module.MUTATION_TOKEN,
                    },
                )
                self.assertEqual(status, 400)
                self.assertFalse((self.inbox / "feedback.json").exists())

        body = json.dumps(self.batch()).encode("utf-8")
        with socket.create_connection(("127.0.0.1", self.server.server_port), timeout=3) as sock:
            request = (
                "POST /__wk/feedback HTTP/1.1\r\n"
                "Host: 127.0.0.1:{}\r\n"
                "Content-Type: application/json\r\n"
                "X-WK-Token: {}\r\n"
                "Content-Length: {}\r\n"
                "Connection: close\r\n\r\n"
            ).format(
                self.server.server_port,
                self.module.MUTATION_TOKEN,
                len(body) + 10,
            ).encode("ascii") + body
            sock.sendall(request)
            sock.shutdown(socket.SHUT_WR)
            response = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response += chunk
        self.assertIn(b" 400 ", response.split(b"\r\n", 1)[0])
        self.assertFalse((self.inbox / "feedback.json").exists())

    def test_data_reads_ignore_symlinks_and_malformed_live_points_fail_closed(self):
        outside = self.root / "outside-feedback.json"
        outside.write_text(json.dumps(self.batch()), encoding="utf-8")
        link = self.inbox / "feedback.json"
        link.symlink_to(outside)
        self.assertIsNone(self.module._load_data("feedback.json"))
        self.assertEqual(self.module._mtime_ns("feedback.json"), 0)
        link.unlink()

        malformed = self.batch()
        malformed["points"] = [None]
        self.write_data("feedback.json", malformed)
        status, _, payload = self.post_json("/__wk/feedback", self.batch("point-2"))
        self.assertEqual(status, 409, payload)
        self.assertEqual(self.read_data("feedback.json"), malformed)

    def test_pre_review_feedback_update_is_persisted_and_accumulates(self):
        self.write_data("feedback.json", self.batch())
        status, _, _ = self.post_json("/__wk/feedback", self.batch("point-2"))
        self.assertEqual(status, 200)
        status, _, _ = self.post_json("/__wk/feedback", self.batch("point-3"))
        self.assertEqual(status, 200)
        self.assertEqual(
            [point["id"] for point in self.read_data("feedback.json")["points"]],
            ["point-1", "point-2", "point-3"],
        )
        self.assertEqual(
            self.read_data("verdicts.json")["addedPointIds"],
            ["point-2", "point-3"],
        )
        status, _, payload = self.state_request()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["phase"], "awaiting_agent")

    def test_pending_feedback_point_edit_is_atomic_revisioned_and_review_safe(self):
        batch = self.batch()
        batch["points"][0]["createdAt"] = "2026-08-22T10:00:00Z"
        status, _, payload = self.post_json("/__wk/feedback", batch)
        self.assertEqual(status, 200, payload)
        self.assertEqual(json.loads(payload)["points"][0]["id"], "point-1")

        invalid_fresh = self.batch("point-2")
        invalid_fresh["points"][0]["revision"] = 2
        status, _, payload = self.post_json("/__wk/feedback", invalid_fresh)
        self.assertEqual(status, 400, payload)
        self.assertIn(b"assigned by the preview server", payload)

        edited = dict(batch["points"][0])
        edited["text"] = "Use the corrected instruction"
        edited["number"] = 999
        edited["page"] = "/attacker-controlled.html"
        edited["createdAt"] = "replaced"
        request = {
            "version": 1,
            "kind": "feedback_edit",
            "batchId": "batch-1",
            "round": 1,
            "pointId": "point-1",
            "expectedRevision": 1,
            "point": edited,
        }
        status, _, payload = self.post_json("/__wk/feedback/edit", request, token=False)
        self.assertEqual(status, 403, payload)
        self.assertEqual(self.read_data("feedback.json"), batch)

        status, _, payload = self.post_json("/__wk/feedback/edit", request)
        self.assertEqual(status, 200, payload)
        updated = json.loads(payload)["point"]
        self.assertEqual(updated["revision"], 2)
        self.assertEqual(updated["text"], "Use the corrected instruction")
        self.assertEqual(updated["number"], 1)
        self.assertEqual(updated["page"], "/index.html")
        self.assertEqual(updated["createdAt"], "2026-08-22T10:00:00Z")
        self.assertEqual(self.read_data("feedback.json")["points"], [updated])
        self.assertEqual(self.read_data("verdicts.json")["addedPointIds"], ["point-1"])

        status, _, payload = self.state_request()
        state = json.loads(payload)
        self.assertEqual(status, 200, payload)
        self.assertEqual(state["phase"], "awaiting_agent")
        self.assertEqual(state["pendingPointIds"], ["point-1"])

        stale = dict(request)
        stale["point"] = dict(edited, text="stale overwrite")
        status, _, payload = self.post_json("/__wk/feedback/edit", stale)
        self.assertEqual(status, 409, payload)
        self.assertEqual(self.read_data("feedback.json")["points"], [updated])

        review = self.review()
        review["points"][0]["feedbackRevision"] = 2
        self.write_data("review.json", review)
        current = dict(request, expectedRevision=2)
        current["point"] = dict(edited, text="too late")
        status, _, payload = self.post_json("/__wk/feedback/edit", current)
        self.assertEqual(status, 409, payload)
        self.assertIn(b"already entered review", payload)

    def test_feedback_edit_revision_forces_stale_agent_work_into_next_review(self):
        status, _, _ = self.post_json("/__wk/feedback", self.batch())
        self.assertEqual(status, 200)
        edited = dict(self.batch()["points"][0], text="Latest instruction")
        status, _, payload = self.post_json("/__wk/feedback/edit", {
            "version": 1,
            "kind": "feedback_edit",
            "batchId": "batch-1",
            "round": 1,
            "pointId": "point-1",
            "expectedRevision": 1,
            "point": edited,
        })
        self.assertEqual(status, 200, payload)

        stale_review = self.review()
        self.write_data("review.json", stale_review)
        status, _, payload = self.state_request()
        self.assertEqual(status, 200, payload)
        self.assertEqual(json.loads(payload)["phase"], "transitioning")

        next_review = self.review(round_number=2)
        next_review["points"][0]["feedbackRevision"] = 2
        status, _, payload = self.post_transition(
            self.transition("feedback-update", next_review=next_review)
        )
        self.assertEqual(status, 200, payload)
        response = json.loads(payload)
        self.assertEqual(response["nextRound"], 2)
        self.assertEqual(response["missingPoints"][0]["revision"], 2)
        self.assertEqual(self.read_data("review.json")["points"][0]["feedbackRevision"], 2)
        status, _, payload = self.state_request()
        self.assertEqual(status, 200, payload)
        self.assertEqual(json.loads(payload)["pendingPointIds"], [])

    def test_feedback_rectangle_contexts_match_each_rectangle(self):
        batch = self.batch()
        point = batch["points"][0]
        point["rect"] = {"x": 10, "y": 20, "w": 30, "h": 40}
        point["rects"] = [point["rect"], {"x": 50, "y": 60, "w": 70, "h": 80}]
        context = [{
            "selector": "#target",
            "tag": "div",
            "text": "Target",
            "box": {"x": 8, "y": 18, "w": 40, "h": 50},
            "role": "primary",
        }]
        point["rectContexts"] = [context, context]
        surface = {
            "geometrySelector": "#target",
            "targetSelector": ".app-setup",
            "anchor": {"selector": "#phone-stage", "mode": "sticky"},
            "scroll": {"x": 0, "y": 5107},
            "stateChain": [{
                "selector": "#phone-stage",
                "attrs": {"data-state": "setup"},
                "classes": ["is-active"],
            }],
        }
        self.assertTrue(self.module._valid_rect_surface({
            key: value for key, value in surface.items() if key != "scroll"
        }))
        point["rectSurfaces"] = [surface, surface]
        status, _, payload = self.post_json("/__wk/feedback", batch)
        self.assertEqual(status, 200, payload)

        invalid = self.batch("point-2")
        invalid_point = invalid["points"][0]
        invalid_point["rect"] = {"x": 10, "y": 20, "w": 30, "h": 40}
        invalid_point["rects"] = [invalid_point["rect"]]
        invalid_point["rectContexts"] = [context, context]
        self.assertEqual(
            self.module._feedback_schema_error(invalid),
            "feedback point rectangle contexts are invalid",
        )

        invalid_surface = self.batch("point-3")
        invalid_surface_point = invalid_surface["points"][0]
        invalid_surface_point["rect"] = {"x": 10, "y": 20, "w": 30, "h": 40}
        invalid_surface_point["rects"] = [invalid_surface_point["rect"]]
        invalid_surface_point["rectSurfaces"] = [{
            **surface,
            "stateChain": [{
                "selector": "#phone-stage",
                "attrs": {"data-state": "setup"},
                "classes": ["is-active", "is-active"],
            }],
        }]
        self.assertEqual(
            self.module._feedback_schema_error(invalid_surface),
            "feedback point rectangle surfaces are invalid",
        )

        invalid_class = self.batch("point-class")
        invalid_class_point = invalid_class["points"][0]
        invalid_class_point["rect"] = {"x": 10, "y": 20, "w": 30, "h": 40}
        invalid_class_point["rects"] = [invalid_class_point["rect"]]
        invalid_class_point["rectSurfaces"] = [{
            **surface,
            "stateChain": [{
                "selector": "#phone-stage",
                "attrs": {"data-state": "setup"},
                "classes": ["active state"],
            }],
        }]
        self.assertEqual(
            self.module._feedback_schema_error(invalid_class),
            "feedback point rectangle surfaces are invalid",
        )

        malformed_class = self.batch("point-malformed-class")
        malformed_class_point = malformed_class["points"][0]
        malformed_class_point["rect"] = {"x": 10, "y": 20, "w": 30, "h": 40}
        malformed_class_point["rects"] = [malformed_class_point["rect"]]
        malformed_class_point["rectSurfaces"] = [{
            **surface,
            "stateChain": [{
                "selector": "#phone-stage",
                "attrs": {"data-state": "setup"},
                "classes": [{}],
            }],
        }]
        self.assertEqual(
            self.module._feedback_schema_error(malformed_class),
            "feedback point rectangle surfaces are invalid",
        )

        invalid_scroll = self.batch("point-4")
        invalid_scroll_point = invalid_scroll["points"][0]
        invalid_scroll_point["rect"] = {"x": 10, "y": 20, "w": 30, "h": 40}
        invalid_scroll_point["rects"] = [invalid_scroll_point["rect"]]
        invalid_scroll_point["rectSurfaces"] = [{
            **surface,
            "scroll": {"x": 0, "y": 10000001},
        }]
        self.assertEqual(
            self.module._feedback_schema_error(invalid_scroll),
            "feedback point rectangle surfaces are invalid",
        )

    def test_existing_feedback_rejects_batch_and_round_skew_but_allows_round_two_additions(self):
        live = self.batch()
        live["round"] = 2
        self.write_data("feedback.json", live)

        stale_batch = self.batch("point-2")
        stale_batch["batchId"] = "batch-stale"
        stale_batch["round"] = 2
        status, _, _ = self.post_json("/__wk/feedback", stale_batch)
        self.assertEqual(status, 409)

        stale_round = self.batch("point-2")
        status, _, _ = self.post_json("/__wk/feedback", stale_round)
        self.assertEqual(status, 409)
        self.assertEqual([point["id"] for point in self.read_data("feedback.json")["points"]], ["point-1"])

        current = self.batch("point-2")
        current["round"] = 2
        current["pages"] = ["/other.html"]
        current["points"][0]["page"] = "/other.html"
        status, _, _ = self.post_json("/__wk/feedback", current)
        self.assertEqual(status, 200)
        merged = self.read_data("feedback.json")
        self.assertEqual([point["id"] for point in merged["points"]], ["point-1", "point-2"])
        self.assertEqual(merged["pages"], ["/index.html", "/other.html"])

    def test_feedback_and_redo_voice_notes_must_be_safe_existing_uploads(self):
        notes = self.inbox / "voice-notes"
        notes.mkdir()
        valid = notes / "voice-123456.webm"
        valid.write_bytes(b"audio")
        valid_note = {
            "path": os.path.relpath(valid, self.module.GIT_ROOT).replace(
                os.sep, "/"
            ),
            "mimeType": "audio/webm",
            "bytes": 5,
            "transcription": "openai",
        }
        feedback = self.batch()
        feedback["points"][0]["voiceNote"] = valid_note
        status, _, _ = self.post_json("/__wk/feedback", feedback)
        self.assertEqual(status, 200)

        (self.inbox / "feedback.json").unlink()
        feedback["batchId"] = "batch-invalid-engine"
        feedback["points"][0]["voiceNote"]["transcription"] = "anthropic"
        status, _, _ = self.post_json("/__wk/feedback", feedback)
        self.assertEqual(status, 400)

        outside = self.root / "outside.webm"
        outside.write_bytes(b"audio")
        feedback["batchId"] = "batch-2"
        feedback["points"][0]["voiceNote"] = {
            "path": os.path.relpath(outside, self.module.GIT_ROOT)
        }
        status, _, _ = self.post_json("/__wk/feedback", feedback)
        self.assertEqual(status, 400)

        link = notes / "voice-654321.webm"
        link.symlink_to(outside)
        feedback["points"][0]["voiceNote"] = {
            "path": os.path.relpath(link, self.module.GIT_ROOT)
        }
        status, _, _ = self.post_json("/__wk/feedback", feedback)
        self.assertEqual(status, 400)

        self.write_data("feedback.json", self.batch())
        self.write_data("review.json", self.review())
        verdicts = self.verdicts((("point-1", "redo"),))
        verdicts["verdicts"][0]["redoVoiceNote"] = {
            "path": os.path.relpath(outside, self.module.GIT_ROOT)
        }
        status, _, _ = self.post_json("/__wk/verdicts", verdicts)
        self.assertEqual(status, 400)
        self.assertFalse((self.inbox / "verdicts.json").exists())

    def test_voice_metadata_matches_file_and_extension_for_feedback_and_redo(self):
        notes = self.inbox / "voice-notes"
        notes.mkdir()
        note_path = notes / "voice-123456.webm"
        note_path.write_bytes(b"audio")
        relative = os.path.relpath(note_path, self.module.GIT_ROOT)

        for mutation in (
            {"path": relative, "mimeType": "audio/webm", "bytes": 6},
            {"path": relative, "mimeType": "audio/ogg", "bytes": 5},
        ):
            with self.subTest(feedback=mutation):
                value = self.batch()
                value["points"][0]["voiceNote"] = mutation
                status, _, payload = self.post_json("/__wk/feedback", value)
                self.assertEqual(status, 400, payload)
                self.assertFalse((self.inbox / "feedback.json").exists())

        self.write_data("feedback.json", self.batch())
        self.write_data("review.json", self.review())
        verdicts = self.verdicts((("point-1", "redo"),))
        verdicts["verdicts"][0]["redoVoiceNote"] = {
            "path": relative, "mimeType": "audio/webm", "bytes": 6,
        }
        status, _, payload = self.post_json("/__wk/verdicts", verdicts)
        self.assertEqual(status, 400, payload)
        self.assertFalse((self.inbox / "verdicts.json").exists())

    def test_voice_upload_is_create_only_and_live_references_block_delete(self):
        path = "/__wk/voice-note?id=voice-123456"
        headers = {
            "Content-Type": "audio/webm",
            "X-WK-Token": self.module.MUTATION_TOKEN,
        }
        status, _, payload = self.request("POST", path, b"audio", headers)
        self.assertEqual(status, 201, payload)
        note = json.loads(payload)["voiceNote"]
        stored = self.root / note["path"]
        self.assertEqual(stored.read_bytes(), b"audio")

        status, _, payload = self.request("POST", path, b"audio", headers)
        self.assertEqual(status, 200, payload)
        self.assertTrue(json.loads(payload)["idempotent"])

        status, _, payload = self.request("POST", path, b"other", headers)
        self.assertEqual(status, 409, payload)
        self.assertEqual(stored.read_bytes(), b"audio")

        feedback = self.batch()
        feedback["points"][0]["voiceNote"] = note
        status, _, payload = self.post_json("/__wk/feedback", feedback)
        self.assertEqual(status, 200, payload)
        status, _, payload = self.post_json(
            "/__wk/voice-note/delete", {"path": note["path"]}
        )
        self.assertEqual(status, 409, payload)
        self.assertTrue(stored.is_file())

    def test_voice_gc_reclaims_old_orphans_but_preserves_recent_and_live_uploads(self):
        notes = self.inbox / "voice-notes"
        notes.mkdir()
        old = notes / "voice-old123.webm"
        recent = notes / "voice-new123.webm"
        live = notes / "voice-live12.webm"
        old.write_bytes(b"old")
        recent.write_bytes(b"recent")
        live.write_bytes(b"live")
        live_relative = os.path.relpath(live, self.module.GIT_ROOT).replace(os.sep, "/")
        feedback = self.batch()
        feedback["points"][0]["voiceNote"] = {
            "path": live_relative,
            "mimeType": "audio/webm",
            "bytes": 4,
        }
        self.write_data("feedback.json", feedback)
        now = time.time()
        os.utime(old, (now - 7200, now - 7200))
        os.utime(live, (now - 7200, now - 7200))

        remaining = self.module._gc_voice_notes(now=now, grace_seconds=3600)

        self.assertFalse(old.exists())
        self.assertTrue(recent.is_file())
        self.assertTrue(live.is_file())
        self.assertEqual(remaining, recent.stat().st_size + live.stat().st_size)

    def test_voice_uploads_obey_the_total_session_storage_cap(self):
        headers = {
            "Content-Type": "audio/webm",
            "X-WK-Token": self.module.MUTATION_TOKEN,
        }
        with mock.patch.object(self.module, "_MAX_VOICE_DIRECTORY_BYTES", 8):
            first, _, _ = self.request(
                "POST", "/__wk/voice-note?id=voice-first1", b"audio", headers
            )
            second, _, payload = self.request(
                "POST", "/__wk/voice-note?id=voice-second", b"other", headers
            )
        self.assertEqual(first, 201)
        self.assertEqual(second, 507, payload)
        self.assertFalse((self.inbox / "voice-notes" / "voice-second.webm").exists())

    def test_voice_uploads_prospectively_obey_the_file_count_cap(self):
        notes = self.inbox / "voice-notes"
        notes.mkdir()
        for index in range(12):
            (notes / "voice-tiny{:03d}.webm".format(index)).write_bytes(b"x")
        headers = {
            "Content-Type": "audio/webm",
            "X-WK-Token": self.module.MUTATION_TOKEN,
        }
        with mock.patch.object(self.module, "_MAX_VOICE_FILES", 12), \
                mock.patch.object(self.module, "_MAX_VOICE_SCAN_ENTRIES", 16):
            status, _, payload = self.request(
                "POST", "/__wk/voice-note?id=voice-overflow", b"x", headers
            )
        self.assertEqual(status, 507, payload)
        self.assertIn(b"12-file session limit", payload)
        self.assertFalse((notes / "voice-overflow.webm").exists())

    def test_voice_uploads_reserve_scan_headroom_before_reading_a_new_file(self):
        notes = self.inbox / "voice-notes"
        notes.mkdir()
        for index in range(12):
            (notes / "voice-tiny{:03d}.webm".format(index)).write_bytes(b"x")
        headers = {
            "Content-Type": "audio/webm",
            "X-WK-Token": self.module.MUTATION_TOKEN,
        }
        with mock.patch.object(self.module, "_MAX_VOICE_FILES", 100), \
                mock.patch.object(self.module, "_MAX_VOICE_SCAN_ENTRIES", 13):
            status, _, payload = self.request(
                "POST", "/__wk/voice-note?id=voice-overflow", b"x", headers
            )
        self.assertEqual(status, 507, payload)
        self.assertIn(b"reserve safe temporary entries", payload)
        self.assertFalse((notes / "voice-overflow.webm").exists())

    def test_voice_gc_bounds_many_tiny_entries_and_rejects_unknown_files(self):
        notes = self.inbox / "voice-notes"
        notes.mkdir()
        for index in range(33):
            (notes / "voice-tiny{:03d}.webm".format(index)).write_bytes(b"x")
        with mock.patch.object(self.module, "_MAX_VOICE_FILES", 100), \
                mock.patch.object(self.module, "_MAX_VOICE_SCAN_ENTRIES", 32):
            with self.assertRaisesRegex(
                self.module._VoiceStorageError, "32-entry scan limit"
            ):
                self.module._gc_voice_notes()

        for path in notes.iterdir():
            path.unlink()
        unexpected = notes / "unmanaged.bin"
        unexpected.write_bytes(b"x")
        with self.assertRaisesRegex(
            self.module._VoiceStorageError, "unexpected entry"
        ):
            self.module._gc_voice_notes()
        self.assertEqual(unexpected.read_bytes(), b"x")

    def test_transition_cleans_voice_notes_only_after_archive_and_retries_cleanup(self):
        def upload(note_id, body):
            status, _, payload = self.request(
                "POST",
                "/__wk/voice-note?id=" + note_id,
                body,
                {
                    "Content-Type": "audio/webm",
                    "X-WK-Token": self.module.MUTATION_TOKEN,
                },
            )
            self.assertEqual(status, 201, payload)
            return json.loads(payload)["voiceNote"]

        initial = upload("voice-123456", b"initial")
        redo = upload("voice-654321", b"redo")
        initial_path = self.root / initial["path"]
        redo_path = self.root / redo["path"]

        feedback = self.batch()
        feedback["points"][0]["voiceNote"] = initial
        status, _, payload = self.post_json("/__wk/feedback", feedback)
        self.assertEqual(status, 200, payload)
        self.write_data("review.json", self.review())
        verdicts = self.verdicts((("point-1", "redo"),))
        verdicts["verdicts"][0]["redoVoiceNote"] = redo
        status, _, payload = self.post_json("/__wk/verdicts", verdicts)
        self.assertEqual(status, 200, payload)

        status, _, payload = self.post_transition(
            self.transition("redo", next_review=self.review(round_number=2))
        )
        self.assertEqual(status, 200, payload)
        first_receipt = self.read_data("history/batch-1-r1/transition.json")
        self.assertEqual(
            first_receipt["voiceNotePaths"],
            sorted((initial["path"], redo["path"])),
        )
        self.assertTrue(initial_path.is_file())
        self.assertFalse(redo_path.exists())

        status, _, payload = self.post_json(
            "/__wk/verdicts", self.verdicts(round_number=2)
        )
        self.assertEqual(status, 200, payload)
        original_unlink = self.module.os.unlink

        def block_initial_cleanup(path):
            if self.module._canonical_path(path) == self.module._canonical_path(
                initial_path
            ):
                raise OSError("simulated cleanup interruption")
            return original_unlink(path)

        with mock.patch.object(
            self.module.os, "unlink", side_effect=block_initial_cleanup
        ):
            status, _, payload = self.post_transition(
                self.transition("complete", round_number=2)
            )
        self.assertEqual(status, 503, payload)
        cleanup_response = json.loads(payload)
        self.assertTrue(cleanup_response["transitionDurable"])
        self.assertEqual(cleanup_response["voiceCleanupPending"], [initial["path"]])
        self.assertTrue(
            (self.inbox / "history" / "batch-1-r2" / "transition.json").is_file()
        )
        self.assertTrue(initial_path.is_file())

        status, _, payload = self.post_transition(
            self.transition("complete", round_number=2)
        )
        self.assertEqual(status, 200, payload)
        self.assertTrue(json.loads(payload)["idempotent"])
        self.assertFalse(initial_path.exists())

    def test_review_commit_semantics_match_handled_state(self):
        feedback = self.batch()
        valid = self.review()
        self.assertIsNone(self.module._review_schema_error(valid, feedback))

        for length in (40, 64):
            with self.subTest(valid_sha_length=length):
                candidate = self.review()
                candidate["beforeRef"] = "a" * length
                candidate["points"][0]["commit"] = "b" * length
                self.assertIsNone(
                    self.module._review_schema_error(candidate, feedback)
                )

        for length in (7, 39, 41, 63, 65):
            with self.subTest(invalid_before_ref_length=length):
                candidate = self.review()
                candidate["beforeRef"] = "a" * length
                self.assertEqual(
                    self.module._review_schema_error(candidate, feedback),
                    "review beforeRef is invalid",
                )
            with self.subTest(invalid_commit_length=length):
                candidate = self.review()
                candidate["points"][0]["commit"] = "b" * length
                self.assertIn(
                    "require a full commit SHA",
                    self.module._review_schema_error(candidate, feedback),
                )

        missing = self.review()
        missing["points"][0].pop("commit")
        self.assertIn(
            "require a full commit SHA",
            self.module._review_schema_error(missing, feedback),
        )

        abc = self.review()
        abc["points"][0].update({
            "handled": "abc",
            "abc": {"scopeId": "hero", "letters": "AB"},
        })
        self.assertIsNone(self.module._review_schema_error(abc, feedback))

        skipped = self.review()
        skipped["points"][0]["handled"] = "skipped"
        self.assertIn(
            "must be omitted or null",
            self.module._review_schema_error(skipped, feedback),
        )
        skipped["points"][0].pop("commit")
        self.assertIsNone(self.module._review_schema_error(skipped, feedback))

    def test_atomic_writers_sync_file_before_replace_and_directory_after(self):
        named_target = self.inbox / "durable-named.json"
        path_target = self.root / "durable-path.json"
        cases = (
            (
                "named",
                named_target,
                lambda: self.module._atomic_write(
                    named_target.name, {"writer": "named"}
                ),
            ),
            (
                "path",
                path_target,
                lambda: self.module._atomic_write_path(
                    str(path_target), {"writer": "path"}
                ),
            ),
        )
        original_fsync = self.module.os.fsync
        original_replace = self.module.os.replace

        for label, target, writer in cases:
            with self.subTest(writer=label):
                events = []

                def record_fsync(descriptor):
                    mode = os.fstat(descriptor).st_mode
                    kind = "file" if stat.S_ISREG(mode) else "directory"
                    events.append(("fsync", kind))
                    return original_fsync(descriptor)

                def record_replace(source, destination):
                    events.append(("replace", os.path.realpath(destination)))
                    return original_replace(source, destination)

                with mock.patch.object(
                    self.module.os, "fsync", side_effect=record_fsync
                ), mock.patch.object(
                    self.module.os, "replace", side_effect=record_replace
                ):
                    writer()

                self.assertEqual(events[0], ("fsync", "file"))
                self.assertEqual(
                    events[1], ("replace", os.path.realpath(target))
                )
                if os.name == "nt":
                    self.assertEqual(len(events), 2)
                else:
                    self.assertEqual(events[2], ("fsync", "directory"))
                self.assertTrue(target.is_file())

    def test_atomic_writers_leave_old_target_and_clean_temp_on_fsync_failure(self):
        named_target = self.inbox / "failure-named.json"
        path_target = self.root / "failure-path.json"
        cases = (
            (
                "named",
                named_target,
                lambda: self.module._atomic_write(
                    named_target.name, {"version": "new"}
                ),
            ),
            (
                "path",
                path_target,
                lambda: self.module._atomic_write_path(
                    str(path_target), {"version": "new"}
                ),
            ),
        )
        original_replace = self.module.os.replace

        for label, target, writer in cases:
            with self.subTest(writer=label):
                old = '{"version":"old"}\n'
                target.write_text(old, encoding="utf-8")
                temporary_pattern = ".{}.*.tmp".format(target.name)
                with mock.patch.object(
                    self.module.os,
                    "fsync",
                    side_effect=OSError("simulated file fsync failure"),
                ), mock.patch.object(
                    self.module.os, "replace", wraps=original_replace
                ) as replace:
                    with self.assertRaisesRegex(OSError, "simulated file fsync"):
                        writer()

                replace.assert_not_called()
                self.assertEqual(target.read_text(encoding="utf-8"), old)
                self.assertEqual(list(target.parent.glob(temporary_pattern)), [])

    def test_verdicts_require_safe_unique_exact_review_ids_and_known_values(self):
        self.write_data("feedback.json", self.batch())
        self.write_data("review.json", self.review())
        cases = [
            [],
            [{"pointId": "point-1", "verdict": "accept"}, {"pointId": "point-1", "verdict": "delete"}],
            [{"pointId": "../../bad", "verdict": "accept"}],
            [{"pointId": "point-1", "verdict": "execute"}],
            [{"pointId": "point-2", "verdict": "accept"}],
        ]
        for items in cases:
            with self.subTest(items=items):
                value = self.verdicts()
                value["verdicts"] = items
                status, _, _ = self.post_json("/__wk/verdicts", value)
                self.assertEqual(status, 400)
                self.assertFalse((self.inbox / "verdicts.json").exists())

        status, _, _ = self.post_json("/__wk/verdicts", self.verdicts())
        self.assertEqual(status, 200)

    def test_verdict_fields_follow_active_review_metadata(self):
        self.write_data("feedback.json", self.batch())
        self.write_data("review.json", self.review())
        invalid_items = (
            {"pointId": "point-1", "verdict": "accept", "chosenLetter": "A"},
            {"pointId": "point-1", "verdict": "accept", "redoText": "again"},
            {"pointId": "point-1", "verdict": "accept", "redoAbcRequest": {"mode": "model", "count": 4}},
            {"pointId": "point-1", "verdict": "delete", "chosenLetter": "A"},
            {"pointId": "point-1", "verdict": "delete", "redoVoiceNote": None},
            {"pointId": "point-1", "verdict": "redo", "redoText": 3},
            {"pointId": "point-1", "verdict": "redo", "redoText": "x" * 10001},
            {"pointId": "point-1", "verdict": "redo", "redoAbcRequest": {"mode": "model", "count": 1}},
        )
        for item in invalid_items:
            with self.subTest(item=item):
                value = self.verdicts()
                value["verdicts"] = [item]
                status, _, _ = self.post_json("/__wk/verdicts", value)
                self.assertEqual(status, 400)
                self.assertFalse((self.inbox / "verdicts.json").exists())

        abc_review = self.review()
        abc_review["points"][0].update({
            "handled": "abc",
            "abc": {"scopeId": "hero", "letters": "ABC"},
        })
        self.write_data("review.json", abc_review)
        for chosen in (None, "", "AB", "D"):
            with self.subTest(chosen=chosen):
                item = {"pointId": "point-1", "verdict": "accept"}
                if chosen is not None:
                    item["chosenLetter"] = chosen
                value = self.verdicts()
                value["verdicts"] = [item]
                status, _, _ = self.post_json("/__wk/verdicts", value)
                self.assertEqual(status, 400)

        accepted = self.verdicts()
        accepted["verdicts"][0]["chosenLetter"] = "B"
        status, _, _ = self.post_json("/__wk/verdicts", accepted)
        self.assertEqual(status, 200)

    def test_redo_accepts_a_valid_fresh_variant_request(self):
        self.write_data("feedback.json", self.batch())
        self.write_data("review.json", self.review())
        value = self.verdicts((("point-1", "redo"),))
        value["verdicts"][0]["redoAbcRequest"] = {"mode": "model", "count": 4}
        status, _, payload = self.post_json("/__wk/verdicts", value)
        self.assertEqual(status, 200, payload)
        self.assertEqual(
            self.read_data("verdicts.json")["verdicts"][0]["redoAbcRequest"],
            {"mode": "model", "count": 4},
        )

    def test_redo_accepts_multiline_human_text_and_rejects_controls(self):
        self.write_data("feedback.json", self.batch())
        self.write_data("review.json", self.review())

        unsafe = self.verdicts((("point-1", "redo"),))
        unsafe["verdicts"][0]["redoText"] = "unsafe\x00text"
        status, _, payload = self.post_json("/__wk/verdicts", unsafe)
        self.assertEqual(status, 400, payload)
        self.assertFalse((self.inbox / "verdicts.json").exists())

        valid = self.verdicts((("point-1", "redo"),))
        valid["verdicts"][0]["redoText"] = (
            "Please keep the layout.\nChange the heading\tand spacing.\rThanks."
        )
        status, _, payload = self.post_json("/__wk/verdicts", valid)
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.read_data("verdicts.json"), valid)

    def test_voice_upload_without_token_is_rejected(self):
        status, _, _ = self.request(
            "POST", "/__wk/voice-note?id=voice-123456", b"audio",
            {"Content-Type": "audio/webm"},
        )
        self.assertEqual(status, 403)
        self.assertFalse((self.inbox / "voice-notes").exists())

    def test_voice_notes_symlink_cannot_redirect_upload_or_delete(self):
        with _WritableTemporaryDirectory() as outside:
            outside_path = Path(outside)
            (self.inbox / "voice-notes").symlink_to(outside_path, target_is_directory=True)

            status, _, _ = self.request(
                "POST", "/__wk/voice-note?id=voice-123456", b"audio",
                {
                    "Content-Type": "audio/webm",
                    "X-WK-Token": self.module.MUTATION_TOKEN,
                },
            )
            self.assertEqual(status, 409)
            self.assertFalse((outside_path / "voice-123456.webm").exists())

            target = outside_path / "voice-654321.webm"
            target.write_bytes(b"keep")
            relative = os.path.relpath(str(target), self.module.GIT_ROOT)
            status, _, _ = self.post_json(
                "/__wk/voice-note/delete", {"path": relative}
            )
            self.assertEqual(status, 409)
            self.assertEqual(target.read_bytes(), b"keep")

    def test_real_verdicts_cannot_be_overwritten_by_late_feedback(self):
        feedback = self.batch()
        review = {"version": 1, "batchId": "batch-1", "round": 1, "points": []}
        verdicts = {"version": 1, "kind": "verdicts", "batchId": "batch-1", "round": 1}
        self.write_data("feedback.json", feedback)
        self.write_data("review.json", review)
        self.write_data("verdicts.json", verdicts)
        before_feedback = (self.inbox / "feedback.json").read_bytes()
        before_verdicts = (self.inbox / "verdicts.json").read_bytes()

        status, _, _ = self.post_json("/__wk/feedback", self.batch("point-2"))

        self.assertEqual(status, 409)
        self.assertEqual((self.inbox / "feedback.json").read_bytes(), before_feedback)
        self.assertEqual((self.inbox / "verdicts.json").read_bytes(), before_verdicts)

    def test_verdict_file_blocks_feedback_through_archive_transition_windows(self):
        feedback = self.batch()
        verdicts = {"version": 1, "kind": "verdicts", "batchId": "batch-1", "round": 1}
        self.write_data("feedback.json", feedback)
        self.write_data("verdicts.json", verdicts)
        before_feedback = (self.inbox / "feedback.json").read_bytes()

        status, _, _ = self.post_json("/__wk/feedback", self.batch("point-2"))

        self.assertEqual(status, 409)
        self.assertEqual((self.inbox / "feedback.json").read_bytes(), before_feedback)

        (self.inbox / "feedback.json").unlink()
        self.write_data("review.json", {
            "version": 1, "batchId": "batch-1", "round": 1, "points": [],
        })
        status, _, _ = self.post_json("/__wk/feedback", self.batch("point-3"))
        self.assertEqual(status, 409)
        self.assertFalse((self.inbox / "feedback.json").exists())

    def test_completed_batch_id_cannot_be_resurrected_from_history(self):
        archived = self.inbox / "history" / "batch-1-r1"
        archived.mkdir(parents=True)
        (archived / "feedback.json").write_text(
            json.dumps(self.batch()), encoding="utf-8"
        )

        status, _, _ = self.post_json("/__wk/feedback", self.batch())
        self.assertEqual(status, 409)
        self.assertFalse((self.inbox / "feedback.json").exists())

        fresh = self.batch("point-2")
        fresh["batchId"] = "batch-2"
        status, _, _ = self.post_json("/__wk/feedback", fresh)
        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads((self.inbox / "feedback.json").read_text(encoding="utf-8"))["batchId"],
            "batch-2",
        )

    def test_feedback_updates_accumulate_without_erasing_earlier_ids(self):
        self.write_data("feedback.json", self.batch())
        self.write_data("review.json", self.review())
        self.write_data("verdicts.json", {
            "version": 1, "kind": "feedback_update", "batchId": "batch-1",
            "round": 1, "addedPointIds": ["point-old"],
        })

        status, _, _ = self.post_json("/__wk/feedback", self.batch("point-2"))

        self.assertEqual(status, 200)
        verdicts = json.loads((self.inbox / "verdicts.json").read_text(encoding="utf-8"))
        self.assertEqual(verdicts["addedPointIds"], ["point-old", "point-2"])

    def test_pending_feedback_update_blocks_verdict_submission(self):
        self.write_data("review.json", {
            "version": 1, "batchId": "batch-1", "round": 1, "points": [],
        })
        pending = {
            "version": 1, "kind": "feedback_update", "batchId": "batch-1",
            "round": 1, "addedPointIds": ["point-2"],
        }
        self.write_data("verdicts.json", pending)
        status, _, _ = self.post_json("/__wk/verdicts", {
            "version": 1, "kind": "verdicts", "batchId": "batch-1", "round": 1,
            "verdicts": [],
        })
        self.assertEqual(status, 409)
        self.assertEqual(
            json.loads((self.inbox / "verdicts.json").read_text(encoding="utf-8")), pending
        )

    def test_first_real_verdict_submission_is_immutable(self):
        self.write_data("feedback.json", self.batch())
        self.write_data("review.json", self.review())
        first = self.verdicts()
        second = dict(first)
        second["verdicts"] = [{"pointId": "point-1", "verdict": "redo"}]
        self.write_data("verdicts.json", first)
        before = (self.inbox / "verdicts.json").read_bytes()

        status, _, _ = self.post_json("/__wk/verdicts", second)
        self.assertEqual(status, 409)
        self.assertEqual((self.inbox / "verdicts.json").read_bytes(), before)

        status, _, _ = self.post_json("/__wk/verdicts", first)
        self.assertEqual(status, 200)
        self.assertEqual((self.inbox / "verdicts.json").read_bytes(), before)

    def test_transition_uses_separate_token_and_rejects_skew_without_mutation(self):
        self.write_data("feedback.json", self.batch())
        self.write_data("review.json", self.review())
        self.write_data("verdicts.json", self.verdicts())
        before = {
            name: (self.inbox / name).read_bytes()
            for name in ("feedback.json", "review.json", "verdicts.json")
        }
        status, _, _ = self.post_json("/__wk/transition", self.transition("complete"))
        self.assertEqual(status, 403)
        status, _, _ = self.post_transition(self.transition("complete"), token=False)
        self.assertEqual(status, 403)
        skew = self.transition("complete")
        skew["round"] = 2
        status, _, _ = self.post_transition(skew)
        self.assertEqual(status, 409)
        self.assertEqual(
            {name: (self.inbox / name).read_bytes() for name in before}, before
        )

    def test_transition_helper_uses_the_private_server_capability(self):
        self.write_data("feedback.json", self.batch())
        self.write_data("review.json", self.review())
        self.write_data("verdicts.json", self.verdicts())
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        config["palette"][0]["port"] = self.server.server_port
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        environment = dict(os.environ)
        environment["WK_CONFIG"] = str(self.config_path)
        result = subprocess.run(
            [
                sys.executable,
                str(TRANSITION_HELPER),
                "blue",
                "complete",
                "batch-1",
                "1",
            ],
            cwd=self.root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["ok"])
        self.assertFalse((self.inbox / "feedback.json").exists())

    def test_lost_pre_review_feedback_is_returned_then_installed_atomically(self):
        self.write_data("feedback.json", self.batch())
        status, _, _ = self.post_json("/__wk/feedback", self.batch("point-2"))
        self.assertEqual(status, 200)
        self.write_data("review.json", self.review())

        status, _, payload = self.post_transition(self.transition("feedback-update"))
        self.assertEqual(status, 409)
        conflict = json.loads(payload)
        self.assertEqual([point["id"] for point in conflict["missingPoints"]], ["point-2"])
        self.assertTrue((self.inbox / "verdicts.json").exists())

        next_review = self.review(("point-1", "point-2"), round_number=2)
        status, _, payload = self.post_transition(
            self.transition("feedback-update", next_review=next_review)
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.read_data("feedback.json")["round"], 2)
        self.assertEqual(self.read_data("review.json"), next_review)
        self.assertFalse((self.inbox / "verdicts.json").exists())
        archived = self.inbox / "history" / "batch-1-r1"
        self.assertTrue((archived / "review.json").is_file())
        self.assertTrue((archived / "verdicts.json").is_file())

    def test_missing_feedback_uses_current_and_archived_review_manifests(self):
        archived = self.inbox / "history" / "batch-1-r1"
        archived.mkdir(parents=True)
        archived_review = self.review(("point-1",))
        (archived / "review.json").write_text(
            json.dumps(archived_review), encoding="utf-8"
        )
        feedback = self.batch()
        feedback["round"] = 2
        feedback["points"] = [
            {"id": point_id, "number": number, "page": "/index.html", "text": "Fix"}
            for number, point_id in enumerate(("point-1", "point-2", "point-3"), 1)
        ]
        self.write_data("feedback.json", feedback)
        self.write_data("review.json", self.review(("point-3",), round_number=2))
        self.write_data("verdicts.json", {
            "version": 1,
            "kind": "feedback_update",
            "batchId": "batch-1",
            "round": 2,
            "addedPointIds": ["point-2", "point-1"],
        })
        status, _, payload = self.post_transition(
            self.transition("feedback-update", round_number=2)
        )
        self.assertEqual(status, 409)
        self.assertEqual(
            [point["id"] for point in json.loads(payload)["missingPoints"]],
            ["point-2"],
        )

    def test_next_review_requires_the_exact_transition_point_set(self):
        feedback = self.batch()
        feedback["points"].append({
            "id": "point-2", "number": 2,
            "page": "/index.html", "text": "Second",
        })
        self.write_data("feedback.json", feedback)
        self.write_data("review.json", self.review())
        self.write_data("verdicts.json", {
            "version": 1,
            "kind": "feedback_update",
            "batchId": "batch-1",
            "round": 1,
            "addedPointIds": ["point-2"],
        })

        missing = self.review(("point-1",), round_number=2)
        status, _, payload = self.post_transition(
            self.transition("feedback-update", next_review=missing)
        )
        self.assertEqual(status, 409, payload)

        extra = self.review(("point-1", "point-2", "point-3"), round_number=2)
        status, _, payload = self.post_transition(
            self.transition("feedback-update", next_review=extra)
        )
        self.assertEqual(status, 409, payload)

        exact = self.review(("point-1", "point-2"), round_number=2)
        status, _, payload = self.post_transition(
            self.transition("feedback-update", next_review=exact)
        )
        self.assertEqual(status, 200, payload)

    def test_transition_revalidates_verdicts_and_never_completes_unreviewed_points(self):
        feedback = self.batch()
        feedback["points"].append({
            "id": "point-2", "number": 2,
            "page": "/index.html", "text": "Second",
        })
        self.write_data("feedback.json", feedback)
        self.write_data("review.json", self.review())
        self.write_data("verdicts.json", self.verdicts())
        status, _, payload = self.post_transition(self.transition("complete"))
        self.assertEqual(status, 409, payload)
        self.assertIn(b"missing from review history", payload)

        corrupted = self.verdicts()
        corrupted["verdicts"] = []
        self.write_data("verdicts.json", corrupted)
        status, _, payload = self.post_transition(self.transition("complete"))
        self.assertEqual(status, 409, payload)
        self.assertIn(b"persisted verdicts are invalid", payload)

    def test_malformed_review_fails_closed_for_state_verdicts_and_transition(self):
        feedback = self.batch()
        malformed = self.review()
        malformed.pop("kind")
        verdicts = self.verdicts()
        self.assertEqual(
            self.module._phase(feedback, malformed, verdicts), "transitioning"
        )
        self.write_data("feedback.json", feedback)
        self.write_data("review.json", malformed)
        status, _, payload = self.post_json("/__wk/verdicts", verdicts)
        self.assertEqual(status, 409, payload)
        self.write_data("verdicts.json", verdicts)
        status, _, payload = self.post_transition(self.transition("complete"))
        self.assertEqual(status, 409, payload)

        phantom = self.review(("point-phantom",))
        self.assertEqual(
            self.module._phase(feedback, phantom, None), "transitioning"
        )

    def test_redo_and_complete_transitions_have_receipts_and_are_idempotent(self):
        self.write_data("feedback.json", self.batch())
        self.write_data("review.json", self.review())
        self.write_data("verdicts.json", self.verdicts((("point-1", "redo"),)))
        next_review = self.review(round_number=2)
        status, _, payload = self.post_transition(
            self.transition("redo", next_review=next_review)
        )
        self.assertEqual(status, 200, payload)
        archived = self.inbox / "history" / "batch-1-r1"
        for name in ("feedback.json", "review.json", "verdicts.json", "transition.json"):
            self.assertTrue((archived / name).is_file(), name)
        self.assertEqual(self.read_data("feedback.json")["round"], 2)
        self.assertEqual(self.read_data("review.json")["round"], 2)
        self.assertFalse((self.inbox / "verdicts.json").exists())

        status, _, _ = self.post_json("/__wk/verdicts", self.verdicts(round_number=2))
        self.assertEqual(status, 200)
        status, _, payload = self.post_transition(self.transition("complete", round_number=2))
        self.assertEqual(status, 200, payload)
        self.assertFalse((self.inbox / "feedback.json").exists())
        self.assertFalse((self.inbox / "review.json").exists())
        self.assertFalse((self.inbox / "verdicts.json").exists())

        fresh = self.batch("point-new")
        fresh["batchId"] = "batch-new"
        self.write_data("feedback.json", fresh)
        before = (self.inbox / "feedback.json").read_bytes()
        status, _, payload = self.post_transition(self.transition("complete", round_number=2))
        self.assertEqual(status, 200, payload)
        self.assertTrue(json.loads(payload)["idempotent"])
        self.assertEqual((self.inbox / "feedback.json").read_bytes(), before)

    def test_feedback_update_history_names_do_not_collide_with_final_archive(self):
        self.write_data("feedback.json", self.batch())
        self.write_data("review.json", self.review())
        self.write_data("verdicts.json", {
            "version": 1,
            "kind": "feedback_update",
            "batchId": "batch-1",
            "round": 1,
            "addedPointIds": ["point-1"],
        })
        status, _, payload = self.post_transition(self.transition("feedback-update"))
        self.assertEqual(status, 200, payload)
        round_dir = self.inbox / "history" / "batch-1-r1"
        self.assertEqual(len(list(round_dir.glob("verdicts-feedback-update-*.json"))), 1)
        self.assertEqual(len(list(round_dir.glob("transition-feedback-update-*.json"))), 1)

        self.write_data("verdicts.json", self.verdicts())
        status, _, payload = self.post_transition(self.transition("complete"))
        self.assertEqual(status, 200, payload)
        for name in ("feedback.json", "review.json", "verdicts.json", "transition.json"):
            self.assertTrue((round_dir / name).is_file(), name)

        status, _, payload = self.post_transition(self.transition("feedback-update"))
        self.assertEqual(status, 409, payload)
        self.assertIn(b"already archived", payload)

    def test_transition_rolls_back_when_an_archive_move_fails(self):
        self.write_data("feedback.json", self.batch())
        self.write_data("review.json", self.review())
        self.write_data("verdicts.json", self.verdicts())
        before = {
            name: (self.inbox / name).read_bytes()
            for name in ("feedback.json", "review.json", "verdicts.json")
        }
        original_rename = self.module.os.rename
        calls = {"count": 0}

        def fail_second(source, target):
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("injected move failure")
            return original_rename(source, target)

        with mock.patch.object(self.module.os, "rename", side_effect=fail_second):
            status, _, payload = self.post_transition(self.transition("complete"))
        self.assertEqual(status, 500, payload)
        self.assertEqual(
            {name: (self.inbox / name).read_bytes() for name in before}, before
        )
        round_dir = self.inbox / "history" / "batch-1-r1"
        self.assertFalse(round_dir.exists())

    def test_history_caps_refuse_archives_without_pruning_receipts(self):
        self.write_data("feedback.json", self.batch())
        self.write_data("review.json", self.review())
        self.write_data("verdicts.json", self.verdicts())

        history = self.inbox / "history"
        tombstone_round = history / "old-batch-r1"
        tombstone_round.mkdir(parents=True)
        tombstone = tombstone_round / "transition.json"
        tombstone.write_bytes(b'{"tombstone":true}\n')
        original_tombstone = tombstone.read_bytes()

        with mock.patch.object(self.module, "_MAX_HISTORY_ROUNDS", 1):
            status, _, payload = self.post_transition(
                self.transition("complete")
            )
        self.assertEqual(status, 507, payload)
        response = json.loads(payload)
        self.assertEqual(response["error"], "history_capacity_exceeded")
        self.assertIn("round limit", response["reason"])
        self.assertEqual(tombstone.read_bytes(), original_tombstone)
        for name in ("feedback.json", "review.json", "verdicts.json"):
            self.assertTrue((self.inbox / name).is_file())

        with mock.patch.object(
            self.module, "_MAX_HISTORY_BYTES", len(original_tombstone)
        ):
            status, _, payload = self.post_transition(
                self.transition("complete")
            )
        self.assertEqual(status, 507, payload)
        self.assertIn(b"byte limit", payload)
        self.assertEqual(tombstone.read_bytes(), original_tombstone)

        with mock.patch.object(self.module, "_MAX_HISTORY_SCAN_ENTRIES", 1):
            with self.assertRaisesRegex(
                self.module._HistoryLimit, "entry scan budget"
            ):
                self.module._history_usage()

    def test_state_read_never_observes_a_partial_transition(self):
        self.write_data("feedback.json", self.batch())
        self.write_data("review.json", self.review())
        self.write_data("verdicts.json", self.verdicts())
        first_move = threading.Event()
        release_move = threading.Event()
        original_rename = self.module.os.rename

        def pause_after_first(source, target):
            result = original_rename(source, target)
            if not first_move.is_set():
                first_move.set()
                release_move.wait(2)
            return result

        transition_result = []
        state_result = []
        with mock.patch.object(self.module.os, "rename", side_effect=pause_after_first):
            transition_thread = threading.Thread(
                target=lambda: transition_result.append(
                    self.post_transition(self.transition("complete"))
                )
            )
            transition_thread.start()
            self.assertTrue(first_move.wait(2))
            state_thread = threading.Thread(
                target=lambda: state_result.append(self.state_request())
            )
            state_thread.start()
            time.sleep(0.05)
            self.assertTrue(state_thread.is_alive())
            release_move.set()
            transition_thread.join(3)
            state_thread.join(3)
        self.assertEqual(transition_result[0][0], 200)
        self.assertEqual(state_result[0][0], 200)
        self.assertEqual(json.loads(state_result[0][2])["phase"], "collecting")

    def test_state_revision_changes_when_content_is_replaced_at_the_same_mtime(self):
        first = self.review()
        self.write_data("review.json", first)
        path = self.inbox / "review.json"
        timestamp = path.stat().st_mtime_ns
        before = self.module._rev()

        second = self.review()
        second["beforeRef"] = "b" * 40
        replacement = self.inbox / "review-replacement.json"
        replacement.write_text(json.dumps(second), encoding="utf-8")
        os.utime(replacement, ns=(timestamp, timestamp))
        os.replace(replacement, path)
        os.utime(path, ns=(timestamp, timestamp))
        after = self.module._rev()
        self.assertNotEqual(after, before)

    def test_protocol_json_reads_are_bounded_and_strict(self):
        oversized = self.inbox / "review.json"
        with oversized.open("wb") as handle:
            handle.seek(self.module._MAX_PROTOCOL_FILE)
            handle.write(b"x")
        self.assertIsNone(self.module._load_data("review.json"))

        archive = self.inbox / "history" / "batch-1-r1"
        archive.mkdir(parents=True)
        with (archive / "review.json").open("wb") as handle:
            handle.seek(self.module._MAX_PROTOCOL_FILE)
            handle.write(b"x")
        self.assertEqual(self.module._archived_review_point_ids("batch-1"), set())

        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        config["grace_seconds"] = float("nan")
        with self.assertRaisesRegex(SystemExit, "not valid JSON"):
            self.import_server(config, force="1")

        for invalid_grace in (0, 29, 86401, True, 30.5, "180"):
            with self.subTest(grace=invalid_grace):
                config["grace_seconds"] = invalid_grace
                with self.assertRaisesRegex(SystemExit, "30 through 86400"):
                    self.import_server(config, force="1")

    def test_config_reader_rejects_oversize_links_and_fifo_without_blocking(self):
        oversized = self.root / "oversized-config.json"
        with oversized.open("wb") as handle:
            handle.seek(self.module._MAX_CONFIG_FILE)
            handle.write(b"x")
        with self.assertRaisesRegex(ValueError, "exceeds 1 MB"):
            self.module._read_config_text(str(oversized))

        linked = self.root / "linked-config.json"
        try:
            linked.symlink_to(self.config_path)
        except (OSError, NotImplementedError) as error:
            self.skipTest("symbolic links unavailable: {}".format(error))
        with self.assertRaisesRegex(ValueError, "regular file"):
            self.module._read_config_text(str(linked))

        if hasattr(os, "mkfifo"):
            fifo = self.root / "config.pipe"
            os.mkfifo(str(fifo))
            started = time.monotonic()
            with self.assertRaisesRegex(ValueError, "regular file"):
                self.module._read_config_text(str(fifo))
            self.assertLess(time.monotonic() - started, 1)

    def test_title_stamping_handles_existing_and_missing_titles(self):
        existing = self.module.stamp("<html><head><title class='x'>Page</title></head></html>")
        self.assertIn("<title>🔵 Page</title>", existing)
        no_title = self.module.stamp("<html><head></head><body>Page</body></html>")
        self.assertEqual(no_title.count("<title>"), 1)
        self.assertIn("<title>🔵 Preview</title>", no_title)
        no_head = self.module.stamp("<html><body>Page</body></html>")
        self.assertEqual(no_head.count("<head>"), 1)
        self.assertIn("<title>🔵 Preview</title>", no_head)

        mixed_case = self.module.stamp(
            "<HTML><HEAD><TiTlE class='old'>Page</tItLe></HEAD><BODY></BODY></HTML>"
        )
        self.assertIn("<title>🔵 Page</title>", mixed_case)
        self.assertNotIn("class='old'", mixed_case)

        literal = "<title>Not the document title</title>"
        inert_documents = (
            "<html><head></head><body><script>const sample = '" + literal + "';</script></body></html>",
            "<html><head></head><body><style>/* " + literal + " */</style></body></html>",
            "<html><head></head><body><!-- " + literal + " --></body></html>",
            "<html><head></head><body><template>" + literal + "</template></body></html>",
            "<html><head></head><body><noscript>" + literal + "</noscript></body></html>",
        )
        for source in inert_documents:
            with self.subTest(source=source):
                stamped = self.module.stamp(source)
                self.assertIn(literal, stamped)
                self.assertIn("<title>🔵 Preview</title>", stamped)

        foreign_titles = (
            "<html><head></head><body>"
            "<svg><title>SVG title</title></svg>"
            "<math><title>Math title</title></math>"
            "</body></html>"
        )
        stamped = self.module.stamp(foreign_titles)
        self.assertIn("<svg><title>SVG title</title></svg>", stamped)
        self.assertIn("<math><title>Math title</title></math>", stamped)
        self.assertIn("<head><title>🔵 Preview</title></head>", stamped)

        tag_literals = (
            "<script>const head = '<head>';</script>",
            "<style>/* <html> */</style>",
            "<!-- <head><title>fake</title> -->",
        )
        for source in tag_literals:
            with self.subTest(source=source):
                stamped = self.module.stamp(source)
                self.assertTrue(stamped.startswith("<head><title>🔵 Preview</title></head>"))
                self.assertTrue(stamped.endswith(source))

        with self.assertRaisesRegex(self.module._CSPTransformError, "title element"):
            self.module.stamp("<html><head><title>unfinished")

    def test_injection_idempotency_checks_the_actual_overlay_tag(self):
        harmless = '<html><body><p>documentation says data-wk-color here</p></body></html>'
        injected = self.module.inject(harmless, "after")
        self.assertEqual(injected.count('src="/__wk/overlay.js"'), 1)
        reinjected = self.module.inject(injected, "after")
        self.assertEqual(reinjected.count('src="/__wk/overlay.js"'), 1)

        unrelated = (
            '<html><body><script data-wk-color="fake" src="/host.js"></script></body></html>'
        )
        self.assertEqual(
            self.module.inject(unrelated, "after").count('src="/__wk/overlay.js"'), 1
        )

        marker = (
            '<script src="/__wk/overlay.js" defer nonce="nonce-value" '
            'data-wk-nonce="nonce-value" '
            'data-wk-trusted-types-policy="wk-overlay-test" '
            'data-wk-color="blue" '
            'data-wk-token="token" data-wk-project="project" '
            'data-wk-mode="after"></script>'
        )
        inert_documents = {
            "active wrong token": "<html><body>" + marker + "</body></html>",
            "script literal": (
                "<html><body><script>const example = '" + marker.split("</script>")[0] +
                "';</script></body></html>"
            ),
            "style literal": "<html><body><style>/* " + marker + " */</style></body></html>",
            "comment literal": "<html><body><!-- " + marker + " --></body></html>",
            "template content": "<html><body><template>" + marker + "</template></body></html>",
            "noscript content": "<html><body><noscript>" + marker + "</noscript></body></html>",
            "data script": (
                "<html><body>" + marker.replace(
                    "<script ", '<script type="application/json" ', 1
                ) + "</body></html>"
            ),
            "nomodule script": (
                "<html><body>" + marker.replace(
                    "<script ", "<script nomodule ", 1
                ) + "</body></html>"
            ),
            "svg content": "<html><body><svg>" + marker + "</svg></body></html>",
            "math content": "<html><body><math>" + marker + "</math></body></html>",
        }
        for label, document in inert_documents.items():
            with self.subTest(label=label):
                result = self.module.inject(document, "after")
                self.assertEqual(result.count('src="/__wk/overlay.js"'), 2, result)

        real_mixed_case = (
            '<HTML><BODY><ScRiPt SrC="/__wk/overlay.js?cache=1" '
            'DEFER NONCE="{nonce}" DATA-WK-NONCE="{nonce}" '
            'DATA-WK-TRUSTED-TYPES-POLICY="wk-overlay-test" '
            'DATA-WK-COLOR="{color}" DATA-WK-TOKEN="{token}" '
            'DATA-WK-PROJECT="{project}" DATA-WK-EMOJI="{emoji}" '
            'DATA-WK-MODE="after" DATA-WK-DICTATION-MODE="speech" '
            'DATA-WK-INTERACTION-MODE="browse-default" '
            'DATA-WK-THEME="black" '
            'DATA-WK-BEFORE-PREFIX="" DATA-WK-HOTKEY-TOGGLE="KeyC" '
            'DATA-WK-HOTKEY-DICTATE="Space"></sCrIpT></BODY></HTML>'
        ).format(
            nonce="a" * 32,
            color=self.module.SLUG,
            token=self.module.MUTATION_TOKEN,
            project=self.module.PROJECT_STORAGE_ID,
            emoji=self.module.COLOR,
        )
        self.assertEqual(self.module.inject(real_mixed_case, "after"), real_mixed_case)

        mismatched_handshakes = (
            (
                'DATA-WK-TOKEN="{}"'.format(self.module.MUTATION_TOKEN),
                'DATA-WK-TOKEN="{}"'.format("f" * 32),
            ),
            (
                'DATA-WK-COLOR="{}"'.format(self.module.SLUG),
                'DATA-WK-COLOR="red"',
            ),
            (
                'DATA-WK-THEME="black"',
                'DATA-WK-THEME="white"',
            ),
            (
                'DATA-WK-PROJECT="{}"'.format(self.module.PROJECT_STORAGE_ID),
                'DATA-WK-PROJECT="other-0123456789abcdef"',
            ),
            ('DATA-WK-MODE="after"', 'DATA-WK-MODE="before"'),
        )
        for current, stale in mismatched_handshakes:
            with self.subTest(stale=stale):
                source = real_mixed_case.replace(current, stale, 1)
                result = self.module.inject(source, "after")
                self.assertEqual(
                    len(re.findall(r'\bsrc="/__wk/overlay\.js', result, re.I)),
                    2,
                    result,
                )

    def test_injection_uses_parsed_close_tags_and_fails_without_a_safe_position(self):
        tolerated = (
            "<html><body><p>live</p></body>"
            "<script>const closing = '</body>';</script></html>"
        )
        result = self.module.inject(tolerated, "after")
        overlay_at = result.index('src="/__wk/overlay.js"')
        self.assertLess(overlay_at, result.index("</body>"))
        self.assertIn("<script>const closing = '</body>';</script>", result)

        template_close = (
            "<html><body><template><p>example</p></body></template>"
            "live</body></html>"
        )
        result = self.module.inject(template_close, "after")
        overlay_at = result.index('src="/__wk/overlay.js"')
        self.assertGreater(overlay_at, result.index("</template>live"))
        self.assertLess(overlay_at, result.rindex("</body>"))

        fragment = "<main>fragment</main>"
        result = self.module.inject(fragment, "after")
        self.assertTrue(result.startswith(fragment + '<script src="/__wk/overlay.js"'))

        for unsafe in (
            "<main><!-- unfinished",
            "<script>const unfinished = true;",
            "<template><p>unfinished</p>",
        ):
            with self.subTest(unsafe=unsafe):
                with self.assertRaisesRegex(
                    self.module._CSPTransformError, "safe overlay insertion point"
                ):
                    self.module.inject(unsafe, "after")

    def test_strict_meta_csp_is_adapted_per_response_without_source_changes(self):
        source = """<html><head>
<meta content="default-src 'none'; script-src 'nonce-host'; style-src 'nonce-host'; connect-src 'none'; trusted-types 'none'; require-trusted-types-for 'script'" HTTP-EQUIV='Content-Security-Policy'>
</head><body><script nonce="host">window.hostRan = true;</script></body></html>"""
        (self.root / "index.html").write_text(source, encoding="utf-8")

        status, headers, first_payload = self.request("GET", "/")
        self.assertEqual(status, 200, first_payload)
        first = first_payload.decode("utf-8")
        script = re.search(
            r'<script src="/__wk/overlay\.js"[^>]* nonce="([^"]+)"[^>]*'
            r'data-wk-nonce="([^"]+)"[^>]*data-wk-trusted-types-policy="([^"]+)"',
            first,
        )
        self.assertIsNotNone(script, first)
        nonce, data_nonce, policy_name = script.groups()
        self.assertEqual(data_nonce, nonce)
        self.assertRegex(nonce, self.module._CSP_NONCE_VALUE)
        self.assertRegex(policy_name, self.module._TRUSTED_TYPES_POLICY_NAME)

        meta = re.search(r"<meta\b[^>]*>", first, re.IGNORECASE)
        self.assertIsNotNone(meta, first)
        transformed = html_lib.unescape(meta.group(0))
        self.assertIn("script-src 'nonce-host' 'nonce-{}'".format(nonce), transformed)
        self.assertIn("style-src 'nonce-host' 'nonce-{}'".format(nonce), transformed)
        self.assertIn("connect-src 'self'", transformed)
        self.assertIn("frame-src 'self'", transformed)
        self.assertIn("style-src-attr 'unsafe-inline'", transformed)
        self.assertIn("trusted-types {}".format(policy_name), transformed)
        self.assertIn("require-trusted-types-for 'script'", transformed)
        self.assertIn('<script nonce="host">', first)
        self.assertEqual((self.root / "index.html").read_text(encoding="utf-8"), source)

        status, _, second_payload = self.request("GET", "/")
        self.assertEqual(status, 200, second_payload)
        second = second_payload.decode("utf-8")
        second_nonce = re.search(
            r'<script src="/__wk/overlay\.js"[^>]* nonce="([^"]+)"', second
        ).group(1)
        self.assertNotEqual(second_nonce, nonce)

        head_status, head_headers, head_payload = self.request("HEAD", "/")
        self.assertEqual(head_status, 200)
        self.assertEqual(head_payload, b"")
        self.assertEqual(head_headers.get("Content-Length"), headers.get("Content-Length"))

    def test_csp_transform_preserves_active_unsafe_inline_and_all_meta_policies(self):
        source = """<html><head>
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'">
<meta content="connect-src https://api.example; trusted-types host-policy" http-equiv="content-security-policy" />
</head><body></body></html>"""
        result = self.module.inject(
            source,
            "after",
            nonce="a" * 32,
            trusted_types_policy="wk-overlay-test",
        )
        policies = [
            html_lib.unescape(value)
            for value in re.findall(r'<meta\b[^>]*\bcontent="([^"]*)"[^>]*>', result, re.I)
        ]
        self.assertEqual(len(policies), 2, result)
        first = {name: tokens for name, tokens in self.module._parse_csp_policy(policies[0])}
        self.assertIn("'unsafe-inline'", first["script-src"])
        self.assertIn("'self'", first["script-src"])
        self.assertNotIn("'nonce-{}'".format("a" * 32), first["script-src"])
        self.assertIn("'unsafe-inline'", first["style-src"])
        self.assertNotIn("'nonce-{}'".format("a" * 32), first["style-src"])
        self.assertEqual(first["connect-src"], ["'self'"])
        self.assertEqual(first["frame-src"], ["'self'"])
        second = {name: tokens for name, tokens in self.module._parse_csp_policy(policies[1])}
        self.assertIn("'self'", second["connect-src"])
        self.assertEqual(second["trusted-types"], ["host-policy", "wk-overlay-test"])
        self.assertEqual(result.count('src="/__wk/overlay.js"'), 1)

        frame_policy = self.module._transform_csp_policy(
            "default-src https://default.example; child-src https://child.example; "
            "frame-src 'none'",
            "a" * 32,
            "wk-overlay-test",
        )
        frame_directives = {
            name: tokens for name, tokens in self.module._parse_csp_policy(frame_policy)
        }
        self.assertEqual(frame_directives["frame-src"], ["'self'"])
        self.assertEqual(frame_directives["child-src"], ["https://child.example"])

    def test_malformed_nonce_and_hash_tokens_do_not_disable_unsafe_inline(self):
        source = """<html><head>
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline' 'nonce-%%%'; style-src 'unsafe-inline' 'sha256-%%%'">
</head><body><script>window.hostInlineRan = true;</script></body></html>"""
        result = self.module.inject(
            source,
            "after",
            nonce="a" * 32,
            trusted_types_policy="wk-overlay-test",
        )
        policy = html_lib.unescape(
            re.search(r'<meta\b[^>]*\bcontent="([^"]*)"[^>]*>', result, re.I).group(1)
        )
        directives = {
            name: tokens for name, tokens in self.module._parse_csp_policy(policy)
        }
        self.assertIn("'unsafe-inline'", directives["script-src"])
        self.assertIn("'self'", directives["script-src"])
        self.assertNotIn("'nonce-{}'".format("a" * 32), directives["script-src"])
        self.assertIn("'unsafe-inline'", directives["style-src"])
        self.assertNotIn("'nonce-{}'".format("a" * 32), directives["style-src"])
        self.assertFalse(
            self.module._has_nonce_or_hash(
                ["'nonce-%%%'", "'sha256-%%%'", "'sha999-YQ=='"]
            )
        )
        self.assertTrue(self.module._has_nonce_or_hash(["'nonce-a'"]))
        self.assertTrue(self.module._has_nonce_or_hash(["'SHA256-YQ=='"]))

    def test_malformed_csp_fails_closed_with_422_for_get_and_head(self):
        invalid_pages = {
            "invalid-directive.html": (
                '<meta http-equiv="Content-Security-Policy" '
                'content="default-src \'none\'; script@src \'self\'">'
            ),
            "duplicate-content.html": (
                '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'" '
                'content="script-src \'self\'">'
            ),
            "control-character.html": (
                '<meta http-equiv="Content-Security-Policy" '
                'content="default-src \'none\'\x00">'
            ),
            "unterminated-http-equiv.html": (
                '<meta http-equiv="Content-Security-Policy content="default-src \'none\'">'
            ),
        }
        for name, meta in invalid_pages.items():
            (self.root / name).write_bytes(
                ("<html><head>" + meta + "</head><body>private</body></html>").encode("utf-8")
            )
            for method in ("GET", "HEAD"):
                with self.subTest(name=name, method=method):
                    status, headers, payload = self.request(method, "/" + name)
                    self.assertEqual(status, 422, payload)
                    self.assertEqual(
                        headers.get("Content-Type"), "application/json; charset=utf-8"
                    )
                    self.assertNotIn(self.module.MUTATION_TOKEN.encode("ascii"), payload)
                    if method == "HEAD":
                        self.assertEqual(payload, b"")

        (self.root / "incomplete-csp.html").write_text(
            '<html><body>private</body></html><meta http-equiv="Content-Security-Policy" '
            'content="default-src \'none\'"',
            encoding="utf-8",
        )
        status, _, payload = self.request("GET", "/incomplete-csp.html")
        self.assertEqual(status, 422, payload)
        self.assertNotIn(self.module.MUTATION_TOKEN.encode("ascii"), payload)

    def test_csp_text_in_code_examples_is_not_mistaken_for_a_policy(self):
        (self.root / "csp-docs.html").write_text(
            """<html><body>
<pre>&lt;meta http-equiv="Content-Security-Policy" content="default-src 'none'"&gt;</pre>
<script>const example = '<meta http-equiv="Content-Security-Policy" content="script-src none">';</script>
</body></html>""",
            encoding="utf-8",
        )
        status, _, payload = self.request("GET", "/csp-docs.html")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload.count(b'src="/__wk/overlay.js"'), 1)

    def test_transformed_live_html_has_a_bounded_response(self):
        (self.root / "bounded.html").write_text(
            '<html><head><meta http-equiv="Content-Security-Policy" '
            'content="default-src \'none\'"></head><body>private</body></html>',
            encoding="utf-8",
        )
        with mock.patch.object(self.module, "_MAX_TRANSFORMED_HTML", 128):
            status, headers, payload = self.request("GET", "/bounded.html")
        self.assertEqual(status, 413, payload)
        self.assertEqual(headers.get("Content-Type"), "application/json; charset=utf-8")
        self.assertNotIn(self.module.MUTATION_TOKEN.encode("ascii"), payload)

    def test_csp_transform_rejects_untrusted_nonce_and_policy_inputs(self):
        source = (
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'">'
        )
        with self.assertRaisesRegex(self.module._CSPTransformError, "nonce"):
            self.module.inject(
                source, "after", nonce="bad nonce", trusted_types_policy="wk-safe"
            )
        with self.assertRaisesRegex(self.module._CSPTransformError, "policy"):
            self.module.inject(
                source, "after", nonce="a" * 32, trusted_types_policy="bad policy"
            )

    def test_palette_display_label_rejects_markup_and_controls(self):
        dangerous = '\"><img src=x onerror=alert(1)>'
        config = {
            "project_name": "safe-project",
            "site_root": ".",
            "feedback_dir": ".webkit/feedback",
            "lock_dir": str(self.root / "locks"),
            "palette": [{"slug": "blue", "emoji": dangerous, "port": 5311}],
        }
        with self.assertRaisesRegex(SystemExit, "short display label"):
            self.import_server(
                config,
                force="1",
                argv=[str(SERVER_PATH), dangerous, "5311", str(self.root)],
            )

        for dangerous_label in (
            "x y", "x\n", "<x>", "x" * 33,
            "safe\u202eevil", "safe\u2066evil", "safe\x01evil",
        ):
            with self.subTest(label=dangerous_label):
                invalid = dict(config)
                invalid["palette"] = [
                    {"slug": "blue", "emoji": dangerous_label, "port": 5311}
                ]
                with self.assertRaisesRegex(SystemExit, "short display label"):
                    self.import_server(
                        invalid,
                        force="1",
                        argv=[str(SERVER_PATH), dangerous_label, "5311", str(self.root)],
                    )

        valid = dict(config)
        valid_label = "👩‍💻️"
        valid["palette"] = [
            {"slug": "blue", "emoji": valid_label, "port": 5311}
        ]
        module = self.import_server(
            valid,
            force="1",
            argv=[str(SERVER_PATH), valid_label, "5311", str(self.root)],
        )
        self.assertEqual(module.COLOR, valid_label)

    def test_health_response_carries_the_preview_instance_identity(self):
        status, headers, _ = self.state_request()
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("X-WK-Preview-Instance"), "test-instance-token-123456")
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")

    def test_state_requires_the_browser_mutation_token(self):
        for token in (False, "wrong-token"):
            with self.subTest(token=token):
                status, _, payload = self.state_request(token=token)
                self.assertEqual(status, 403)
                self.assertNotIn(b'"batch"', payload)
        status, _, payload = self.state_request()
        self.assertEqual(status, 200)
        self.assertIn(b'"phase"', payload)

    def test_overlay_handshake_requires_the_current_browser_token(self):
        for token in (None, "wrong-token", "f" * 32):
            with self.subTest(token=token):
                headers = {"X-WK-Token": token} if token is not None else {}
                status, _, payload = self.request(
                    "GET", "/__wk/handshake", headers=headers
                )
                self.assertEqual(status, 403)
                self.assertIn(b"invalid Webkit mutation token", payload)

        status, headers, payload = self.request(
            "GET",
            "/__wk/handshake",
            headers={"X-WK-Token": self.module.MUTATION_TOKEN},
        )
        self.assertEqual(status, 204)
        self.assertEqual(headers.get("Content-Length"), "0")
        self.assertEqual(payload, b"")

    def test_state_phase_requires_a_coherent_three_file_round(self):
        batch = self.batch()
        review = self.review()
        verdicts = self.verdicts()
        update = {
            "version": 1,
            "kind": "feedback_update",
            "batchId": "batch-1",
            "round": 1,
            "addedPointIds": ["point-2"],
        }
        cases = (
            (None, None, None, "collecting"),
            (None, review, None, "transitioning"),
            (None, None, verdicts, "transitioning"),
            (batch, None, None, "awaiting_agent"),
            (batch, None, update, "awaiting_agent"),
            (batch, review, None, "reviewing"),
            (batch, review, verdicts, "verdicts_sent"),
        )
        for batch_value, review_value, verdict_value, expected in cases:
            with self.subTest(expected=expected, files=(batch_value, review_value, verdict_value)):
                self.assertEqual(
                    self.module._phase(batch_value, review_value, verdict_value), expected
                )
        round_two_batch = self.batch()
        round_two_batch["round"] = 2
        round_two_review = self.review(round_number=2)
        self.assertEqual(
            self.module._phase(round_two_batch, round_two_review, None), "reviewing"
        )
        stale = self.verdicts(round_number=2)
        self.assertEqual(self.module._phase(batch, review, stale), "transitioning")
        malformed = dict(batch)
        malformed["round"] = "one"
        self.assertEqual(self.module._phase(malformed, None, None), "transitioning")

    def test_transition_token_is_private_owner_safe_and_not_statically_served(self):
        token_path = Path(self.module.TRANSITION_TOKEN_PATH)
        self.assertTrue(token_path.is_file())
        self.assertFalse(token_path.is_symlink())
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(token_path.stat().st_mode), 0o600)
        relative = "/.webkit/feedback/blue/transition-token"
        for method in ("GET", "HEAD"):
            status, _, payload = self.request(method, relative)
            self.assertEqual(status, 404)
            self.assertNotIn(self.module.TRANSITION_TOKEN.encode("ascii"), payload)

        (self.inbox / "linked-public-file").symlink_to(self.root / "index.html")
        for method in ("GET", "HEAD"):
            status, _, payload = self.request(
                method, "/.webkit/feedback/blue/linked-public-file"
            )
            self.assertEqual(status, 404)
            self.assertNotIn(b"Preview", payload)

        token_path.write_text("foreign-owner-token-1234567890\n", encoding="utf-8")
        self.module._remove_transition_token()
        self.assertTrue(token_path.is_file())
        self.module._publish_transition_token()

    def test_custom_feedback_directory_is_hidden_through_a_public_symlink_alias(self):
        custom = self.root / "runtime-feedback" / "blue"
        custom.mkdir(parents=True)
        secret = "private-transition-token-1234567890"
        (custom / "transition-token").write_text(secret + "\n", encoding="utf-8")
        (custom / "index.html").write_text(
            "<html><body>{}</body></html>".format(secret), encoding="utf-8"
        )
        alias = self.root / "public-inbox"
        public_directory = self.root / "public-directory"
        public_directory.mkdir()
        try:
            alias.symlink_to(custom, target_is_directory=True)
            (public_directory / "index.html").symlink_to(custom / "index.html")
        except (OSError, NotImplementedError):
            self.skipTest("directory symlinks are unavailable")

        with mock.patch.object(self.module, "FEEDBACK_DIR", str(custom)):
            for method in ("GET", "HEAD"):
                for path in (
                    "/public-inbox/transition-token", "/public-directory/"
                ):
                    with self.subTest(method=method, path=path):
                        status, _, payload = self.request(method, path)
                        self.assertEqual(status, 404, payload)
                        self.assertNotIn(secret.encode("ascii"), payload)

    def test_server_request_threads_are_joined_during_close(self):
        self.assertFalse(self.module.PreviewHTTPServer.daemon_threads)
        self.assertTrue(self.module.PreviewHTTPServer.block_on_close)

    def test_server_close_drains_an_active_request(self):
        entered = threading.Event()
        release = threading.Event()
        result = {}

        class SlowHandler(self.module.Handler):
            def _wk_get(handler_self, raw):
                if raw == "/__wk/drain-test":
                    entered.set()
                    if not release.wait(timeout=3):
                        return handler_self._send_json(
                            500, {"error": "test request timed out"}
                        )
                    return handler_self._send_json(200, {"ok": True})
                return super()._wk_get(raw)

        server = self.module.PreviewHTTPServer(("127.0.0.1", 0), SlowHandler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()

        def request():
            connection = http.client.HTTPConnection(
                "127.0.0.1", server.server_port, timeout=3
            )
            try:
                connection.request("GET", "/__wk/drain-test")
                response = connection.getresponse()
                result["status"] = response.status
                response.read()
            finally:
                connection.close()

        request_thread = threading.Thread(target=request)
        request_thread.start()
        close_thread = None
        try:
            self.assertTrue(entered.wait(timeout=3))
            server.shutdown()
            close_thread = threading.Thread(target=server.server_close)
            close_thread.start()
            close_thread.join(timeout=0.1)
            self.assertTrue(
                close_thread.is_alive(),
                "server_close returned before the active request completed",
            )
            release.set()
            close_thread.join(timeout=3)
            request_thread.join(timeout=3)
            self.assertFalse(close_thread.is_alive())
            self.assertFalse(request_thread.is_alive())
            self.assertEqual(result.get("status"), 200)
        finally:
            release.set()
            if server_thread.is_alive():
                server.shutdown()
            server.server_close()
            server_thread.join(timeout=3)
            request_thread.join(timeout=3)
            if close_thread is not None:
                close_thread.join(timeout=3)

    def test_partial_client_cannot_block_server_close_forever(self):
        accepted = threading.Event()

        class PartialHandler(self.module.Handler):
            def setup(handler_self):
                super().setup()
                accepted.set()

        with mock.patch.object(
            self.module, "_CLIENT_IO_TIMEOUT_SECONDS", 0.2
        ):
            server = self.module.PreviewHTTPServer(
                ("127.0.0.1", 0), PartialHandler
            )
            server_thread = threading.Thread(
                target=server.serve_forever, daemon=True
            )
            server_thread.start()
            sock = socket.create_connection(
                ("127.0.0.1", server.server_port), timeout=3
            )
            sock.sendall(
                (
                    "POST /__wk/feedback HTTP/1.1\r\n"
                    "Host: 127.0.0.1:{}\r\n"
                ).format(server.server_port).encode("ascii")
            )
            self.assertTrue(accepted.wait(timeout=3))

        try:
            server.shutdown()
            started = time.monotonic()
            server.server_close()
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 2)
        finally:
            sock.close()
            if server_thread.is_alive():
                server.shutdown()
            server.server_close()
            server_thread.join(timeout=3)
            self.assertFalse(server_thread.is_alive())

    @unittest.skipUnless(hasattr(signal, "SIGTERM"), "SIGTERM is unavailable")
    def test_startup_gc_and_sigterm_cleanup_are_owner_safe(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        config["palette"][0]["port"] = port
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        token_path = Path(self.module.TRANSITION_TOKEN_PATH)
        original_token = "foreign-transition-token-" + ("x" * 32)
        token_path.write_text(original_token + "\n", encoding="utf-8")
        notes = self.inbox / "voice-notes"
        notes.mkdir()
        stale_orphan = notes / "voice-stale1.webm"
        stale_orphan.write_bytes(b"stale")
        stale_time = time.time() - self.module._VOICE_ORPHAN_GRACE_SECONDS - 60
        os.utime(stale_orphan, (stale_time, stale_time))
        environment = dict(os.environ)
        environment.update({
            "WK_CONFIG": str(self.config_path),
            "WK_COLOR_FORCE": "1",
            "PYTHONPYCACHEPREFIX": str(self.root / "pycache"),
        })
        popen_options = {}
        if os.name == "nt":
            popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        process = subprocess.Popen(
            [sys.executable, str(SERVER_PATH), "🔵", str(port), str(self.root)],
            cwd=self.root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **popen_options,
        )
        stop_signal = (
            signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM
        )
        try:
            deadline = time.monotonic() + 60
            child_token = original_token
            while time.monotonic() < deadline:
                if token_path.is_file():
                    child_token = token_path.read_text(encoding="utf-8").strip()
                    if child_token != original_token:
                        break
                if process.poll() is not None:
                    break
                time.sleep(0.02)
            if child_token == original_token:
                if process.poll() is None:
                    process.send_signal(stop_signal)
                stdout, stderr = process.communicate(timeout=5)
                self.fail(
                    "preview subprocess did not publish its token; return code {}; "
                    "stdout={!r}; stderr={!r}".format(
                        process.returncode, stdout[-2000:], stderr[-2000:]
                    )
                )
            self.assertNotEqual(child_token, original_token)
            self.assertFalse(stale_orphan.exists())
            process.send_signal(stop_signal)
            _, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertFalse(token_path.exists())
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=3)
            self.module._publish_transition_token()

    def test_head_matches_webkit_get_metadata_without_a_body(self):
        for path in (
            "/__wk/handshake", "/__wk/state", "/__wk/overlay.js", "/__wk/overlay.css"
        ):
            with self.subTest(path=path):
                headers = (
                    {"X-WK-Token": self.module.MUTATION_TOKEN}
                    if path in ("/__wk/handshake", "/__wk/state") else {}
                )
                get_status, get_headers, get_payload = self.request(
                    "GET", path, headers=headers
                )
                head_status, head_headers, head_payload = self.request(
                    "HEAD", path, headers=headers
                )
                self.assertEqual(head_status, get_status)
                self.assertEqual(head_headers.get("Content-Type"), get_headers.get("Content-Type"))
                self.assertEqual(head_headers.get("Content-Length"), str(len(get_payload)))
                self.assertEqual(head_payload, b"")

    def test_oversized_html_is_rejected_before_decode_or_transformation(self):
        oversized = self.root / "oversized.html"
        with oversized.open("wb") as handle:
            handle.seek(self.module._MAX_TRANSFORM_HTML)
            handle.write(b"x")
        for method in ("GET", "HEAD"):
            with self.subTest(method=method):
                status, headers, payload = self.request(method, "/oversized.html")
                self.assertEqual(status, 413)
                self.assertEqual(headers.get("Content-Type"), "application/json; charset=utf-8")
                self.assertNotIn(self.module.MUTATION_TOKEN.encode("ascii"), payload)
                if method == "HEAD":
                    self.assertEqual(payload, b"")

    def test_untrusted_host_cannot_read_token_bearing_html(self):
        status, _, payload = self.request(
            "GET", "/", headers={"Host": "attacker.example:{}".format(self.server.server_port)}
        )
        self.assertEqual(status, 421)
        self.assertNotIn(self.module.MUTATION_TOKEN.encode("ascii"), payload)

    def test_ipv4_preview_rejects_bracketed_ipv6_host_header(self):
        status, _, payload = self.request(
            "GET", "/",
            headers={"Host": "[::1]:{}".format(self.server.server_port)},
        )
        self.assertEqual(status, 421)
        self.assertNotIn(self.module.MUTATION_TOKEN.encode("ascii"), payload)

    def test_bind_validation_rejects_ipv6_and_specific_interface_addresses(self):
        base = json.loads(self.config_path.read_text(encoding="utf-8"))
        for bind_host in ("::1", "[::1]", "192.168.1.20"):
            with self.subTest(bind_host=bind_host):
                config = dict(base)
                config["bind_host"] = bind_host
                config["allowed_hosts"] = ["preview.lan"]
                with self.assertRaisesRegex(
                    SystemExit, "IPv6 and specific non-loopback binds"
                ):
                    self.import_server(config, force="1")

        config = dict(base)
        config["bind_host"] = "0.0.0.0"
        config["allowed_hosts"] = ["[::1]"]
        with self.assertRaisesRegex(SystemExit, "IPv6 is not supported"):
            self.import_server(config, force="1")

        config = dict(base)
        config["bind_host"] = "::1"
        config["allowed_hosts"] = ["[::1]"]
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        environment = dict(os.environ)
        environment.update({
            "WK_CONFIG": str(self.config_path),
            "WK_COLOR_FORCE": "1",
        })
        result = subprocess.run(
            [sys.executable, "-B", str(SERVER_PATH), "🔵", "5311", str(self.root)],
            cwd=self.root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "IPv6 and specific non-loopback binds",
            result.stdout + result.stderr,
        )

    def test_non_loopback_bind_requires_and_enforces_allowed_hosts(self):
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        config["bind_host"] = "0.0.0.0"
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        module_name = "preview_server_lan_missing_hosts_{}".format(uuid.uuid4().hex)
        spec = importlib.util.spec_from_file_location(module_name, SERVER_PATH)
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(os.environ, {
            "WK_CONFIG": str(self.config_path),
            "WK_COLOR_FORCE": "1",
        }), mock.patch.object(
            sys, "argv", [str(SERVER_PATH), "🔵", "5311", str(self.root)]
        ):
            with self.assertRaisesRegex(SystemExit, "requires a non-empty allowed_hosts"):
                spec.loader.exec_module(module)

        config["allowed_hosts"] = ["preview.lan", "192.168.1.20"]
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        module_name = "preview_server_lan_allowed_hosts_{}".format(uuid.uuid4().hex)
        spec = importlib.util.spec_from_file_location(module_name, SERVER_PATH)
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(os.environ, {
            "WK_CONFIG": str(self.config_path),
            "WK_COLOR_FORCE": "1",
        }), mock.patch.object(
            sys, "argv", [str(SERVER_PATH), "🔵", "5311", str(self.root)]
        ):
            spec.loader.exec_module(module)

        server = module.PreviewHTTPServer(("127.0.0.1", 0), module.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection(
                "127.0.0.1", server.server_port, timeout=3
            )
            connection.request(
                "GET", "/",
                headers={"Host": "preview.lan:{}".format(server.server_port)},
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            connection.close()

            connection = http.client.HTTPConnection(
                "127.0.0.1", server.server_port, timeout=3
            )
            connection.request(
                "GET", "/",
                headers={"Host": "localhost:{}".format(server.server_port)},
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            connection.close()

            connection = http.client.HTTPConnection(
                "127.0.0.1", server.server_port, timeout=3
            )
            connection.request(
                "GET", "/",
                headers={"Host": "attacker.example:{}".format(server.server_port)},
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 421)
            response.read()
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())

    def test_api_proxy_rejects_cross_origin_posts(self):
        self.module.API_PROXY_ORIGIN = "http://127.0.0.1:1"
        status, _, _ = self.request(
            "POST", "/api/change", b"{}",
            {"Content-Type": "text/plain", "Origin": "https://attacker.example"},
        )
        self.assertEqual(status, 403)

    def test_api_proxy_forwards_only_the_exact_control_token_header(self):
        self.module.API_PROXY_ORIGIN = "http://127.0.0.1:43210"
        captured = {}

        class FakeResponse:
            status = 200
            headers = {"Content-Type": "application/json"}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, _limit=-1):
                return b'{"ok":true}'

        def fake_urlopen(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse()

        origin = "http://127.0.0.1:{}".format(self.server.server_port)
        with mock.patch.object(self.module, "_proxy_urlopen", side_effect=fake_urlopen):
            status, _, _ = self.request(
                "POST",
                "/api/change",
                b"{}",
                {
                    "Content-Type": "application/json",
                    "Origin": origin,
                    "X-WKCC-Token": "allowed-control-token-123456",
                    "Cookie": "session=secret; wkcc=allowed; wkcc-extra=no; other=value",
                },
            )
        self.assertEqual(status, 200)
        forwarded = {key.lower(): value for key, value in captured["request"].header_items()}
        self.assertEqual(
            forwarded.get("x-wkcc-token"), "allowed-control-token-123456"
        )
        self.assertNotIn("cookie", forwarded)
        self.assertNotIn("origin", forwarded)
        self.assertNotIn("session=secret", " ".join(forwarded.values()))

    def test_api_proxy_drops_malformed_control_token_and_all_cookies(self):
        self.module.API_PROXY_ORIGIN = "http://127.0.0.1:43210"
        captured = {}

        class FakeResponse:
            status = 200
            headers = {"Content-Type": "application/json"}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, _limit=-1):
                return b'{"ok":true}'

        def fake_urlopen(request, timeout):
            captured["request"] = request
            return FakeResponse()

        origin = "http://127.0.0.1:{}".format(self.server.server_port)
        with mock.patch.object(self.module, "_proxy_urlopen", side_effect=fake_urlopen):
            status, _, _ = self.request(
                "POST",
                "/api/change",
                b"{}",
                {
                    "Content-Type": "application/json",
                    "Origin": origin,
                    "X-WKCC-Token": "bad token with spaces",
                    "Cookie": "wkcc=must-not-forward; session=must-not-forward",
                },
            )
        self.assertEqual(status, 200)
        forwarded = {
            key.lower(): value
            for key, value in captured["request"].header_items()
        }
        self.assertNotIn("x-wkcc-token", forwarded)
        self.assertNotIn("cookie", forwarded)

    def test_api_proxy_requires_process_opt_in_and_safe_localhost_resolution(self):
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        config["api_proxy_origin"] = "http://127.0.0.1:8790"
        with self.assertRaisesRegex(SystemExit, "WK_ENABLE_API_PROXY=1"):
            self.import_server(
                config, force="1", env_overrides={"WK_ENABLE_API_PROXY": ""}
            )

        enabled = self.import_server(
            config, force="1", env_overrides={"WK_ENABLE_API_PROXY": "1"}
        )
        self.assertEqual(enabled.API_PROXY_ORIGIN, "http://127.0.0.1:8790")

        config["api_proxy_origin"] = "http://localhost:8790"
        non_loopback = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.10", 8790))
        ]
        with mock.patch("socket.getaddrinfo", return_value=non_loopback):
            with self.assertRaisesRegex(SystemExit, "resolve only to loopback"):
                self.import_server(
                    config,
                    force="1",
                    env_overrides={"WK_ENABLE_API_PROXY": "1"},
                )

        loopback = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 8790))
        ]
        with mock.patch("socket.getaddrinfo", return_value=loopback):
            localhost = self.import_server(
                config,
                force="1",
                env_overrides={"WK_ENABLE_API_PROXY": "1"},
            )
        self.assertEqual(localhost.API_PROXY_ORIGIN, "http://127.0.0.1:8790")
        self.assertEqual(localhost.API_PROXY_HOST_HEADER, "localhost:8790")

    def test_api_proxy_bounds_upstream_response_body(self):
        self.module.API_PROXY_ORIGIN = "http://127.0.0.1:43210"

        class LargeResponse:
            status = 200
            headers = {"Content-Type": "application/octet-stream"}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, limit=-1):
                return b"x" * limit

        with mock.patch.object(
            self.module, "_proxy_urlopen", return_value=LargeResponse()
        ):
            status, _, payload = self.request("GET", "/api/large")
        self.assertEqual(status, 502)
        self.assertIn(b"exceeded 30 MB", payload)

    def test_api_proxy_rejects_redirects_and_incomplete_post_bodies(self):
        self.module.API_PROXY_ORIGIN = "http://127.0.0.1:43210"

        class RedirectResponse:
            status = 302
            headers = {"Location": "http://example.test/steal"}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, _limit=-1):
                return b""

        with mock.patch.object(
            self.module, "_proxy_urlopen", return_value=RedirectResponse()
        ):
            status, _, payload = self.request("GET", "/api/redirect")
        self.assertEqual(status, 502, payload)
        self.assertIn(b"redirects are not allowed", payload)

        body = b"{}"
        with socket.create_connection(("127.0.0.1", self.server.server_port), timeout=3) as sock:
            request = (
                "POST /api/change HTTP/1.1\r\n"
                "Host: 127.0.0.1:{}\r\n"
                "Origin: http://127.0.0.1:{}\r\n"
                "Content-Type: application/json\r\n"
                "Content-Length: 10\r\n"
                "Connection: close\r\n\r\n"
            ).format(
                self.server.server_port, self.server.server_port
            ).encode("ascii") + body
            sock.sendall(request)
            sock.shutdown(socket.SHUT_WR)
            response = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response += chunk
        self.assertIn(b" 400 ", response.split(b"\r\n", 1)[0])

    def test_static_symlink_cannot_escape_document_root(self):
        with _WritableTemporaryDirectory() as outside:
            secret = Path(outside) / "secret.txt"
            secret.write_text("not public", encoding="utf-8")
            (self.root / "leak.txt").symlink_to(secret)
            status, _, payload = self.request("GET", "/leak.txt")
            self.assertEqual(status, 403)
            self.assertNotIn(b"not public", payload)

    def test_static_policy_hides_secrets_encoded_paths_and_directory_listings(self):
        secret = self.root / ".env"
        secret.write_text("TOP_SECRET=value", encoding="utf-8")
        key = self.root / "private.pem"
        key.write_text("PRIVATE KEY", encoding="utf-8")
        linked = self.root / "public-env"
        linked.symlink_to(secret)
        no_index = self.root / "assets"
        no_index.mkdir()
        (no_index / "visible.txt").write_text("listing leak", encoding="utf-8")

        paths = (
            "/.env",
            "/%2eenv",
            "/.%65nv",
            "/.git/config",
            "/%2egit/config",
            "/private.pem",
            "/public-env",
            "/webkit/webkit.config.json",
            "/assets",
            "/assets/",
        )
        for method in ("GET", "HEAD"):
            for path in paths:
                with self.subTest(method=method, path=path):
                    status, headers, payload = self.request(method, path)
                    self.assertEqual(status, 404, (method, path, payload))
                    self.assertNotIn("Location", headers)
                    self.assertNotIn(b"TOP_SECRET", payload)
                    self.assertNotIn(b"listing leak", payload)
        status, _, payload = self.request("GET", "/__wk/before/%2eenv")
        self.assertEqual(status, 403)
        self.assertNotIn(b"TOP_SECRET", payload)

    def test_head_symlink_cannot_escape_document_root(self):
        with _WritableTemporaryDirectory() as outside:
            secret = Path(outside) / "secret.txt"
            secret.write_text("not public", encoding="utf-8")
            (self.root / "head-leak.txt").symlink_to(secret)
            status, _, payload = self.request("HEAD", "/head-leak.txt")
            self.assertEqual(status, 403)
            self.assertEqual(payload, b"")

    def test_before_route_symlink_cannot_escape_document_root(self):
        with _WritableTemporaryDirectory() as outside:
            secret = Path(outside) / "secret.txt"
            secret.write_text("not public", encoding="utf-8")
            (self.root / "before-leak.txt").symlink_to(secret)
            self.write_data("review.json", self.review())
            status, _, payload = self.request(
                "GET", self.before_path("/before-leak.txt")
            )
            self.assertEqual(status, 403)
            self.assertNotIn(b"not public", payload)

    def test_before_snapshot_requires_the_current_review_capability(self):
        historical = self.root / "deleted-secret.txt"
        historical.write_text("historical secret", encoding="utf-8")
        subprocess.run(
            ["git", "add", "deleted-secret.txt"], cwd=self.root, check=True
        )
        subprocess.run(
            [
                "git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
                "commit", "-m", "historical secret",
            ],
            cwd=self.root,
            check=True,
            capture_output=True,
        )
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.root, check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        historical.unlink()
        review = self.review()
        review["beforeRef"] = commit
        self.write_data("review.json", review)

        for guessed in (
            "/__wk/before/deleted-secret.txt",
            "/__wk/before/" + "0" * 64 + "/deleted-secret.txt",
        ):
            status, _, payload = self.request("GET", guessed)
            self.assertEqual(status, 403)
            self.assertNotIn(b"historical secret", payload)

        status, _, payload = self.request(
            "GET", self.before_path("/deleted-secret.txt")
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload, b"historical secret")

    def test_before_snapshot_with_malformed_csp_fails_closed(self):
        (self.root / "index.html").write_text(
            """<html><head><meta http-equiv="Content-Security-Policy"
content="default-src 'none'; invalid@directive 'self'"></head><body>historical private value</body></html>""",
            encoding="utf-8",
        )
        subprocess.run(["git", "add", "index.html"], cwd=self.root, check=True)
        subprocess.run(
            [
                "git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
                "commit", "-m", "malformed historical CSP",
            ],
            cwd=self.root,
            check=True,
            capture_output=True,
        )
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        review = self.review()
        review["beforeRef"] = commit
        self.write_data("review.json", review)

        status, headers, payload = self.request("GET", self.before_path("/index.html"))
        self.assertEqual(status, 422, payload)
        self.assertEqual(headers.get("Content-Type"), "application/json; charset=utf-8")
        self.assertNotIn(b"historical private value", payload)
        self.assertNotIn(self.module.MUTATION_TOKEN.encode("ascii"), payload)

    def test_before_snapshot_rewrites_assets_through_its_capability(self):
        (self.root / "index.html").write_text(
            """<html><head>
<link rel="stylesheet" href="/styles.css">
<style>.hero { background: url('/inline.png'); } @import "/inline.css";</style>
</head><body>
<img src="/image.png" srcset="/small.png 1x, /large.png 2x">
<img src="relative.png"><script src="/__wk/host.js"></script>
</body></html>""",
            encoding="utf-8",
        )
        (self.root / "styles.css").write_text(
            ".hero { background: url(/background.png); } @import '/theme.css';",
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "add", "index.html", "styles.css"],
            cwd=self.root,
            check=True,
        )
        subprocess.run(
            [
                "git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
                "commit", "-m", "snapshot with assets",
            ],
            cwd=self.root,
            check=True,
            capture_output=True,
        )
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        review = self.review()
        review["beforeRef"] = commit
        self.write_data("review.json", review)
        prefix = self.module._before_prefix(review)

        status, _, payload = self.request("GET", prefix + "/index.html")
        self.assertEqual(status, 200, payload)
        html = payload.decode("utf-8")
        self.assertIn('href="{}/styles.css"'.format(prefix), html)
        self.assertIn("url('{}/inline.png')".format(prefix), html)
        self.assertIn('@import "{}/inline.css"'.format(prefix), html)
        self.assertIn('src="{}/image.png"'.format(prefix), html)
        self.assertIn(
            'srcset="{}/small.png 1x, {}/large.png 2x"'.format(
                prefix, prefix
            ),
            html,
        )
        self.assertIn('src="relative.png"', html)
        self.assertIn('src="/__wk/host.js"', html)
        self.assertIn('data-wk-before-prefix="{}"'.format(prefix), html)

        status, _, payload = self.request("GET", prefix + "/styles.css")
        self.assertEqual(status, 200, payload)
        css = payload.decode("utf-8")
        self.assertIn("url({}/background.png)".format(prefix), css)
        self.assertIn("@import '{}/theme.css'".format(prefix), css)

        updated = self.review(round_number=2)
        updated["beforeRef"] = commit
        self.write_data("review.json", updated)
        status, _, payload = self.request("GET", prefix + "/index.html")
        self.assertEqual(status, 403, payload)
        self.assertNotIn(b"snapshot with assets", payload)

    def test_before_html_rewrite_is_scoped_to_real_attributes_and_styles(self):
        prefix = "/__wk/before/" + "c" * 64
        source = """<html><head>
<style>
.one { background: url('/one.png'); }
.two { background: url("/two.png"); }
.data { background: url("data:text/plain,url(/nested.png)"); }
.literal::before { content: "url(/literal.png)"; }
/* url(/comment.png) */
@import "/theme.css";
</style>
</head><body>
<img src=/unquoted.png poster='/poster.png'
  srcset="data:image/svg+xml,/payload 1x, /real.png 2x"
  style=background:url(/inline.png) data-note=" src=/not-an-attribute.png">
<script>const html = '<img src="/script.png">'; const css = 'url(/script.png)';</script>
<script type="application/json">{"src":"/data-script.png","css":"url(/data.png)"}</script>
<!-- <img src="/comment.png"><style>url(/comment-style.png)</style> -->
<template><img src="/template.png"><style>url(/template-style.png)</style></template>
</body></html>"""
        rewritten = self.module._rewrite_before_html(source, prefix)

        self.assertIn("src={}/unquoted.png".format(prefix), rewritten)
        self.assertIn("poster='{}/poster.png'".format(prefix), rewritten)
        self.assertIn("style=background:url({}/inline.png)".format(prefix), rewritten)
        self.assertIn(
            'srcset="data:image/svg+xml,/payload 1x, {}/real.png 2x"'.format(prefix),
            rewritten,
        )
        self.assertIn("url('{}/one.png')".format(prefix), rewritten)
        self.assertIn('url("{}/two.png")'.format(prefix), rewritten)
        self.assertIn('@import "{}/theme.css"'.format(prefix), rewritten)

        for unchanged in (
            'url("data:text/plain,url(/nested.png)")',
            'content: "url(/literal.png)"',
            "/* url(/comment.png) */",
            'data-note=" src=/not-an-attribute.png"',
            "const html = '<img src=\"/script.png\">'",
            "const css = 'url(/script.png)'",
            '{"src":"/data-script.png","css":"url(/data.png)"}',
            '<!-- <img src="/comment.png"><style>url(/comment-style.png)</style> -->',
            '<template><img src="/template.png"><style>url(/template-style.png)</style></template>',
        ):
            with self.subTest(unchanged=unchanged):
                self.assertIn(unchanged, rewritten)

        css = (
            ".a{background:url('/a.png')} .b{background:url(\"/b.png\")} "
            ".c{background:url(\"data:text/plain,url(/nested.png)\")} "
            ".d::before{content:'url(/literal.png)'} /* url(/comment.png) */"
        )
        rewritten_css = self.module._rewrite_before_css(css, prefix)
        self.assertIn("url('{}/a.png')".format(prefix), rewritten_css)
        self.assertIn('url("{}/b.png")'.format(prefix), rewritten_css)
        self.assertIn('url("data:text/plain,url(/nested.png)")', rewritten_css)
        self.assertIn("content:'url(/literal.png)'", rewritten_css)
        self.assertIn("/* url(/comment.png) */", rewritten_css)

    def test_before_snapshot_caps_rewrite_amplification(self):
        html = "<html><body>{}</body></html>".format(
            '<img src="/asset.png">' * 2000
        )
        css = ".item{{background:url(/asset.png)}}\n" * 2000
        (self.root / "index.html").write_text(html, encoding="utf-8")
        (self.root / "styles.css").write_text(css, encoding="utf-8")
        subprocess.run(
            ["git", "add", "index.html", "styles.css"],
            cwd=self.root,
            check=True,
        )
        subprocess.run(
            [
                "git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
                "commit", "-m", "amplification fixtures",
            ],
            cwd=self.root,
            check=True,
            capture_output=True,
        )
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        review = self.review()
        review["beforeRef"] = commit
        self.write_data("review.json", review)
        prefix = self.module._before_prefix(review)

        html_limit = len(self.module.stamp(html).encode("utf-8")) + 2048
        with mock.patch.object(
            self.module, "_MAX_TRANSFORMED_BEFORE_HTML", html_limit
        ):
            status, _, payload = self.request("GET", prefix + "/index.html")
        self.assertEqual(status, 413, payload)
        self.assertIn(b"transformed before snapshot HTML", payload)

        css_limit = len(css.encode("utf-8")) + 2048
        with mock.patch.object(
            self.module, "_MAX_TRANSFORMED_BEFORE_CSS", css_limit
        ):
            status, _, payload = self.request("GET", prefix + "/styles.css")
        self.assertEqual(status, 413, payload)
        self.assertIn(b"transformed before snapshot CSS", payload)

    def test_before_snapshot_rejects_a_blob_above_the_response_limit(self):
        subprocess.run(["git", "add", "index.html"], cwd=self.root, check=True)
        subprocess.run(
            [
                "git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
                "commit", "-m", "snapshot",
            ],
            cwd=self.root,
            check=True,
            capture_output=True,
        )
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        review = self.review()
        review["beforeRef"] = commit
        self.write_data("review.json", review)
        previous = self.module._MAX_BEFORE_BODY
        self.module._MAX_BEFORE_BODY = 4
        try:
            status, _, payload = self.request(
                "GET", self.before_path("/index.html")
            )
        finally:
            self.module._MAX_BEFORE_BODY = previous
        self.assertEqual(status, 413, payload)

    def test_configured_site_root_cannot_escape_through_a_symlink(self):
        with _WritableTemporaryDirectory() as outside:
            linked = self.root / "linked-site"
            linked.symlink_to(outside, target_is_directory=True)
            config = json.loads(self.config_path.read_text(encoding="utf-8"))
            config["site_root"] = "linked-site"
            self.config_path.write_text(json.dumps(config), encoding="utf-8")
            module_name = "preview_server_escape_test_{}".format(uuid.uuid4().hex)
            spec = importlib.util.spec_from_file_location(module_name, SERVER_PATH)
            module = importlib.util.module_from_spec(spec)
            with mock.patch.dict(os.environ, {
                "WK_CONFIG": str(self.config_path),
                "WK_COLOR_FORCE": "1",
            }), mock.patch.object(sys, "argv", [str(SERVER_PATH), "🔵", "5311"]):
                with self.assertRaisesRegex(SystemExit, "site_root must stay inside"):
                    spec.loader.exec_module(module)

    def test_config_json_rejects_unpaired_surrogates_but_accepts_emoji_pairs(self):
        base = json.loads(self.config_path.read_text(encoding="utf-8"))
        for unsafe in ("\ud800", "\udfff"):
            with self.subTest(unsafe=ascii(unsafe)):
                config = dict(base)
                config["project_name"] = unsafe
                with self.assertRaisesRegex(SystemExit, "unpaired Unicode surrogate"):
                    self.import_server(config, force="1")

        config = dict(base)
        config["palette"] = [
            {"slug": "blue", "emoji": "😀", "port": 5311}
        ]
        module = self.import_server(
            config,
            force="1",
            argv=[str(SERVER_PATH), "😀", "5311", str(self.root)],
        )
        self.assertEqual(module.COLOR, "😀")

    def test_feedback_directory_and_slug_cannot_escape_repository(self):
        with _WritableTemporaryDirectory() as outside:
            outside_path = Path(outside)
            linked = self.root / "linked-feedback"
            linked.symlink_to(outside_path, target_is_directory=True)
            base = json.loads(self.config_path.read_text(encoding="utf-8"))

            cases = (
                (str(outside_path), base["palette"], "feedback_dir must be a non-empty relative path"),
                ("../../outside-feedback", base["palette"], "feedback_dir must stay inside"),
                ("linked-feedback", base["palette"], "must not traverse symbolic links"),
                (".webkit/feedback", [{"slug": "../blue", "emoji": "🔵", "port": 5311}], "safe slug"),
            )
            for feedback_dir, palette, message in cases:
                with self.subTest(feedback_dir=feedback_dir, palette=palette):
                    config = dict(base)
                    config["feedback_dir"] = feedback_dir
                    config["palette"] = palette
                    self.config_path.write_text(json.dumps(config), encoding="utf-8")
                    module_name = "preview_server_path_test_{}".format(uuid.uuid4().hex)
                    spec = importlib.util.spec_from_file_location(module_name, SERVER_PATH)
                    module = importlib.util.module_from_spec(spec)
                    with mock.patch.dict(os.environ, {
                        "WK_CONFIG": str(self.config_path),
                        "WK_COLOR_FORCE": "1",
                    }), mock.patch.object(
                        sys, "argv", [str(SERVER_PATH), "🔵", "5311", str(self.root)]
                    ):
                        with self.assertRaisesRegex(SystemExit, message):
                            spec.loader.exec_module(module)

    def test_feedback_directory_cannot_traverse_an_in_repo_symlink(self):
        actual = self.root / "actual-feedback"
        actual.mkdir()
        (self.root / "linked-feedback").symlink_to(actual, target_is_directory=True)
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        config["feedback_dir"] = "linked-feedback"
        with self.assertRaisesRegex(SystemExit, "must not traverse symbolic links"):
            self.import_server(config, force="1")

    def test_feedback_directory_must_be_untracked_and_git_ignored(self):
        base = json.loads(self.config_path.read_text(encoding="utf-8"))

        repository_root = dict(base)
        repository_root["feedback_dir"] = "."
        with self.assertRaisesRegex(SystemExit, "repository root"):
            self.import_server(repository_root, force="1")

        unignored = dict(base)
        unignored["feedback_dir"] = "custom-feedback"
        with self.assertRaisesRegex(SystemExit, "must be Git-ignored"):
            self.import_server(unignored, force="1")

        tracked_root = self.root / "tracked-feedback"
        tracked_root.mkdir()
        (tracked_root / "keep.txt").write_text("tracked", encoding="utf-8")
        subprocess.run(
            ["git", "add", "-f", "tracked-feedback/keep.txt"],
            cwd=self.root,
            check=True,
        )
        with (self.root / ".gitignore").open("a", encoding="utf-8") as handle:
            handle.write("tracked-feedback/\ncustom-feedback/\n")
        tracked = dict(base)
        tracked["feedback_dir"] = "tracked-feedback"
        with self.assertRaisesRegex(SystemExit, "contains tracked files"):
            self.import_server(tracked, force="1")

        safe = self.import_server(unignored, force="1")
        self.assertEqual(
            Path(safe.FEEDBACK_DIR),
            (self.root / "custom-feedback" / "blue").resolve(),
        )
        self.assertTrue(Path(safe.FEEDBACK_DIR).is_dir())

    def test_project_storage_identity_is_repo_unique_and_shared_by_worktrees(self):
        with _WritableTemporaryDirectory() as workspace:
            workspace = Path(workspace)
            main = workspace / "main"
            linked = workspace / "linked"
            other = workspace / "other"
            main.mkdir()
            other.mkdir()
            for repository in (main, other):
                subprocess.run(
                    ["git", "init", "-b", "main"],
                    cwd=repository,
                    check=True,
                    capture_output=True,
                )
                (repository / ".gitignore").write_text(
                    ".webkit/feedback/\n", encoding="utf-8"
                )
            (main / "index.html").write_text("main", encoding="utf-8")
            subprocess.run(
                ["git", "add", "index.html", ".gitignore"],
                cwd=main,
                check=True,
            )
            subprocess.run(
                [
                    "git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
                    "commit", "-m", "initial",
                ],
                cwd=main,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "worktree", "add", str(linked), "HEAD"],
                cwd=main,
                check=True,
                capture_output=True,
            )
            (other / "index.html").write_text("other", encoding="utf-8")

            def load_for(repository):
                webkit = repository / "webkit"
                webkit.mkdir(exist_ok=True)
                config_path = webkit / "webkit.config.json"
                config = {
                    "project_name": "same-project",
                    "site_root": ".",
                    "feedback_dir": ".webkit/feedback",
                    "lock_dir": str(workspace / "locks"),
                    "palette": [{"slug": "blue", "emoji": "🔵", "port": 5311}],
                }
                config_path.write_text(json.dumps(config), encoding="utf-8")
                name = "preview_server_identity_{}".format(uuid.uuid4().hex)
                spec = importlib.util.spec_from_file_location(name, SERVER_PATH)
                module = importlib.util.module_from_spec(spec)
                with mock.patch.dict(os.environ, {
                    "WK_CONFIG": str(config_path),
                    "WK_COLOR_FORCE": "1",
                    "WK_ENABLE_API_PROXY": "",
                }), mock.patch.object(
                    sys,
                    "argv",
                    [str(SERVER_PATH), "🔵", "5311", str(repository)],
                ):
                    spec.loader.exec_module(module)
                return module

            main_module = load_for(main)
            linked_module = load_for(linked)
            other_module = load_for(other)
            self.assertEqual(
                main_module.PROJECT_STORAGE_ID, linked_module.PROJECT_STORAGE_ID
            )
            self.assertNotEqual(
                main_module.PROJECT_STORAGE_ID, other_module.PROJECT_STORAGE_ID
            )
            self.assertTrue(
                main_module.PROJECT_STORAGE_ID.startswith("same-project-")
            )
            injected = main_module.inject("<html><body></body></html>", "after")
            self.assertIn(
                'data-wk-project="{}"'.format(main_module.PROJECT_STORAGE_ID),
                injected,
            )

    def test_force_bypass_is_exact_and_subdirectory_site_uses_git_root_owner(self):
        config = {
            "site_root": ".",
            "feedback_dir": ".webkit/feedback",
            "lock_dir": str(self.root / "locks"),
            "palette": [{"slug": "blue", "emoji": "🔵", "port": 5311}],
        }
        with self.assertRaisesRegex(SystemExit, "no claim lock"):
            self.import_server(config, force="true")
        forced = self.import_server(config, force="1")
        self.assertIsNone(forced.CLAIM)

        site = self.root / "site"
        site.mkdir()
        (site / "index.html").write_text("<html></html>", encoding="utf-8")
        config["site_root"] = "site"
        port_registry = self.root / "port-locks"
        with mock.patch.dict(
            os.environ, {"WK_PORT_LOCKDIR": str(port_registry)}, clear=False
        ):
            claim_color(config["lock_dir"], "blue", 5311, str(self.root), 180)
            try:
                claimed = self.import_server(
                    config,
                    force=None,
                    argv=[str(SERVER_PATH), "🔵", "5311"],
                    env_overrides={"WK_PORT_LOCKDIR": str(port_registry)},
                )
                self.assertEqual(Path(claimed.ROOT), site.resolve())
                self.assertEqual(Path(claimed.GIT_ROOT), self.root.resolve())
                self.assertEqual(Path(claimed.CLAIM[1]), self.root.resolve())
                self.assertEqual(len(claimed.CLAIM), 3)
            finally:
                release_color(
                    config["lock_dir"], "blue", 5311, str(self.root)
                )

    def test_claim_heartbeat_touches_only_a_still_owned_regular_lock_and_stops(self):
        lock = self.root / "heartbeat.lock"
        lock.mkdir()
        owner = lock / "owner"
        owner.write_text(str(self.root) + "\n", encoding="utf-8")
        old = time.time() - 60
        os.utime(lock, (old, old))
        claim = (str(lock), str(self.root.resolve()))
        self.assertTrue(self.module._touch_owned_claim(claim))
        self.assertGreater(lock.stat().st_mtime, old)

        owner.write_text(str(self.root / "someone-else") + "\n", encoding="utf-8")
        os.utime(lock, (old, old))
        self.assertFalse(self.module._touch_owned_claim(claim))
        self.assertEqual(int(lock.stat().st_mtime), int(old))

        owner.unlink()
        owner.symlink_to(self.root / "index.html")
        self.assertFalse(self.module._touch_owned_claim(claim))

        symlink_target = self.root / "symlink-lock-target"
        symlink_target.mkdir()
        (symlink_target / "owner").write_text(str(self.root) + "\n", encoding="utf-8")
        symlink_lock = self.root / "symlink.lock"
        symlink_lock.symlink_to(symlink_target, target_is_directory=True)
        os.utime(symlink_target, (old, old))
        self.assertFalse(
            self.module._touch_owned_claim((str(symlink_lock), str(self.root.resolve())))
        )
        self.assertEqual(int(symlink_target.stat().st_mtime), int(old))

        owner.unlink()
        owner.write_text(str(self.root) + "\n", encoding="utf-8")
        os.utime(lock, (old, old))
        stop = threading.Event()
        worker = threading.Thread(
            target=self.module._claim_heartbeat,
            args=(stop, 0.01, claim),
        )
        worker.start()
        deadline = time.time() + 1
        while lock.stat().st_mtime <= old and time.time() < deadline:
            time.sleep(0.01)
        self.assertGreater(lock.stat().st_mtime, old)
        stop.set()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        stopped_mtime = lock.stat().st_mtime_ns
        time.sleep(0.04)
        self.assertEqual(lock.stat().st_mtime_ns, stopped_mtime)

    def test_config_object_proxy_and_port_validation_fail_cleanly(self):
        base = {
            "site_root": ".",
            "feedback_dir": ".webkit/feedback",
            "lock_dir": str(self.root / "locks"),
            "palette": [{"slug": "blue", "emoji": "🔵", "port": 5311}],
        }
        with self.assertRaisesRegex(SystemExit, "JSON object"):
            self.import_server([], force="1")

        invalid_project = dict(base)
        invalid_project["project_name"] = "bad project<script>"
        with self.assertRaisesRegex(SystemExit, "project_name must be a safe token"):
            self.import_server(invalid_project, force="1")

        invalid_site_root = dict(base)
        invalid_site_root["site_root"] = ["."]
        with self.assertRaisesRegex(SystemExit, "site_root must be a non-empty relative path"):
            self.import_server(
                invalid_site_root,
                force="1",
                argv=[str(SERVER_PATH), "🔵", "5311"],
            )

        invalid_lock_dir = dict(base)
        invalid_lock_dir["lock_dir"] = "relative-locks"
        with self.assertRaisesRegex(SystemExit, "lock_dir must be an absolute"):
            self.import_server(invalid_lock_dir, force=None)

        for origin in (
            123,
            "https://127.0.0.1:1234",
            "http://example.com:1234",
            "http://user@127.0.0.1:1234",
            "http://127.0.0.1:1234/path",
            "http://127.0.0.1",
        ):
            with self.subTest(origin=origin):
                config = dict(base)
                config["api_proxy_origin"] = origin
                with self.assertRaises(SystemExit):
                    self.import_server(
                        config,
                        force="1",
                        env_overrides={"WK_ENABLE_API_PROXY": "1"},
                    )

        for port in (True, 0, 65536, "5311"):
            with self.subTest(port=port):
                config = dict(base)
                config["palette"] = [{"slug": "blue", "emoji": "🔵", "port": port}]
                with self.assertRaisesRegex(SystemExit, "integer ports"):
                    self.import_server(config, force="1")

        duplicate = dict(base)
        duplicate["palette"] = [
            {"slug": "blue", "emoji": "🔵", "port": 5311},
            {"slug": "red", "emoji": "🔴", "port": 5311},
        ]
        with self.assertRaisesRegex(SystemExit, "port values must be unique"):
            self.import_server(duplicate, force="1")

        with self.assertRaisesRegex(SystemExit, "between 1 and 65535"):
            self.import_server(
                base,
                force="1",
                argv=[str(SERVER_PATH), "🔵", "70000", str(self.root)],
            )


if __name__ == "__main__":
    unittest.main()
