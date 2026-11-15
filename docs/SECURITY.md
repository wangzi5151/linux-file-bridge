# Security model

`lfb` is a **file API with bearer-token authorization**, not a sandbox, not an
SSH replacement, and not a multi-tenant file server. This document describes
what it protects against and what it does not.

## Assets

- Files under configured roots in every environment.
- Bearer tokens in the config file and in memory.
- Optional command execution (`exec`) and its output.

## Trust boundaries

```text
mobile app / agent  --(MCP over stdio)-->  lfb mcp process
        |                                      |
        |  (HTTP + token, optional)            |  local filesystem
        v                                      v
   lfb gateway  --(HTTP + token)-->  lfb bridge  --(same OS user)--> files
```

The bridge assumes the **local OS user** is trusted. It protects the root
boundary and the token, not the host from that user.

## Protections in place

### Root confinement

- Paths are split into components; `..` and NUL bytes are rejected.
- The final path is resolved with `os.path.realpath`; the result must have the
  root's real path as a prefix (`os.path.commonpath`).
- Symlinks are followed only if their resolved target stays inside the root.
  Escaping symlinks are reported as `"blocked": true` in listings and cannot be
  read, written, moved or removed through the bridge.
- For a new file, the closest existing parent is resolved and checked; a
  symlinked parent cannot escape.
- Directory operations (`mkdir`, `move`, `remove`, `search`, `tree`) perform the
  same checks. Removing a root itself is refused.
- Directory listings do not descend into symlinked directories.
- TOCTOU: an attacker with local write access to the root could swap a symlink
  between resolution and use. That attacker is already inside the trust
  boundary. Use OS-level permissions for defense in depth.

### Write safety

- Files are written to a temporary file in the destination directory, fsynced,
  chmodded (preserving the old mode when replacing), then atomically renamed.
- `overwrite=false` returns `409` rather than silently clobbering.
- Upload size is bounded by `limits.max_upload` while streaming; temporary
  files are removed on error.
- A short read (`Content-Length` mismatch) aborts and deletes the temp file.

### Authentication

- Bearer token in `Authorization` or `X-LFB-Token`.
- Compared with `hmac.compare_digest`.
- Authentication failures return `401` with `WWW-Authenticate`.
- The gateway checks its own token and replaces it with the remote bridge token
  when proxying; remote tokens are not exposed to clients.
- `/v1/health` and `/` are intentionally public and contain no file data.

### Configuration permissions

- Config is written atomically with mode `0600`; its directory is set to `0700`.
- `lfb doctor` warns if the config is group/world readable.
- Tokens are never logged by the server. `lfb env token NAME` prints a token
  only when explicitly requested.

### Network defaults

- `serve` and `gateway` bind `127.0.0.1` by default.
- TLS is available with `--tls-cert`/`--tls-key` (TLS 1.2+).
- CORS is disabled unless `--cors ORIGIN` is supplied.

### Command execution

- Disabled by default (`exec.enabled=false`).
- Shell mode additionally requires `exec.allow_shell=true`.
- Runs with a minimal environment (not the bridge's full environment, so tokens
  in env vars are less likely to leak), a timeout, output caps, and a new
  process group that is killed on timeout.
- It is **not** a sandbox: the command has the same filesystem and network
  access as the `lfb` process and can read anything that user can read, not just
  the configured roots. Do not enable it for untrusted clients.

## Not protected / known limitations

- **No encryption at rest.** Config and files are ordinary plaintext.
- **No multi-user ACLs or rate limiting.** One token grants all roots in that
  environment. Run separate bridges with separate tokens/users if needed.
- **No audit log** in 0.1.0; access logs go to stderr.
- **No file locks or conflict detection.** Concurrent writes are last-writer-
  wins; atomic replacement prevents torn files, not lost updates.
- **No chunked upload support.** A client must send `Content-Length`; a stream
  without a length is rejected.
- **Large reads are capped** (`max_read`, default 1 MiB; MCP default 256 KiB).
  This limits memory use, not authorization.
- **Escaping symlinks cannot be deleted via the bridge** because removal also
  goes through resolution. Remove them on the host if needed.
- **`sync --delete` is destructive.** It requires explicit `--delete` and, when
  the destination is a root, `--allow-root-delete`.
- **Android process freezing** can make a bridge look "down"; it is not a
  security boundary.
- **Debug logs and tracebacks** go to stderr. Do not expose stderr to untrusted
  clients.

## Deployment recommendations

1. Keep the default loopback bind. Use `ssh -L` / Tailscale / WireGuard for
   remote access instead of binding publicly.
2. If you must bind a LAN address, use TLS with a real certificate (or a trusted
   private CA), a long random token, and a firewall.
3. Use one bridge per trust boundary (per user or per project), with only the
   needed roots and `ro` where possible.
4. Leave `exec` off unless you understand that it is equivalent to shell access.
5. Rotate tokens with `lfb env token NAME --rotate` if a token leaks, then
   restart the affected bridge/gateway. Update clients.
6. Do not commit `~/.config/lfb/config.json`; it contains tokens.
7. Run `lfb doctor` after changing mounts, proot paths, or ports.

## Reporting a vulnerability

Please open a private security advisory on GitHub, or email the maintainer
listed in `pyproject.toml`. Include a minimal reproduction and the `lfb
--version` output. Do not open a public issue with a working exploit.
