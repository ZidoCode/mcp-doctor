"""Tests for mcp-doctor — umbrella CLI over session/schema/collision auditors."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mcp_doctor


def jl(obj):
    return json.dumps(obj)


INIT_OK = {"direction": "response", "id": 1, "result": {
    "protocolVersion": "2025-11-25", "serverInfo": {"name": "srv", "version": "1.0"}}}


def write(path, lines):
    with open(path, "w") as fh:
        fh.write("\n".join(lines))
    return path


class TestCLI(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.clean = write(os.path.join(self.d, "clean.jsonl"), [
            jl(INIT_OK),
            jl({"direction": "request", "id": 2, "method": "tools/call",
                "params": {"name": "t"}}),
            jl({"direction": "response", "id": 2,
                "result": {"content": [{"type": "text", "text": "ok"}]}}),
            jl({"direction": "response", "id": 3, "result": {"tools": [
                {"name": "t", "inputSchema": {"type": "object", "properties": {}}}]}}),
        ])
        self.dirty = write(os.path.join(self.d, "dirty.jsonl"), [
            jl(INIT_OK),
            jl({"direction": "request", "id": 2, "method": "tools/call",
                "params": {"name": "t"}}),
            # dangling: no response for id 2
            jl({"direction": "response", "id": 3, "result": {"tools": [
                {"name": "t", "inputSchema": {}}  # schema-empty
            ]}}),
        ])
        self.script = os.path.join(os.path.dirname(mcp_doctor.__file__), "mcp_doctor.py")

    def _run(self, *argv):
        return subprocess.run([sys.executable, self.script, *argv],
                              capture_output=True, text=True)

    def test_help(self):
        r = self._run("--help")
        self.assertEqual(r.returncode, 0)
        for cmd in ("session", "schema", "check", "collide"):
            self.assertIn(cmd, r.stdout)

    def test_session_clean(self):
        r = self._run("session", self.clean)
        self.assertEqual(r.returncode, 0)

    def test_session_dirty(self):
        r = self._run("session", self.dirty)
        self.assertEqual(r.returncode, 2)
        self.assertIn("dangling", r.stdout)

    def test_schema_dirty(self):
        r = self._run("schema", self.dirty)
        self.assertEqual(r.returncode, 2)
        self.assertIn("gitlab-mcp #286", r.stdout)

    def test_check_clean(self):
        r = self._run("check", self.clean)
        self.assertEqual(r.returncode, 0)

    def test_check_dirty_combines(self):
        r = self._run("check", self.dirty)
        self.assertEqual(r.returncode, 2)
        # both audit families fire
        self.assertIn("dangling", r.stdout)
        self.assertIn("inputSchema", r.stdout)

    def test_check_counts(self):
        r = self._run("check", self.dirty)
        self.assertIn("verdict:", r.stdout)

    def test_no_args_exits_nonzero(self):
        r = self._run()
        self.assertNotEqual(r.returncode, 0)

    def test_version(self):
        r = self._run("--version")
        self.assertEqual(r.returncode, 0)
        self.assertIn("0.1.0", r.stdout)

    def test_modules_are_same_auditors(self):
        # vendored modules must be importable and functional
        self.assertTrue(hasattr(mcp_doctor.mcp_session_audit, "audit"))
        self.assertTrue(hasattr(mcp_doctor.mcp_schema_audit, "audit"))
        f = mcp_doctor.mcp_session_audit.audit([jl({"direction": "request", "id": 1,
                                                    "method": "tools/call", "params": {}})])
        self.assertTrue(f)


if __name__ == "__main__":
    unittest.main()
