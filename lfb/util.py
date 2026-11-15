"""Small dependency-free helpers used across the project."""

from __future__ import annotations

import base64
import datetime
import errno
import fnmatch
import hashlib
import hmac
import os
import re
import secrets
import stat
import tempfile
import time
from typing import BinaryIO, Iterator, List, Optional, Sequence, Tuple

from .errors import BadRequest

DEFAULT_CHUNK = 64 * 1024
_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgtp]?i?b?)?\s*$", re.IGNORECASE)


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def iso_time(epoch: Optional[float]) -> Optional[str]:
    if epoch is None:
        return None
    try:
        return (
            datetime.datetime.fromtimestamp(float(epoch), datetime.timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    except (OverflowError, OSError, ValueError):
        return None


def parse_size(value: object, *, name: str = "size") -> Optional[int]:
    """Parse ``1024``, ``512K``, ``10M`` or ``1.5GiB`` into bytes."""

    if value is None:
        return None
    if isinstance(value, bool):
        raise BadRequest("invalid %s: %r" % (name, value))
    if isinstance(value, (int, float)):
        if value < 0:
            raise BadRequest("%s must be >= 0" % name)
        return int(value)
    text = str(value).strip()
    if text.lower() in ("", "none", "null", "off"):
        return None
    match = _SIZE_RE.match(text)
    if not match:
        raise BadRequest("invalid %s: %r (use e.g. 64M)" % (name, value))
    number = float(match.group(1))
    suffix = (match.group(2) or "").lower()
    if suffix.endswith("ib"):
        suffix = suffix[:-2]
    elif suffix.endswith("b"):
        suffix = suffix[:-1]
    factors = {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4, "p": 1024 ** 5}
    if suffix not in factors:
        raise BadRequest("invalid %s suffix: %r" % (name, value))
    return int(number * factors[suffix])


def human_size(size: Optional[int]) -> str:
    if size is None:
        return "-"
    value = float(size)
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return "%d B" % int(value)
            return "%.1f %s" % (value, unit)
        value /= 1024.0
    return "%d B" % size


def format_mode(mode: int) -> str:
    return stat.filemode(mode)


def file_sha256(path: str, chunk_size: int = DEFAULT_CHUNK) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def bytes_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def generate_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def constant_time_eq(left: str, right: str) -> bool:
    try:
        return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
    except (AttributeError, UnicodeError):
        return False


def is_text_bytes(data: bytes) -> bool:
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def b64encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def atomic_write_bytes(path: str, data: bytes, mode: Optional[int] = None) -> None:
    """Write ``data`` to ``path`` atomically, preserving the old mode if any."""

    directory = os.path.dirname(path) or "."
    if mode is None:
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
        except FileNotFoundError:
            mode = 0o644
    fd, tmp = tempfile.mkstemp(prefix=".lfb-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def ensure_dir(path: str, mode: int = 0o755) -> None:
    try:
        os.makedirs(path, mode=mode, exist_ok=True)
    except OSError as exc:
        if exc.errno == errno.EEXIST and os.path.isdir(path):
            return
        raise


def note(text: str) -> None:
    """Print a diagnostic message to stderr without polluting stdout."""

    import sys

    sys.stderr.write(text.rstrip("\n") + "\n")
    sys.stderr.flush()


def split_rel_path(rel: str) -> List[str]:
    """Split a root-relative path into safe components.

    Leading slashes are treated as root-relative for convenience.  ``..`` and
    NUL bytes are rejected outright; symlinks are checked separately after
    resolution.
    """

    if rel is None:
        return []
    rel = str(rel)
    if "\x00" in rel:
        raise BadRequest("path contains a NUL byte")
    if rel.startswith("/"):
        rel = rel.lstrip("/")
    parts: List[str] = []
    for part in rel.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise BadRequest("path must not contain '..'", code="path_escape", status=403)
        parts.append(part)
    return parts


def join_rel(parts: Sequence[str]) -> str:
    return "/".join(parts)


def match_path(pattern: str, relpath: str) -> bool:
    """Match a glob against both the full relative path and the basename."""

    pattern = pattern or "*"
    relpath = relpath.replace(os.sep, "/")
    base = relpath.rsplit("/", 1)[-1]
    return fnmatch.fnmatchcase(relpath, pattern) or fnmatch.fnmatchcase(base, pattern)


def compile_optional_regex(pattern: Optional[str]) -> Optional["re.Pattern[str]"]:
    if not pattern:
        return None
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise BadRequest("invalid regular expression: %s" % exc)


def iter_file_chunks(path: str, chunk_size: int = DEFAULT_CHUNK) -> Iterator[bytes]:
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk_size)
            if not block:
                break
            yield block


def copy_stream(source: BinaryIO, target: BinaryIO, chunk_size: int = DEFAULT_CHUNK) -> int:
    total = 0
    while True:
        block = source.read(chunk_size)
        if not block:
            break
        target.write(block)
        total += len(block)
    return total


def read_exact(stream: BinaryIO, length: int) -> bytes:
    chunks = []
    remaining = length
    while remaining > 0:
        block = stream.read(min(remaining, DEFAULT_CHUNK))
        if not block:
            break
        chunks.append(block)
        remaining -= len(block)
    return b"".join(chunks)


def parse_content_range(value: str, size: int) -> Tuple[int, int]:
    """Parse a single HTTP ``Range`` header into inclusive (start, end)."""

    text = (value or "").strip()
    if not text.lower().startswith("bytes="):
        raise BadRequest("only byte ranges are supported")
    spec = text[6:].strip()
    if "," in spec:
        raise BadRequest("multiple ranges are not supported")
    first, _, last = spec.partition("-")
    try:
        if first == "":
            suffix = int(last)
            if suffix <= 0:
                raise ValueError
            start = max(0, size - suffix)
            end = size - 1
        else:
            start = int(first)
            end = int(last) if last else size - 1
    except ValueError:
        raise BadRequest("invalid Range header")
    if start < 0 or start >= size or end < start:
        raise BadRequest("range not satisfiable", code="range_not_satisfiable", status=416)
    return start, min(end, size - 1)


def truncate_text(text: str, max_chars: int) -> Tuple[str, bool]:
    if max_chars <= 0 or len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def unix_time() -> float:
    return time.time()


class LimitedReader(object):
    """Wrap a stream and never read past ``length`` bytes.

    ``http.server`` exposes the raw request socket; a plain ``read()`` would
    block waiting for the next request after the body.  This wrapper makes the
    end of the body look like EOF.
    """

    def __init__(self, stream: BinaryIO, length: int) -> None:
        self.stream = stream
        self.remaining = max(0, int(length))

    def read(self, size: int = -1) -> bytes:
        if self.remaining <= 0:
            return b""
        if size is None or size < 0:
            size = self.remaining
        size = min(size, self.remaining)
        data = self.stream.read(size)
        if not data:
            self.remaining = 0
            return b""
        self.remaining -= len(data)
        return data

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0
