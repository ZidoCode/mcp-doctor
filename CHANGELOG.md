# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.2.0] - 2026-10-21

### Fixed

- `collide`: `--timeout` is now a hard deadline for the whole MCP handshake
  (initialize + tools/list). Previously a spawned server that never
  answered hung the scan forever; it is now killed at the deadline and
  reported as timed out.
- `collide`: config files with `//` inside string values (e.g. URLs in
  args) no longer break JSONC parsing; comments and trailing commas are
  stripped by a string-aware scanner.
- `collide check`: tool-list files sharing a basename no longer overwrite
  each other, so collisions between them are no longer missed.
- `collide scan`: malformed server entries (non-object entries, non-string
  commands, non-list args, non-object env) are reported as skipped
  servers (exit 1, incomplete) instead of crashing with a traceback.
- `collide`: non-object JSON-RPC responses and non-list `tools` payloads
  are reported as inspection failures instead of crashing.
- `session`: recordings mixing integer and string request ids no longer
  crash the audit (`sorted()` TypeError); ids sort deterministically.
- `session`: a second `tools/list` no longer replaces the known-tool set,
  so calls to tools from an earlier listing are no longer false
  `unknown-tool` findings.
- `session`: orphan responses are now reported unconditionally,
  including error responses in recordings with no requests.
- `schema`: tuple `items:[...]` is now flagged under the 2020-12 default
  dialect too, not only when draft-07 is declared.
- `schema`: duplicate tool names now always report the first line seen,
  not the previous occurrence.
- `doctor`: missing recordings now exit 2 with a clean error instead of
  a traceback with exit 1.
- `doctor`: unreadable or empty recordings are errors (rc 2), never
  ALL CLEAR, via the shared `recording_io` module (upstream fix merged
  from main).
- `doctor collide`: an explicit `--timeout 0` is now forwarded (and
  clamped to 1s by `collide`) instead of silently becoming the 20s
  default.

### Added

- Pinned sdist contents in `pyproject.toml` so stray build-directory
  files (e.g. `.github/workflows/ci.yml`) can no longer ship in releases.
- CI gate asserting the built sdist contains exactly the pinned files.
- `LICENSE` (MIT), `SECURITY.md`, `CHANGELOG.md`, and `.gitignore`.
- Regression tests (`test_hardening.py`) covering every fix above.
