# Changelog

All notable changes are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Tests for two previously untested modules: `lfb/errors.py` (error
  codes/statuses are the HTTP API and MCP contract; `from_payload`
  round-trips every mapped code) and `lfb/targets.py` (`env:root/path`
  parsing, `..` escape rejection, explicit env/root arguments).
  Suite grows from 53 to 75 tests.
- Root `SECURITY.md` with the vulnerability reporting policy, pointing
  to the full security model in `docs/SECURITY.md`.
- GitHub Actions workflow (`.github/workflows/ci.yml`): test matrix on Python
  3.8-3.13, CLI smoke test, sdist/wheel build installed into a clean venv, and a
  bridge end-to-end smoke (health, list, `lfb ls`, `lfb cat`).
- README CI badge.

## [0.1.0] - 2025-01-01

### Added

- Single-environment HTTP bridge with named roots, bearer auth, path
  confinement, atomic writes, range downloads, search, tree, stat/hash.
- Streaming HTTP client and CLI (`init`, `env`, `serve`, `gateway`, `mcp`,
  `health`, `roots`, `ls`, `tree`, `stat`, `cat`, `get`, `put`, `mkdir`, `mv`,
  `rm`, `find`, `exec`, `sync`/`copy`/`cp`, `doctor`).
- Multi-environment gateway with `?env=` / `X-LFB-Env` routing and streaming
  reverse proxy for bridge environments.
- MCP stdio server with 11 tools (`lfb_list_envs`, `lfb_list`, `lfb_tree`,
  `lfb_stat`, `lfb_read`, `lfb_write`, `lfb_mkdir`, `lfb_move`, `lfb_delete`,
  `lfb_search`, `lfb_exec`).
- Cross-environment `sync` with mtime preservation, checksum mode, dry-run,
  exclude patterns, and guarded `--delete`.
- Read-only roots, per-environment limits, optional command execution, TLS,
  CORS, and `lfb doctor`.
- Tests (53) covering path traversal, symlink escapes, HTTP, gateway, MCP,
  transfer, CLI, and config; GitHub Actions CI for Python 3.8/3.9/3.12.

### Security

- Symlink targets must resolve inside the configured root.
- Config written with mode `0600`; tokens compared in constant time.
- Command execution disabled by default.
