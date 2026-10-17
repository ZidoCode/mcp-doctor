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
  doctor collide --client <claude|cursor|cline|raw> [--timeout N]
                                         tool-name collision scan
  doctor check   <recording.jsonl>       session + schema in one pass

Each subcommand inherits its module's exit codes (0 clean / 1 warnings /
2 critical) so CI can gate on any of them.
"""

import argparse
import json
import sys

import mcp_session_audit
import mcp_schema_audit

VERSION = "0.1.0"


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


def cmd_collide(args):
    import mcp_collision
    argv = ["--client", args.client]
    if args.timeout:
        argv += ["--timeout", str(args.timeout)]
    return mcp_collision.main(argv)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(
        prog="mcp-doctor",
        description="One command, every MCP audit — session hygiene, wire shapes, name collisions.")
    ap.add_argument("--version", action="version", version=f"mcp-doctor {VERSION}")
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
    s.add_argument("--client", default="claude", choices=["claude", "cursor", "cline", "raw"])
    s.add_argument("--timeout", type=int, default=20)
    s.set_defaults(fn=cmd_collide)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
