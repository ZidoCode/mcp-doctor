"""Exit-code contract for `collide`, and the prog/exit-code documentation.

These guard the rules a CI job gates on, so a later refactor cannot quietly
turn an incomplete scan back into a clean one:

  0  clean          — every configured server answered (empty inventory is fine)
  1  incomplete     — a server could not be inspected at all
  2  critical       — a real collision, which outranks an incomplete scan
"""
import json
import os
import subprocess
import sys
import unittest

import mcp_doctor

HERE = os.path.dirname(os.path.abspath(__file__))


class TestCollideExitCodes(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.d = tempfile.mkdtemp()

    def _cfg(self, body):
        p = os.path.join(self.d, "mcp.json")
        with open(p, "w") as f:
            json.dump(body, f)
        return p

    def _run(self, *argv):
        return subprocess.run([sys.executable, os.path.join(HERE, "mcp_doctor.py"), *argv],
                              capture_output=True, text=True)

    def test_uninspected_server_is_incomplete(self):
        """A server that cannot spawn must not read as clean."""
        cfg = self._cfg({"mcpServers": {"ghost": {"command": "/nonexistent/x"}}})
        r = self._run("collide", cfg, "--client", "raw", "--timeout", "2")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("incomplete", r.stderr)

    def test_empty_inventory_is_clean(self):
        """An answering server with no tools is data, not a gap."""
        cfg = self._cfg({"mcpServers": {}})
        r = self._run("collide", cfg, "--client", "raw")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_collision_outranks_incomplete(self):
        """A real collision is 2 even when another server could not be inspected."""
        a = os.path.join(self.d, "a.json")
        b = os.path.join(self.d, "b.json")
        for p in (a, b):
            with open(p, "w") as f:
                json.dump([{"name": "dup.tool"}], f)
        r = self._run("collide", "--tools", a, b, "--client", "raw")
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("COLLISION", r.stdout)


class TestProgName(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()

    def test_prog_follows_invoked_binary(self):
        """The usage line must name the binary the user actually invoked.

        A console script sets sys.argv[0] to its own path before dispatching;
        the launcher below reproduces exactly that, without needing an install.
        """
        for name in ("mcpmedic", "mcp-doctor"):
            launcher = os.path.join(self.tmp, name)
            with open(launcher, "w") as f:
                f.write(
                    "#!%s\n"
                    "import sys\n"
                    "sys.argv[0] = %r\n"
                    "sys.path.insert(0, %r)\n"
                    "import mcp_doctor\n"
                    "sys.exit(mcp_doctor.main())\n" % (sys.executable, name, HERE))
            os.chmod(launcher, 0o755)
            r = subprocess.run([launcher, "--help"], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn(f"usage: {name}", r.stdout,
                          f"--help must say '{name}', not a fixed name")

    def test_dev_invocation_uses_canonical_name(self):
        """`python mcp_doctor.py` is a dev run, not an installed script."""
        r = subprocess.run([sys.executable, os.path.join(HERE, "mcp_doctor.py"), "nope"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage: mcpmedic", r.stderr)

    def test_prog_falls_back_for_bare_interpreter(self):
        old = sys.argv[0]
        try:
            sys.argv[0] = "python3"
            self.assertEqual(mcp_doctor._prog(), "mcpmedic")
            sys.argv[0] = "/usr/local/bin/mcp-doctor"
            self.assertEqual(mcp_doctor._prog(), "mcp-doctor")
        finally:
            sys.argv[0] = old


class TestDocumentedContract(unittest.TestCase):
    def test_docstrings_state_the_contract(self):
        for fname in ("mcp_doctor.py", "mcp_collision.py"):
            with open(os.path.join(HERE, fname), encoding="utf-8") as f:
                src = f.read()
            self.assertRegex(src, r"0 clean", f"{fname} must document exit code 0")
            self.assertRegex(src, r"1 [a-z ]*incomplete|incomplete \(1\)",
                             f"{fname} must document the incomplete exit code")


if __name__ == "__main__":
    unittest.main()
