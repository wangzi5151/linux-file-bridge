"""Command line interface for linux-file-bridge."""

from __future__ import annotations

import argparse
import copy
import datetime
import json
import logging
import os
import shutil
import sys
import time
from typing import Any, Dict, List, Optional, Sequence

from . import transfer
from .api import make_server, server_url, serve_forever
from .backends import open_backend
from .config import (
    Config,
    Environment,
    build_bridge_env,
    build_local_env,
    create_config,
    default_config_path,
    validate_name,
)
from .errors import ConfigError, LfbError, NotFound
from .gateway import make_gateway_server
from .mcp import McpServer
from .targets import TargetResolver
from .util import human_size, note
from .version import __version__


# --------------------------------------------------------------------- helpers
def _resolve_config_path(args: argparse.Namespace) -> str:
    return os.path.abspath(os.path.expanduser(getattr(args, "config", None) or default_config_path()))


def _load_config(args: argparse.Namespace) -> Config:
    return Config.load(_resolve_config_path(args))


def _emit_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _mtime_text(value: Optional[float]) -> str:
    if value is None:
        return "-"
    try:
        return datetime.datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "-"


def _type_marker(entry: Dict[str, Any]) -> str:
    if entry.get("symlink"):
        return "l"
    return {"dir": "d", "file": "-"}.get(str(entry.get("type")), "?")


def _print_entry(entry: Dict[str, Any], long: bool = False) -> None:
    name = entry.get("name") or entry.get("path") or ""
    if entry.get("type") == "dir":
        name += "/"
    if long:
        size = human_size(entry.get("size")) if entry.get("type") == "file" else "-"
        print(
            "%s %s %10s %s %s"
            % (
                entry.get("mode") or "---------",
                _mtime_text(entry.get("mtime")),
                size,
                _type_marker(entry),
                name,
            )
        )
    else:
        print(name)


def _print_stat(entry: Dict[str, Any]) -> None:
    print("path:    %s" % (entry.get("path") or "/"))
    print("type:    %s" % entry.get("type"))
    if entry.get("size") is not None:
        print("size:    %d (%s)" % (entry["size"], human_size(entry["size"])))
    print("mtime:   %s" % _mtime_text(entry.get("mtime")))
    print("mode:    %s" % entry.get("mode"))
    if entry.get("symlink"):
        print("symlink: yes -> %s" % entry.get("symlink_target", "?"))
    if entry.get("sha256"):
        print("sha256:  %s" % entry["sha256"])


def _parse_roots(values: Optional[Sequence[str]], *, default_cwd: bool = False) -> Dict[str, str]:
    if not values:
        if default_cwd:
            return {"project": os.getcwd()}
        raise ConfigError("at least one --root NAME=PATH is required")
    roots: Dict[str, str] = {}
    for value in values:
        if "=" in value:
            name, path = value.split("=", 1)
        elif "/" in value or value.startswith("."):
            name, path = os.path.basename(os.path.abspath(value)) or "root", value
        else:
            raise ConfigError("invalid --root %r (expected NAME=PATH)" % value)
        name = name.strip()
        path = path.strip()
        if not name or not path:
            raise ConfigError("invalid --root %r (expected NAME=PATH)" % value)
        validate_name(name, "root name")
        if name in roots:
            raise ConfigError("duplicate root name %r" % name)
        roots[name] = path
    return roots


def _stdout_bytes(data: bytes) -> None:
    stream = getattr(sys.stdout, "buffer", None)
    if stream is None:
        sys.stdout.write(data.decode("utf-8", "replace"))
    else:
        stream.write(data)
        stream.flush()


def _print_stats(stats: Dict[str, Any]) -> None:
    if stats.get("dry_run"):
        print("dry run (nothing written)")
    if "files_copied" in stats:
        print(
            "copied %d file(s), skipped %d, deleted %d, %s"
            % (
                stats.get("files_copied", 0),
                stats.get("files_skipped", 0),
                stats.get("deleted", 0),
                human_size(int(stats.get("bytes") or 0)),
            )
        )
        if stats.get("source"):
            print("source:      %s" % stats["source"])
        if stats.get("destination"):
            print("destination: %s" % stats["destination"])
    else:
        _emit_json(stats)


# ------------------------------------------------------------------ commands
def cmd_init(args: argparse.Namespace) -> int:
    config_path = _resolve_config_path(args)
    roots = _parse_roots(args.root, default_cwd=True)
    if os.path.exists(config_path) and args.force:
        backup = "%s.bak.%s" % (config_path, time.strftime("%Y%m%d-%H%M%S"))
        try:
            shutil.copy2(config_path, backup)
            note("backup written to %s" % backup)
        except OSError as exc:
            note("warning: could not create backup: %s" % exc)
    config = create_config(
        config_path,
        args.name,
        roots,
        force=args.force,
        token=args.token,
        gateway_token=args.gateway_token,
        gateway_port=args.gateway_port,
        exec_enabled=args.exec or args.allow_shell,
        allow_shell=args.allow_shell,
    )
    env = config.get_env()
    print("config:  %s" % config.path)
    print("env:     %s (local)" % env.name)
    for name, root in env.roots.items():
        print("root:    %s -> %s [%s]" % (name, root["path"], root["mode"]))
    print("gateway: http://%s:%d" % (config.gateway["host"], config.gateway["port"]))
    print("")
    print("Next steps:")
    print("  lfb serve --env %s          # expose this environment" % env.name)
    print("  lfb env token %s            # print its bearer token" % env.name)
    print("  lfb --config %s env add dev --url http://127.0.0.1:8791 --token TOKEN" % config.path)
    print("  lfb --config %s mcp         # use it from the mobile app agent" % config.path)
    return 0


def _env_summary(env: Environment, default: bool) -> str:
    marker = "*" if default else " "
    if env.kind == "local":
        roots = ", ".join("%s=%s" % (name, info["path"]) for name, info in env.roots.items())
        return "%s %-16s local  %s" % (marker, env.name, roots)
    return "%s %-16s bridge %s" % (marker, env.name, env.url)


def cmd_env_list(args: argparse.Namespace) -> int:
    config = _load_config(args)
    if args.json:
        _emit_json(
            {
                "config": config.path,
                "default_env": config.default_env,
                "environments": {
                    name: env.to_public_dict() for name, env in config.environments.items()
                },
            }
        )
        return 0
    print("config: %s" % config.path)
    for name, env in config.environments.items():
        print(_env_summary(env, name == config.default_env))
    return 0


def cmd_env_add(args: argparse.Namespace) -> int:
    config = _load_config(args)
    name = validate_name(args.name, "environment name")
    exists = name in config.environments
    if exists and not args.force:
        raise ConfigError("environment %r already exists (use --force to replace)" % name)
    if args.url:
        token = args.token or os.environ.get("LFB_TOKEN", "")
        if not token:
            raise ConfigError("--token (or LFB_TOKEN) is required for a bridge environment")
        verify: Any = True
        if args.insecure:
            verify = False
        elif args.ca:
            verify = os.path.abspath(os.path.expanduser(args.ca))
        env = build_bridge_env(
            name, args.url, token, verify_tls=verify, timeout=args.timeout
        )
    else:
        roots = _parse_roots(args.root)
        env = build_local_env(
            name,
            roots,
            token=args.token or None,
            read_only=args.read_only,
            exec_enabled=args.exec or args.allow_shell,
            allow_shell=args.allow_shell,
        )
    if exists:
        old_default = config.default_env == name
        del config.environments[name]
        config.environments[name] = env
        if old_default:
            config.default_env = name
    else:
        config.add_env(env, make_default=args.default)
    if args.default:
        config.set_default(name)
    if not config.gateway.get("token"):
        from .util import generate_token

        config.gateway["token"] = generate_token()
    config.save()
    print("added %s environment %r to %s" % (env.kind, name, config.path))
    if env.kind == "local":
        print("token: lfb env token %s" % name)
    return 0


def cmd_env_remove(args: argparse.Namespace) -> int:
    config = _load_config(args)
    config.remove_env(args.name)
    config.save()
    print("removed environment %r" % args.name)
    return 0


def cmd_env_default(args: argparse.Namespace) -> int:
    config = _load_config(args)
    config.set_default(args.name)
    config.save()
    print("default environment: %s" % args.name)
    return 0


def cmd_env_show(args: argparse.Namespace) -> int:
    config = _load_config(args)
    env = config.get_env(args.name)
    _emit_json(env.to_public_dict())
    return 0


def cmd_env_token(args: argparse.Namespace) -> int:
    config = _load_config(args)
    env = config.get_env(args.name)
    token = env.token
    if args.rotate or not token:
        from .util import generate_token

        token = generate_token()
        env.token = token
        env.raw["token"] = token
        config.save()
        note("token rotated for %r; restart its bridge if it is running" % env.name)
    print(token)
    return 0


def _build_serve_env(env: Environment, root_overrides: Optional[Sequence[str]]) -> Environment:
    if not root_overrides:
        return env
    roots = _parse_roots(root_overrides)
    raw = copy.deepcopy(env.raw)
    raw["roots"] = {name: {"path": path, "mode": "rw"} for name, path in roots.items()}
    return Environment(env.name, raw)


def cmd_serve(args: argparse.Namespace) -> int:
    config = _load_config(args)
    env = config.get_env(args.env)
    if env.kind != "local":
        raise ConfigError("'serve' needs a local environment; %r is a bridge" % env.name)
    env = _build_serve_env(env, args.root)
    token = args.token or env.token
    if not token:
        from .util import generate_token

        token = generate_token()
        env.token = token
        env.raw["token"] = token
        config.save()
        note("generated and saved a new token for %r" % env.name)
    from .backends import Backend

    backend = Backend(env)
    host = args.host or env.serve.get("host") or "127.0.0.1"
    port = args.port if args.port is not None else int(env.serve.get("port", 8790))
    tls_cert = args.tls_cert or env.serve.get("tls_cert")
    tls_key = args.tls_key or env.serve.get("tls_key")
    httpd = make_server(
        backend.impl,
        token,
        host=host,
        port=port,
        tls_cert=tls_cert,
        tls_key=tls_key,
        cors_origin=args.cors,
        quiet=args.quiet,
    )
    url = server_url(httpd, host, tls=bool(tls_cert))
    note("lfb bridge %r listening on %s" % (env.name, url))
    note("roots: %s" % ", ".join(backend.roots_info().keys()))
    if args.print_token:
        note("token: %s" % token)
    try:
        serve_forever(httpd)
    except KeyboardInterrupt:
        note("stopped")
    return 0


def cmd_gateway(args: argparse.Namespace) -> int:
    config = _load_config(args)
    httpd = make_gateway_server(
        config,
        host=args.host or config.gateway.get("host") or "127.0.0.1",
        port=args.port if args.port is not None else int(config.gateway.get("port", 8787)),
        token=args.token,
        default_env=args.default_env,
        tls_cert=args.tls_cert or config.gateway.get("tls_cert"),
        tls_key=args.tls_key or config.gateway.get("tls_key"),
        cors_origin=args.cors,
        quiet=args.quiet,
    )
    effective_tls = bool(args.tls_cert or config.gateway.get("tls_cert"))
    url = server_url(httpd, args.host or "127.0.0.1", tls=effective_tls)
    note("lfb gateway listening on %s" % url)
    note("environments: %s" % ", ".join(httpd.backends.keys()))
    if args.print_token:
        note("token: %s" % httpd.token)
    try:
        serve_forever(httpd)
    except KeyboardInterrupt:
        note("stopped")
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    config = _load_config(args)
    server = McpServer(config, quiet=args.quiet)
    return server.run()


def cmd_health(args: argparse.Namespace) -> int:
    config = _load_config(args)
    names = [args.env] if args.env else list(config.environments.keys())
    results = []
    failed = False
    for name in names:
        env = config.get_env(name)
        item: Dict[str, Any] = {"name": name, "kind": env.kind}
        try:
            backend = open_backend(env)
            health = backend.health()
            item["ok"] = True
            item["health"] = health
            item["roots"] = backend.roots_info()
        except LfbError as exc:
            item["ok"] = False
            item["error"] = {"code": exc.code, "message": exc.message}
            failed = True
        results.append(item)
    if args.json:
        _emit_json({"environments": results})
    else:
        for item in results:
            status = "OK" if item["ok"] else "FAIL"
            print("%-6s %-16s %-8s %s" % (status, item["name"], item["kind"], item.get("error", {}).get("message", "")))
    return 1 if failed else 0


def cmd_roots(args: argparse.Namespace) -> int:
    config = _load_config(args)
    env = config.get_env(args.env)
    backend = open_backend(env)
    roots = backend.roots_info()
    if args.json:
        _emit_json({"environment": env.name, "roots": roots})
    else:
        print("environment: %s" % env.name)
        for name, info in roots.items():
            print("  %-16s %s [%s]" % (name, info.get("path", ""), info.get("mode", "rw")))
    return 0


# --------------------------------------------------------------- file commands
def _resolver(args: argparse.Namespace) -> TargetResolver:
    return TargetResolver(_load_config(args))


def cmd_ls(args: argparse.Namespace) -> int:
    resolver = _resolver(args)
    target = resolver.parse(args.path or "", env=args.env, root=args.root)
    stat = target.backend.stat(target.root, target.path)
    if stat.get("type") != "dir":
        if args.json:
            _emit_json(stat)
        else:
            _print_stat(stat)
        return 0
    data = target.backend.list_dir(
        target.root, target.path, show_hidden=not args.no_hidden
    )
    if args.json:
        _emit_json(data)
        return 0
    print("environment: %s  root: %s  path: /%s" % (target.env, target.root, target.path))
    for entry in data["entries"]:
        entry = dict(entry)
        entry["name"] = entry.get("name", "")
        _print_entry(entry, long=args.long or args.detail)
    return 0


def _render_tree(entries: List[Dict[str, Any]], prefix: str = "") -> None:
    for index, entry in enumerate(entries):
        last = index == len(entries) - 1
        connector = "`-- " if last else "|-- "
        name = entry.get("name", "")
        if entry.get("type") == "dir":
            name += "/"
        print(prefix + connector + name)
        children = entry.get("children") or []
        if children:
            _render_tree(children, prefix + ("    " if last else "|   "))


def cmd_tree(args: argparse.Namespace) -> int:
    resolver = _resolver(args)
    target = resolver.parse(args.path or "", env=args.env, root=args.root)
    data = target.backend.tree(
        target.root,
        target.path,
        depth=args.depth,
        show_hidden=not args.no_hidden,
    )
    if args.json:
        _emit_json(data)
        return 0
    print("%s:%s" % (target.env, target.root))
    _render_tree(data.get("entries") or [])
    return 0


def cmd_stat(args: argparse.Namespace) -> int:
    resolver = _resolver(args)
    target = resolver.parse(args.path, env=args.env, root=args.root)
    entry = target.backend.stat(target.root, target.path, want_hash=args.hash)
    if args.json:
        _emit_json(entry)
    else:
        _print_stat(entry)
    return 0


def cmd_cat(args: argparse.Namespace) -> int:
    resolver = _resolver(args)
    target = resolver.parse(args.path, env=args.env, root=args.root)
    if args.offset or args.max is not None:
        data, _entry, truncated = target.backend.read_bytes(
            target.root, target.path, offset=args.offset, max_bytes=args.max
        )
        _stdout_bytes(data)
        if truncated and not args.quiet:
            note("warning: output truncated; use --offset to continue")
        return 0
    handle, _entry = target.backend.open_read(target.root, target.path)
    try:
        out = getattr(sys.stdout, "buffer", None)
        while True:
            block = handle.read(64 * 1024)
            if not block:
                break
            if out is not None:
                out.write(block)
            else:
                sys.stdout.write(block.decode("utf-8", "replace"))
        if out is not None:
            out.flush()
        else:
            sys.stdout.flush()
    finally:
        handle.close()
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    resolver = _resolver(args)
    src = resolver.parse(args.source, env=args.env, root=args.root)
    local = args.destination or os.path.basename(src.path.rstrip("/")) or "download"
    if local == "-":
        stat = src.backend.stat(src.root, src.path)
        if stat.get("type") != "file":
            raise ConfigError("stdout download requires a regular file")
        handle, _entry = src.backend.open_read(src.root, src.path)
        try:
            out = getattr(sys.stdout, "buffer", None)
            while True:
                block = handle.read(64 * 1024)
                if not block:
                    break
                if out is not None:
                    out.write(block)
                else:
                    sys.stdout.write(block.decode("utf-8", "replace"))
            if out is not None:
                out.flush()
        finally:
            handle.close()
        return 0
    stats = transfer.download(
        src,
        local,
        recursive=args.recursive,
        overwrite=not args.no_overwrite,
        progress=(lambda message: note(message)) if not args.quiet else None,
    )
    if not args.quiet:
        _print_stats(stats)
    return 0


def cmd_put(args: argparse.Namespace) -> int:
    resolver = _resolver(args)
    if args.source == "-":
        data = sys.stdin.buffer.read()
        target = resolver.parse(args.destination, env=args.env, root=args.root)
        entry = target.backend.write_bytes(
            target.root,
            target.path,
            data,
            overwrite=not args.no_overwrite,
            create_dirs=True,
            mtime=time.time(),
        )
        if not args.quiet:
            print("uploaded %d bytes to %s:%s" % (len(data), target.env, target.path))
        if args.json:
            _emit_json(entry)
        return 0
    source = os.path.abspath(os.path.expanduser(args.source))
    if os.path.isdir(source):
        if not args.recursive:
            raise ConfigError("source is a directory; add --recursive")
        target = resolver.parse(args.destination, env=args.env, root=args.root)
        stats = transfer.upload(
            source,
            target,
            recursive=True,
            overwrite=not args.no_overwrite,
            progress=(lambda message: note(message)) if not args.quiet else None,
        )
    else:
        target = resolver.parse(args.destination, env=args.env, root=args.root)
        stats = transfer.upload(
            source,
            target,
            recursive=False,
            overwrite=not args.no_overwrite,
            progress=(lambda message: note(message)) if not args.quiet else None,
        )
    if not args.quiet:
        _print_stats(stats)
    return 0


def cmd_mkdir(args: argparse.Namespace) -> int:
    resolver = _resolver(args)
    target = resolver.parse(args.path, env=args.env, root=args.root)
    entry = target.backend.mkdir(
        target.root,
        target.path,
        parents=args.parents,
        exist_ok=not args.no_exist_ok,
    )
    if args.json:
        _emit_json(entry)
    elif not args.quiet:
        print("created %s:%s" % (target.env, entry.get("path") or "/"))
    return 0


def cmd_mv(args: argparse.Namespace) -> int:
    resolver = _resolver(args)
    src = resolver.parse(args.source, env=args.env, root=args.root)
    dst_env = args.env
    if dst_env is None and not (
        ":" in args.destination
        and args.destination.split(":", 1)[0] in resolver.env_names()
    ):
        dst_env = src.env
    dst = resolver.parse(args.destination, env=dst_env, root=args.root)
    if src.env != dst.env:
        raise ConfigError("mv cannot cross environments; use sync")
    entry = src.backend.move(
        src.root, src.path, dst.root, dst.path, overwrite=args.overwrite
    )
    if args.json:
        _emit_json(entry)
    elif not args.quiet:
        print("moved %s -> %s" % (src.label(), dst.label()))
    return 0


def cmd_rm(args: argparse.Namespace) -> int:
    resolver = _resolver(args)
    target = resolver.parse(args.path, env=args.env, root=args.root)
    try:
        result = target.backend.remove(target.root, target.path, recursive=args.recursive)
    except NotFound:
        if args.force:
            if not args.quiet:
                note("not found (ignored): %s" % target.label())
            return 0
        raise
    if args.json:
        _emit_json(result)
    elif not args.quiet:
        print("removed %s" % target.label())
    return 0


def cmd_find(args: argparse.Namespace) -> int:
    resolver = _resolver(args)
    target = resolver.parse(args.path or "", env=args.env, root=args.root)
    if not any((args.glob, args.regex, args.contains)):
        args.glob = "*"
    excludes = []
    for item in args.exclude or []:
        excludes.extend(part for part in item.split(",") if part)
    data = target.backend.search(
        target.root,
        target.path,
        glob=args.glob,
        regex=args.regex,
        contains=args.contains,
        want_type=args.type,
        max_results=args.max,
        exclude=excludes,
        show_hidden=not args.no_hidden,
    )
    if args.json:
        _emit_json(data)
        return 0
    for entry in data.get("results") or []:
        suffix = "/" if entry.get("type") == "dir" else ""
        line = entry.get("path", "") + suffix
        if entry.get("line"):
            line += ":%d" % entry["line"]
        print(line)
    if data.get("truncated") and not args.quiet:
        note("warning: results truncated at %d (use --max)" % len(data.get("results") or []))
    return 0


def cmd_exec(args: argparse.Namespace) -> int:
    resolver = _resolver(args)
    target = resolver.parse(args.target or "", env=args.env, root=args.root)
    command = list(args.command or [])
    if not command and not args.shell:
        raise ConfigError("provide a command, e.g. lfb exec project -- ls -la")
    if args.shell:
        data = target.backend.exec_command(
            target.root,
            target.path,
            command=" ".join(command),
            shell=True,
            timeout=args.timeout,
        )
    else:
        data = target.backend.exec_command(
            target.root,
            target.path,
            argv=command,
            shell=False,
            timeout=args.timeout,
        )
    if args.json:
        _emit_json(data)
    else:
        if data.get("stdout"):
            sys.stdout.write(data["stdout"])
            if not data["stdout"].endswith("\n"):
                sys.stdout.write("\n")
        if data.get("stderr"):
            sys.stderr.write(data["stderr"])
            if not data["stderr"].endswith("\n"):
                sys.stderr.write("\n")
        if data.get("timed_out"):
            note("command timed out")
        if data.get("truncated"):
            note("output truncated")
    return int(data.get("exit_code") or 0)


def cmd_sync(args: argparse.Namespace) -> int:
    resolver = _resolver(args)
    src = resolver.parse(args.source, env=args.src_env, root=args.src_root)
    dst_env = args.dst_env or (None if ":" in args.destination and args.destination.split(":", 1)[0] in resolver.env_names() else None)
    dst = resolver.parse(args.destination, env=dst_env, root=args.dst_root)
    stats = transfer.sync(
        src,
        dst,
        delete=args.delete,
        checksum=args.checksum,
        overwrite=True,
        excludes=args.exclude or [],
        progress=(lambda message: note(message)) if not args.quiet else None,
        dry_run=args.dry_run,
        allow_root_delete=args.allow_root_delete,
    )
    if args.json:
        _emit_json(stats)
    else:
        _print_stats(stats)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    config = _load_config(args)
    checks: List[Dict[str, Any]] = []

    def add(name: str, status: str, detail: str) -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    add("python", "ok" if sys.version_info >= (3, 8) else "fail", sys.version.split()[0])
    try:
        mode = os.stat(config.path).st_mode & 0o777
        if mode & 0o077:
            add("config permissions", "warn", "%s is %o (should be 600)" % (config.path, mode))
        else:
            add("config permissions", "ok", "%s is %o" % (config.path, mode))
    except OSError as exc:
        add("config permissions", "fail", str(exc))

    for name, env in config.environments.items():
        if not env.token:
            add("token:%s" % name, "fail" if env.kind == "bridge" else "warn", "no token configured")
        else:
            add("token:%s" % name, "ok", "configured")
        if env.kind == "local":
            for root_name, root in env.roots.items():
                path = root["path"]
                if not os.path.isdir(path):
                    add("root:%s/%s" % (name, root_name), "fail", "not a directory: %s" % path)
                    continue
                writable = os.access(path, os.W_OK)
                status = "ok"
                if root["mode"] == "rw" and not writable:
                    status = "warn"
                add(
                    "root:%s/%s" % (name, root_name),
                    status,
                    "%s [%s]%s" % (path, root["mode"], "" if writable else " (not writable)"),
                )
            if env.exec["enabled"]:
                add("exec:%s" % name, "warn", "command execution is enabled")
        else:
            try:
                backend = open_backend(env)
                health = backend.health()
                add("bridge:%s" % name, "ok", "%s -> %s" % (env.url, health.get("environment", "?")))
            except LfbError as exc:
                add("bridge:%s" % name, "fail", "%s: %s" % (env.url, exc.message))

    if config.gateway.get("token"):
        add("gateway token", "ok", "configured")
    else:
        add("gateway token", "warn", "empty (falling back to the default environment token)")

    if args.json:
        _emit_json({"config": config.path, "checks": checks})
    else:
        for check in checks:
            print("%-6s %-24s %s" % (check["status"].upper(), check["check"], check["detail"]))
    return 1 if any(check["status"] == "fail" for check in checks) else 0


# --------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lfb",
        description="Linux File Bridge: share project files between Android and Linux environments.",
    )
    parser.add_argument("--config", help="config file (default: %s)" % default_config_path())
    parser.add_argument("--version", action="version", version="lfb %s" % __version__)
    parser.add_argument("-v", "--verbose", action="count", default=0, help="verbose diagnostics")
    parser.add_argument("-q", "--quiet", action="store_true", help="suppress non-error output")
    sub = parser.add_subparsers(dest="command")

    def add_common(p: argparse.ArgumentParser, json_flag: bool = False) -> None:
        p.add_argument("--config", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        p.add_argument("-q", "--quiet", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        if json_flag:
            p.add_argument("--json", action="store_true", help="print machine-readable JSON")

    def add_env_root(p: argparse.ArgumentParser) -> None:
        p.add_argument("--env", help="environment name")
        p.add_argument("--root", help="root name")

    p_init = sub.add_parser("init", help="create a config with a local environment")
    add_common(p_init)
    p_init.add_argument("--name", default="ubuntu", help="environment name (default: ubuntu)")
    p_init.add_argument("--root", action="append", metavar="NAME=PATH", help="root to expose (repeatable)")
    p_init.add_argument("--force", action="store_true", help="overwrite an existing config")
    p_init.add_argument("--token", help="explicit bearer token for the local environment")
    p_init.add_argument("--gateway-token", help="explicit gateway bearer token")
    p_init.add_argument("--gateway-port", type=int, default=8787)
    p_init.add_argument("--exec", action="store_true", help="enable command execution")
    p_init.add_argument("--allow-shell", action="store_true", help="allow shell commands (implies --exec)")
    p_init.set_defaults(func=cmd_init)

    p_env = sub.add_parser("env", help="manage environments")
    add_common(p_env, json_flag=True)
    p_env.set_defaults(func=cmd_env_list)
    env_sub = p_env.add_subparsers(dest="env_command")

    p = env_sub.add_parser("list", help="list environments")
    add_common(p, json_flag=True)
    p.set_defaults(func=cmd_env_list)

    p = env_sub.add_parser("add", help="add an environment")
    add_common(p)
    p.add_argument("name")
    p.add_argument("--url", help="remote bridge URL (creates a bridge environment)")
    p.add_argument("--token", help="bearer token")
    p.add_argument("--root", action="append", metavar="NAME=PATH", help="local root (repeatable)")
    p.add_argument("--read-only", action="store_true")
    p.add_argument("--exec", action="store_true")
    p.add_argument("--allow-shell", action="store_true")
    p.add_argument("--insecure", action="store_true", help="do not verify TLS for a bridge")
    p.add_argument("--ca", help="CA bundle for a bridge")
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--default", action="store_true", help="make this the default environment")
    p.add_argument("--force", action="store_true", help="replace an existing environment")
    p.set_defaults(func=cmd_env_add)

    p = env_sub.add_parser("remove", help="remove an environment")
    add_common(p)
    p.add_argument("name")
    p.set_defaults(func=cmd_env_remove)

    p = env_sub.add_parser("default", help="set the default environment")
    add_common(p)
    p.add_argument("name")
    p.set_defaults(func=cmd_env_default)

    p = env_sub.add_parser("show", help="show one environment")
    add_common(p, json_flag=True)
    p.add_argument("name")
    p.set_defaults(func=cmd_env_show)

    p = env_sub.add_parser("token", help="print or rotate a token")
    add_common(p)
    p.add_argument("name")
    p.add_argument("--rotate", action="store_true")
    p.set_defaults(func=cmd_env_token)

    p_serve = sub.add_parser("serve", help="run a bridge for one local environment")
    add_common(p_serve)
    p_serve.add_argument("--env", help="local environment (default: %(default)s)")
    p_serve.add_argument("--host")
    p_serve.add_argument("--port", type=int)
    p_serve.add_argument("--token", help="override the stored token")
    p_serve.add_argument("--root", action="append", metavar="NAME=PATH", help="override roots")
    p_serve.add_argument("--tls-cert")
    p_serve.add_argument("--tls-key")
    p_serve.add_argument("--cors", help="allow this CORS origin")
    p_serve.add_argument("--print-token", action="store_true")
    p_serve.set_defaults(func=cmd_serve)

    p_gateway = sub.add_parser("gateway", help="run the multi-environment gateway")
    add_common(p_gateway)
    p_gateway.add_argument("--host")
    p_gateway.add_argument("--port", type=int)
    p_gateway.add_argument("--token")
    p_gateway.add_argument("--default-env")
    p_gateway.add_argument("--tls-cert")
    p_gateway.add_argument("--tls-key")
    p_gateway.add_argument("--cors")
    p_gateway.add_argument("--print-token", action="store_true")
    p_gateway.set_defaults(func=cmd_gateway)

    p_mcp = sub.add_parser("mcp", help="run the MCP stdio server for app agents")
    add_common(p_mcp)
    p_mcp.set_defaults(func=cmd_mcp)

    p_health = sub.add_parser("health", help="check environment connectivity")
    add_common(p_health, json_flag=True)
    p_health.add_argument("--env")
    p_health.set_defaults(func=cmd_health)

    p_roots = sub.add_parser("roots", help="list roots of an environment")
    add_common(p_roots, json_flag=True)
    p_roots.add_argument("--env")
    p_roots.set_defaults(func=cmd_roots)

    p_ls = sub.add_parser("ls", help="list a directory")
    add_common(p_ls, json_flag=True)
    add_env_root(p_ls)
    p_ls.add_argument("path", nargs="?", default="")
    p_ls.add_argument("-l", "--long", action="store_true")
    p_ls.add_argument("--detail", action="store_true")
    p_ls.add_argument("--no-hidden", action="store_true")
    p_ls.set_defaults(func=cmd_ls)

    p_tree = sub.add_parser("tree", help="show a directory tree")
    add_common(p_tree, json_flag=True)
    add_env_root(p_tree)
    p_tree.add_argument("path", nargs="?", default="")
    p_tree.add_argument("--depth", type=int, default=2)
    p_tree.add_argument("--no-hidden", action="store_true")
    p_tree.set_defaults(func=cmd_tree)

    p_stat = sub.add_parser("stat", help="show file metadata")
    add_common(p_stat, json_flag=True)
    add_env_root(p_stat)
    p_stat.add_argument("path")
    p_stat.add_argument("--hash", action="store_true")
    p_stat.set_defaults(func=cmd_stat)

    p_cat = sub.add_parser("cat", help="write a file to stdout")
    add_common(p_cat)
    add_env_root(p_cat)
    p_cat.add_argument("path")
    p_cat.add_argument("--offset", type=int, default=0)
    p_cat.add_argument("--max", type=int)
    p_cat.set_defaults(func=cmd_cat)

    p_get = sub.add_parser("get", help="download from an environment")
    add_common(p_get)
    add_env_root(p_get)
    p_get.add_argument("source")
    p_get.add_argument("destination", nargs="?")
    p_get.add_argument("-r", "--recursive", action="store_true")
    p_get.add_argument("--no-overwrite", action="store_true")
    p_get.set_defaults(func=cmd_get)

    p_put = sub.add_parser("put", help="upload to an environment")
    add_common(p_put)
    add_env_root(p_put)
    p_put.add_argument("source")
    p_put.add_argument("destination")
    p_put.add_argument("-r", "--recursive", action="store_true")
    p_put.add_argument("--no-overwrite", action="store_true")
    p_put.set_defaults(func=cmd_put)

    p_mkdir = sub.add_parser("mkdir", help="create a directory")
    add_common(p_mkdir, json_flag=True)
    add_env_root(p_mkdir)
    p_mkdir.add_argument("path")
    p_mkdir.add_argument("-p", "--parents", action="store_true")
    p_mkdir.add_argument("--no-exist-ok", action="store_true")
    p_mkdir.set_defaults(func=cmd_mkdir)

    p_mv = sub.add_parser("mv", help="move or rename inside one environment")
    add_common(p_mv, json_flag=True)
    add_env_root(p_mv)
    p_mv.add_argument("source")
    p_mv.add_argument("destination")
    p_mv.add_argument("--overwrite", action="store_true")
    p_mv.set_defaults(func=cmd_mv)

    p_rm = sub.add_parser("rm", help="remove a file or directory")
    add_common(p_rm, json_flag=True)
    add_env_root(p_rm)
    p_rm.add_argument("path")
    p_rm.add_argument("-r", "--recursive", action="store_true")
    p_rm.add_argument("-f", "--force", action="store_true")
    p_rm.set_defaults(func=cmd_rm)

    p_find = sub.add_parser("find", help="search names or contents")
    add_common(p_find, json_flag=True)
    add_env_root(p_find)
    p_find.add_argument("path", nargs="?", default="")
    p_find.add_argument("--glob")
    p_find.add_argument("--regex")
    p_find.add_argument("--contains")
    p_find.add_argument("--name", dest="glob", help=argparse.SUPPRESS)
    p_find.add_argument("--type", choices=["all", "file", "dir", "symlink"], default="all")
    p_find.add_argument("--max", type=int, default=100)
    p_find.add_argument("--exclude", action="append")
    p_find.add_argument("--no-hidden", action="store_true")
    p_find.set_defaults(func=cmd_find)

    p_exec = sub.add_parser("exec", help="run a command (must be enabled in config)")
    add_common(p_exec, json_flag=True)
    add_env_root(p_exec)
    p_exec.add_argument("target", nargs="?", default="")
    p_exec.add_argument("--shell", action="store_true")
    p_exec.add_argument("--timeout", type=float)
    p_exec.add_argument("command", nargs=argparse.REMAINDER)
    p_exec.set_defaults(func=cmd_exec)

    p_sync = sub.add_parser(
        "sync", aliases=["copy", "cp"], help="copy/mirror between two environments"
    )
    add_common(p_sync, json_flag=True)
    p_sync.add_argument("source")
    p_sync.add_argument("destination")
    p_sync.add_argument("--src-env")
    p_sync.add_argument("--src-root")
    p_sync.add_argument("--dst-env")
    p_sync.add_argument("--dst-root")
    p_sync.add_argument("--delete", action="store_true", help="delete destination extras")
    p_sync.add_argument("--allow-root-delete", action="store_true")
    p_sync.add_argument("--dry-run", action="store_true")
    p_sync.add_argument("--checksum", action="store_true")
    p_sync.add_argument("--exclude", action="append")
    p_sync.set_defaults(func=cmd_sync)

    p_doctor = sub.add_parser("doctor", help="diagnose configuration and connectivity")
    add_common(p_doctor, json_flag=True)
    p_doctor.set_defaults(func=cmd_doctor)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", 0) > 1 else (logging.INFO if getattr(args, "verbose", 0) else logging.WARNING),
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        return int(args.func(args) or 0)
    except LfbError as exc:
        note("lfb: error [%s]: %s" % (exc.code, exc.message))
        return 1
    except KeyboardInterrupt:
        note("interrupted")
        return 130
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except Exception:
            pass
        return 0
    except OSError as exc:
        note("lfb: error: %s" % exc)
        return 1
