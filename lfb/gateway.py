"""Unified multi-environment gateway.

The gateway exposes exactly the same ``/v1/*`` API as a normal bridge, but adds
an ``env`` selector (query parameter or ``X-LFB-Env`` header).  Local
environments are served in-process; remote bridges are reverse-proxied with
streaming so large uploads/downloads stay out of memory.
"""

from __future__ import annotations

import logging
import socket
from typing import Any, Dict, Optional
from urllib.parse import parse_qsl, urlsplit, urlencode

from .api import ApiHandler, BridgeHTTPServer, Query
from .backends import Backend, open_backend
from .config import Config
from .errors import BadRequest, LfbError, NotFound, RemoteError
from .util import DEFAULT_CHUNK, LimitedReader
from .version import PROTOCOL_VERSION, USER_AGENT, __version__

LOG = logging.getLogger("lfb.gateway")

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "authorization",
    "expect",
}


class GatewayHandler(ApiHandler):
    def _dispatch(self, method: str) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = Query(parsed.query)
        self.query = query  # type: ignore[attr-defined]

        if path == "/":
            self._send_json(
                {
                    "ok": True,
                    "data": {
                        "service": "linux-file-bridge-gateway",
                        "version": __version__,
                        "protocol": PROTOCOL_VERSION,
                        "default_env": self.server.default_env,  # type: ignore[attr-defined]
                        "environments": list(self.server.backends.keys()),  # type: ignore[attr-defined]
                    },
                }
            )
            return
        if path == "/v1/health":
            self._send_json(
                {
                    "ok": True,
                    "data": {
                        "service": "linux-file-bridge-gateway",
                        "version": __version__,
                        "protocol": PROTOCOL_VERSION,
                        "default_env": self.server.default_env,  # type: ignore[attr-defined]
                        "environments": list(self.server.backends.keys()),  # type: ignore[attr-defined]
                    },
                }
            )
            return

        if not path.startswith("/v1/"):
            raise NotFound("unknown endpoint: %s" % path)

        self._require_auth()

        if path == "/v1/envs":
            self._handle_envs()
            return

        env_name = (
            self.query.get("env")
            or self.headers.get("X-LFB-Env")
            or self.server.default_env  # type: ignore[attr-defined]
        )
        try:
            backend = self.server.backends[env_name]  # type: ignore[attr-defined]
        except KeyError:
            raise NotFound(
                "unknown environment %r (available: %s)"
                % (env_name, ", ".join(self.server.backends.keys()))  # type: ignore[attr-defined]
            )

        if backend.kind == "local":
            self._override_fs = backend.impl
            try:
                ApiHandler._dispatch(self, method)
            finally:
                self._override_fs = None
            return
        self._proxy(backend, method, path, parsed.query)

    # ------------------------------------------------------------------ envs
    def _handle_envs(self) -> None:
        environments = []
        for name, backend in self.server.backends.items():  # type: ignore[attr-defined]
            item: Dict[str, Any] = {"name": name, "kind": backend.kind}
            if backend.kind == "bridge":
                item["url"] = backend.env.url
            try:
                item["read_only"] = backend.read_only
                item["exec_enabled"] = backend.exec_enabled
                item["roots"] = backend.roots_info()
            except LfbError as exc:
                item["error"] = {"code": exc.code, "message": exc.message}
            environments.append(item)
        self._send_json(
            {
                "ok": True,
                "data": {
                    "default_env": self.server.default_env,  # type: ignore[attr-defined]
                    "environments": environments,
                },
            }
        )

    # ----------------------------------------------------------------- proxy
    def _proxy(self, backend: Backend, method: str, path: str, raw_query: str) -> None:
        client = backend.impl
        params = [(key, value) for key, value in parse_qsl(raw_query, keep_blank_values=True) if key != "env"]
        target = client.base_path + path
        if params:
            target += "?" + urlencode(params)

        length = self._content_length()
        body = LimitedReader(self.rfile, length) if length else None
        headers = {
            "Authorization": "Bearer %s" % client.token,
            "User-Agent": USER_AGENT,
            "Accept": self.headers.get("Accept", "application/json"),
            "Connection": "close",
        }
        content_type = self.headers.get("Content-Type")
        if content_type:
            headers["Content-Type"] = content_type
        if length:
            headers["Content-Length"] = str(length)
        for key in ("Range", "If-None-Match", "If-Modified-Since"):
            value = self.headers.get(key)
            if value:
                headers[key] = value

        conn = client._connect(timeout=max(60.0, float(client.timeout)))
        try:
            conn.request(method, target, body=body, headers=headers)
            response = conn.getresponse()
        except (OSError, socket.error, Exception) as exc:  # noqa: BLE001 - mapped to 502
            try:
                conn.close()
            except Exception:
                pass
            if isinstance(exc, LfbError):
                raise
            raise RemoteError(
                "gateway could not reach environment %r at %s: %s" % (backend.name, client.url, exc)
            )

        try:
            self.send_response(response.status)
            self.send_header("X-LFB-Version", __version__)
            self.send_header("X-LFB-Protocol", str(PROTOCOL_VERSION))
            self.send_header("X-LFB-Env", backend.name)
            cors = getattr(self.server, "cors_origin", None)
            if cors:
                self.send_header("Access-Control-Allow-Origin", cors)
                self.send_header("Vary", "Origin")
            has_length = False
            for key, value in response.getheaders():
                lower = key.lower()
                if lower in HOP_BY_HOP or lower in ("server", "date"):
                    continue
                if lower.startswith("access-control-"):
                    continue
                if lower in ("x-lfb-version", "x-lfb-protocol", "x-lfb-env"):
                    continue
                if lower == "content-length":
                    has_length = True
                self.send_header(key, value)
            if not has_length:
                # Without a length the client can only detect EOF by closing.
                self.close_connection = True
            self.end_headers()
            self._headers_sent = True
            if self.command == "HEAD":
                return
            remaining = int(response.getheader("Content-Length") or -1)
            while True:
                block = response.read(DEFAULT_CHUNK)
                if not block:
                    break
                self.wfile.write(block)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        finally:
            try:
                response.close()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass


def make_gateway_server(
    config: Config,
    host: str = "127.0.0.1",
    port: int = 8787,
    *,
    token: Optional[str] = None,
    default_env: Optional[str] = None,
    tls_cert: Optional[str] = None,
    tls_key: Optional[str] = None,
    cors_origin: Optional[str] = None,
    quiet: bool = False,
) -> BridgeHTTPServer:
    import ssl

    backends: Dict[str, Backend] = {}
    for name, env in config.environments.items():
        backends[name] = open_backend(env)
    chosen_default = default_env or config.gateway.get("default_env") or config.default_env
    if chosen_default not in backends:
        raise BadRequest("gateway default env %r is not configured" % chosen_default)
    gateway_token = token if token is not None else config.gateway_token()
    if not gateway_token:
        raise BadRequest("gateway needs a token (set gateway.token or pass --token)")

    httpd = BridgeHTTPServer((host, port), GatewayHandler)
    httpd.backends = backends  # type: ignore[attr-defined]
    httpd.default_env = chosen_default  # type: ignore[attr-defined]
    httpd.token = gateway_token  # type: ignore[attr-defined]
    httpd.environment_name = "gateway"  # type: ignore[attr-defined]
    httpd.cors_origin = cors_origin  # type: ignore[attr-defined]
    httpd.quiet = quiet  # type: ignore[attr-defined]
    if tls_cert or tls_key:
        if not tls_cert or not tls_key:
            raise ValueError("both tls_cert and tls_key are required for TLS")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(certfile=tls_cert, keyfile=tls_key)
        httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    return httpd
