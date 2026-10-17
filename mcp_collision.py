#!/usr/bin/env python3
"""mcp-collision — predict MCP tool name collisions before your client does.

Multiple MCP servers installed side-by-side can expose tools with the same
name. Clients react differently: some prefix (server__tool), some silently
drop, some shadow. The collision is invisible until your agent calls the
wrong tool (VSCode #324992, CopilotKit #2409, PraisonAI #2711 — and the
shadowing bug class behind Hermes #72032).

mcp-collision reads your client's MCP config, talks to each server over
stdio MCP (initialize -> tools/list), collects every tool name, and reports
exactly which calls would be ambiguous — per sanitization scheme.

Usage:
  python3 mcp_collision.py scan <config.json> [--client claude|cursor|cline|raw]
  python3 mcp_collision.py check <tools1.json> <tools2.json> [...]   # pre-dumped lists

Config format: any JSON with a "mcpServers" object (Claude Desktop,
Cursor .mcp.json, Cline, etc.), stdio servers only (command-based).

Zero dependencies. Python 3.8+.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile


# ---------------------------------------------------------------- sanitize

def sanitize(client, server, tool):
    """How a client names a tool from server S with tool name T."""
    if client == "raw":
        return tool
    sep = "__" if client in ("claude", "cline") else "-"
    name = f"{server}{sep}{tool}"
    if client == "claude":
        # Claude Code sanitizes invalid chars per Hermes #72032 class of bugs:
        # chars outside [a-zA-Z0-9_-] collapse to _, so different raw names
        # can collapse to the same sanitized name.
        name = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
    return name


# ---------------------------------------------------------------- mcp stdio

def list_tools(command, cwd=None, env_extra=None, timeout=20):
    """Spawn an MCP stdio server, initialize, return its tools/list.
    Returns (tools, error). tools = list of {name, description}."""
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    try:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stdin=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, cwd=cwd, env=env, text=True)
    except OSError as e:
        return None, f"cannot spawn {command[0]!r}: {e}"

    def send(obj):
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def recv():
        line = proc.stdout.readline()
        if not line:
            return None
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            return None

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "mcp-collision", "version": "0.1"}}})
        resp = recv()
        if not resp or "result" not in (resp or {}):
            return None, f"no initialize response ({(resp or {}).get('error', {}).get('message', 'timeout/invalid')})"
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        # tolerate notifications between response frames
        for _ in range(10):
            resp = recv()
            if resp is None:
                break
            if resp.get("id") == 2:
                break
        if not resp or resp.get("id") != 2:
            return None, "no tools/list response"
        tools = resp["result"].get("tools") or []
        return tools, None
    finally:
        try:
            proc.stdin.close()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


def load_config(path):
    """Load an MCP config file, return {server_name: {command: [...], env: {}}}."""
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    # tolerate JSONC: strip // and /* */ comments, trailing commas
    text = re.sub(r"//[^\n]*", "", text)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    cfg = json.loads(text)
    servers = cfg.get("mcpServers") or cfg.get("mcp_servers") or {}
    if not isinstance(servers, dict):
        raise ValueError("mcpServers is not an object")
    return servers


# ---------------------------------------------------------------- analysis

def analyze(tool_map, client):
    """tool_map: {server: [tool names]}. Returns (collisions, sanitized_names).
    collisions: {final_name: [server, tool, ...]} with len > 1."""
    sanitized = {}   # final name -> [(server, raw_tool)]
    for server, tools in tool_map.items():
        for t in tools:
            final = sanitize(client, server, t)
            sanitized.setdefault(final, []).append((server, t))
    collisions = {k: v for k, v in sanitized.items() if len(v) > 1}
    return collisions, sanitized


# ---------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(prog="mcp-collision",
                                 description="Predict MCP tool name collisions before your client does.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_scan = sub.add_parser("scan", help="scan an MCP config file (live servers)")
    p_scan.add_argument("config")
    p_scan.add_argument("--client", default="claude",
                        choices=["claude", "cursor", "cline", "raw"])
    p_scan.add_argument("--timeout", type=int, default=20)
    p_scan.add_argument("--json", action="store_true", help="machine-readable output")

    p_check = sub.add_parser("check", help="check pre-dumped tool name lists")
    p_check.add_argument("files", nargs="+")
    p_check.add_argument("--client", default="claude",
                         choices=["claude", "cursor", "cline", "raw"])
    p_check.add_argument("--json", action="store_true", help="machine-readable output")

    args = ap.parse_args(argv)
    args.timeout = max(1, getattr(args, "timeout", 20))

    if args.cmd == "check":
        tool_map = {}
        for f in args.files:
            name = os.path.splitext(os.path.basename(f))[0]
            with open(f, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            tools = data if isinstance(data, list) else data.get("tools", [])
            tool_map[name] = [t if isinstance(t, str) else t.get("name", "?") for t in tools]
        return _report(tool_map, args.client, args.json)

    # scan
    try:
        servers = load_config(args.config)
    except OSError as e:
        print(f"error: cannot read config {args.config}: {e}", file=sys.stderr)
        return 2
    except (json.JSONDecodeError, ValueError) as e:
        print(f"error: invalid config {args.config}: {e}", file=sys.stderr)
        return 2
    if not servers:
        print("no mcpServers found in config")
        return 0

    tool_map = {}
    failures = []
    for name, spec in servers.items():
        cmd = spec.get("command")
        if not cmd:
            failures.append((name, "not a stdio server (no command) — remote servers skipped"))
            continue
        command = [cmd] + (spec.get("args") or [])
        tools, err = list_tools(command, env_extra=spec.get("env"), timeout=args.timeout)
        if err:
            failures.append((name, err))
        else:
            tool_map[name] = [t.get("name", "?") for t in tools]

    rc = _report(tool_map, args.client, args.json)
    for name, err in failures:
        print(f"  skipped {name}: {err}", file=sys.stderr)
    return rc


def _report(tool_map, client, as_json):
    total = sum(len(v) for v in tool_map.values())
    collisions, _ = analyze(tool_map, client)
    if as_json:
        print(json.dumps({
            "client": client, "servers": {k: len(v) for k, v in tool_map.items()},
            "total_tools": total, "collisions": {k: v for k, v in collisions.items()},
        }, indent=2))
    else:
        print(f"client: {client} — {len(tool_map)} servers, {total} tools")
        for s in sorted(tool_map):
            print(f"  {s:24s} {len(tool_map[s]):>3} tools")
        if not collisions:
            print("\nno collisions detected")
            return 0
        print(f"\n{len(collisions)} COLLISION(S):")
        for final in sorted(collisions):
            print(f"  {final}")
            for server, tool in collisions[final]:
                print(f"    <- {server}: {tool}")
        print("\nthese calls are ambiguous — your client picks one, you may never know which.")
    return 2 if collisions else 0


if __name__ == "__main__":
    sys.exit(main())
