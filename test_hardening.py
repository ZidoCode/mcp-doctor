"""Regression tests for the hardening pass (see CHANGELOG, Unreleased).

Every test here reproduces a bug found in the 0.1.5 review before it was
fixed; each one fails on the pre-fix code.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mcp_collision
import mcp_schema_audit
import mcp_session_audit

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "mcp_doctor.py")
COLLISION_SCRIPT = os.path.join(HERE, "mcp_collision.py")


def jl(obj):
    return json.dumps(obj)


def write_tmp(d, name, text):
    p = os.path.join(d, name)
    with open(p, "w") as f:
        f.write(text)
    return p


SLEEP_SERVER = "import time\ntime.sleep(60)\n"

WEIRD_SERVER = (
    "import sys, time\n"
    "sys.stdout.write('[1,2]\\n'); sys.stdout.flush()\n"
    "time.sleep(30)\n"
)


class TestCollideTimeout(unittest.TestCase):
    def test_hung_server_is_killed_at_deadline(self):
        d = tempfile.mkdtemp()
        fake = write_tmp(d, "sleep_server.py", SLEEP_SERVER)
        start = time.monotonic()
        tools, err = mcp_collision.list_tools([sys.executable, fake], timeout=2)
        elapsed = time.monotonic() - start
        self.assertIsNone(tools)
        self.assertIn("timed out", err)
        # Old code blocked until the server's 60s sleep ended.
        self.assertLess(elapsed, 30)


class TestJsonc(unittest.TestCase):
    def _cfg(self, text):
        return write_tmp(tempfile.mkdtemp(), "mcp.json", text)

    def test_url_inside_string_survives(self):
        servers = mcp_collision.load_config(self._cfg(
            '{"mcpServers": {"web": {"command": "npx", '
            '"args": ["-y", "https://example.com/server"]}}}'))
        self.assertEqual(servers["web"]["args"],
                         ["-y", "https://example.com/server"])

    def test_comments_and_trailing_commas_still_stripped(self):
        servers = mcp_collision.load_config(self._cfg(
            '{\n// a comment\n"mcpServers": {"s": {"command": "x", '
            '/* inline */ "args": ["a",],}},}'))
        self.assertEqual(servers["s"]["args"], ["a"])

    def test_comma_inside_string_preserved(self):
        servers = mcp_collision.load_config(self._cfg(
            '{"mcpServers": {"s": {"command": "x", "args": ["a,}"]}}}'))
        self.assertEqual(servers["s"]["args"], ["a,}"])


class TestCheckBasename(unittest.TestCase):
    def test_same_basename_files_both_scanned(self):
        d = tempfile.mkdtemp()
        paths = []
        for sub in ("a", "b"):
            dd = os.path.join(d, sub)
            os.makedirs(dd)
            p = os.path.join(dd, "tools.json")
            with open(p, "w") as f:
                json.dump([{"name": "dup.tool"}], f)
            paths.append(p)
        r = subprocess.run([sys.executable, COLLISION_SCRIPT, "check",
                            *paths, "--client", "raw"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("COLLISION", r.stdout)


class TestScanHardening(unittest.TestCase):
    def _run_doctor(self, *argv):
        return subprocess.run([sys.executable, SCRIPT, *argv],
                              capture_output=True, text=True)

    def test_malformed_server_entries_are_skipped_not_crash(self):
        d = tempfile.mkdtemp()
        cfg = write_tmp(d, "mcp.json", jl({"mcpServers": {
            "bad_args": {"command": "x", "args": "--not-a-list"},
            "bad_entry": "not-an-object"}}))
        r = self._run_doctor("collide", cfg, "--client", "raw", "--timeout", "2")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("skipped", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_non_object_response_is_failure_not_crash(self):
        d = tempfile.mkdtemp()
        fake = write_tmp(d, "weird_server.py", WEIRD_SERVER)
        cfg = write_tmp(d, "mcp.json", jl({"mcpServers": {
            "weird": {"command": sys.executable, "args": [fake]}}}))
        r = self._run_doctor("collide", cfg, "--client", "raw", "--timeout", "3")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stderr)


class TestSessionHardening(unittest.TestCase):
    def test_mixed_id_types_do_not_crash(self):
        lines = [
            jl({"direction": "request", "id": 1, "method": "tools/call",
                "params": {"name": "t"}}),
            jl({"direction": "request", "id": "x", "method": "tools/call",
                "params": {"name": "t"}}),
        ]
        findings = mcp_session_audit.audit(lines)
        self.assertEqual(
            sum(1 for f in findings if f[1] == "dangling-call"), 2)

    def test_known_tools_union_across_listings(self):
        lines = [
            jl({"direction": "response", "id": 1, "result": {"tools": [
                {"name": "alpha", "inputSchema": {"type": "object"}}]}}),
            jl({"direction": "response", "id": 2, "result": {"tools": [
                {"name": "beta", "inputSchema": {"type": "object"}}]}}),
            jl({"direction": "request", "id": 3, "method": "tools/call",
                "params": {"name": "alpha"}}),
            jl({"direction": "response", "id": 3, "result": {
                "content": [{"type": "text", "text": "ok"}]}}),
        ]
        findings = mcp_session_audit.audit(lines)
        self.assertFalse([f for f in findings if f[1] == "unknown-tool"])

    def test_unhashable_ids_do_not_crash(self):
        lines = [
            jl({"direction": "request", "id": {"a": 1}, "method": "tools/call",
                "params": {"name": "t"}}),
            jl({"direction": "response", "id": [1, 2],
                "result": {"content": [{"type": "text", "text": "ok"}]}}),
        ]
        findings = mcp_session_audit.audit(lines)
        self.assertTrue([f for f in findings if f[1] == "dangling-call"])
        self.assertTrue([f for f in findings if f[1] == "orphan-response"])

    def test_orphan_error_response_without_requests_is_flagged(self):
        lines = [jl({"direction": "response", "id": 9,
                     "error": {"code": -32603, "message": "boom"}})]
        findings = mcp_session_audit.audit(lines)
        self.assertTrue([f for f in findings if f[1] == "orphan-response"])


class TestSchemaHardening(unittest.TestCase):
    def test_tuple_items_without_schema_dialect_flagged(self):
        lines = [jl({"direction": "response", "id": 1, "result": {"tools": [
            {"name": "t", "inputSchema": {"type": "object", "properties": {
                "x": {"items": [{"type": "string"}, {"type": "number"}]}}}}]}})]
        findings = mcp_schema_audit.audit(lines)
        self.assertTrue([f for f in findings
                         if f[1] == "tuple-items" and f[0] == "CRITICAL"])

    def test_duplicate_third_occurrence_reports_first_line(self):
        mk = {"name": "dup", "inputSchema": {"type": "object"}}
        lines = [jl({"direction": "response", "id": i, "result": {"tools": [mk]}})
                 for i in (1, 2, 3)]
        dups = [f for f in mcp_schema_audit.audit(lines)
                if f[1] == "duplicate-tool"]
        self.assertEqual(len(dups), 2)
        self.assertTrue(all("first seen line 1" in f[2] for f in dups))


class TestDoctorHardening(unittest.TestCase):
    def _run(self, *argv):
        return subprocess.run([sys.executable, SCRIPT, *argv],
                              capture_output=True, text=True)

    def test_collide_timeout_zero_is_accepted(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "tools.json")
        with open(p, "w") as f:
            json.dump([{"name": "a.b"}], f)
        r = self._run("collide", "--tools", p, "--timeout", "0")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class TestPackaging(unittest.TestCase):
    def test_sdist_pin_lists_expected_files(self):
        with open(os.path.join(HERE, "pyproject.toml"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("[tool.hatch.build.targets.sdist]", text)
        for name in ("mcp_doctor.py", "mcp_session_audit.py",
                     "mcp_schema_audit.py", "mcp_collision.py",
                     "recording_io.py", "test_hardening.py", "README.md",
                     "LICENSE", ".gitignore"):
            self.assertIn(name, text)


if __name__ == "__main__":
    unittest.main()
