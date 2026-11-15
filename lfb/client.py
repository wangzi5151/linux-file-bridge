"""HTTP client for a single bridge environment."""

from __future__ import annotations

import base64
import http.client
import io
import json
import os
import socket
import ssl
import tempfile
import time
from typing import Any, BinaryIO, Dict, List, Optional, Tuple
from urllib.parse import urlencode, urlsplit

from .errors import (
    BadRequest,
    Conflict,
    LfbError,
    NotFound,
    RemoteError,
    Unauthorized,
    from_payload,
)
from .util import DEFAULT_CHUNK
from .version import USER_AGENT


class RemoteStream(object):
    def __init__(self, conn: http.client.HTTPConnection, response: http.client.HTTPResponse) -> None:
        self.conn = conn
        self.response = response
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        return self.response.read(size)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.response.close()
        finally:
            self.conn.close()

    def __enter__(self) -> "RemoteStream":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


class BridgeClient(object):
    """A small streaming client speaking the lfb HTTP protocol."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        verify_tls: Any = True,
        timeout: float = 30.0,
        name: str = "",
    ) -> None:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https"):
            raise BadRequest("bridge url must start with http:// or https://")
        if not parsed.hostname:
            raise BadRequest("bridge url has no hostname")
        self.url = url.rstrip("/")
        self.scheme = parsed.scheme
        self.host = parsed.hostname
        self.port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self.base_path = parsed.path.rstrip("/")
        self.token = token
        self.verify_tls = verify_tls
        self.timeout = float(timeout)
        self.name = name
        self._roots_cache: Optional[Dict[str, Any]] = None
        self._roots_lock = __import__("threading").Lock()

    # ------------------------------------------------------------- transport
    def _ssl_context(self) -> ssl.SSLContext:
        if self.verify_tls is True:
            return ssl.create_default_context()
        if self.verify_tls is False:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            return context
        return ssl.create_default_context(cafile=str(self.verify_tls))

    def _connect(self, timeout: Optional[float] = None) -> http.client.HTTPConnection:
        timeout_value = float(timeout if timeout is not None else self.timeout)
        if self.scheme == "https":
            return http.client.HTTPSConnection(
                self.host, self.port, timeout=timeout_value, context=self._ssl_context()
            )
        return http.client.HTTPConnection(self.host, self.port, timeout=timeout_value)

    def _url_path(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> str:
        path = self.base_path + endpoint
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                path += "?" + urlencode(clean, doseq=True)
        return path

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers = {
            "Authorization": "Bearer %s" % self.token,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "Connection": "close",
        }
        if extra:
            headers.update(extra)
        return headers

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        body: Any = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
        read_limit: int = 64 * 1024 * 1024,
    ) -> Tuple[int, Dict[str, str], bytes]:
        conn = self._connect(timeout=timeout)
        path = self._url_path(endpoint, params)
        try:
            conn.request(method, path, body=body, headers=self._headers(headers))
            response = conn.getresponse()
            status = response.status
            response_headers = {k.lower(): v for k, v in response.getheaders()}
            raw = response.read(read_limit)
            return status, response_headers, raw
        except (OSError, socket.error, ssl.SSLError, http.client.HTTPException) as exc:
            raise RemoteError(
                "cannot reach bridge %s (%s): %s" % (self.url, self.name or "remote", exc)
            )
        finally:
            conn.close()

    def _json_request(
        self,
        method: str,
        endpoint: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        payload: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        body = None
        headers = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers = {"Content-Type": "application/json; charset=utf-8"}
        status, _headers, raw = self._request(
            method,
            endpoint,
            params=params,
            body=body,
            headers=headers,
            timeout=timeout,
            read_limit=8 * 1024 * 1024,
        )
        data: Any = None
        if raw:
            try:
                data = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                data = None
        if status >= 400:
            if isinstance(data, dict):
                raise from_payload(data, status=status)
            if status == 401:
                raise Unauthorized("unauthorized (check the token for %s)" % (self.name or self.url))
            raise RemoteError(
                "bridge returned HTTP %d: %s" % (status, raw[:300].decode("utf-8", "replace"))
            )
        if not isinstance(data, dict) or not data.get("ok"):
            if isinstance(data, dict) and data.get("error"):
                raise from_payload(data, status=status)
            raise RemoteError("bridge returned an invalid response")
        return data.get("data") or {}

    # ------------------------------------------------------------------ info
    def health(self) -> Dict[str, Any]:
        status, _headers, raw = self._request("GET", "/v1/health", read_limit=1024 * 1024)
        if status >= 400:
            raise RemoteError("health check failed with HTTP %d" % status)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise RemoteError("bridge health check returned invalid JSON")
        return data.get("data") or {}

    def _load_info(self) -> Dict[str, Any]:
        with self._roots_lock:
            if self._roots_cache is not None:
                return self._roots_cache
        data = self._json_request("GET", "/v1/roots", timeout=max(self.timeout, 15.0))
        with self._roots_lock:
            self._roots_cache = data
        return data

    def public_roots(self) -> Dict[str, Any]:
        return dict(self._load_info().get("roots") or {})

    def roots_info(self) -> Dict[str, Any]:
        return dict(self._load_info())

    @property
    def default_root(self) -> str:
        roots = self.public_roots()
        if not roots:
            raise RemoteError("bridge reports no roots")
        return next(iter(roots))

    @property
    def limits(self) -> Dict[str, int]:
        limits = self._load_info().get("limits") or {}
        return {
            "max_read": int(limits.get("max_read", 1024 * 1024)),
            "max_upload": int(limits.get("max_upload", 512 * 1024 * 1024)),
            "max_search_results": int(limits.get("max_search_results", 200)),
        }

    @property
    def exec_enabled(self) -> bool:
        return bool((self._load_info().get("exec") or {}).get("enabled"))

    @property
    def exec_allow_shell(self) -> bool:
        return bool((self._load_info().get("exec") or {}).get("allow_shell"))

    # ------------------------------------------------------------------ files
    def list_dir(
        self, root: Optional[str], path: str, *, show_hidden: bool = True
    ) -> Dict[str, Any]:
        return self._json_request(
            "GET",
            "/v1/list",
            params={"root": root, "path": path, "hidden": "1" if show_hidden else "0"},
        )

    def stat(self, root: Optional[str], path: str, *, want_hash: bool = False) -> Dict[str, Any]:
        return self._json_request(
            "GET",
            "/v1/stat",
            params={"root": root, "path": path, "hash": "1" if want_hash else "0"},
        )

    def read_bytes(
        self,
        root: Optional[str],
        path: str,
        *,
        offset: int = 0,
        max_bytes: Optional[int] = None,
    ) -> Tuple[bytes, Dict[str, Any], bool]:
        data = self._json_request(
            "GET",
            "/v1/read",
            params={
                "root": root,
                "path": path,
                "offset": offset,
                "max": max_bytes,
                "binary": "1",
            },
        )
        content = data.get("content") or ""
        if data.get("encoding") == "base64":
            try:
                raw = base64.b64decode(content)
            except (ValueError, TypeError):
                raise RemoteError("bridge returned invalid base64 content")
        else:
            raw = content.encode("utf-8")
        entry = {
            "path": data.get("path", path),
            "size": data.get("size"),
            "mtime": data.get("mtime"),
            "type": "file",
        }
        return raw, entry, bool(data.get("truncated"))

    def open_read(
        self, root: Optional[str], path: str
    ) -> Tuple[RemoteStream, Dict[str, Any]]:
        conn = self._connect()
        target = self._url_path("/v1/download", {"root": root, "path": path})
        try:
            conn.request("GET", target, headers=self._headers())
            response = conn.getresponse()
            if response.status >= 400:
                raw = response.read(1024 * 1024)
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    payload = None
                if isinstance(payload, dict):
                    raise from_payload(payload, status=response.status)
                raise RemoteError("download failed with HTTP %d" % response.status)
            length = response.getheader("Content-Length")
            entry = {
                "path": path,
                "type": "file",
                "size": int(length) if length is not None else None,
                "mtime": None,
            }
            return RemoteStream(conn, response), entry
        except LfbError:
            conn.close()
            raise
        except (OSError, socket.error, ssl.SSLError, http.client.HTTPException) as exc:
            conn.close()
            raise RemoteError("download failed: %s" % exc)

    def download_to(
        self,
        root: Optional[str],
        path: str,
        destination: str,
        *,
        overwrite: bool = True,
        create_dirs: bool = False,
        mtime: Optional[float] = None,
    ) -> Dict[str, Any]:
        if os.path.exists(destination) and not overwrite:
            raise Conflict("local destination already exists: %s" % destination)
        parent = os.path.dirname(destination) or "."
        if not os.path.isdir(parent):
            if not create_dirs:
                raise NotFound("local parent directory does not exist: %s" % parent)
            os.makedirs(parent, exist_ok=True)
        stream, entry = self.open_read(root, path)
        fd, tmp = tempfile.mkstemp(prefix=".lfb-download-", suffix=".tmp", dir=parent)
        written = 0
        try:
            with os.fdopen(fd, "wb") as handle:
                while True:
                    block = stream.read(DEFAULT_CHUNK)
                    if not block:
                        break
                    handle.write(block)
                    written += len(block)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o644)
            os.replace(tmp, destination)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        finally:
            stream.close()
        if mtime:
            try:
                os.utime(destination, (time.time(), float(mtime)))
            except OSError:
                pass
        return {"path": destination, "size": written, "source": entry.get("path", path)}

    def upload_file(
        self,
        root: Optional[str],
        path: str,
        fileobj: BinaryIO,
        length: int,
        *,
        overwrite: bool = True,
        create_dirs: bool = False,
        mtime: Optional[float] = None,
    ) -> Dict[str, Any]:
        conn = self._connect()
        target = self._url_path(
            "/v1/write",
            {
                "root": root,
                "path": path,
                "overwrite": "1" if overwrite else "0",
                "mkdir": "1" if create_dirs else "0",
            },
        )
        extra_headers = {"Content-Type": "application/octet-stream", "Content-Length": str(length)}
        if mtime is not None:
            extra_headers["X-LFB-Mtime"] = "%.6f" % float(mtime)
        headers = self._headers(extra_headers)
        try:
            conn.request("PUT", target, body=fileobj, headers=headers)
            response = conn.getresponse()
            raw = response.read(8 * 1024 * 1024)
        except (OSError, socket.error, ssl.SSLError, http.client.HTTPException) as exc:
            conn.close()
            raise RemoteError("upload failed: %s" % exc)
        finally:
            try:
                conn.close()
            except Exception:
                pass
        data: Any = None
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            data = None
        if response.status >= 400:
            if isinstance(data, dict):
                raise from_payload(data, status=response.status)
            raise RemoteError("upload failed with HTTP %d" % response.status)
        if not isinstance(data, dict) or not data.get("ok"):
            raise RemoteError("bridge returned an invalid upload response")
        return data.get("data") or {}

    def write_stream(
        self,
        root: Optional[str],
        path: str,
        stream: Any,
        *,
        length: Optional[int] = None,
        overwrite: bool = True,
        create_dirs: bool = False,
        mtime: Optional[float] = None,
    ) -> Dict[str, Any]:
        if length is None:
            data = stream.read()
            length = len(data)
            stream = io.BytesIO(data)
        return self.upload_file(
            root,
            path,
            stream,
            int(length),
            overwrite=overwrite,
            create_dirs=create_dirs,
            mtime=mtime,
        )

    def write_bytes(
        self,
        root: Optional[str],
        path: str,
        data: bytes,
        *,
        overwrite: bool = True,
        create_dirs: bool = False,
        mtime: Optional[float] = None,
    ) -> Dict[str, Any]:
        return self.upload_file(
            root,
            path,
            io.BytesIO(data),
            len(data),
            overwrite=overwrite,
            create_dirs=create_dirs,
            mtime=mtime,
        )

    # ------------------------------------------------------------------- ops
    def mkdir(
        self,
        root: Optional[str],
        path: str,
        *,
        parents: bool = False,
        exist_ok: bool = True,
    ) -> Dict[str, Any]:
        return self._json_request(
            "POST",
            "/v1/mkdir",
            payload={"root": root, "path": path, "parents": parents, "exist_ok": exist_ok},
        )

    def move(
        self,
        root: Optional[str],
        src: str,
        dst_root: Optional[str],
        dst: str,
        *,
        overwrite: bool = False,
    ) -> Dict[str, Any]:
        return self._json_request(
            "POST",
            "/v1/move",
            payload={
                "root": root,
                "src": src,
                "dst_root": dst_root,
                "dst": dst,
                "overwrite": overwrite,
            },
        )

    def remove(self, root: Optional[str], path: str, *, recursive: bool = False) -> Dict[str, Any]:
        return self._json_request(
            "POST",
            "/v1/remove",
            payload={"root": root, "path": path, "recursive": recursive},
        )

    def search(
        self,
        root: Optional[str],
        path: str,
        *,
        glob: Optional[str] = None,
        regex: Optional[str] = None,
        contains: Optional[str] = None,
        want_type: str = "all",
        max_results: int = 200,
        exclude: Optional[List[str]] = None,
        show_hidden: bool = True,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "root": root,
            "path": path,
            "glob": glob,
            "regex": regex,
            "contains": contains,
            "type": want_type,
            "max": max_results,
            "hidden": "1" if show_hidden else "0",
        }
        if exclude:
            params["exclude"] = list(exclude)
        return self._json_request("GET", "/v1/search", params=params)

    def tree(
        self,
        root: Optional[str],
        path: str,
        *,
        depth: int = 2,
        show_hidden: bool = True,
        max_nodes: int = 1000,
    ) -> Dict[str, Any]:
        return self._json_request(
            "GET",
            "/v1/tree",
            params={
                "root": root,
                "path": path,
                "depth": depth,
                "hidden": "1" if show_hidden else "0",
            },
        )

    def exec_command(
        self,
        root: Optional[str],
        path: str,
        *,
        argv: Optional[List[str]] = None,
        command: Optional[str] = None,
        shell: bool = False,
        timeout: Optional[float] = None,
        max_output: Optional[int] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "root": root,
            "path": path,
            "shell": shell,
            "timeout": timeout,
            "max_output": max_output,
        }
        if argv is not None:
            payload["argv"] = argv
        if command is not None:
            payload["command"] = command
        return self._json_request(
            "POST",
            "/v1/exec",
            payload=payload,
            timeout=max(self.timeout, float(timeout or 30) + 5.0),
        )

    # ------------------------------------------------------------------ misc
    def delete(self, root: Optional[str], path: str, *, recursive: bool = False) -> Dict[str, Any]:
        return self.remove(root, path, recursive=recursive)

    def describe(self) -> str:
        return "%s (%s)" % (self.name or "bridge", self.url)
