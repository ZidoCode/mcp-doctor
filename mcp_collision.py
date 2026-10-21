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

Exit codes:
  0 clean — every configured server answered (an empty inventory is fine)
    and no name collides
  1 incomplete — at least one server was never inspected (refused to start,
    timed out, or failed the MCP handshake), so the result is not conclusive
  2 critical — a real collision was found, or the input could not be read
"""

import argparse
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time


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
    Returns (tools, error). tools = list of {name, description}.
    `timeout` is a hard deadline in seconds for the whole handshake
    (initialize + tools/list). A server that spawns but never answers is
    killed at the deadline and reported as timed out, never hung on.
    Frames are read on a daemon thread so a blocked readline cannot
    defeat the deadline, and every frame is validated as a JSON object
    before any field is touched."""
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    try:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stdin=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, cwd=cwd, env=env, text=True)
    except (OSError, TypeError, ValueError) as e:
        return None, f"cannot spawn {command[0]!r}: {e}"

    _TIMEOUT = object()
    frames = queue.Queue()

    def _reader():
        try:
            for line in proc.stdout:
                frames.put(line)
        except Exception:
            pass
        finally:
            frames.put(None)  # EOF sentinel: no more frames will ever arrive

    threading.Thread(target=_reader, daemon=True).start()
    deadline = time.monotonic() + max(1, timeout)

    def _kill():
        try:
            proc.kill()
        except Exception:
            pass

    def send(obj):
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def recv():
        """Next frame: dict, None on EOF/invalid JSON, _TIMEOUT on deadline."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _TIMEOUT
        try:
            line = frames.get(timeout=remaining)
        except queue.Empty:
            return _TIMEOUT
        if line is None:
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
        if resp is _TIMEOUT:
            _kill()
            return None, f"timed out after {timeout}s waiting for initialize"
        if not isinstance(resp, dict):
            return None, "invalid initialize response (not a JSON object)"
        if "result" not in resp:
            err = resp.get("error")
            msg = err.get("message") if isinstance(err, dict) else "timeout/invalid"
            return None, f"no initialize response ({msg})"
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        # tolerate notifications between response frames
        for _ in range(10):
            resp = recv()
            if resp is _TIMEOUT:
                _kill()
                return None, f"timed out after {timeout}s waiting for tools/list"
            if resp is None or not isinstance(resp, dict):
                break
            if resp.get("id") == 2:
                break
        if not isinstance(resp, dict) or resp.get("id") != 2:
            return None, "no tools/list response"
        if "error" in resp:
            err = resp.get("error")
            msg = err.get("message") if isinstance(err, dict) else "unknown error"
            return None, f"tools/list failed ({msg})"
        result = resp.get("result")
        if not isinstance(result, dict):
            return None, "invalid tools/list response (result is not an object)"
        tools = result.get("tools")
        if tools is None:
            tools = []
        if not isinstance(tools, list):
            return None, "invalid tools/list response (tools is not a list)"
        return tools, (None if tools else "returned no tools")
    except (BrokenPipeError, OSError) as e:
        return None, f"server closed the connection during handshake ({e})"
    finally:
        # Close stdin first so a cooperative server exits on EOF; kill a
        # hung one after the grace period; only then close stdout, once
        # the reader thread can no longer be blocked in a pipe read
        # (closing it earlier stalls the whole call on a sleeping server).
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            _kill()
        try:
            proc.stdout.close()
        except Exception:
            pass


def _strip_jsonc(text):
    """Strip JSONC comments and trailing commas, respecting string literals.

    The old regexes stripped // anywhere, so a URL inside a string value
    ("https://…") corrupted valid configs into unparseable JSON. This
    single-pass scanner only treats comment markers and commas as syntax
    when outside a string; escapes inside strings are preserved."""
    out = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            if end == -1:
                break
            i = end + 2
            continue
        if ch == ",":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "}]":
                i += 1
                continue
            out.append(ch)
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def load_config(path):
    """Load an MCP config file, return {server_name: {command: [...], env: {}}}."""
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    # tolerate JSONC: strip // and /* */ comments, trailing commas
    cfg = json.loads(_strip_jsonc(text))
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
            stem = os.path.splitext(os.path.basename(f))[0]
            name = stem
            if name in tool_map:
                # Same basename in two directories used to silently
                # overwrite the first file's tools, hiding collisions.
                parent = os.path.basename(os.path.dirname(os.path.abspath(f)))
                if parent:
                    name = f"{stem} ({parent})"
            suffix = 2
            while name in tool_map:
                name = f"{stem} ({suffix})"
                suffix += 1
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except OSError as e:
                print(f"error: cannot read tool list {f}: {e}", file=sys.stderr)
                return 2
            except ValueError as e:
                print(f"error: tool list is not valid JSON: {f} ({e})", file=sys.stderr)
                return 2
            if isinstance(data, list):
                tools = data
            elif isinstance(data, dict) and isinstance(data.get("tools"), list):
                tools = data["tools"]
            else:
                print(f"error: tool list {f} is neither a list nor an object "
                      "with a tools list", file=sys.stderr)
                return 2
            tool_map[name] = [
                t if isinstance(t, str)
                else (t.get("name", "?") if isinstance(t, dict) else "?")
                for t in tools]
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
        # A malformed server entry used to crash the whole scan with a
        # traceback (args as a string, entry not an object, …). Treat it
        # like any other uninspectable server: skip it, exit incomplete.
        if not isinstance(spec, dict):
            failures.append((name, "server entry is not an object — skipped"))
            continue
        cmd = spec.get("command")
        if not isinstance(cmd, str) or not cmd:
            failures.append((name, "not a stdio server (no command) — remote servers skipped"))
            continue
        server_args = spec.get("args") or []
        if not isinstance(server_args, list) or not all(isinstance(a, str) for a in server_args):
            failures.append((name, "server args is not a list of strings — skipped"))
            continue
        env = spec.get("env")
        if env is not None and not isinstance(env, dict):
            failures.append((name, "server env is not an object — skipped"))
            continue
        command = [cmd] + server_args
        tools, err = list_tools(command, env_extra=env, timeout=args.timeout)
        if tools is not None:
            # The server answered. An empty inventory is data, not a gap: record it.
            tool_map[name] = [t.get("name", "?") for t in tools]
        if err and err != "returned no tools":
            failures.append((name, err))

    rc = _report(tool_map, args.client, args.json)
    for name, err in failures:
        print(f"  skipped {name}: {err}", file=sys.stderr)
    if rc == 2:
        return 2          # a real collision outranks an incomplete scan
    if failures:
        # Say it out loud: exit 1 is the difference between "no collisions"
        # and "could not check", and stderr is the only place CI logs see it.
        print(f"\n  verdict: {len(failures)} server(s) could not be inspected — "
              "the scan is incomplete, not clean.", file=sys.stderr)
        return 1
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
