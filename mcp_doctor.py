#!/usr/bin/env python3
"""mcp-doctor — one command, every MCP audit.

MCP integrations break in three places and none of them crash: the recorded
session (dangling tool calls, swallowed isError, error-channel confusion),
the advertised listings (schemas that make strict clients silently reject
tools), and the tool names themselves (collision between servers that makes
an agent call the wrong tool). Each of those has a dedicated auditor; this
is the one door that runs them.

Subcommands:
  doctor session <recording.jsonl>       session hygiene audit
  doctor schema  <recording.jsonl>       wire-shape conformance audit
  doctor collide <config.json> [--client <claude|cursor|cline|raw>] [--timeout N]
                                         tool-name collision scan (live servers)
  doctor collide --tools <f.json> [...]  collision scan on pre-dumped tool lists
  doctor check   <recording.jsonl>       session + schema in one pass

Exit codes: 0 clean, 1 warnings or an incomplete audit, 2 critical.

`collide` treats a server it could not inspect as incomplete (1), not clean
(0), and lets a real collision (2) outrank it. `session`, `schema` and
`check` inherit their auditor's 0/1/2 so CI can gate on any of them.
"""

import argparse
import json
import os
import sys

import mcp_session_audit
import mcp_schema_audit

def _version():
    """Report the installed distribution version, not a hand-edited constant."""
    try:
        from importlib.metadata import version as _dist_version
        return f"mcpmedic {_dist_version('mcpmedic')}"
    except Exception:
        pass
    try:
        import pathlib
        import re
        pyproject = pathlib.Path(__file__).with_name("pyproject.toml")
        m = re.search(r'^version\s*=\s*"([^"]+)"', pyproject.read_text(encoding="utf-8"), re.M)
        if m:
            return f"mcpmedic {m.group(1)}"
    except Exception:
        pass
    return "mcpmedic unknown"


def _load_jsonl(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return [ln for ln in (l.strip() for l in f) if ln]


def cmd_session(args):
    return mcp_session_audit.report(args.recording,
                                    mcp_session_audit.audit(_load_jsonl(args.recording)))


def cmd_schema(args):
    return mcp_schema_audit.report(args.recording,
                                   mcp_schema_audit.audit(_load_jsonl(args.recording)))


def cmd_check(args):
    """Session + schema on the same recording, one combined report."""
    lines = _load_jsonl(args.recording)
    s_findings = mcp_session_audit.audit(list(lines))
    sch_findings = mcp_schema_audit.audit(list(lines))
    combined = s_findings + sch_findings
    print(f"mcp-doctor check: {args.recording} (session + schema)")
    if not combined:
        print("  ALL CLEAR — session hygiene and wire shapes are both client-safe.")
        return 0
    for sev, code, msg in combined:
        sym = {"CRITICAL": "[CRITICAL]", "WARNING": "[WARNING ]", "INFO": "[INFO    ]"}[sev]
        print(f"  {sym} [{code}] {msg}")
    counts = {"CRITICAL": 0, "WARNING": 0, "INFO": 0}
    for sev, _, _ in combined:
        counts[sev] += 1
    parts = [f"{counts[s]} {s.lower()}" for s in ("CRITICAL", "WARNING", "INFO") if counts[s]]
    print(f"\n  verdict: {', '.join(parts)}.")
    if counts["CRITICAL"]:
        print("  the session or listing will fail in a way the model or client cannot see.")
        return 2
    return 1


def _check_tool_lists(paths):
    """Return an error string when a --tools path is unreadable or not JSON."""
    for p in paths:
        try:
            with open(p, "r", encoding="utf-8") as fh:
                json.load(fh)
        except FileNotFoundError:
            return f"tool list not found: {p}"
        except OSError as exc:
            return f"cannot read tool list {p}: {exc.strerror or exc}"
        except ValueError as exc:
            line = getattr(exc, "lineno", "?")
            return f"tool list is not valid JSON: {p} (line {line})"
    return None


def cmd_collide(args):
    """Forward a real subcommand to mcp_collision.

    mcp_collision.main() takes {scan,check} as a required positional first
    argument. Building argv as ["--client", X] made argparse treat the
    client *value* as the subcommand:

        mcp-collision: error: argument cmd: invalid choice: 'raw'

    so `doctor collide` never ran. It also had no `config` positional, which
    scan requires.
    """
    import mcp_collision

    if args.tools:
        bad = _check_tool_lists(args.tools)
        if bad:
            print(f"error: {bad}", file=sys.stderr)
            return 2
        argv = ["check", *args.tools, "--client", args.client]
        if args.json:
            argv.append("--json")
        return mcp_collision.main(argv)

    if not args.config:
        print("error: collide needs a config file (scan) or --tools <list...> (check)",
              file=sys.stderr)
        return 2

    argv = ["scan", args.config, "--client", args.client]
    if args.timeout:
        argv += ["--timeout", str(args.timeout)]
    if args.json:
        argv.append("--json")
    return mcp_collision.main(argv)


class _Parser(argparse.ArgumentParser):
    """Usage errors exit 2, never 0.

    argparse raises SystemExit(2) on a usage error; if that surfaces as a
    normal return anywhere up the stack the process exits 0, and a CI job
    gating on `mcp-doctor ...` sees success for a command that never ran.
    2 also matches the documented critical exit code.
    """

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: {message}\n")


def _prog():
    """Name the program the user actually typed.

    Both `mcpmedic` and `mcp-doctor` are installed console scripts, so a
    hardcoded prog makes --help and usage errors name a binary the user may
    not have invoked. Fall back to the canonical name when argv[0] is not
    useful (empty, or a bare interpreter such as `python -m`).
    """
    name = os.path.basename(sys.argv[0] or "").strip()
    if not name or name in ("-c", "-m", "python", "python3"):
        return "mcpmedic"
    if name.endswith(".py"):
        # A direct `python mcp_doctor.py` is a dev invocation, not an installed
        # script; keep the canonical name so output stays predictable.
        return "mcpmedic"
    return name


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = _Parser(
        prog=_prog(),
        description="One command, every MCP audit — session hygiene, wire shapes, name collisions.")
    ap.add_argument("--version", action="version", version=_version())
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("session", help="session hygiene audit (dangling calls, swallowed isError)")
    s.add_argument("recording")
    s.set_defaults(fn=cmd_session)

    s = sub.add_parser("schema", help="wire-shape conformance audit (client-rejecting schemas)")
    s.add_argument("recording")
    s.set_defaults(fn=cmd_schema)

    s = sub.add_parser("check", help="session + schema combined, one pass")
    s.add_argument("recording")
    s.set_defaults(fn=cmd_check)

    s = sub.add_parser("collide", help="tool-name collision scan across installed servers")
    s.add_argument("config", nargs="?", default=None,
                   help="MCP config JSON with an mcpServers object (live scan)")
    s.add_argument("--tools", nargs="+", default=None, metavar="JSON",
                   help="pre-dumped tool lists instead of a live scan")
    s.add_argument("--client", default="claude", choices=["claude", "cursor", "cline", "raw"])
    s.add_argument("--timeout", type=int, default=20)
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.set_defaults(fn=cmd_collide)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
