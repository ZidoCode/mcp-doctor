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


FAKE_EMPTY_SERVER = r'''
import json, sys
def send(o):
    sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
for line in sys.stdin:
    try:
        msg = json.loads(line)
    except Exception:
        continue
    if msg.get("method") == "initialize":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "serverInfo": {"name": "empty", "version": "0"}}})
    elif msg.get("method") == "tools/list":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": []}})
'''

# Answers the handshake with a JSON-RPC error, so inspection fails immediately.
# Deterministic "could not be inspected" without depending on exit timing.
FAKE_REFUSE_SERVER = r'''
import json, sys
def send(o):
    sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
for line in sys.stdin:
    try:
        msg = json.loads(line)
    except Exception:
        continue
    if msg.get("method") == "initialize":
        send({"jsonrpc": "2.0", "id": msg["id"],
              "error": {"code": -32601, "message": "handshake refused"}})
'''


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

    def test_collide_no_args_is_actionable(self):
        r = self._run("collide")
        self.assertEqual(r.returncode, 2)
        self.assertIn("needs a config file", r.stderr)

    def test_collide_tools_mode_runs(self):
        p = os.path.join(self.d, "tools.json")
        with open(p, "w") as f:
            f.write(jl([{"name": "fs.read"}, {"name": "fs.write"},
                        {"name": "fs.read_file"}]))
        r = self._run("collide", "--tools", p, "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["collisions"], {})

    def test_collide_uninspected_server_exits_one(self):
        # Deterministic stand-in for "never answered": the server refuses the
        # handshake outright, so inspection fails without depending on
        # process-exit timing.
        fake = os.path.join(self.d, "refuse_server.py")
        with open(fake, "w") as f:
            f.write(FAKE_REFUSE_SERVER)
        p = os.path.join(self.d, "mcp.json")
        with open(p, "w") as f:
            f.write(jl({"mcpServers": {"half": {
                "command": sys.executable, "args": [fake]}}}))
        r = self._run("collide", p, "--client", "claude", "--timeout", "5")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)  # never inspected
        self.assertIn("client: claude", r.stdout)
        self.assertIn("could not be inspected", r.stderr)
        self.assertNotIn("invalid choice", r.stderr)

    def test_collide_answered_but_empty_inventory_is_clean(self):
        fake = os.path.join(self.d, "fake_empty.py")
        with open(fake, "w") as f:
            f.write(FAKE_EMPTY_SERVER)
        p = os.path.join(self.d, "mcp.json")
        with open(p, "w") as f:
            f.write(jl({"mcpServers": {"empty": {
                "command": sys.executable, "args": [fake]}}}))
        r = self._run("collide", p, "--timeout", "5")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("1 servers, 0 tools", r.stdout)

    def test_collide_missing_tool_list_is_clean_error(self):
        r = self._run("collide", "--tools", os.path.join(self.d, "nope.json"))
        self.assertEqual(r.returncode, 2)
        self.assertIn("tool list not found", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_collide_invalid_tool_list_json(self):
        p = os.path.join(self.d, "bad.json")
        with open(p, "w") as f:
            f.write("not json\n")
        r = self._run("collide", "--tools", p)
        self.assertEqual(r.returncode, 2)
        self.assertIn("not valid JSON", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

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

    def test_version_prefix_follows_invoked_name(self):
        # argv[0] is what a console script (or a shell alias) sets; assert the
        # version line uses it instead of a hardcoded distribution name.
        code = ("import sys; sys.argv = ['mcp-doctor', '--version']; "
                "import mcp_doctor; mcp_doctor.main()")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                           cwd=os.path.dirname(os.path.abspath(self.script)))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout.strip(), r"^mcp-doctor \d+\.\d+\.\d+")

    def test_version_uses_metadata_branch(self):
        # Covers the branch CI hits only when the package is installed, so the
        # hardcoded-prefix regression cannot hide behind a missing metadata.
        from unittest import mock
        with mock.patch.object(mcp_doctor, "_prog", return_value="mcp-doctor"):
            with mock.patch("importlib.metadata.version", return_value="9.9.9"):
                self.assertEqual(mcp_doctor._version(), "mcp-doctor 9.9.9")

    def test_version_falls_back_to_pyproject(self):
        from unittest import mock
        import importlib.metadata
        with mock.patch.object(mcp_doctor, "_prog", return_value="mcpmedic"):
            with mock.patch("importlib.metadata.version",
                            side_effect=importlib.metadata.PackageNotFoundError):
                out = mcp_doctor._version()
        self.assertRegex(out, r"^mcpmedic \d+\.\d+\.\d+$")

    def test_version_unknown_when_pyproject_missing(self):
        from unittest import mock
        import importlib.metadata
        import pathlib
        with mock.patch.object(mcp_doctor, "_prog", return_value="mcp-doctor"):
            with mock.patch("importlib.metadata.version",
                            side_effect=importlib.metadata.PackageNotFoundError):
                with mock.patch.object(pathlib.Path, "read_text", side_effect=OSError("nope")):
                    self.assertEqual(mcp_doctor._version(), "mcp-doctor unknown")

    def test_version(self):
        r = self._run("--version")
        self.assertEqual(r.returncode, 0)
        self.assertRegex(r.stdout.strip(), r"^mcpmedic \d+\.\d+\.\d+")

    def test_modules_are_same_auditors(self):
        # vendored modules must be importable and functional
        self.assertTrue(hasattr(mcp_doctor.mcp_session_audit, "audit"))
        self.assertTrue(hasattr(mcp_doctor.mcp_schema_audit, "audit"))
        f = mcp_doctor.mcp_session_audit.audit([jl({"direction": "request", "id": 1,
                                                    "method": "tools/call", "params": {}})])
        self.assertTrue(f)


if __name__ == "__main__":
    unittest.main()
