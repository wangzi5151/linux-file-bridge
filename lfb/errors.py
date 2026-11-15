"""Error types shared by the bridge server, client, gateway and CLI.

Every error has a stable machine-readable ``code`` that is returned by the HTTP
API and by the MCP tools.  Messages are meant for humans.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class LfbError(Exception):
    """Base class for all expected (non-crash) bridge errors."""

    code = "internal_error"
    status = 500

    def __init__(
        self,
        message: str,
        *,
        code: Optional[str] = None,
        status: Optional[int] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status is not None:
            self.status = status
        self.details = details or {}

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "ok": False,
            "error": {"code": self.code, "message": self.message},
        }
        if self.details:
            payload["error"]["details"] = self.details
        return payload


class ConfigError(LfbError):
    code = "config_error"
    status = 400


class BadRequest(LfbError):
    code = "bad_request"
    status = 400


class Unauthorized(LfbError):
    code = "unauthorized"
    status = 401


class Forbidden(LfbError):
    code = "forbidden"
    status = 403


class PathEscape(Forbidden):
    code = "path_escape"


class ReadOnly(Forbidden):
    code = "read_only"


class NotFound(LfbError):
    code = "not_found"
    status = 404


class Conflict(LfbError):
    code = "conflict"
    status = 409


class TooLarge(LfbError):
    code = "too_large"
    status = 413


class Unsupported(LfbError):
    code = "unsupported"
    status = 415


class RemoteError(LfbError):
    code = "remote_error"
    status = 502


class TimeoutError_(LfbError):
    code = "timeout"
    status = 504


def from_payload(payload: Dict[str, Any], status: int = 500) -> LfbError:
    """Rebuild an exception from an API error payload."""

    err = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(err, dict):
        return LfbError("unexpected response from bridge", status=status)
    message = str(err.get("message") or "unknown bridge error")
    code = str(err.get("code") or "remote_error")
    mapping = {
        "bad_request": BadRequest,
        "unauthorized": Unauthorized,
        "forbidden": Forbidden,
        "path_escape": PathEscape,
        "read_only": ReadOnly,
        "not_found": NotFound,
        "conflict": Conflict,
        "too_large": TooLarge,
        "unsupported": Unsupported,
        "config_error": ConfigError,
        "timeout": TimeoutError_,
        "remote_error": RemoteError,
    }
    cls = mapping.get(code, LfbError)
    return cls(message, code=code, status=status, details=err.get("details") or {})
