# Security policy

## Reporting a vulnerability

Please **do not** open a public issue for security problems. Use GitHub's
private vulnerability reporting: repository → Security → Report a
vulnerability.

Include the affected version (`lfb --version`), reproduction steps, and
the impact you believe it has (for example: a request that escapes a
configured root, or a token check that can be bypassed).

## Supported versions

Only the latest release receives security fixes.

## Security model

`lfb` is a file API with bearer-token authorization — not a sandbox and
not a multi-tenant file server. The full model (assets, trust boundaries,
what it does and does not protect against) lives in
[docs/SECURITY.md](docs/SECURITY.md). Read it before exposing a bridge
beyond your own machines: anyone holding an environment's token can use
everything that environment exposes, including `exec` if it is enabled.
