import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "webkit" / "scripts" / "read-feedback.py"
spec = importlib.util.spec_from_file_location("read_feedback", str(SCRIPT))
READER = importlib.util.module_from_spec(spec)
spec.loader.exec_module(READER)


class FeedbackReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "feedback.json"

    def write(self, points, kind="feedback"):
        self.path.write_text(json.dumps({
            "version": 1, "kind": kind, "batchId": "batch-reader", "round": 2,
            "points" if kind == "feedback" else "verdicts": points,
        }, ensure_ascii=False), encoding="utf-8")

    def run_reader(self, *args):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), str(self.path)] + list(args),
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_inventory_and_individual_read_preserve_instruction_with_large_context(self):
        instruction = "Reduce this empty space.\nלהקטין את הרווח\nKeep the next heading visible."
        point = {
            "id": "p-gap", "number": 1, "revision": 3, "page": "/index.html",
            "text": instruction, "voiceNote": None, "abcRequest": None,
            "rect": {"x": 100, "y": 400, "w": 300, "h": 120},
            "context": [], "rectContexts": [[{"text": "scene " * 10000}]],
        }
        self.write([point])
        original = self.path.read_bytes()
        inventory = self.run_reader()
        self.assertIn("p-gap | text=PRESENT", inventory)
        output = self.run_reader("--point", "p-gap")
        self.assertIn(instruction, output)
        self.assertIn("revision: 3", output)
        self.assertIn('"y": 400', output)
        self.assertNotIn("scene ", output)
        self.assertLess(len(output), 1000)
        self.assertIn("scene ", self.run_reader("--point", "p-gap", "--context"))
        self.assertEqual(self.path.read_bytes(), original)

    def test_missing_empty_voice_and_variants_remain_distinct(self):
        note = {"path": ".webkit/feedback/blue/voice-notes/test.webm", "language": "he"}
        variants = {"mode": "user", "prompts": {"A": "Tighter", "B": "Wider"}}
        self.write([
            {"id": "p-missing"},
            {"id": "p-voice", "text": " \n", "voiceNote": note},
            {"id": "p-variants", "text": "", "abcRequest": variants},
        ])
        inventory = self.run_reader()
        self.assertIn("text=MISSING FIELD", inventory)
        self.assertIn("text=EMPTY (2 characters) | voiceNote=PRESENT", inventory)
        self.assertIn("abcRequest=PRESENT", inventory)
        self.assertIn(note["path"], self.run_reader("--point", "p-voice"))
        self.assertIn('"A": "Tighter"', self.run_reader("--point", "p-variants"))

    def test_redo_preserves_all_instruction_sources(self):
        self.write([{
            "pointId": "p-redo", "verdict": "redo", "redoText": "Tighter again\nThanks",
            "redoVoiceNote": {"path": "voice.webm"},
            "redoAbcRequest": {"mode": "user", "prompts": {"A": "Compact"}},
        }], "verdicts")
        output = self.run_reader("--point", "p-redo")
        self.assertIn("Tighter again\nThanks", output)
        self.assertIn('redoVoiceNote: {"path": "voice.webm"}', output)
        self.assertIn('"A": "Compact"', output)

    def test_read_errors_never_report_empty_feedback(self):
        for raw, args in [
            ("{", []),
            (json.dumps({"version": 1, "kind": "feedback_update"}), []),
            (json.dumps({"version": 1, "kind": "feedback", "points": [
                {"id": "p-one", "text": "Keep me"}, {"id": "p-one"}
            ]}), []),
            (json.dumps({"version": 1, "kind": "feedback", "points": []}), ["--point", "p-absent"]),
        ]:
            with self.subTest(raw=raw):
                self.path.write_text(raw, encoding="utf-8")
                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    code = READER.main([str(self.path)] + args)
                self.assertEqual(code, 2)
                self.assertEqual(stdout.getvalue(), "")
                self.assertIn("read-feedback.py:", stderr.getvalue())

    def test_input_limit_is_bounded(self):
        self.path.write_bytes(b" " * (READER.MAX_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "2 MiB"):
            READER.load_document(self.path)


if __name__ == "__main__":
    unittest.main()
