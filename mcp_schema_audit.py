#!/usr/bin/env python3
"""mcp-schema-audit — audit recorded MCP listings for client-rejecting wire shapes.

MCP servers publish broken tool listings in ways that are invisible on the
server side: the endpoint returns 200, no log fires, and the failure happens
entirely in the client's validation — tools get silently dropped (langfuse
#13804), the whole tool list is rejected (gitlab-mcp #286, strapi #27395), or
a dropped schema is replaced with {} and the model has to reverse-engineer
arguments one failed call at a time (prime-agent #1073). Quality linters
(mcp-schema-lint, mcp-assert) grade descriptions and naming; none of them
check the shapes that make strict clients REJECT tools outright.

This tool does. Input: JSONL of recorded MCP messages (same format as
mcp-session-audit):

  {"direction":"request","id":1,"method":"initialize","params":{...}}
  {"direction":"response","id":1,"result":{"protocolVersion":"2025-11-25",...}}
  {"direction":"response","id":2,"result":{"tools":[...]}}

Checks:
  1. inputSchema missing or {}            — clients reject or drop the tool
  2. inputSchema.type missing/not "object" — TS SDK rejects (langfuse #13804)
  3. draft-07 dialect + tuple items:[...]  — strict 2020-12 clients reject
                                            everything (strapi #27395)
  4. duplicate tool names
  5. protocolVersion echoed / negotiated that is not a known MCP revision
     (go-sdk #1033 echoed 2026-07-28 through the legacy handshake)
  6. initialize result missing serverInfo name/version
  7. empty listing — a tools/list result with zero tools

Exit codes: 0 clean, 1 warnings only, 2 any CRITICAL.
"""

import json
import sys

CRITICAL = "CRITICAL"
WARNING = "WARNING"
INFO = "INFO"

# known MCP protocol revisions (legacy era + modern era)
KNOWN_VERSIONS = {
    "2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25",
    "2026-03-01", "2026-07-28",
}
DRAFT07 = "http://json-schema.org/draft-07/schema"


def _schema_findings(where, schema, findings):
    """Validate one tool schema dict for rejection-blocking shapes."""
    if schema is None:
        findings.append((CRITICAL, "schema-missing",
                         f"{where}: inputSchema missing — clients cannot construct "
                         "arguments; many drop the tool (gitlab-mcp #286)"))
        return
    if not isinstance(schema, dict):
        findings.append((WARNING, "schema-not-object",
                         f"{where}: inputSchema is {type(schema).__name__}, not an object"))
        return
    if not schema:
        findings.append((CRITICAL, "schema-empty",
                         f"{where}: inputSchema is {{}} — violates the MCP schema spec; "
                         "strict clients reject the tool (gitlab-mcp #286, prime-agent #1073)"))
        return
    if schema.get("type") != "object":
        findings.append((CRITICAL, "root-type-not-object",
                         f"{where}: inputSchema.type is {schema.get('type')!r}, expected "
                         "'object' — MCP TS SDK rejects the tool (langfuse #13804)"))
    dialect = schema.get("$schema")
    if isinstance(dialect, str) and "draft-07" in dialect:
        # structural draft-07: tuple syntax items:[...] (invalid in 2020-12)
        def _tuples(node, path="$"):
            hits = []
            if isinstance(node, dict):
                if isinstance(node.get("items"), list):
                    hits.append(path + ".items")
                for k, v in node.items():
                    hits.extend(_tuples(v, f"{path}.{k}"))
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    hits.extend(_tuples(v, f"{path}[{i}]"))
            return hits
        tuples = _tuples(schema)
        if tuples:
            findings.append((CRITICAL, "draft07-dialect",
                             f"{where}: advertises draft-07 with tuple syntax "
                             f"({tuples[0]}) — strict 2020-12 clients reject the tool "
                             "(strapi #27395, TS SDK #2084)"))
        else:
            findings.append((WARNING, "draft07-dialect",
                             f"{where}: advertises draft-07 — SEP-1613 default is "
                             "2020-12; strict validators may reject"))


def audit(lines):
    """Audit parsed JSONL lines. Returns list of (severity, code, msg)."""
    findings = []
    requested_versions = []  # protocolVersion the client asked for
    server_version = None
    server_info = None
    tool_lists = 0
    saw_init = False
    total_tools = 0
    names = {}

    for n, line in enumerate(lines, 1):
        if isinstance(line, str):
            try:
                line = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                findings.append((WARNING, "unparseable-line", f"line {n}: not valid JSON"))
                continue
        if not isinstance(line, dict):
            findings.append((WARNING, "unparseable-line", f"line {n}: not an object"))
            continue

        method = line.get("method")
        result = line.get("result")
        params = line.get("params")

        # client's requested version
        if method == "initialize" and isinstance(params, dict):
            rv = params.get("protocolVersion")
            if rv:
                requested_versions.append(rv)

        if not isinstance(result, dict):
            continue

        # initialize result
        if "serverInfo" in result or "protocolVersion" in result:
            saw_init = True
            server_version = result.get("protocolVersion")
            server_info = result.get("serverInfo")
            if server_version is not None and str(server_version) not in KNOWN_VERSIONS:
                findings.append((CRITICAL, "unknown-protocol-version",
                                 f"line {n}: server negotiated {server_version!r} — not a "
                                 "known MCP revision; clients will fail to bind session "
                                 "semantics (go-sdk #1033 echoed a modern era version "
                                 "through the legacy handshake)"))
            if requested_versions and server_version is not None:
                if str(requested_versions[-1]) != str(server_version):
                    findings.append((INFO, "version-renegotiated",
                                     f"line {n}: client requested "
                                     f"{requested_versions[-1]!r}, server answered "
                                     f"{server_version!r} — legal (MUST respond with a "
                                     "supported version) but worth confirming intent "
                                     "(ferrum-edge #2168 hard-errored instead)"))

        # tools/list result
        tools = result.get("tools")
        if isinstance(tools, list):
            tool_lists += 1
            total_tools += len(tools)
            if not tools:
                findings.append((WARNING, "empty-tools",
                                 f"line {n}: tools/list returned zero tools — server "
                                 "exposes nothing; most UIs show a dead connector"))
            for t in tools:
                if not isinstance(t, dict):
                    continue
                name = t.get("name")
                if not isinstance(name, str) or not name:
                    findings.append((CRITICAL, "tool-unnamed",
                                     f"line {n}: tool without a name — uncallable"))
                    continue
                if name in names:
                    findings.append((CRITICAL, "duplicate-tool",
                                     f"line {n}: duplicate tool name '{name}' (first seen "
                                     f"line {names[name]}) — only one can be called"))
                names[name] = n
                _schema_findings(f"tool '{name}' (line {n})", t.get("inputSchema"),
                                 findings)

    if saw_init and (not isinstance(server_info, dict) or not server_info.get("name")):
        findings.append((WARNING, "serverinfo-unnamed",
                         "initialize result has no serverInfo.name — shows up as a "
                         "blank connector in logs and UIs"))
    if not saw_init and (tool_lists or requested_versions):
        findings.append((WARNING, "no-initialize",
                         "no initialize result in the recording — can't audit "
                         "negotiation or serverInfo"))

    return findings


SEV_SYM = {CRITICAL: "[CRITICAL]", WARNING: "[WARNING ]", INFO: "[INFO    ]"}


def report(path, findings):
    counts = {CRITICAL: 0, WARNING: 0, INFO: 0}
    for sev, _, _ in findings:
        counts[sev] += 1
    print(f"mcp-schema-audit: {path}")
    if not findings:
        print("  ALL CLEAR — every tool schema is client-acceptable, negotiation is sane.")
        return 0
    for sev, code, msg in findings:
        print(f"  {SEV_SYM[sev]} {msg}")
    parts = [f"{counts[s]} {s.lower()}" for s in (CRITICAL, WARNING, INFO) if counts[s]]
    print(f"\n  verdict: {', '.join(parts)}.")
    if counts[CRITICAL]:
        print("  strict clients will silently drop these tools or refuse the listing entirely.")
        return 2
    return 1


def load_lines(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return [ln for ln in (l.strip() for l in f) if ln]


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    return report(argv[0], audit(load_lines(argv[0])))


if __name__ == "__main__":
    sys.exit(main())
