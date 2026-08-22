import http.client
import importlib.util
import errno
import json
import os
import stat
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_script(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TRANSITION = load_script(
    "transition_round", ROOT / "webkit" / "scripts" / "transition-round.py"
)
TRANSCRIBE = load_script(
    "transcribe_voice_note", ROOT / "webkit" / "scripts" / "transcribe-voice-note.py"
)


class TransitionRoundTests(unittest.TestCase):
    def test_control_center_mode_queues_transition_without_local_network_or_token(self):
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw) / "repo"
            repo.mkdir()
            subprocess.run(
                ["git", "init", "-b", "main"], cwd=repo,
                check=True, capture_output=True,
            )
            inbox = repo / ".webkit" / "feedback" / "blue"
            inbox.mkdir(parents=True)
            config = repo / "webkit.config.json"
            config.write_text(json.dumps({
                "feedback_dir": ".webkit/feedback",
                "palette": [{"slug": "blue", "port": 5311}],
            }), encoding="utf-8")
            next_review = repo / "next-review.json"
            next_review.write_text(json.dumps({
                "version": 1,
                "kind": "review",
                "batchId": "batch-1",
                "round": 2,
                "beforeRef": "a" * 40,
                "points": [],
            }), encoding="utf-8")
            with mock.patch.dict(os.environ, {
                "WK_CONTROL_CENTER": "1",
                "WK_CONFIG": str(config),
            }, clear=False), mock.patch.object(
                TRANSITION.os, "getcwd", return_value=str(repo)
            ), mock.patch.object(
                TRANSITION, "request_transition"
            ) as request, mock.patch("builtins.print"):
                code = TRANSITION.main([
                    "blue", "redo", "batch-1", "1",
                    "--next-review", str(next_review),
                ])

            self.assertEqual(code, 0)
            request.assert_not_called()
            queued = json.loads(
                (inbox / "transition-request.json").read_text(encoding="utf-8")
            )
            self.assertEqual(queued["mode"], "redo")
            self.assertEqual(queued["nextReview"]["round"], 2)
            self.assertFalse((inbox / "transition-token").exists())
            if os.name == "posix":
                self.assertEqual(
                    stat.S_IMODE((inbox / "transition-request.json").stat().st_mode),
                    0o600,
                )

    def test_control_center_queue_replaces_a_symlink_without_touching_its_target(self):
        with tempfile.TemporaryDirectory() as raw:
            inbox = Path(raw) / "feedback"
            inbox.mkdir()
            sentinel = Path(raw) / "sentinel.json"
            sentinel.write_text('{"safe": true}\n', encoding="utf-8")
            request = inbox / "transition-request.json"
            try:
                request.symlink_to(sentinel)
            except (OSError, NotImplementedError) as error:
                self.skipTest("symbolic links unavailable: {}".format(error))

            TRANSITION.queue_control_center_transition(inbox, {
                "version": 1,
                "mode": "complete",
                "batchId": "batch-1",
                "round": 1,
            })

            self.assertEqual(
                sentinel.read_text(encoding="utf-8"), '{"safe": true}\n'
            )
            self.assertFalse(request.is_symlink())
            self.assertEqual(json.loads(request.read_text(encoding="utf-8"))["mode"], "complete")

    def test_control_center_queue_tolerates_agent_sandbox_fsync_eperm(self):
        with tempfile.TemporaryDirectory() as raw:
            inbox = Path(raw) / "feedback"
            inbox.mkdir()
            real_fsync = TRANSITION.os.fsync

            def sandboxed_fsync(descriptor):
                if stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise OSError(errno.EPERM, "Operation not permitted")
                return real_fsync(descriptor)

            with mock.patch.object(TRANSITION.os, "fsync", side_effect=sandboxed_fsync):
                TRANSITION.queue_control_center_transition(inbox, {
                    "version": 1,
                    "mode": "complete",
                    "batchId": "batch-1",
                    "round": 1,
                })

            queued = json.loads(
                (inbox / "transition-request.json").read_text(encoding="utf-8")
            )
            self.assertEqual(queued["mode"], "complete")

    def test_control_center_queue_keeps_real_fsync_failures_fatal(self):
        with tempfile.TemporaryDirectory() as raw:
            inbox = Path(raw) / "feedback"
            inbox.mkdir()
            with mock.patch.object(
                TRANSITION.os, "fsync", side_effect=OSError(errno.EIO, "I/O error")
            ):
                with self.assertRaisesRegex(ValueError, "could not be queued"):
                    TRANSITION.queue_control_center_transition(inbox, {
                        "version": 1,
                        "mode": "complete",
                        "batchId": "batch-1",
                        "round": 1,
                    })
            self.assertFalse((inbox / "transition-request.json").exists())

    def test_config_requires_a_bounded_stable_regular_file(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            valid = base / "valid-config.json"
            valid.write_text('{"palette": []}', encoding="utf-8")
            with mock.patch.dict(os.environ, {"WK_CONFIG": str(valid)}, clear=False):
                self.assertEqual(
                    TRANSITION.load_config(base / "scripts"), {"palette": []}
                )

            oversized = base / "oversized-config.json"
            with oversized.open("wb") as handle:
                handle.seek(TRANSITION._MAX_CONFIG)
                handle.write(b"x")
            with mock.patch.dict(os.environ, {"WK_CONFIG": str(oversized)}, clear=False):
                with self.assertRaisesRegex(ValueError, "exceeds"):
                    TRANSITION.load_config(base / "scripts")

            linked = base / "linked-config.json"
            try:
                linked.symlink_to(valid)
            except (OSError, NotImplementedError) as error:
                self.skipTest("symbolic links unavailable: {}".format(error))
            with mock.patch.dict(os.environ, {"WK_CONFIG": str(linked)}, clear=False):
                with self.assertRaisesRegex(ValueError, "regular file"):
                    TRANSITION.load_config(base / "scripts")

            if hasattr(os, "mkfifo"):
                fifo = base / "config.pipe"
                os.mkfifo(str(fifo))
                with mock.patch.dict(os.environ, {"WK_CONFIG": str(fifo)}, clear=False):
                    with self.assertRaisesRegex(ValueError, "regular file"):
                        TRANSITION.load_config(base / "scripts")

    def test_transition_token_requires_a_stable_private_regular_file(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            token = base / "transition-token"
            token.write_text("safe-token-1234567890\n", encoding="utf-8")
            if os.name == "posix":
                token.chmod(0o600)
            self.assertEqual(
                TRANSITION.read_private_token(token), "safe-token-1234567890"
            )

            oversized = base / "oversized-token"
            oversized.write_bytes(b"a" * 257)
            if os.name == "posix":
                oversized.chmod(0o600)
            with self.assertRaisesRegex(ValueError, "unexpectedly large"):
                TRANSITION.read_private_token(oversized)

            linked = base / "linked-token"
            try:
                linked.symlink_to(token)
            except (OSError, NotImplementedError) as error:
                self.skipTest("symbolic links unavailable: {}".format(error))
            with self.assertRaisesRegex(ValueError, "regular file"):
                TRANSITION.read_private_token(linked)

    def test_review_requires_bounded_regular_strict_json_object(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            valid = base / "valid.json"
            valid.write_text('{"ok": true}', encoding="utf-8")
            self.assertEqual(TRANSITION.load_review(valid), {"ok": True})

            for index, value in enumerate(("[]", "NaN", "Infinity", "-Infinity")):
                with self.subTest(value=value):
                    invalid = base / ("invalid-{}.json".format(index))
                    invalid.write_text(value, encoding="utf-8")
                    with self.assertRaises(ValueError):
                        TRANSITION.load_review(invalid)

            oversized = base / "oversized.json"
            oversized.write_bytes(b"{" + b" " * TRANSITION._MAX_NEXT_REVIEW)
            with self.assertRaisesRegex(ValueError, "exceeds 2 MB"):
                TRANSITION.load_review(oversized)

            linked = base / "linked.json"
            try:
                linked.symlink_to(valid)
            except (OSError, NotImplementedError) as error:
                self.skipTest("symbolic links unavailable: {}".format(error))
            with self.assertRaisesRegex(ValueError, "regular file"):
                TRANSITION.load_review(linked)

            if hasattr(os, "mkfifo"):
                fifo = base / "review.pipe"
                os.mkfifo(str(fifo))
                with self.assertRaisesRegex(ValueError, "regular file"):
                    TRANSITION.load_review(fifo)

    def test_transition_json_rejects_unpaired_surrogates_and_accepts_pairs(self):
        for payload in (
            r'{"value":"\ud800"}',
            r'{"\udfff":"value"}',
        ):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(
                    ValueError, "unpaired Unicode surrogate"
                ):
                    TRANSITION.strict_json_loads(payload)

        self.assertEqual(
            TRANSITION.strict_json_loads(r'{"emoji":"\ud83d\ude00"}'),
            {"emoji": "😀"},
        )

    def test_review_detects_in_place_mutation_metadata(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "review.json"
            path.write_text('{"ok": true}', encoding="utf-8")
            real_fstat = os.fstat
            calls = []

            def changed_fstat(descriptor):
                value = real_fstat(descriptor)
                calls.append(value)
                if len(calls) != 2:
                    return value
                return types.SimpleNamespace(
                    st_mode=value.st_mode,
                    st_dev=value.st_dev,
                    st_ino=value.st_ino,
                    st_size=value.st_size + 1,
                    st_mtime_ns=value.st_mtime_ns,
                )

            with mock.patch.object(TRANSITION.os, "fstat", side_effect=changed_fstat):
                with self.assertRaisesRegex(ValueError, "changed while it was read"):
                    TRANSITION.load_review(path)

    def test_request_transition_bounds_and_validates_http_response(self):
        response = mock.Mock(status=200)
        response.read.return_value = b"x" * (TRANSITION._MAX_RESPONSE + 1)
        connection = mock.Mock()
        connection.getresponse.return_value = response
        with mock.patch.object(
            TRANSITION.http.client, "HTTPConnection", return_value=connection
        ):
            with self.assertRaisesRegex(ValueError, "exceeded 2 MB"):
                TRANSITION.request_transition(5311, "safe-token-123456", b"{}")
        response.read.assert_called_once_with(TRANSITION._MAX_RESPONSE + 1)
        connection.close.assert_called_once_with()

        connection = mock.Mock()
        connection.request.side_effect = http.client.BadStatusLine("bad status")
        with mock.patch.object(
            TRANSITION.http.client, "HTTPConnection", return_value=connection
        ):
            with self.assertRaisesRegex(ValueError, "preview server request failed"):
                TRANSITION.request_transition(5311, "safe-token-123456", b"{}")
        connection.close.assert_called_once_with()


class TranscriptionTests(unittest.TestCase):
    def test_local_command_has_a_clear_configurable_timeout(self):
        timeout = subprocess.TimeoutExpired(["whisper"], 1.5)
        with mock.patch.dict(os.environ, {"WK_TRANSCRIPTION_TIMEOUT": "1.5"}), mock.patch.object(
            TRANSCRIBE.subprocess, "run", side_effect=timeout
        ) as run:
            with self.assertRaisesRegex(
                RuntimeError, "Python Whisper transcription timed out after 1.5 seconds"
            ):
                TRANSCRIBE.run_local_command(["whisper"], "Python Whisper transcription")
        self.assertEqual(run.call_args[1]["timeout"], 1.5)

    def test_transcription_timeout_must_be_positive_and_finite(self):
        for value in ("0", "-1", "nan", "inf", "not-a-number"):
            with self.subTest(value=value), mock.patch.dict(
                os.environ, {"WK_TRANSCRIPTION_TIMEOUT": value}
            ):
                with self.assertRaisesRegex(RuntimeError, "positive"):
                    TRANSCRIBE.command_timeout_seconds()

    def test_openai_transcription_sends_saved_audio_and_returns_text(self):
        with tempfile.TemporaryDirectory() as raw:
            audio = Path(raw) / "voice.webm"
            audio.write_bytes(b"safe-audio")
            response = mock.MagicMock()
            response.__enter__.return_value = response
            response.read.return_value = json.dumps({
                "text": "A clear cloud transcript."
            }).encode("utf-8")
            with mock.patch.dict(
                os.environ,
                {"OPENAI_API_KEY": "test-key-not-real", "WK_TRANSCRIPTION_TIMEOUT": "12"},
                clear=False,
            ), mock.patch.object(
                TRANSCRIBE.urllib.request, "urlopen", return_value=response
            ) as urlopen:
                result = TRANSCRIBE.transcribe_openai(audio, "en")

        self.assertEqual(result, "A clear cloud transcript.")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, TRANSCRIBE.OPENAI_TRANSCRIPTION_URL)
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 12)
        self.assertIn(b'gpt-4o-transcribe', request.data)
        self.assertIn(b'safe-audio', request.data)
        self.assertNotIn(b'test-key-not-real', request.data)

    def test_openai_transcription_requires_an_explicit_api_key(self):
        with tempfile.TemporaryDirectory() as raw:
            audio = Path(raw) / "voice.webm"
            audio.write_bytes(b"safe-audio")
            with mock.patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False):
                with self.assertRaisesRegex(RuntimeError, "OPENAI_API_KEY"):
                    TRANSCRIBE.transcribe_openai(audio, "en")


if __name__ == "__main__":
    unittest.main()
