"""Model Context Protocol (stdio) server for mobile-app agents.

The server speaks newline-delimited JSON-RPC 2.0 on stdin/stdout.  Diagnostics
go to stderr exclusively so stdout stays a clean protocol channel.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import sys
from typing import Any, Dict, List, Optional, TextIO

from .config import Config
from .errors import BadRequest, LfbError
from .targets import Target, TargetResolver
from .version import __version__

LOG = logging.getLogger("lfb.mcp")

SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")
DEFAULT_PROTOCOL_VERSION = "2024-11-05"

ENV_PROP = {
    "type": "string",
    "description": "Environment name (see lfb_list_envs). Omit to use the default environment.",
}
ROOT_PROP = {
    "type": "string",
    "description": "Root name inside the environment (for example 'project'). Omit to use the default root.",
}
PATH_PROP = {
    "type": "string",
    "description": "Path relative to the root. It may also be written as env:root/path.",
}


def _schema(properties: Dict[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
    schema: Dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    return schema


TOOLS: List[Dict[str, Any]] = [
    {
        "name": "lfb_list_envs",
        "description": "List configured Linux environments, their roots, read-only status and exec availability.",
        "inputSchema": _schema({}),
    },
    {
        "name": "lfb_list",
        "description": "List a directory (depth=1) or show a small tree (depth>1) in any configured environment.",
        "inputSchema": _schema(
            {
                "env": ENV_PROP,
                "root": ROOT_PROP,
                "path": PATH_PROP,
                "depth": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5,
                    "description": "1 lists the directory; 2-5 returns a tree.",
                },
                "show_hidden": {"type": "boolean", "description": "Include dotfiles (default true)."},
            },
            [],
        ),
    },
    {
        "name": "lfb_tree",
        "description": "Show a nested directory tree, up to 20 levels deep.",
        "inputSchema": _schema(
            {
                "env": ENV_PROP,
                "root": ROOT_PROP,
                "path": PATH_PROP,
                "depth": {"type": "integer", "minimum": 1, "maximum": 20},
                "show_hidden": {"type": "boolean"},
            },
            [],
        ),
    },
    {
        "name": "lfb_stat",
        "description": "Return metadata for a file or directory, optionally including its SHA-256 hash.",
        "inputSchema": _schema(
            {
                "env": ENV_PROP,
                "root": ROOT_PROP,
                "path": PATH_PROP,
                "hash": {"type": "boolean", "description": "Hash regular files (limit 64 MiB)."},
            },
            ["path"],
        ),
    },
    {
        "name": "lfb_read",
        "description": (
            "Read a UTF-8 text file. Use offset/limit (bytes) for large files. "
            "Binary files are rejected; use the HTTP /v1/download endpoint for those."
        ),
        "inputSchema": _schema(
            {
                "env": ENV_PROP,
                "root": ROOT_PROP,
                "path": PATH_PROP,
                "offset": {"type": "integer", "minimum": 0},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 4194304,
                    "description": "Maximum bytes to return (default 262144).",
                },
            },
            ["path"],
        ),
    },
    {
        "name": "lfb_write",
        "description": "Create or atomically replace a text file in any configured environment.",
        "inputSchema": _schema(
            {
                "env": ENV_PROP,
                "root": ROOT_PROP,
                "path": PATH_PROP,
                "content": {"type": "string", "description": "File content (UTF-8 text, or base64 when encoding=base64)."},
                "encoding": {"type": "string", "enum": ["utf-8", "base64"]},
                "create_dirs": {"type": "boolean", "description": "Create missing parent directories (default true)."},
                "overwrite": {"type": "boolean", "description": "Replace an existing file (default true)."},
            },
            ["path", "content"],
        ),
    },
    {
        "name": "lfb_mkdir",
        "description": "Create a directory (parents are created by default).",
        "inputSchema": _schema(
            {
                "env": ENV_PROP,
                "root": ROOT_PROP,
                "path": PATH_PROP,
                "parents": {"type": "boolean"},
                "exist_ok": {"type": "boolean"},
            },
            ["path"],
        ),
    },
    {
        "name": "lfb_move",
        "description": "Rename or move a path inside one environment.",
        "inputSchema": _schema(
            {
                "env": ENV_PROP,
                "root": ROOT_PROP,
                "src": {"type": "string", "description": "Source path (optionally env:root/path)."},
                "dst": {"type": "string", "description": "Destination path (optionally env:root/path)."},
                "overwrite": {"type": "boolean", "description": "Replace an existing file (default false)."},
            },
            ["src", "dst"],
        ),
    },
    {
        "name": "lfb_delete",
        "description": "Delete a file, symlink or directory. Directories require recursive=true.",
        "inputSchema": _schema(
            {
                "env": ENV_PROP,
                "root": ROOT_PROP,
                "path": PATH_PROP,
                "recursive": {"type": "boolean"},
            },
            ["path"],
        ),
    },
    {
        "name": "lfb_search",
        "description": (
            "Search file names, relative paths or text contents. The pattern is matched "
            "with a glob against both the path and basename."
        ),
        "inputSchema": _schema(
            {
                "env": ENV_PROP,
                "root": ROOT_PROP,
                "path": PATH_PROP,
                "glob": {"type": "string", "description": "Glob such as '*.py' or 'src/**/*.ts'."},
                "regex": {"type": "string", "description": "Python regular expression on the relative path."},
                "contains": {"type": "string", "description": "Only match files containing this text."},
                "type": {"type": "string", "enum": ["all", "file", "dir", "symlink"]},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 200},
                "exclude": {"type": "array", "items": {"type": "string"}, "description": "Glob patterns to skip."},
                "show_hidden": {"type": "boolean"},
            },
            [],
        ),
    },
    {
        "name": "lfb_exec",
        "description": (
            "Run a command in an environment (disabled unless exec.enabled=true in the config). "
            "Prefer argv mode; shell mode requires exec.allow_shell=true."
        ),
        "inputSchema": _schema(
            {
                "env": ENV_PROP,
                "root": ROOT_PROP,
                "path": {"type": "string", "description": "Working directory relative to the root."},
                "argv": {"type": "array", "items": {"type": "string"}, "description": "Command and arguments (no shell)."},
                "command": {"type": "string", "description": "Shell command string (requires shell=true)."},
                "shell": {"type": "boolean"},
                "timeout": {"type": "number", "minimum": 0.1, "maximum": 3600},
                "max_output": {"type": "integer", "minimum": 1024},
            },
            [],
        ),
    },
]


class McpServer(object):
    def __init__(self, config: Config, resolver: Optional[TargetResolver] = None, quiet: bool = False) -> None:
        self.config = config
        self.resolver = resolver or TargetResolver(config)
        self.quiet = quiet
        self.initialized = False

    # ------------------------------------------------------------- transport
    def run(self, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> int:
        if not self.quiet:
            LOG.info(
                "lfb MCP server ready: %s",
                ", ".join(self.config.environments.keys()),
            )
        while True:
            line = stdin.readline()
            if not line:
                return 0
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                self._write_message(stdout, self._error(None, -32700, "parse error: %s" % exc))
                continue
            response = self.handle(message)
            if response is not None:
                self._write_message(stdout, response)

    def _write_message(self, stdout: TextIO, payload: Dict[str, Any]) -> None:
        stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        stdout.flush()

    # -------------------------------------------------------------- protocol
    def handle(self, message: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(message, dict):
            return self._error(None, -32600, "invalid request")
        is_notification = "id" not in message
        request_id = message.get("id")
        method = message.get("method")
        if not isinstance(method, str):
            return None if is_notification else self._error(request_id, -32600, "missing method")

        try:
            if method == "initialize":
                result = self._initialize(message.get("params") or {})
            elif method in ("notifications/initialized", "initialized"):
                self.initialized = True
                return None
            elif method in ("notifications/cancelled", "notifications/cancel", "cancelled"):
                return None
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                result = self._tools_call(message.get("params") or {})
            elif method == "resources/list":
                result = {"resources": []}
            elif method == "prompts/list":
                result = {"prompts": []}
            elif method == "logging/setLevel":
                result = {}
            else:
                if is_notification:
                    return None
                return self._error(request_id, -32601, "method not found: %s" % method)
        except LfbError as exc:
            if is_notification:
                return None
            return self._error(request_id, -32000, exc.message, data={"code": exc.code})
        except Exception as exc:  # pragma: no cover - defensive
            LOG.exception("unhandled MCP error")
            if is_notification:
                return None
            return self._error(request_id, -32603, "internal error: %s" % exc)

        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _initialize(self, params: Any) -> Dict[str, Any]:
        if not isinstance(params, dict):
            params = {}
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else DEFAULT_PROTOCOL_VERSION
        instructions = (
            "Linux File Bridge gives you access to projects in every configured Linux "
            "environment (Ubuntu proot/chroot, Termux, WSL or a remote server). "
            "Call lfb_list_envs first, then use lfb_list/lfb_read/lfb_search/lfb_write. "
            "Paths are relative to a named root such as 'project'; they may also be "
            "written as env:root/path."
        )
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "linux-file-bridge", "version": __version__},
            "instructions": instructions,
        }

    def _error(
        self, request_id: Any, code: int, message: str, data: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        error: Dict[str, Any] = {"code": code, "message": message}
        if data:
            error["data"] = data
        return {"jsonrpc": "2.0", "id": request_id, "error": error}

    # ----------------------------------------------------------------- tools
    def _tools_call(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(params, dict):
            raise BadRequest("tools/call params must be an object")
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or not name:
            raise BadRequest("tools/call is missing a tool name")
        if not isinstance(arguments, dict):
            raise BadRequest("tool arguments must be an object")
        handler = {
            "lfb_list_envs": self._list_envs,
            "lfb_list": self._list,
            "lfb_tree": self._tree,
            "lfb_stat": self._stat,
            "lfb_read": self._read,
            "lfb_write": self._tool_write,
            "lfb_mkdir": self._mkdir,
            "lfb_move": self._move,
            "lfb_delete": self._delete,
            "lfb_search": self._search,
            "lfb_exec": self._exec,
        }.get(name)
        if handler is None:
            return self._tool_error("unknown tool: %s" % name, code="unknown_tool")
        try:
            data = handler(arguments)
        except LfbError as exc:
            return self._tool_error(exc.message, code=exc.code, details=exc.details)
        except Exception as exc:  # pragma: no cover - defensive
            LOG.exception("tool %s failed", name)
            return self._tool_error("internal error: %s" % exc, code="internal_error")
        return {
            "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=2)}],
            "isError": False,
        }

    def _tool_error(
        self, message: str, *, code: str = "error", details: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"code": code, "message": message}
        if details:
            payload["details"] = details
        return {
            "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
            "isError": True,
        }

    def _target(self, args: Dict[str, Any]) -> Target:
        return self.resolver.parse(
            args.get("path", ""),
            env=args.get("env"),
            root=args.get("root"),
        )

    # --------------------------------------------------------------- tool impl
    def _list_envs(self, args: Dict[str, Any]) -> Dict[str, Any]:
        envs = []
        for name, env in self.config.environments.items():
            item: Dict[str, Any] = {"name": name, "kind": env.kind, "default": name == self.config.default_env}
            if env.kind == "bridge":
                item["url"] = env.url
            try:
                backend = self.resolver.backend(name)
                item["roots"] = backend.roots_info()
                item["read_only"] = backend.read_only
                item["exec_enabled"] = backend.exec_enabled
            except LfbError as exc:
                item["error"] = {"code": exc.code, "message": exc.message}
            envs.append(item)
        return {"default_env": self.config.default_env, "environments": envs}

    def _list(self, args: Dict[str, Any]) -> Dict[str, Any]:
        target = self._target(args)
        depth = int(args.get("depth", 1) or 1)
        if depth > 1:
            data = target.backend.tree(
                target.root,
                target.path,
                depth=min(depth, 20),
                show_hidden=bool(args.get("show_hidden", True)),
            )
        else:
            data = target.backend.list_dir(
                target.root,
                target.path,
                show_hidden=bool(args.get("show_hidden", True)),
            )
        data["environment"] = target.env
        return data

    def _tree(self, args: Dict[str, Any]) -> Dict[str, Any]:
        target = self._target(args)
        data = target.backend.tree(
            target.root,
            target.path,
            depth=int(args.get("depth", 3) or 3),
            show_hidden=bool(args.get("show_hidden", True)),
        )
        data["environment"] = target.env
        return data

    def _stat(self, args: Dict[str, Any]) -> Dict[str, Any]:
        target = self._target(args)
        data = target.backend.stat(
            target.root, target.path, want_hash=bool(args.get("hash", False))
        )
        data["environment"] = target.env
        data["root"] = target.root
        return data

    def _read(self, args: Dict[str, Any]) -> Dict[str, Any]:
        target = self._target(args)
        offset = int(args.get("offset", 0) or 0)
        limit = args.get("limit")
        limit = int(limit) if limit is not None else 256 * 1024
        limit = max(1, min(limit, 4 * 1024 * 1024))
        data, entry, truncated = target.backend.read_bytes(
            target.root, target.path, offset=offset, max_bytes=limit
        )
        if b"\x00" in data:
            raise BadRequest(
                "file appears to be binary; use the HTTP /v1/download endpoint or the CLI 'lfb get'"
            )
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise BadRequest(
                "file is not valid UTF-8; use the HTTP /v1/download endpoint or the CLI 'lfb get'"
            )
        return {
            "environment": target.env,
            "root": target.root,
            "path": entry.get("path", target.path),
            "size": entry.get("size"),
            "offset": offset,
            "returned": len(data),
            "truncated": truncated,
            "content": text,
        }

    def _tool_write(self, args: Dict[str, Any]) -> Dict[str, Any]:
        target = self._target(args)
        content = args.get("content")
        if not isinstance(content, str):
            raise BadRequest("content must be a string")
        encoding = str(args.get("encoding", "utf-8")).lower()
        if encoding == "base64":
            try:
                raw = base64.b64decode(content, validate=True)
            except (binascii.Error, ValueError):
                raise BadRequest("content is not valid base64")
        elif encoding in ("utf-8", "utf8", "text"):
            raw = content.encode("utf-8")
        else:
            raise BadRequest("encoding must be 'utf-8' or 'base64'")
        entry = target.backend.write_bytes(
            target.root,
            target.path,
            raw,
            overwrite=bool(args.get("overwrite", True)),
            create_dirs=bool(args.get("create_dirs", True)),
        )
        entry["environment"] = target.env
        entry["root"] = target.root
        entry["bytes_written"] = len(raw)
        return entry

    def _mkdir(self, args: Dict[str, Any]) -> Dict[str, Any]:
        target = self._target(args)
        entry = target.backend.mkdir(
            target.root,
            target.path,
            parents=bool(args.get("parents", True)),
            exist_ok=bool(args.get("exist_ok", True)),
        )
        entry["environment"] = target.env
        entry["root"] = target.root
        return entry

    def _move(self, args: Dict[str, Any]) -> Dict[str, Any]:
        src_spec = args.get("src")
        dst_spec = args.get("dst")
        if not src_spec or not dst_spec:
            raise BadRequest("src and dst are required")
        src = self.resolver.parse(src_spec, env=args.get("env"), root=args.get("root"))
        dst_env = args.get("env")
        if dst_env is None and not (
            ":" in str(dst_spec) and str(dst_spec).split(":", 1)[0] in self.resolver.env_names()
        ):
            dst_env = src.env
        dst = self.resolver.parse(dst_spec, env=dst_env, root=args.get("root"))
        if src.env != dst.env:
            raise BadRequest(
                "src and dst are in different environments; use 'lfb sync' or copy files explicitly"
            )
        entry = src.backend.move(
            src.root,
            src.path,
            dst.root,
            dst.path,
            overwrite=bool(args.get("overwrite", False)),
        )
        entry["environment"] = src.env
        return entry

    def _delete(self, args: Dict[str, Any]) -> Dict[str, Any]:
        target = self._target(args)
        result = target.backend.remove(
            target.root, target.path, recursive=bool(args.get("recursive", False))
        )
        result["environment"] = target.env
        return result

    def _search(self, args: Dict[str, Any]) -> Dict[str, Any]:
        target = self._target(args)
        glob = args.get("glob")
        regex = args.get("regex")
        if not glob and not regex and not args.get("contains"):
            glob = "*"
        data = target.backend.search(
            target.root,
            target.path,
            glob=glob,
            regex=regex,
            contains=args.get("contains"),
            want_type=args.get("type", "all") or "all",
            max_results=int(args.get("max_results", 100) or 100),
            exclude=args.get("exclude") or [],
            show_hidden=bool(args.get("show_hidden", True)),
        )
        data["environment"] = target.env
        return data

    def _exec(self, args: Dict[str, Any]) -> Dict[str, Any]:
        target = self._target(args)
        argv = args.get("argv")
        command = args.get("command")
        if argv is not None and not isinstance(argv, list):
            raise BadRequest("argv must be an array of strings")
        if not argv and not command:
            raise BadRequest("provide argv or command")
        data = target.backend.exec_command(
            target.root,
            target.path,
            argv=[str(x) for x in argv] if argv else None,
            command=command,
            shell=bool(args.get("shell", False)),
            timeout=args.get("timeout"),
            max_output=args.get("max_output"),
        )
        data["environment"] = target.env
        return data
