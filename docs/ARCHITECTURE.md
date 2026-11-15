# Architecture

## Goals

1. Run on a bare Android Ubuntu proot / Termux with **no third-party packages**.
2. Expose only explicitly configured roots, with strong path confinement.
3. Offer one conceptual filesystem interface to an agent, regardless of whether
   an environment is local or remote.
4. Keep large file transfers streaming and bounded in memory.
5. Be small enough to audit in an afternoon.

## Components

```text
Config  ──►  Environment (local | bridge)
                 │
                 ├── LocalFS   (path policy, atomic IO, search, exec)
                 └── BridgeClient (HTTP + streaming)
                          │
                Backend facade (uniform API)
                 │        │          │
               CLI      MCP       Gateway
```

### `config.py`

Loads/validates JSON, normalizes roots to absolute paths, parses sizes
(`64M`), validates names, stores tokens, and writes the file atomically with
mode `0600`. Environment objects know whether they are local or bridge, their
roots, limits and exec policy.

### `fsops.py`

The only module that touches local files directly. All public methods take
`(root_name, relative_path)`.

Resolution algorithm:

1. Split relative path; reject `..`/NUL.
2. `root_real = realpath(root.path)`; require it to be a directory.
3. If the target exists: `real = realpath(candidate)`; `real` must be under
   `root_real`. Record whether the original path was a symlink.
4. If it does not exist and this is a write: resolve the parent, check
   containment, then append the final component; also check that the final
   target's `realpath` is contained (it may be a broken inside symlink).

Reads use the resolved path. Writes use `mkstemp` + `fsync` + `rename` in the
resolved target directory, preserving the previous mode and optionally the
source mtime (used by `sync`). Directory walks use `os.walk(followlinks=False)`
and prune symlinked directories. Exec runs via `subprocess` with a timeout and
output caps, in a new process group.

### `client.py`

An `http.client` client with no connection pooling. Each request creates a
connection and sends `Connection: close`; this keeps thread-safety trivial and
avoids stale keep-alive sockets after Android suspends the process. JSON calls
use a small read limit; downloads/upload use streaming `LimitedReader` /
`RemoteStream`. TLS verification can be `true`, `false` or a CA bundle path.

### `api.py`

`ThreadingHTTPServer` + `BaseHTTPRequestHandler`. Responsibilities:

- Parse query/form bodies, enforce auth and `Content-Length` bounds.
- Route `/v1/*` to handlers.
- Convert `LfbError` into JSON with a stable code/status.
- Stream downloads with optional single-range support.
- Pass request bodies to `LocalFS.write_stream` through a `LimitedReader`, so
  the server never blocks after the declared body.
- Optional TLS wrapping and CORS.

`/v1/health` and `/` are public; everything else requires the token.

### `gateway.py`

A subclass of the API handler. For each authenticated `/v1/*` request it:

1. Selects the environment from `?env=`, `X-LFB-Env`, or the default.
2. For a local backend, sets a per-request filesystem override and delegates to
   the same handler code used by a bridge.
3. For a bridge backend, opens a connection to the remote URL and forwards the
   method, path (minus `env`), headers, and body stream. The response headers
   and body are streamed back. The gateway token is replaced by the remote
   token, and `Content-Length` is preserved so clients can detect the end of
   the body.

`/v1/envs` lists all environments without exposing tokens.

### `targets.py`

Parses `env:root/path` with explicit `--env`/`--root` overrides. The first path
component is treated as a root only if it matches a configured root name;
otherwise the default root is used. This keeps common paths short while
remaining unambiguous.

### `transfer.py`

Implements `sync`, `download` and `upload` on top of the Backend facade:

- A flat manifest is built with iterative directory listing (no recursion
  depth limit, symlinks skipped, capped at 200k entries).
- Directories are created before files.
- Files are skipped if size and mtime match (within 2 seconds) or SHA-256
  matches with `--checksum`.
- Changed files are streamed from source to destination and written with the
  source mtime.
- `--delete` builds a destination manifest, removes files/symlinks not in the
  source, then removes extra directories deepest-first. Deleting at a root
  requires `--allow-root-delete`.
- Local downloads write atomically to the destination directory; local uploads
  walk with `followlinks=False`.

### `mcp.py`

Newline-delimited JSON-RPC on stdio. The loop never prints to stdout except
protocol messages. `initialize` negotiates one of three MCP versions and
advertises only `tools`. `tools/call` dispatches to methods that reuse the same
Backend objects as the CLI. Tool failures are returned as `isError: true`
results (so the model can recover) rather than JSON-RPC errors; malformed
protocol requests use standard JSON-RPC error codes.

### `cli.py`

Argparse front end. Commands only orchestrate config, backends, transfer and
servers; all file semantics live in the modules above. Human output goes to
stdout, diagnostics and progress go to stderr, and `--json` prints raw data for
scripts.

## Concurrency

- The bridge is thread-per-request. `LocalFS` has no mutable shared state per
  operation; config is read-only after startup.
- `BridgeClient` creates a connection per call, so there are no shared sockets.
- `sync` is sequential. Running multiple syncs into the same destination is not
  coordinated and may interleave.
- `mcp` is single-threaded and processes one JSON-RPC message at a time.

## Failure handling

- Expected errors are `LfbError` subclasses with code/status; they become JSON
  errors or MCP tool errors.
- Unexpected exceptions are logged with a traceback to stderr and returned as a
  generic `internal_error` (no traceback to the client).
- If a response has already started, the connection is closed instead of trying
  to inject an error body.
- Auth failures and upload errors set `close_connection` to avoid HTTP keep-alive
  desynchronization from an unread request body.

## Testing strategy

- Unit tests for parsing/config, path traversal, symlink escapes, read-only
  roots, atomic writes, mtime, search, tree and exec.
- Integration tests that start real `ThreadingHTTPServer` instances on port 0
  and exercise them with the real client, including Range requests, binary
  round-trips, gateway local dispatch and gateway streaming proxy.
- MCP tests call `handle()` directly and also run the subprocess transport.
- CLI tests call `main()` in-process and one MCP subprocess.

The suite is `unittest`-only, so CI needs no test dependencies.
