"""Secure local filesystem operations used by the bridge and the gateway.

All paths are addressed as ``root + relative path``.  A root is an explicitly
configured directory, and every resolved path (including symlink targets) must
stay inside that root.  Operations are deliberately small and boring: no shell
interpolation, no implicit writes outside a root, and atomic file replacement.
"""

from __future__ import annotations

import errno
import fnmatch
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import Environment
from .errors import (
    BadRequest,
    Conflict,
    Forbidden,
    NotFound,
    PathEscape,
    ReadOnly,
    TooLarge,
)
from .util import (
    DEFAULT_CHUNK,
    file_sha256,
    format_mode,
    human_size,
    iso_time,
    join_rel,
    match_path,
    split_rel_path,
)


class Resolution(object):
    __slots__ = ("root_name", "root_real", "rel", "path", "original", "is_symlink", "mode")

    def __init__(
        self,
        root_name: str,
        root_real: str,
        rel: str,
        path: str,
        original: str,
        is_symlink: bool,
        mode: str,
    ) -> None:
        self.root_name = root_name
        self.root_real = root_real
        self.rel = rel
        self.path = path
        self.original = original
        self.is_symlink = is_symlink
        self.mode = mode


def _is_within(root: str, candidate: str) -> bool:
    try:
        root = os.path.abspath(root)
        candidate = os.path.abspath(candidate)
        if root == os.sep:
            return candidate.startswith(os.sep)
        return os.path.commonpath([root, candidate]) == root
    except (ValueError, OSError):
        return False


def _kind(mode: int) -> str:
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
        return "device"
    return "other"


class LocalFS(object):
    """Filesystem view of one local :class:`Environment`."""

    def __init__(self, env: Environment) -> None:
        self.env = env
        self.name = env.name
        self.read_only = env.read_only
        self.limits = env.limits
        self.exec_config = env.exec
        self._roots: Dict[str, Dict[str, Any]] = {}
        for name, root in env.roots.items():
            mode = "ro" if self.read_only else root["mode"]
            self._roots[name] = {
                "path": root["path"],
                "real": os.path.realpath(root["path"]),
                "mode": mode,
                "description": root.get("description", ""),
                "max_read": root.get("max_read") or self.limits["max_read"],
            }

    # ---------------------------------------------------------------- basics
    @property
    def roots(self) -> Dict[str, Dict[str, Any]]:
        return self._roots

    @property
    def default_root(self) -> str:
        return next(iter(self._roots))

    def public_roots(self) -> Dict[str, Dict[str, Any]]:
        result: Dict[str, Dict[str, Any]] = {}
        for name, root in self._roots.items():
            result[name] = {
                "path": root["path"],
                "mode": root["mode"],
                "description": root["description"],
            }
        return result

    def _root(self, name: Optional[str]) -> Dict[str, Any]:
        target = name or self.default_root
        try:
            return self._roots[target]
        except KeyError:
            raise NotFound(
                "unknown root %r (available: %s)" % (target, ", ".join(self._roots.keys()))
            )

    def _write_allowed(self, root: Dict[str, Any], root_name: str) -> None:
        if root["mode"] != "rw":
            raise ReadOnly("root %r is read-only" % root_name)

    def _check_root_exists(self, root: Dict[str, Any], name: str) -> None:
        if not os.path.isdir(root["real"]):
            raise NotFound("root %r does not exist or is not a directory: %s" % (name, root["path"]))

    def resolve(self, root_name: Optional[str], rel: str, *, for_write: bool = False) -> Resolution:
        root = self._root(root_name)
        name = root_name or self.default_root
        parts = split_rel_path(rel)
        rel_norm = join_rel(parts)
        root_real = root["real"]
        self._check_root_exists(root, name)
        candidate = os.path.join(root_real, *parts) if parts else root_real
        if os.path.lexists(candidate):
            real = os.path.realpath(candidate)
            if not _is_within(root_real, real):
                raise PathEscape(
                    "path %r resolves outside root %r" % (rel_norm or ".", name)
                )
            is_link = os.path.islink(candidate)
            return Resolution(name, root_real, rel_norm, real, candidate, is_link, root["mode"])
        if not for_write:
            raise NotFound("path not found: %s" % (rel_norm or "/"))
        # For a new target, resolve the closest existing ancestor and make sure
        # no symlinked ancestor escapes the root.
        parent_rel = parts[:-1]
        parent = os.path.realpath(os.path.join(root_real, *parent_rel)) if parent_rel else root_real
        if not _is_within(root_real, parent):
            raise PathEscape("parent path resolves outside root %r" % name)
        target = os.path.join(parent, parts[-1]) if parts else root_real
        if not _is_within(root_real, os.path.realpath(target)):
            raise PathEscape("target path resolves outside root %r" % name)
        return Resolution(name, root_real, rel_norm, target, candidate, False, root["mode"])

    # ------------------------------------------------------------------ stat
    def stat(self, root_name: Optional[str], rel: str, *, want_hash: bool = False) -> Dict[str, Any]:
        res = self.resolve(root_name, rel)
        try:
            lst = os.lstat(res.original)
        except OSError as exc:
            raise self._os_error(exc, rel)
        st = os.stat(res.path)
        kind = _kind(st.st_mode)
        entry = self._stat_dict(res, st, kind, lst)
        if want_hash:
            if kind != "file":
                raise BadRequest("hash is only available for regular files")
            if st.st_size > 64 * 1024 * 1024:
                raise TooLarge("file is too large to hash (limit 64 MiB)")
            entry["sha256"] = file_sha256(res.path)
        return entry

    def _stat_dict(
        self,
        res: Resolution,
        st: os.stat_result,
        kind: str,
        lst: Optional[os.stat_result] = None,
    ) -> Dict[str, Any]:
        symlink = bool(res.is_symlink or (lst is not None and stat.S_ISLNK(lst.st_mode)))
        entry: Dict[str, Any] = {
            "name": os.path.basename(res.original) if symlink else (os.path.basename(res.path) if res.rel else ""),
            "path": res.rel,
            "type": kind,
            "size": int(st.st_size) if kind == "file" else None,
            "mtime": float(st.st_mtime),
            "mtime_iso": iso_time(st.st_mtime),
            "mode": format_mode(st.st_mode),
            "symlink": symlink,
        }
        if symlink:
            target = os.path.relpath(res.path, res.root_real).replace(os.sep, "/")
            entry["symlink_target"] = "" if target == "." else target
        return entry

    # ------------------------------------------------------------------- list
    def list_dir(
        self,
        root_name: Optional[str],
        rel: str,
        *,
        show_hidden: bool = True,
    ) -> Dict[str, Any]:
        res = self.resolve(root_name, rel)
        if not os.path.isdir(res.path):
            raise BadRequest("not a directory: %s" % (rel or "/"))
        entries: List[Dict[str, Any]] = []
        try:
            with os.scandir(res.path) as iterator:
                for item in iterator:
                    if not show_hidden and item.name.startswith("."):
                        continue
                    entries.append(self._scan_entry(item, res.root_real, res.rel))
        except PermissionError as exc:
            raise Forbidden("permission denied: %s" % exc.filename)
        except OSError as exc:
            raise self._os_error(exc, rel)
        entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower(), e["name"]))
        return {
            "root": res.root_name,
            "path": res.rel,
            "count": len(entries),
            "entries": entries,
        }

    def _scan_entry(self, item: "os.DirEntry[str]", root_real: str, base_rel: str = "") -> Dict[str, Any]:
        entry_rel = join_rel([base_rel, item.name]) if base_rel else item.name
        try:
            lst = item.stat(follow_symlinks=False)
        except OSError:
            return {
                "name": item.name,
                "path": entry_rel,
                "type": "unknown",
                "size": None,
                "mtime": None,
                "mtime_iso": None,
                "mode": "---------",
                "symlink": False,
            }
        if stat.S_ISLNK(lst.st_mode):
            real = os.path.realpath(item.path)
            if not _is_within(root_real, real):
                return {
                    "name": item.name,
                    "path": entry_rel,
                    "type": "symlink",
                    "size": int(lst.st_size),
                    "mtime": float(lst.st_mtime),
                    "mtime_iso": iso_time(lst.st_mtime),
                    "mode": format_mode(lst.st_mode),
                    "symlink": True,
                    "blocked": True,
                }
            try:
                st = os.stat(real)
            except OSError:
                return {
                    "name": item.name,
                    "path": entry_rel,
                    "type": "symlink",
                    "size": int(lst.st_size),
                    "mtime": float(lst.st_mtime),
                    "mtime_iso": iso_time(lst.st_mtime),
                    "mode": format_mode(lst.st_mode),
                    "symlink": True,
                    "broken": True,
                }
            kind = _kind(st.st_mode)
            return {
                "name": item.name,
                "path": entry_rel,
                "type": kind,
                "size": int(st.st_size) if kind == "file" else None,
                "mtime": float(st.st_mtime),
                "mtime_iso": iso_time(st.st_mtime),
                "mode": format_mode(st.st_mode),
                "symlink": True,
                "symlink_target": os.path.relpath(real, root_real).replace(os.sep, "/"),
            }
        kind = _kind(lst.st_mode)
        return {
            "name": item.name,
            "path": entry_rel,
            "type": kind,
            "size": int(lst.st_size) if kind == "file" else None,
            "mtime": float(lst.st_mtime),
            "mtime_iso": iso_time(lst.st_mtime),
            "mode": format_mode(lst.st_mode),
            "symlink": False,
        }

    # ------------------------------------------------------------------- read
    def open_read(self, root_name: Optional[str], rel: str) -> Tuple[Any, Dict[str, Any]]:
        res = self.resolve(root_name, rel)
        if os.path.isdir(res.path):
            raise BadRequest("cannot read a directory: %s" % (rel or "/"))
        if not os.path.isfile(res.path):
            raise BadRequest("not a regular file: %s" % (rel or "/"))
        try:
            handle = open(res.path, "rb")
        except OSError as exc:
            raise self._os_error(exc, rel)
        st = os.fstat(handle.fileno())
        entry = self._stat_dict(res, st, "file")
        return handle, entry

    def read_bytes(
        self,
        root_name: Optional[str],
        rel: str,
        *,
        offset: int = 0,
        max_bytes: Optional[int] = None,
    ) -> Tuple[bytes, Dict[str, Any], bool]:
        if offset < 0:
            raise BadRequest("offset must be >= 0")
        res = self.resolve(root_name, rel)
        if not os.path.isfile(res.path):
            raise BadRequest("not a regular file: %s" % (rel or "/"))
        root = self._root(root_name)
        limit = max_bytes if max_bytes is not None else root["max_read"]
        if limit is not None and limit < 0:
            raise BadRequest("max_bytes must be >= 0")
        st = os.stat(res.path)
        if offset > st.st_size:
            raise BadRequest("offset %d is beyond end of file (%d bytes)" % (offset, st.st_size))
        with open(res.path, "rb") as handle:
            if offset:
                handle.seek(offset)
            data = handle.read(limit) if limit is not None else handle.read()
        entry = self._stat_dict(res, st, "file")
        truncated = offset + len(data) < st.st_size
        return data, entry, truncated

    # ------------------------------------------------------------------ write
    def write_bytes(
        self,
        root_name: Optional[str],
        rel: str,
        data: bytes,
        *,
        overwrite: bool = True,
        create_dirs: bool = False,
        mtime: Optional[float] = None,
    ) -> Dict[str, Any]:
        import io

        return self.write_stream(
            root_name,
            rel,
            io.BytesIO(data),
            length=len(data),
            overwrite=overwrite,
            create_dirs=create_dirs,
            mtime=mtime,
        )

    def write_stream(
        self,
        root_name: Optional[str],
        rel: str,
        stream: Any,
        *,
        length: Optional[int] = None,
        overwrite: bool = True,
        create_dirs: bool = False,
        mtime: Optional[float] = None,
    ) -> Dict[str, Any]:
        res = self.resolve(root_name, rel, for_write=True)
        root = self._root(root_name)
        self._write_allowed(root, res.root_name)
        if not res.rel:
            raise BadRequest("cannot write to a root directory")
        parent = os.path.dirname(res.path)
        if not os.path.isdir(parent):
            if not create_dirs:
                raise NotFound("parent directory does not exist: %s" % os.path.dirname(res.rel))
            self._mkdirs_within(res.root_real, parent)
        if os.path.isdir(res.path):
            raise Conflict("destination is a directory: %s" % res.rel)
        if os.path.lexists(res.original) and not overwrite:
            raise Conflict("destination already exists: %s" % res.rel)
        max_upload = self.limits["max_upload"]
        if length is not None and length > max_upload:
            raise TooLarge("upload is larger than the configured limit (%s)" % human_size(max_upload))
        try:
            mode = stat.S_IMODE(os.stat(res.path).st_mode)
        except OSError:
            mode = 0o644
        fd, tmp = tempfile.mkstemp(prefix=".lfb-upload-", suffix=".tmp", dir=parent)
        written = 0
        try:
            with os.fdopen(fd, "wb") as handle:
                while True:
                    block = stream.read(DEFAULT_CHUNK)
                    if not block:
                        break
                    written += len(block)
                    if written > max_upload:
                        raise TooLarge(
                            "upload is larger than the configured limit (%s)"
                            % human_size(max_upload)
                        )
                    handle.write(block)
                handle.flush()
                os.fsync(handle.fileno())
            if length is not None and written != length:
                raise BadRequest(
                    "upload length mismatch: expected %d bytes, received %d" % (length, written)
                )
            os.chmod(tmp, mode)
            os.replace(tmp, res.path)
            if mtime is not None:
                try:
                    os.utime(res.path, (float(mtime), float(mtime)))
                except OSError:
                    pass
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        st = os.stat(res.path)
        return self._stat_dict(res, st, "file")

    def _mkdirs_within(self, root_real: str, target: str) -> None:
        current = target
        chain: List[str] = []
        while not os.path.exists(current):
            chain.append(current)
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
        if not _is_within(root_real, os.path.realpath(current)):
            raise PathEscape("parent path resolves outside root")
        for directory in reversed(chain):
            try:
                os.mkdir(directory, 0o755)
            except FileExistsError:
                if not os.path.isdir(directory):
                    raise Conflict("not a directory: %s" % directory)

    def mkdir(
        self,
        root_name: Optional[str],
        rel: str,
        *,
        parents: bool = False,
        exist_ok: bool = True,
    ) -> Dict[str, Any]:
        res = self.resolve(root_name, rel, for_write=True)
        root = self._root(root_name)
        self._write_allowed(root, res.root_name)
        if not res.rel:
            return self.stat(root_name, "")
        if os.path.exists(res.path):
            if os.path.isdir(res.path):
                if exist_ok:
                    return self.stat(root_name, res.rel)
                raise Conflict("directory already exists: %s" % res.rel)
            raise Conflict("path already exists and is not a directory: %s" % res.rel)
        parent = os.path.dirname(res.path)
        if not os.path.isdir(parent):
            if not parents:
                raise NotFound("parent directory does not exist: %s" % os.path.dirname(res.rel))
            self._mkdirs_within(res.root_real, parent)
        try:
            os.mkdir(res.path, 0o755)
        except OSError as exc:
            raise self._os_error(exc, rel)
        return self.stat(root_name, res.rel)

    # ------------------------------------------------------------------ move
    def move(
        self,
        root_name: Optional[str],
        src_rel: str,
        dst_root_name: Optional[str],
        dst_rel: str,
        *,
        overwrite: bool = False,
    ) -> Dict[str, Any]:
        if dst_root_name and dst_root_name != (root_name or self.default_root):
            raise BadRequest("move between different roots is not supported; use 'lfb copy'")
        src = self.resolve(root_name, src_rel)
        if not src.rel:
            raise BadRequest("cannot move a root directory")
        dst = self.resolve(root_name, dst_rel, for_write=True)
        root = self._root(src.root_name)
        self._write_allowed(root, src.root_name)
        if not dst.rel:
            raise BadRequest("cannot move onto a root directory")
        if src.path == dst.path:
            return self.stat(root_name, src.rel)
        if os.path.lexists(dst.original):
            if not overwrite:
                raise Conflict("destination already exists: %s" % dst.rel)
            if dst.is_symlink:
                raise Conflict("refusing to overwrite a symlink: %s" % dst.rel)
            if os.path.isdir(dst.path):
                raise Conflict("refusing to overwrite an existing directory: %s" % dst.rel)
        if os.path.isdir(src.path) and _is_within(src.path, dst.path):
            raise BadRequest("cannot move a directory into itself")
        parent = os.path.dirname(dst.path)
        if not os.path.isdir(parent):
            raise NotFound("destination parent does not exist: %s" % os.path.dirname(dst.rel))
        if os.path.lexists(dst.original):
            os.unlink(dst.original)
        try:
            os.rename(src.path, dst.path)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise self._os_error(exc, src.rel)
            shutil.move(src.path, dst.path)
        return self.stat(root_name, dst.rel)

    # ---------------------------------------------------------------- remove
    def remove(self, root_name: Optional[str], rel: str, *, recursive: bool = False) -> Dict[str, Any]:
        res = self.resolve(root_name, rel)
        root = self._root(res.root_name)
        self._write_allowed(root, res.root_name)
        if not res.rel:
            raise BadRequest("refusing to remove a root directory")
        if os.path.islink(res.original):
            os.unlink(res.original)
            return {"removed": res.rel, "recursive": recursive}
        if os.path.isdir(res.path):
            if recursive:
                shutil.rmtree(res.path)
            else:
                try:
                    os.rmdir(res.path)
                except OSError as exc:
                    if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
                        raise Conflict("directory is not empty: %s (use recursive)" % res.rel)
                    raise self._os_error(exc, rel)
        else:
            try:
                os.unlink(res.path)
            except OSError as exc:
                raise self._os_error(exc, rel)
        return {"removed": res.rel, "recursive": recursive}

    # ---------------------------------------------------------------- search
    def search(
        self,
        root_name: Optional[str],
        rel: str,
        *,
        glob: Optional[str] = None,
        regex: Optional[str] = None,
        contains: Optional[str] = None,
        want_type: str = "all",
        max_results: int = 200,
        exclude: Optional[Sequence[str]] = None,
        show_hidden: bool = True,
    ) -> Dict[str, Any]:
        base = self.resolve(root_name, rel)
        if not os.path.isdir(base.path):
            raise BadRequest("search base is not a directory: %s" % (rel or "/"))
        if want_type not in ("all", "file", "dir", "symlink"):
            raise BadRequest("type must be one of: all, file, dir, symlink")
        if max_results <= 0:
            raise BadRequest("max_results must be > 0")
        max_results = min(max_results, int(self.limits["max_search_results"]))
        if regex:
            try:
                pattern = re.compile(regex)
            except re.error as exc:
                raise BadRequest("invalid regular expression: %s" % exc)
        else:
            pattern = None
        excludes = [e for e in (exclude or []) if e]
        results: List[Dict[str, Any]] = []
        truncated = False
        scanned = 0

        for current, dirnames, filenames in os.walk(base.path, followlinks=False):
            symlink_dirs = [d for d in dirnames if os.path.islink(os.path.join(current, d))]
            keep_dirs = [
                d
                for d in dirnames
                if d not in symlink_dirs
                and not any(fnmatch.fnmatchcase(d, ex) for ex in excludes)
                and (show_hidden or not d.startswith("."))
            ]
            dirnames[:] = sorted(keep_dirs)

            candidates: List[Tuple[str, bool]] = []
            if want_type == "dir":
                candidates.extend((name, True) for name in dirnames)
            else:
                candidates.extend((name, True) for name in symlink_dirs)
                candidates.extend((name, False) for name in sorted(filenames))
            for name, is_dir_candidate in candidates:
                if not show_hidden and name.startswith("."):
                    continue
                full = os.path.join(current, name)
                rel_to_base = os.path.relpath(full, base.path).replace(os.sep, "/")
                if any(fnmatch.fnmatchcase(rel_to_base, ex) for ex in excludes):
                    continue
                scanned += 1
                if scanned > 200000:
                    truncated = True
                    break
                is_link = os.path.islink(full)
                real = os.path.realpath(full)
                if is_link:
                    kind = "symlink"
                elif is_dir_candidate or os.path.isdir(real):
                    kind = "dir"
                else:
                    kind = "file"
                if want_type != "all" and kind != want_type:
                    continue
                if is_link and not _is_within(base.root_real, real):
                    # Never expose a symlink that escapes the configured root.
                    continue
                if not self._matches(rel_to_base, name, glob, pattern):
                    continue
                match_line = None
                if contains is not None:
                    if kind != "file":
                        continue
                    found = self._file_contains(real, contains)
                    if found is None:
                        continue
                    match_line = found
                entry = self._search_entry(full, rel_to_base, kind, is_link, real)
                entry["root"] = base.root_name
                if match_line is not None:
                    entry["line"] = match_line
                results.append(entry)
                if len(results) >= max_results:
                    truncated = True
                    break
            if truncated:
                break

        return {
            "root": base.root_name,
            "path": base.rel,
            "results": results,
            "count": len(results),
            "truncated": truncated,
            "scanned": scanned,
        }

    def _matches(self, relpath: str, name: str, glob: Optional[str], pattern: Any) -> bool:
        if glob and not match_path(glob, relpath):
            return False
        if pattern is not None and not pattern.search(relpath):
            return False
        return True

    def _file_contains(self, path: str, needle: str) -> Optional[int]:
        try:
            if os.path.getsize(path) > 2 * 1024 * 1024:
                return None
            with open(path, "rb") as handle:
                data = handle.read()
            if b"\x00" in data:
                return None
            text = data.decode("utf-8", "replace")
        except OSError:
            return None
        index = text.find(needle)
        if index < 0:
            return None
        return text.count("\n", 0, index) + 1

    def _search_entry(
        self, full: str, relpath: str, kind: str, is_link: bool, real: str
    ) -> Dict[str, Any]:
        try:
            st = os.stat(real)
        except OSError:
            st = None
        entry: Dict[str, Any] = {
            "path": relpath,
            "name": os.path.basename(relpath),
            "type": kind,
            "symlink": is_link,
            "size": int(st.st_size) if (st and kind == "file") else None,
            "mtime": float(st.st_mtime) if st else None,
        }
        if is_link:
            entry["symlink_target"] = os.path.relpath(real, os.path.dirname(full))
        return entry

    # ------------------------------------------------------------------- tree
    def tree(
        self,
        root_name: Optional[str],
        rel: str,
        *,
        depth: int = 2,
        show_hidden: bool = True,
        max_nodes: int = 1000,
    ) -> Dict[str, Any]:
        if depth < 0 or depth > 20:
            raise BadRequest("depth must be between 0 and 20")
        base = self.resolve(root_name, rel)
        if not os.path.isdir(base.path):
            raise BadRequest("not a directory: %s" % (rel or "/"))
        budget = [max_nodes]

        def walk(path: str, current_depth: int, root_real: str) -> List[Dict[str, Any]]:
            if current_depth <= 0 or budget[0] <= 0:
                return []
            try:
                names = sorted(os.listdir(path))
            except OSError:
                return []
            items: List[Dict[str, Any]] = []
            for name in names:
                if not show_hidden and name.startswith("."):
                    continue
                full = os.path.join(path, name)
                if os.path.islink(full):
                    real = os.path.realpath(full)
                    if not _is_within(root_real, real):
                        items.append({"name": name, "type": "symlink", "blocked": True})
                        budget[0] -= 1
                        continue
                    if os.path.isdir(real):
                        kind = "dir"
                        children = walk(real, current_depth - 1, root_real)
                    else:
                        kind = "file"
                        children = None
                elif os.path.isdir(full):
                    kind = "dir"
                    children = walk(full, current_depth - 1, root_real)
                else:
                    kind = "file"
                    children = None
                if budget[0] <= 0:
                    break
                budget[0] -= 1
                item: Dict[str, Any] = {"name": name, "type": kind}
                if kind == "file":
                    try:
                        item["size"] = os.path.getsize(full)
                    except OSError:
                        pass
                if children:
                    item["children"] = children
                items.append(item)
            items.sort(key=lambda e: (e.get("type") != "dir", e.get("name", "").lower()))
            return items

        return {
            "root": base.root_name,
            "path": base.rel,
            "depth": depth,
            "entries": walk(base.path, depth, base.root_real),
            "truncated": budget[0] <= 0,
        }

    # ------------------------------------------------------------------- exec
    def exec_command(
        self,
        root_name: Optional[str],
        rel: str,
        *,
        argv: Optional[Sequence[str]] = None,
        command: Optional[str] = None,
        shell: bool = False,
        timeout: Optional[float] = None,
        max_output: Optional[int] = None,
    ) -> Dict[str, Any]:
        if not self.exec_config["enabled"]:
            raise Forbidden(
                "command execution is disabled for environment %r "
                "(set exec.enabled=true in the config)" % self.name
            )
        if shell and not self.exec_config["allow_shell"]:
            raise Forbidden("shell execution is disabled (set exec.allow_shell=true)")
        if not shell and not argv:
            raise BadRequest("argv must be a non-empty list, or use shell mode")
        if shell and not command:
            raise BadRequest("shell mode requires a command string")
        res = self.resolve(root_name, rel)
        if not os.path.isdir(res.path):
            raise BadRequest("working directory is not a directory: %s" % (rel or "/"))
        root = self._root(res.root_name)
        self._write_allowed(root, res.root_name)
        timeout_value = float(timeout if timeout is not None else self.exec_config["timeout"])
        if timeout_value <= 0 or timeout_value > 3600:
            raise BadRequest("timeout must be between 0 and 3600 seconds")
        output_limit = int(max_output or self.exec_config["max_output"])
        if output_limit <= 0:
            raise BadRequest("max_output must be > 0")

        env = {
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "HOME": res.root_real,
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", ""),
            "TERM": "dumb",
        }
        for key, value in self.exec_config.get("env", {}).items():
            env[str(key)] = str(value)
        env = {k: v for k, v in env.items() if v != ""}

        started = time.time()
        with tempfile.TemporaryDirectory(prefix="lfb-exec-") as tmpdir:
            out_path = os.path.join(tmpdir, "stdout")
            err_path = os.path.join(tmpdir, "stderr")
            with open(out_path, "wb") as out, open(err_path, "wb") as err:
                try:
                    if shell:
                        proc = subprocess.Popen(
                            ["/bin/sh", "-c", str(command)],
                            cwd=res.path,
                            env=env,
                            stdin=subprocess.DEVNULL,
                            stdout=out,
                            stderr=err,
                            start_new_session=True,
                        )
                    else:
                        proc = subprocess.Popen(
                            [str(x) for x in (argv or [])],
                            cwd=res.path,
                            env=env,
                            stdin=subprocess.DEVNULL,
                            stdout=out,
                            stderr=err,
                            start_new_session=True,
                        )
                except FileNotFoundError as exc:
                    raise BadRequest("command not found: %s" % exc.filename)
                except OSError as exc:
                    raise BadRequest("cannot start command: %s" % exc)
                timed_out = False
                try:
                    proc.wait(timeout=timeout_value)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    try:
                        os.killpg(proc.pid, 9)
                    except OSError:
                        proc.kill()
                    proc.wait()
            duration = time.time() - started
            stdout_bytes, out_truncated = self._read_limited(out_path, output_limit)
            stderr_bytes, err_truncated = self._read_limited(err_path, output_limit)
        return {
            "exit_code": proc.returncode,
            "timed_out": timed_out,
            "duration": round(duration, 3),
            "stdout": stdout_bytes.decode("utf-8", "replace"),
            "stderr": stderr_bytes.decode("utf-8", "replace"),
            "truncated": bool(out_truncated or err_truncated),
            "cwd": res.rel,
        }

    def _read_limited(self, path: str, limit: int) -> Tuple[bytes, bool]:
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as handle:
                data = handle.read(limit)
            return data, size > limit
        except OSError:
            return b"", False

    # ---------------------------------------------------------------- helpers
    def _os_error(self, exc: OSError, rel: str) -> Exception:
        if isinstance(exc, FileNotFoundError):
            return NotFound("path not found: %s" % (rel or "/"))
        if isinstance(exc, PermissionError):
            return Forbidden("permission denied: %s" % (rel or "/"))
        if isinstance(exc, FileExistsError):
            return Conflict("path already exists: %s" % (rel or "/"))
        if isinstance(exc, IsADirectoryError):
            return BadRequest("not a regular file: %s" % (rel or "/"))
        return BadRequest(str(exc))
