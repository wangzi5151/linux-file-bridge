# API reference

The bridge and the gateway speak the same HTTP API. The only difference is that
the gateway accepts an environment selector.

## Conventions

- Base URL: `http://127.0.0.1:8790` (bridge) or `http://127.0.0.1:8787` (gateway).
- Authentication: `Authorization: Bearer <token>` or `X-LFB-Token: <token>`.
- `GET /v1/health` is the only unauthenticated `/v1` endpoint. `/` is a public
  service descriptor with no filesystem data.
- Responses are JSON with `Content-Type: application/json; charset=utf-8`:

  ```json
  {"ok": true, "data": { ... }}
  ```

  Errors use the HTTP status code and:

  ```json
  {"ok": false, "error": {"code": "not_found", "message": "path not found: x"}}
  ```

- Request IDs, pagination and ETag-based conditional writes are not implemented
  in 0.1.0.
- Paths are **root-relative**. A leading `/` is accepted and ignored. `..` is
  always rejected.

## Error codes

| Code | HTTP | Meaning |
| --- | --- | --- |
| `bad_request` | 400 | Invalid argument, path or body |
| `unauthorized` | 401 | Missing/invalid token |
| `forbidden` | 403 | Permission denied, exec disabled, CORS disabled |
| `path_escape` | 403 | `..` or a symlink tries to leave the root |
| `read_only` | 403 | Root is `ro` |
| `not_found` | 404 | Path, root or environment does not exist |
| `conflict` | 409 | Destination exists, non-empty directory, etc. |
| `too_large` | 413 | Upload or search limit exceeded |
| `unsupported` | 415 | Binary file on a text endpoint |
| `range_not_satisfiable` | 416 | Invalid `Range` |
| `remote_error` | 502 | Gateway could not reach a remote bridge |
| `timeout` | 504 | Remote operation timed out |
| `internal_error` | 500 | Bug; details are logged to stderr |

## Authentication

```bash
TOKEN=$(lfb env token ubuntu-main)
curl -s -H "Authorization: Bearer $TOKEN" \
  'http://127.0.0.1:8790/v1/roots'
```

A missing/wrong token returns `401` with `WWW-Authenticate: Bearer realm="lfb"`.
Tokens are compared with `hmac.compare_digest`. They are stored in the config
file in plain text, which is why the config is written with mode `0600`.

## Endpoints

### `GET /v1/health`

No authentication. Safe to use for liveness checks.

```json
{
  "ok": true,
  "data": {
    "service": "linux-file-bridge",
    "version": "0.1.0",
    "protocol": 1,
    "environment": "ubuntu-main",
    "auth": "bearer",
    "time": "2025-01-01T00:00:00Z"
  }
}
```

### `GET /v1/roots`

Returns the public view of roots, limits and exec flags. No token is exposed.

```json
{
  "ok": true,
  "data": {
    "environment": "ubuntu-main",
    "kind": "local",
    "roots": {
      "project": {"path": "/workspace/linux", "mode": "rw", "description": ""}
    },
    "limits": {"max_read": 1048576, "max_upload": 536870912, "max_search_results": 200},
    "exec": {"enabled": false, "allow_shell": false}
  }
}
```

### `GET /v1/list?root=ROOT&path=PATH&hidden=0`

`hidden=0` hides dotfiles. Entries include `path` relative to the root, `type`
(`file`, `dir`, `symlink`, ...), `size`, `mtime` (epoch), `mtime_iso`, `mode`,
`symlink`, and for symlinks `symlink_target`; escaping symlinks are marked
`"blocked": true` and cannot be accessed.

### `GET /v1/stat?root=ROOT&path=PATH&hash=1`

Same metadata as a list entry. `hash=1` adds `sha256` for regular files up to
64 MiB.

### `GET /v1/read?root=ROOT&path=PATH&offset=N&max=BYTES&binary=1`

Reads up to `max` bytes (capped by the root's `max_read`). UTF-8 text is
returned in `content` with `encoding: "utf-8"`. Binary content is rejected
unless `binary=1`, in which case `content` is base64 and `encoding` is
`"base64"`. `truncated` tells you whether more bytes exist; use `offset` to
continue.

```json
{
  "ok": true,
  "data": {
    "root": "project",
    "path": "README.md",
    "size": 1200,
    "offset": 0,
    "returned": 1200,
    "truncated": false,
    "encoding": "utf-8",
    "content": "# ..."
  }
}
```

### `GET /v1/download?root=ROOT&path=PATH`

Raw bytes with `Content-Disposition`, `ETag`, `Last-Modified`,
`Accept-Ranges: bytes`. A single byte range is supported:

```bash
curl -H "Authorization: Bearer $TOKEN" -H 'Range: bytes=0-99' \
  'http://127.0.0.1:8790/v1/download?root=project&path=big.iso' -o part
```

`HEAD` is supported and returns the headers without a body.

### `PUT /v1/write?root=ROOT&path=PATH&overwrite=1&mkdir=1`

Raw request body; **`Content-Length` is required**. Writes to a temporary file
in the destination directory, fsyncs, then atomically renames. `overwrite=0`
returns `409` if the file exists; `mkdir=1` creates missing parents. Optional
`X-LFB-Mtime` (Unix seconds) preserves the source mtime (used by `lfb sync`).

```bash
printf 'hello\n' | curl -s -X PUT \
  -H "Authorization: Bearer $TOKEN" \
  --data-binary @- \
  'http://127.0.0.1:8790/v1/write?root=project&path=hello.txt'
```

The response is the new file's stat object.

### `POST /v1/mkdir`

```json
{"root": "project", "path": "src/app", "parents": true, "exist_ok": true}
```

### `POST /v1/move`

```json
{"root": "project", "src": "a.txt", "dst": "archive/a.txt", "overwrite": false}
```

Moves happen inside one environment and one root. `dst_root` is accepted for
compatibility but must be the same root.

### `POST /v1/remove` or `DELETE /v1/remove`

POST body:

```json
{"root": "project", "path": "build", "recursive": true}
```

DELETE query:

```text
/v1/remove?root=project&path=build&recursive=1
```

Removing a root itself is always refused. Deleting a symlink removes the link,
not its target (but escaping symlinks are rejected by path resolution).

### `GET /v1/search`

| Parameter | Description |
| --- | --- |
| `glob` / `name` | Glob on relative path and basename (`*.py`, `src/**`) |
| `regex` | Python regex on relative paths |
| `contains` | Only files containing this text (max 2 MiB, UTF-8) |
| `type` | `all`, `file`, `dir`, `symlink` |
| `max` | Result cap (also capped by `limits.max_search_results`) |
| `exclude` | Comma-separated or repeated glob patterns |
| `hidden` | `0` to skip dotfiles |

Search does not follow symlinked directories. Results include `root`, `path`,
`count`, `truncated`, `scanned`.

### `GET /v1/tree?root=ROOT&path=PATH&depth=2`

Nested `entries` with `children`. `depth` is 0–20. Node count is capped to avoid
runaway output.

### `POST /v1/exec`

Disabled unless the environment config has `exec.enabled: true`. Requires the
root to be writable.

```json
{"root": "project", "path": "", "argv": ["git", "status", "--short"]}
```

or shell mode (requires `exec.allow_shell: true`):

```json
{"root": "project", "path": "", "command": "git status --short", "shell": true, "timeout": 10}
```

Returns `exit_code`, `timed_out`, `duration`, `stdout`, `stderr`, `truncated`.
The command runs as the same OS user as the bridge; **there is no sandbox**.

## Gateway

`lfb gateway` exposes the same endpoints plus:

- `GET /v1/envs` — list environments and their roots, read-only and exec state.
- Environment selection for every other endpoint:
  - query `?env=ubuntu-dev`
  - header `X-LFB-Env: ubuntu-dev`
  - fall back to `gateway.default_env`
- Response header `X-LFB-Env` identifies the environment that served the request.
- Local environments are handled in-process; bridge environments are proxied
  with streaming request/response bodies. The gateway's own token is checked on
  ingress and replaced with the remote bridge token on egress.

```bash
curl -s -H "Authorization: Bearer $GATEWAY_TOKEN" \
  'http://127.0.0.1:8787/v1/list?env=ubuntu-dev&root=project&path='
```

## MCP

`lfb mcp` is a stdio MCP server. Messages are newline-delimited JSON-RPC 2.0 on
stdin/stdout; all logs go to stderr. Supported protocol versions:

- `2024-11-05`
- `2025-03-26`
- `2025-06-18`

The server announces the `tools` capability only. `resources/list` and
`prompts/list` return empty lists for compatibility.

### Tools

| Name | Required | Optional |
| --- | --- | --- |
| `lfb_list_envs` | — | — |
| `lfb_list` | — | `env`, `root`, `path`, `depth`, `show_hidden` |
| `lfb_tree` | — | `env`, `root`, `path`, `depth`, `show_hidden` |
| `lfb_stat` | `path` | `env`, `root`, `hash` |
| `lfb_read` | `path` | `env`, `root`, `offset`, `limit` |
| `lfb_write` | `path`, `content` | `env`, `root`, `encoding`, `create_dirs`, `overwrite` |
| `lfb_mkdir` | `path` | `env`, `root`, `parents`, `exist_ok` |
| `lfb_move` | `src`, `dst` | `env`, `root`, `overwrite` |
| `lfb_delete` | `path` | `env`, `root`, `recursive` |
| `lfb_search` | — | `env`, `root`, `path`, `glob`, `regex`, `contains`, `type`, `max_results`, `exclude`, `show_hidden` |
| `lfb_exec` | one of `argv`/`command` | `env`, `root`, `path`, `shell`, `timeout`, `max_output` |

Tool failures are returned as MCP tool results with `"isError": true` and a
JSON text payload, so the agent can recover. JSON-RPC protocol errors use
standard codes (`-32600`, `-32601`, `-32602`, `-32603`).

Example session:

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"android-agent","version":"1"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/list"}
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"lfb_read","arguments":{"env":"ubuntu-dev","root":"project","path":"README.md"}}}
```

## HTTP client / scripting

Any HTTP client works. For streaming uploads, remember to send
`Content-Length`. For downloads, support `Accept-Ranges`. The `lfb` CLI and
Python `BridgeClient` are reference implementations in `lfb/client.py`.
