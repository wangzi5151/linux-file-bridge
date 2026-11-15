"""File transfer and mirroring between environments."""

from __future__ import annotations

import fnmatch
import os
import tempfile
from typing import Any, Callable, Dict, Iterable, List, Optional, Set

from .errors import BadRequest, Conflict, NotFound, TooLarge
from .targets import Target

Progress = Optional[Callable[[str], None]]
MAX_WALK_ENTRIES = 200000


def join_remote(base: str, rel: str) -> str:
    base = base or ""
    rel = rel or ""
    if not base:
        return rel
    if not rel:
        return base
    return base.rstrip("/") + "/" + rel.lstrip("/")


def join_local(base: str, rel: str) -> str:
    rel = (rel or "").replace("/", os.sep)
    if not rel:
        return base
    result = os.path.abspath(os.path.join(base, rel))
    Base = os.path.abspath(base)
    try:
        if os.path.commonpath([Base, result]) != Base:
            raise BadRequest("refusing to write outside destination: %s" % result)
    except ValueError:
        raise BadRequest("refusing to write outside destination: %s" % result)
    return result


def _stat_optional(backend: Any, root: str, path: str) -> Optional[Dict[str, Any]]:
    try:
        return backend.stat(root, path)
    except NotFound:
        return None


def _stat(backend: Any, root: str, path: str) -> Dict[str, Any]:
    return backend.stat(root, path)


def _echo(progress: Progress, message: str) -> None:
    if progress:
        progress(message)


def _excluded(rel: str, excludes: Iterable[str]) -> bool:
    base = rel.rsplit("/", 1)[-1]
    for pattern in excludes:
        if fnmatch.fnmatchcase(rel, pattern) or fnmatch.fnmatchcase(base, pattern):
            return True
    return False


def walk_tree(
    backend: Any,
    root: str,
    path: str,
    *,
    excludes: Optional[Iterable[str]] = None,
    show_hidden: bool = True,
) -> List[Dict[str, Any]]:
    """Return a flat manifest for a directory, skipping symlinks."""

    excludes = list(excludes or [])
    results: List[Dict[str, Any]] = []
    stack: List[str] = [path]
    visited = 0
    while stack:
        current = stack.pop()
        listing = backend.list_dir(root, current, show_hidden=show_hidden)
        for entry in listing.get("entries") or []:
            visited += 1
            if visited > MAX_WALK_ENTRIES:
                raise TooLarge("directory has too many entries to sync (limit %d)" % MAX_WALK_ENTRIES)
            rel = join_remote(current, entry["name"])
            relative = rel[len(path) + 1 :] if path and rel.startswith(path + "/") else rel
            if _excluded(relative, excludes):
                continue
            item = {
                "path": rel,
                "rel": relative,
                "name": entry["name"],
                "type": entry.get("type"),
                "symlink": bool(entry.get("symlink")),
                "size": entry.get("size"),
                "mtime": entry.get("mtime"),
            }
            if item["symlink"]:
                item["type"] = "symlink"
            results.append(item)
            if item["type"] == "dir" and not item["symlink"]:
                stack.append(rel)
    return results


def _same_file(src: Target, src_stat: Dict[str, Any], dst: Target, dst_stat: Optional[Dict[str, Any]], rel: str) -> bool:
    if dst_stat is None:
        return False
    if dst_stat.get("type") != "file":
        return False
    if src_stat.get("size") != dst_stat.get("size"):
        return False
    src_mtime = src_stat.get("mtime")
    dst_mtime = dst_stat.get("mtime")
    if src_mtime is None or dst_mtime is None:
        return False
    return abs(float(src_mtime) - float(dst_mtime)) <= 2.0


def _checksum_equal(src: Any, src_root: str, src_path: str, dst: Any, dst_root: str, dst_path: str) -> bool:
    try:
        left = src.stat(src_root, src_path, want_hash=True)
        right = dst.stat(dst_root, dst_path, want_hash=True)
        return bool(left.get("sha256")) and left.get("sha256") == right.get("sha256")
    except TooLarge:
        return False


def copy_file(
    src: Target,
    src_rel: str,
    dst_backend: Any,
    dst_root: str,
    dst_rel: str,
    *,
    overwrite: bool = True,
    checksum: bool = False,
    progress: Progress = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    if (
        src.env == getattr(dst_backend, "name", None)
        and src.root == dst_root
        and src.path == dst_rel
    ):
        return {"action": "skip", "path": src_rel, "reason": "same path"}
    source_backend = src.backend
    src_stat = source_backend.stat(src.root, src_rel)
    if src_stat.get("type") != "file":
        raise BadRequest("source is not a regular file: %s" % src_rel)
    dst_stat = _stat_optional(dst_backend, dst_root, dst_rel)
    if dst_stat is not None and dst_stat.get("type") == "dir":
        raise Conflict("destination is a directory: %s" % dst_rel)
    if dst_stat is not None and not overwrite:
        raise Conflict("destination already exists: %s" % dst_rel)
    if dst_stat is not None and checksum and _checksum_equal(
        source_backend, src.root, src_rel, dst_backend, dst_root, dst_rel
    ):
        return {"action": "skip", "path": src_rel, "reason": "checksum"}
    if dry_run:
        return {"action": "copy", "path": src_rel, "size": src_stat.get("size")}
    handle, _entry = source_backend.open_read(src.root, src_rel)
    try:
        length = src_stat.get("size")
        dst_backend.write_stream(
            dst_root,
            dst_rel,
            handle,
            length=int(length) if length is not None else None,
            overwrite=True,
            create_dirs=True,
            mtime=src_stat.get("mtime"),
        )
    finally:
        try:
            handle.close()
        except Exception:
            pass
    _echo(progress, "copied %s" % src_rel)
    return {"action": "copy", "path": src_rel, "size": src_stat.get("size")}


def _ensure_remote_dir(backend: Any, root: str, path: str, *, dry_run: bool) -> bool:
    if not path:
        return False
    existing = _stat_optional(backend, root, path)
    if existing is not None:
        if existing.get("type") != "dir":
            raise Conflict("destination path is not a directory: %s" % path)
        return False
    if not dry_run:
        backend.mkdir(root, path, parents=True, exist_ok=True)
    return True


def sync(
    src: Target,
    dst: Target,
    *,
    delete: bool = False,
    checksum: bool = False,
    overwrite: bool = True,
    excludes: Optional[Iterable[str]] = None,
    progress: Progress = None,
    dry_run: bool = False,
    allow_root_delete: bool = False,
) -> Dict[str, Any]:
    """Copy ``src`` to ``dst``.

    A file is copied to the destination path; a directory's *contents* are
    mirrored into the destination directory.
    """

    excludes = list(excludes or [])
    source_stat = src.backend.stat(src.root, src.path)
    if source_stat.get("type") == "dir":
        if (
            src.env == dst.env
            and src.root == dst.root
            and dst.path
            and (dst.path == src.path or dst.path.startswith(src.path.rstrip("/") + "/"))
        ):
            raise BadRequest("destination directory is inside the source directory")
        dst_stat = _stat_optional(dst.backend, dst.root, dst.path)
        if dst_stat is not None and dst_stat.get("type") != "dir":
            raise Conflict("destination is not a directory: %s" % dst.path)
        if delete and not dst.path and not allow_root_delete:
            raise BadRequest(
                "refusing --delete when the destination is a root directory; "
                "pass --allow-root-delete if this is really intended"
            )
        created = _ensure_remote_dir(dst.backend, dst.root, dst.path, dry_run=dry_run)
        manifest = walk_tree(
            src.backend, src.root, src.path, excludes=excludes
        )
        stats: Dict[str, Any] = {
            "source": src.label(),
            "destination": dst.label(),
            "directories": 0,
            "files_copied": 0,
            "files_skipped": 0,
            "deleted": 0,
            "bytes": 0,
            "dry_run": dry_run,
            "created_destination": created,
        }
        if created:
            stats["directories"] += 1
        manifest_dirs: Set[str] = set()
        manifest_files: Set[str] = set()
        for entry in sorted(manifest, key=lambda item: (item["type"] != "dir", item["path"])):
            relative = entry["rel"]
            if entry["type"] == "dir":
                destination_path = join_remote(dst.path, relative)
                manifest_dirs.add(relative)
                if _ensure_remote_dir(dst.backend, dst.root, destination_path, dry_run=dry_run):
                    stats["directories"] += 1
                    _echo(progress, "mkdir %s" % relative)
                continue
            if entry["type"] == "symlink":
                _echo(progress, "skip symlink %s" % relative)
                stats["files_skipped"] += 1
                continue
            if entry["type"] != "file":
                _echo(progress, "skip special file %s" % relative)
                stats["files_skipped"] += 1
                continue
            manifest_files.add(relative)
            destination_path = join_remote(dst.path, relative)
            dst_stat = _stat_optional(dst.backend, dst.root, destination_path)
            if dst_stat is not None and _same_file(src, entry, dst, dst_stat, relative) and not checksum:
                stats["files_skipped"] += 1
                continue
            if checksum and dst_stat is not None and _checksum_equal(
                src.backend, src.root, entry["path"], dst.backend, dst.root, destination_path
            ):
                stats["files_skipped"] += 1
                continue
            if not dry_run:
                copy_file(
                    src,
                    entry["path"],
                    dst.backend,
                    dst.root,
                    destination_path,
                    overwrite=True,
                    checksum=False,
                    progress=None,
                    dry_run=False,
                )
            stats["files_copied"] += 1
            stats["bytes"] += int(entry.get("size") or 0)
            _echo(progress, "copied %s" % relative)
        if delete:
            deleted = _delete_extras(
                dst,
                manifest_dirs,
                manifest_files,
                excludes=excludes,
                progress=progress,
                dry_run=dry_run,
            )
            stats["deleted"] = deleted
        return stats

    # Single file
    destination_path = dst.path
    dst_stat = _stat_optional(dst.backend, dst.root, dst.path)
    if dst_stat is not None and dst_stat.get("type") == "dir":
        destination_path = join_remote(dst.path, os.path.basename(src.path.rstrip("/")))
        dst_stat = _stat_optional(dst.backend, dst.root, destination_path)
    if dst_stat is not None and not overwrite:
        raise Conflict("destination already exists: %s" % destination_path)
    result = copy_file(
        src,
        src.path,
        dst.backend,
        dst.root,
        destination_path,
        overwrite=overwrite,
        checksum=checksum,
        progress=progress,
        dry_run=dry_run,
    )
    return {
        "source": src.label(),
        "destination": dst.label(),
        "files_copied": 1 if result["action"] == "copy" else 0,
        "files_skipped": 1 if result["action"] == "skip" else 0,
        "bytes": int(result.get("size") or 0),
        "dry_run": dry_run,
    }


def _delete_extras(
    dst: Target,
    manifest_dirs: Set[str],
    manifest_files: Set[str],
    *,
    excludes: Iterable[str],
    progress: Progress,
    dry_run: bool,
) -> int:
    manifest = walk_tree(dst.backend, dst.root, dst.path, excludes=excludes)
    extras: List[Dict[str, Any]] = []
    for entry in manifest:
        if entry["type"] == "dir":
            if entry["rel"] not in manifest_dirs:
                extras.append(entry)
        elif entry["rel"] not in manifest_files:
            extras.append(entry)
    # Remove deepest entries first so recursive directory deletion is safe.
    extras.sort(key=lambda item: item["rel"].count("/"), reverse=True)
    removed = 0
    for entry in extras:
        full = join_remote(dst.path, entry["rel"])
        if dry_run:
            _echo(progress, "would delete %s" % entry["rel"])
            removed += 1
            continue
        if entry["type"] == "dir":
            dst.backend.remove(dst.root, full, recursive=True)
        else:
            dst.backend.remove(dst.root, full, recursive=False)
        removed += 1
        _echo(progress, "deleted %s" % entry["rel"])
    return removed


# ---------------------------------------------------------------- local <-> remote
def _local_atomic_write(handle: Any, destination: str, mtime: Optional[float]) -> int:
    directory = os.path.dirname(destination) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".lfb-download-", suffix=".tmp", dir=directory)
    written = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                block = handle.read(64 * 1024)
                if not block:
                    break
                out.write(block)
                written += len(block)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, destination)
        if mtime:
            os.utime(destination, (float(mtime), float(mtime)))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return written


def _safe_local_target(base: str, rel: str) -> str:
    return join_local(base, rel)


def download(
    src: Target,
    local_path: str,
    *,
    recursive: bool = False,
    overwrite: bool = True,
    progress: Progress = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    local_path = os.path.abspath(os.path.expanduser(local_path))
    source_stat = src.backend.stat(src.root, src.path)
    if source_stat.get("type") == "file":
        destination = local_path
        if os.path.isdir(destination):
            destination = os.path.join(destination, os.path.basename(src.path.rstrip("/")))
        if os.path.exists(destination) and not overwrite:
            raise Conflict("local destination already exists: %s" % destination)
        if dry_run:
            return {"files_copied": 1, "bytes": int(source_stat.get("size") or 0), "dry_run": True}
        handle, _ = src.backend.open_read(src.root, src.path)
        try:
            written = _local_atomic_write(handle, destination, source_stat.get("mtime"))
        finally:
            handle.close()
        _echo(progress, "downloaded %s" % destination)
        return {"files_copied": 1, "bytes": written, "destination": destination, "dry_run": False}
    if source_stat.get("type") != "dir":
        raise BadRequest("source is not a file or directory")
    if not recursive:
        raise BadRequest("source is a directory; pass --recursive to download it")
    if os.path.exists(local_path) and not os.path.isdir(local_path):
        raise Conflict("local destination is not a directory: %s" % local_path)
    if not dry_run:
        os.makedirs(local_path, exist_ok=True)
    manifest = walk_tree(src.backend, src.root, src.path)
    stats: Dict[str, Any] = {"files_copied": 0, "bytes": 0, "directories": 0, "dry_run": dry_run}
    for entry in sorted(manifest, key=lambda item: (item["type"] != "dir", item["path"])):
        destination = _safe_local_target(local_path, entry["rel"])
        if entry["type"] == "dir":
            if not dry_run:
                os.makedirs(destination, exist_ok=True)
            stats["directories"] += 1
            continue
        if entry["type"] == "symlink":
            _echo(progress, "skip symlink %s" % entry["rel"])
            continue
        if entry["type"] != "file":
            continue
        if os.path.exists(destination) and not overwrite:
            raise Conflict("local destination already exists: %s" % destination)
        if dry_run:
            stats["files_copied"] += 1
            stats["bytes"] += int(entry.get("size") or 0)
            continue
        handle, _ = src.backend.open_read(src.root, entry["path"])
        try:
            written = _local_atomic_write(handle, destination, entry.get("mtime"))
        finally:
            handle.close()
        stats["files_copied"] += 1
        stats["bytes"] += written
        _echo(progress, "downloaded %s" % entry["rel"])
    return stats


def upload(
    local_path: str,
    dst: Target,
    *,
    recursive: bool = False,
    overwrite: bool = True,
    progress: Progress = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    local_path = os.path.abspath(os.path.expanduser(local_path))
    if not os.path.exists(local_path):
        raise NotFound("local path does not exist: %s" % local_path)
    if os.path.isfile(local_path):
        if os.path.islink(local_path):
            raise BadRequest("refusing to upload a symlink: %s" % local_path)
        destination = dst.path
        dst_stat = _stat_optional(dst.backend, dst.root, dst.path)
        if dst_stat is not None and dst_stat.get("type") == "dir":
            destination = join_remote(dst.path, os.path.basename(local_path))
            dst_stat = _stat_optional(dst.backend, dst.root, destination)
        if dst_stat is not None and not overwrite:
            raise Conflict("remote destination already exists: %s" % destination)
        size = os.path.getsize(local_path)
        if dry_run:
            return {"files_copied": 1, "bytes": size, "dry_run": True}
        with open(local_path, "rb") as handle:
            dst.backend.write_stream(
                dst.root,
                destination,
                handle,
                length=size,
                overwrite=True,
                create_dirs=True,
                mtime=os.path.getmtime(local_path),
            )
        _echo(progress, "uploaded %s" % destination)
        return {"files_copied": 1, "bytes": size, "destination": destination, "dry_run": False}
    if not os.path.isdir(local_path):
        raise BadRequest("local source is not a regular file or directory")
    if not recursive:
        raise BadRequest("local source is a directory; pass --recursive to upload it")
    dst_stat = _stat_optional(dst.backend, dst.root, dst.path)
    if dst_stat is not None and dst_stat.get("type") != "dir":
        raise Conflict("remote destination is not a directory: %s" % dst.path)
    if not dry_run and dst.path:
        dst.backend.mkdir(dst.root, dst.path, parents=True, exist_ok=True)
    stats: Dict[str, Any] = {"files_copied": 0, "bytes": 0, "directories": 0, "dry_run": dry_run}
    for current, dirnames, filenames in os.walk(local_path, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not os.path.islink(os.path.join(current, d)))
        rel_dir = os.path.relpath(current, local_path)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
        for name in sorted(dirnames):
            rel = join_remote(rel_dir, name)
            remote_dir = join_remote(dst.path, rel)
            if not dry_run:
                dst.backend.mkdir(dst.root, remote_dir, parents=True, exist_ok=True)
            stats["directories"] += 1
        for name in sorted(filenames):
            full = os.path.join(current, name)
            if os.path.islink(full):
                _echo(progress, "skip symlink %s" % full)
                continue
            rel = join_remote(rel_dir, name)
            remote_file = join_remote(dst.path, rel)
            size = os.path.getsize(full)
            if dry_run:
                stats["files_copied"] += 1
                stats["bytes"] += size
                continue
            with open(full, "rb") as handle:
                dst.backend.write_stream(
                    dst.root,
                    remote_file,
                    handle,
                    length=size,
                    overwrite=True,
                    create_dirs=True,
                    mtime=os.path.getmtime(full),
                )
            stats["files_copied"] += 1
            stats["bytes"] += size
            _echo(progress, "uploaded %s" % rel)
    return stats
