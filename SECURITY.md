# Security Policy

## Reporting a vulnerability

If you find a security issue in mcp-doctor, please report it through
GitHub Security Advisories on this repository instead of opening a public
issue, so a fix can be prepared before details are disclosed.

## Scope notes

mcp-doctor spawns the MCP servers listed in the config you give it, with
your environment, and only over stdio. It never sends your config,
recordings, or environment to any third party. Review the server commands
in a config file before scanning it, the same way you would review any
script you execute.
