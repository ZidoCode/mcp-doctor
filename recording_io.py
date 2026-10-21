#!/usr/bin/env python3
"""Shared recording loader for the mcp-doctor family.

An audit tool that prints "ALL CLEAR" for a recording it never read is worse
than useless: the caller trusts a green result that audited nothing. Every
unreadable or empty input is an error (rc 2), never a pass.
"""

import sys


def read_recording(path):
    """Return (lines, rc). rc is 2 when the recording could not be audited."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = [ln for ln in (l.strip() for l in f) if ln]
    except FileNotFoundError:
        print(f"error: recording not found: {path}", file=sys.stderr)
        return [], 2
    except IsADirectoryError:
        print(f"error: recording is a directory: {path}", file=sys.stderr)
        return [], 2
    except OSError as exc:
        print(f"error: cannot read recording {path}: {exc.strerror or exc}", file=sys.stderr)
        return [], 2

    if not lines:
        print(f"error: recording is empty, nothing was audited: {path}", file=sys.stderr)
        return [], 2
    return lines, 0
