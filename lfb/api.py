"""HTTP API shared by a single-environment bridge and the multi-env gateway."""

from __future__ import annotations

import email.utils
import json
import logging
import os
import ssl
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, urlsplit

from .errors import LfbError, TooLarge, Unauthorized, Unsupported
from .fsops import LocalFS
from .util import (
    DEFAULT_CHUNK,
    LimitedReader,
    constant_time_eq,
    is_text_bytes,
    b64encode,
    parse_content_range,
    parse_size,
    utc_now,
)
from .version import PROTOCOL_VERSION, __version__

LOG = logging.getLogger("lfb.http")


class Query(object):
    def __init__(self, raw_query: str) -> None:
        self.raw = parse_qs(raw_query, keep_blank_values=True)

    def get(self, name: str, default: Optional[str] = None) -> Optional[str]:
        values = self.raw.get(name)
        if not values:
            return default
        return values[-1]

    def get_all(self, name: str) -> List[str]:
        return list(self.raw.get(name, []))

    def int(self, name: str, default: int, minimum: Optional[int] = None, maximum: Optional[int] = None) -> int:
        value = self.get(name)
        if value is None or value == "":
            result = default
        else:
            try:
                result = int(value)
            except (TypeError, ValueError):
                from .errors import BadRequest

                raise BadRequest("query parameter %r must be an integer" % name)
        if minimum is not None and result < minimum:
            from .errors import BadRequest

            raise BadRequest("query parameter %r must be >= %d" % (name, minimum))
        if maximum is not None and result > maximum:
            from .errors import BadRequest

            raise BadRequest("query parameter %r must be <= %d" % (name, maximum))
        return result

    def bool(self, name: str, default: bool = False) -> bool:
        value = self.get(name)
        if value is None:
            return default
        return value.strip().lower() in ("1", "true", "yes", "on")

    def size(self, name: str, default: Optional[int] = None) -> Optional[int]:
        value = self.get(name)
        if value is None or value == "":
            return default
        return parse_size(value, name=name)


def _quote_filename(name: str) -> str:
    quoted = quote(name, safe="")
    return "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (
        name.replace('"', "_").replace("\\", "_"),
        quoted,
    )


class ApiHandler(BaseHTTPRequestHandler):
    """One request handler.  Subclasses/factories provide ``api_context``."""

    protocol_version = "HTTP/1.1"
    server_version = "lfb/%s" % __version__
    sys_version = ""

    # ------------------------------------------------------------------ setup
    def setup(self) -> None:
        BaseHTTPRequestHandler.setup(self)
        self._headers_sent = False
        self._is_head = False

    def log_message(self, fmt: str, *args: Any) -> None:
        if not self.server.quiet:  # type: ignore[attr-defined]
            sys.stderr.write(
                "%s - %s\n" % (self.address_string(), fmt % args)
            )
            sys.stderr.flush()

    def log_error(self, fmt: str, *args: Any) -> None:
        self.log_message(fmt, *args)

    # --------------------------------------------------------------- methods
    def do_OPTIONS(self) -> None:
        origin = getattr(self.server, "cors_origin", None)
        if origin:
            self._cors_headers()
            self.send_response(204)
            self.send_header("Access-Control-Allow-Methods", "GET, HEAD, PUT, POST, DELETE, OPTIONS")
            self.send_header(
                "Access-Control-Allow-Headers",
                "Authorization, Content-Type, X-LFB-Token, X-LFB-Env",
            )
            self.send_header("Access-Control-Max-Age", "86400")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._send_error_json(
                LfbError("CORS is not enabled", code="forbidden", status=403)
            )

    def do_GET(self) -> None:
        self._safe_dispatch("GET")

    def do_HEAD(self) -> None:
        self._is_head = True
        try:
            self._safe_dispatch("HEAD")
        finally:
            self._is_head = False

    def do_PUT(self) -> None:
        self._safe_dispatch("PUT")

    def do_POST(self) -> None:
        self._safe_dispatch("POST")

    def do_DELETE(self) -> None:
        self._safe_dispatch("DELETE")

    # ------------------------------------------------------------- dispatch
    def _safe_dispatch(self, method: str) -> None:
        try:
            self._dispatch(method)
        except LfbError as exc:
            self._send_error_json(exc)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:
            LOG.error("unhandled error while serving %s %s\n%s", method, self.path, traceback.format_exc())
            self.close_connection = True
            try:
                self._send_error_json(
                    LfbError("internal server error", code="internal_error", status=500)
                )
            except Exception:
                pass

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
                        "service": "linux-file-bridge",
                        "version": __version__,
                        "protocol": PROTOCOL_VERSION,
                        "environment": self.server.environment_name,  # type: ignore[attr-defined]
                        "endpoints": ["/v1/health", "/v1/roots", "/v1/list", "/v1/read", "/v1/download"],
                    },
                }
            )
            return
        if path == "/v1/health":
            self._send_json(
                {
                    "ok": True,
                    "data": {
                        "service": "linux-file-bridge",
                        "version": __version__,
                        "protocol": PROTOCOL_VERSION,
                        "environment": self.server.environment_name,  # type: ignore[attr-defined]
                        "auth": "bearer",
                        "time": utc_now(),
                    },
                }
            )
            return

        if not path.startswith("/v1/"):
            from .errors import NotFound

            raise NotFound("unknown endpoint: %s" % path)

        self._require_auth()

        if method in ("GET", "HEAD"):
            if path == "/v1/roots":
                self._handle_roots()
            elif path == "/v1/list":
                self._handle_list()
            elif path == "/v1/stat":
                self._handle_stat()
            elif path == "/v1/read":
                self._handle_read()
            elif path == "/v1/download":
                self._handle_download()
            elif path == "/v1/search":
                self._handle_search()
            elif path == "/v1/tree":
                self._handle_tree()
            else:
                from .errors import NotFound

                raise NotFound("unknown endpoint: %s" % path)
        elif method == "PUT" and path == "/v1/write":
            self._handle_write()
        elif method == "POST" and path == "/v1/mkdir":
            self._handle_mkdir()
        elif method == "POST" and path == "/v1/move":
            self._handle_move()
        elif method in ("POST", "DELETE") and path == "/v1/remove":
            self._handle_remove()
        elif method == "POST" and path == "/v1/exec":
            self._handle_exec()
        else:
            from .errors import BadRequest

            raise BadRequest("method %s is not allowed for %s" % (method, path))

    # ------------------------------------------------------------------ auth
    def _require_auth(self) -> None:
        expected = getattr(self.server, "token", "")
        if not expected:
            return
        header = self.headers.get("Authorization", "")
        supplied = ""
        if header.lower().startswith("bearer "):
            supplied = header[7:].strip()
        if not supplied:
            supplied = self.headers.get("X-LFB-Token", "").strip()
        if not supplied or not constant_time_eq(supplied, expected):
            self.close_connection = True
            raise Unauthorized("missing or invalid bearer token")
        self._auth_ok = True

    # ------------------------------------------------------------- responses
    def _common_headers(self) -> None:
        self.send_header("X-LFB-Version", __version__)
        self.send_header("X-LFB-Protocol", str(PROTOCOL_VERSION))
        origin = getattr(self.server, "cors_origin", None)
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def _cors_headers(self) -> None:
        origin = getattr(self.server, "cors_origin", None)
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def _send_json(self, payload: Dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._common_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self._headers_sent = True
        if not self._is_head:
            self.wfile.write(body)

    def _send_error_json(self, exc: LfbError) -> None:
        if self._headers_sent:
            self.close_connection = True
            return
        payload = exc.to_dict()
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(exc.status)
            self._common_headers()
            if exc.status == 401:
                self.send_header("WWW-Authenticate", 'Bearer realm="lfb"')
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self._headers_sent = True
            if not self._is_head:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _read_json_body(self) -> Dict[str, Any]:
        length = self._content_length(maximum=4 * 1024 * 1024)
        if length == 0:
            return {}
        data = self.rfile.read(length)
        if len(data) != length:
            from .errors import BadRequest

            raise BadRequest("incomplete request body")
        try:
            payload = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            from .errors import BadRequest

            raise BadRequest("invalid JSON body: %s" % exc)
        if not isinstance(payload, dict):
            from .errors import BadRequest

            raise BadRequest("JSON body must be an object")
        return payload

    def _content_length(self, maximum: Optional[int] = None) -> int:
        raw = self.headers.get("Content-Length")
        if raw is None:
            if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
                from .errors import BadRequest

                raise BadRequest(
                    "chunked request bodies are not supported; send Content-Length"
                )
            return 0
        try:
            length = int(raw)
        except ValueError:
            from .errors import BadRequest

            raise BadRequest("invalid Content-Length header")
        if length < 0:
            from .errors import BadRequest

            raise BadRequest("invalid Content-Length header")
        if maximum is not None and length > maximum:
            raise TooLarge("request body is limited to %d bytes" % maximum)
        return length

    # --------------------------------------------------------------- helpers
    def _root_path(self) -> Tuple[Optional[str], str]:
        return self.query.get("root"), self.query.get("path", "") or ""

    def _fs(self) -> LocalFS:
        override = getattr(self, "_override_fs", None)
        if override is not None:
            return override
        return self.server.fs  # type: ignore[attr-defined]

    # -------------------------------------------------------------- handlers
    def _handle_roots(self) -> None:
        fs = self._fs()
        self._send_json(
            {
                "ok": True,
                "data": {
                    "environment": fs.name,
                    "kind": "local",
                    "roots": fs.public_roots(),
                    "limits": fs.limits,
                    "exec": {
                        "enabled": bool(fs.exec_config["enabled"]),
                        "allow_shell": bool(fs.exec_config["allow_shell"]),
                    },
                },
            }
        )

    def _handle_list(self) -> None:
        root, path = self._root_path()
        result = self._fs().list_dir(
            root, path, show_hidden=self.query.bool("hidden", True)
        )
        self._send_json({"ok": True, "data": result})

    def _handle_stat(self) -> None:
        root, path = self._root_path()
        result = self._fs().stat(root, path, want_hash=self.query.bool("hash", False))
        self._send_json({"ok": True, "data": result})

    def _handle_read(self) -> None:
        root, path = self._root_path()
        fs = self._fs()
        offset = self.query.int("offset", 0, minimum=0)
        configured = fs.limits["max_read"]
        requested = self.query.size("max", None)
        limit = min(requested, configured) if requested is not None else configured
        data, entry, truncated = fs.read_bytes(
            root, path, offset=offset, max_bytes=limit
        )
        binary = not is_text_bytes(data)
        if binary and not self.query.bool("binary", False):
            raise Unsupported(
                "file looks binary (%d bytes returned); pass binary=1 for base64 "
                "or use /v1/download" % len(data),
                details={"size": entry["size"], "path": entry["path"]},
            )
        payload: Dict[str, Any] = {
            "root": root or fs.default_root,
            "path": entry["path"],
            "size": entry["size"],
            "offset": offset,
            "returned": len(data),
            "truncated": truncated,
            "encoding": "base64" if binary else "utf-8",
            "content": b64encode(data) if binary else data.decode("utf-8"),
            "mtime": entry.get("mtime"),
        }
        self._send_json({"ok": True, "data": payload})

    def _handle_download(self) -> None:
        root, path = self._root_path()
        fs = self._fs()
        handle, entry = fs.open_read(root, path)
        size = int(entry["size"] or 0)
        start = 0
        end = size - 1
        status = 200
        range_header = self.headers.get("Range")
        if range_header and size > 0:
            try:
                start, end = parse_content_range(range_header, size)
            except LfbError:
                handle.close()
                raise
            status = 206
        length = max(0, end - start + 1)
        self.send_response(status)
        self._common_headers()
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition", _quote_filename(os.path.basename(entry["path"]) or "download"))
        self.send_header("ETag", '"%d-%d"' % (size, int(float(entry.get("mtime") or 0))))
        last_modified = entry.get("mtime")
        if last_modified:
            self.send_header("Last-Modified", email.utils.formatdate(float(last_modified), usegmt=True))
        if status == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        self._headers_sent = True
        if self._is_head:
            handle.close()
            return
        try:
            if start:
                handle.seek(start)
            remaining = length
            while remaining > 0:
                block = handle.read(min(DEFAULT_CHUNK, remaining))
                if not block:
                    break
                self.wfile.write(block)
                remaining -= len(block)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        finally:
            handle.close()

    def _handle_search(self) -> None:
        root, path = self._root_path()
        excludes: List[str] = []
        for item in self.query.get_all("exclude"):
            excludes.extend(part for part in item.split(",") if part)
        result = self._fs().search(
            root,
            path,
            glob=self.query.get("glob") or self.query.get("name"),
            regex=self.query.get("regex"),
            contains=self.query.get("contains"),
            want_type=self.query.get("type", "all") or "all",
            max_results=self.query.int("max", 200, minimum=1),
            exclude=excludes,
            show_hidden=self.query.bool("hidden", True),
        )
        self._send_json({"ok": True, "data": result})

    def _handle_tree(self) -> None:
        root, path = self._root_path()
        result = self._fs().tree(
            root,
            path,
            depth=self.query.int("depth", 2, minimum=0, maximum=20),
            show_hidden=self.query.bool("hidden", True),
        )
        self._send_json({"ok": True, "data": result})

    def _handle_write(self) -> None:
        root, path = self._root_path()
        fs = self._fs()
        length = self._content_length(maximum=fs.limits["max_upload"])
        stream = LimitedReader(self.rfile, length)
        try:
            entry = fs.write_stream(
                root,
                path,
                stream,
                length=length,
                overwrite=self.query.bool("overwrite", True),
                create_dirs=self.query.bool("mkdir", False),
                mtime=self._mtime_header(),
            )
        except LfbError:
            self.close_connection = True
            raise
        if not stream.exhausted:
            self.close_connection = True
        self._send_json({"ok": True, "data": entry})

    def _mtime_header(self) -> Optional[float]:
        raw = self.headers.get("X-LFB-Mtime")
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            from .errors import BadRequest

            raise BadRequest("invalid X-LFB-Mtime header")

    def _handle_mkdir(self) -> None:
        body = self._read_json_body()
        entry = self._fs().mkdir(
            body.get("root"),
            body.get("path", ""),
            parents=bool(body.get("parents", False)),
            exist_ok=bool(body.get("exist_ok", True)),
        )
        self._send_json({"ok": True, "data": entry})

    def _handle_move(self) -> None:
        body = self._read_json_body()
        entry = self._fs().move(
            body.get("root"),
            body.get("src", ""),
            body.get("dst_root"),
            body.get("dst", ""),
            overwrite=bool(body.get("overwrite", False)),
        )
        self._send_json({"ok": True, "data": entry})

    def _handle_remove(self) -> None:
        if self.command == "DELETE":
            root = self.query.get("root")
            path = self.query.get("path", "") or ""
            recursive = self.query.bool("recursive", False)
        else:
            body = self._read_json_body()
            root = body.get("root")
            path = body.get("path", "")
            recursive = bool(body.get("recursive", False))
        result = self._fs().remove(root, path, recursive=recursive)
        self._send_json({"ok": True, "data": result})

    def _handle_exec(self) -> None:
        body = self._read_json_body()
        argv = body.get("argv")
        if argv is not None and not isinstance(argv, list):
            from .errors import BadRequest

            raise BadRequest("argv must be an array of strings")
        result = self._fs().exec_command(
            body.get("root"),
            body.get("path", ""),
            argv=argv,
            command=body.get("command"),
            shell=bool(body.get("shell", False)),
            timeout=body.get("timeout"),
            max_output=body.get("max_output"),
        )
        self._send_json({"ok": True, "data": result})


class BridgeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(
    fs: LocalFS,
    token: str,
    host: str = "127.0.0.1",
    port: int = 8790,
    *,
    tls_cert: Optional[str] = None,
    tls_key: Optional[str] = None,
    cors_origin: Optional[str] = None,
    quiet: bool = False,
) -> BridgeHTTPServer:
    class Handler(ApiHandler):
        pass

    if not token:
        raise ValueError("a non-empty bearer token is required")
    httpd = BridgeHTTPServer((host, port), Handler)
    httpd.fs = fs  # type: ignore[attr-defined]
    httpd.token = token  # type: ignore[attr-defined]
    httpd.environment_name = fs.name  # type: ignore[attr-defined]
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


def server_url(httpd: BridgeHTTPServer, host: str, tls: bool = False) -> str:
    actual_host = host
    actual_port = httpd.server_address[1]
    display_host = actual_host
    if display_host in ("0.0.0.0", "::", ""):
        display_host = "127.0.0.1"
    scheme = "https" if tls else "http"
    if ":" in display_host and not display_host.startswith("["):
        display_host = "[%s]" % display_host
    return "%s://%s:%d" % (scheme, display_host, actual_port)


def serve_forever(httpd: BridgeHTTPServer) -> None:
    try:
        httpd.serve_forever(poll_interval=0.25)
    finally:
        httpd.server_close()
