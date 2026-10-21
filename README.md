# mcp-doctor

**One command, every MCP audit — session hygiene, wire shapes, name collisions.**

[![PyPI](https://img.shields.io/pypi/v/mcpmedic?cacheSeconds=300)](https://pypi.org/project/mcpmedic/)
[![CI](https://github.com/ZidoCode/mcp-doctor/actions/workflows/ci.yml/badge.svg)](https://github.com/ZidoCode/mcp-doctor/actions)

MCP integrations break in three places and none of them crash:

| Where | What breaks | Auditor |
|---|---|---|
| **Recorded session** | dangling tool_call_id poisons later requests ([opencode #44852](https://github.com/anomalyco/opencode/issues/44852)), swallowed `isError` ([zeroclaw #7122](https://github.com/zeroclaw-labs/zeroclaw/pull/7122)), error-channel confusion ([memoria #258](https://github.com/matrixorigin/memoria/issues/258)) | `session` — [mcp-session-audit](https://github.com/ZidoCode/mcp-session-audit) |
| **Advertised listings** | schemas that make strict clients silently reject tools ([gitlab-mcp #286](https://github.com/zereight/gitlab-mcp/issues/286), [langfuse #13804](https://github.com/langfuse/langfuse/issues/13804), [strapi #27395](https://github.com/strapi/strapi/issues/27395)) | `schema` — [mcp-schema-audit](https://github.com/ZidoCode/mcp-schema-audit) |
| **Tool names** | collision between servers makes the agent call the wrong tool ([VSCode #324992](https://github.com/microsoft/vscode/issues/324992), Copilot #11480) | `collide` — [mcp-collision](https://github.com/ZidoCode/mcp-collision) |

## Install

```
pip install mcpmedic
```

## Names

PyPI `mcpmedic` is this project. The names `mcp-doctor` and `mcp_doctor` on PyPI belong to two unrelated projects by another author (Jiansen He, 0.1.0), and PyPI has no rename. The two console scripts are `mcpmedic` and `mcp-doctor`: same entry point, installed together — no alias needed beyond that.

## Usage

```
mcp-doctor check recording.jsonl                  # session + schema, one pass
mcp-doctor session recording.jsonl                # session hygiene only
mcp-doctor schema recording.jsonl                 # wire-shape conformance only
mcp-doctor collide config.json --client claude    # name collisions across installed servers
mcp-doctor collide --tools a.json b.json          # collisions across pre-dumped tool lists
```

Input: JSONL of recorded MCP messages — a CI log, a debug dump, a user-reported
session file. No live server needed.

```jsonl
{"direction":"request","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25"}}
{"direction":"response","id":1,"result":{"protocolVersion":"2025-11-25","serverInfo":{"name":"srv","version":"1.0"}}}
{"direction":"response","id":2,"result":{"tools":[{"name":"search","inputSchema":{"type":"object","properties":{}}}]}}
```

Exit codes (CI-friendly): **0** clean · **1** warnings, or an incomplete
`collide` scan (a server could not be inspected) · **2** critical. A real
collision (2) outranks an incomplete scan (1).

## Member auditors

- [mcp-session-audit](https://github.com/ZidoCode/mcp-session-audit) — dangling calls, orphan/duplicate responses, isError swallowed, error-as-text, unknown tools
- [mcp-schema-audit](https://github.com/ZidoCode/mcp-schema-audit) — empty schemas, root type violations, draft-07 tuples, duplicate names, protocol version sanity
- [mcp-collision](https://github.com/ZidoCode/mcp-collision) — predicts tool-name collisions before your agent does

## Development

```
python -m unittest        # full suite, no extra dependencies needed
python -m build --sdist   # sdist contents are pinned in pyproject.toml
```

MIT License.
