"""Uniform backend object for local and remote environments."""

from __future__ import annotations

from typing import Any, Dict, Optional

from .client import BridgeClient
from .config import Environment
from .fsops import LocalFS


class Backend(object):
    """A thin facade so the CLI, gateway and MCP server can treat every
    environment the same way."""

    def __init__(self, env: Environment) -> None:
        self.env = env
        self.name = env.name
        self.kind = env.kind
        if env.kind == "local":
            self.impl: Any = LocalFS(env)
        else:
            self.impl = BridgeClient(
                env.url,
                env.token,
                verify_tls=env.verify_tls,
                timeout=env.timeout,
                name=env.name,
            )

    # ------------------------------------------------------------------ info
    @property
    def read_only(self) -> bool:
        if self.kind == "local":
            return bool(self.impl.read_only)
        roots = self.roots_info()
        return bool(roots) and all(root.get("mode") == "ro" for root in roots.values())

    def roots_info(self) -> Dict[str, Any]:
        return self.impl.public_roots()

    @property
    def default_root(self) -> str:
        return self.impl.default_root

    @property
    def limits(self) -> Dict[str, int]:
        return self.impl.limits

    @property
    def exec_enabled(self) -> bool:
        if self.kind == "local":
            return bool(self.impl.exec_config.get("enabled"))
        return bool(self.impl.exec_enabled)

    @property
    def exec_allow_shell(self) -> bool:
        if self.kind == "local":
            return bool(self.impl.exec_config.get("allow_shell"))
        return bool(self.impl.exec_allow_shell)

    def health(self) -> Dict[str, Any]:
        if self.kind == "local":
            return {
                "environment": self.name,
                "kind": "local",
                "roots": len(self.roots_info()),
            }
        return self.impl.health()

    def describe(self) -> Dict[str, Any]:
        data = {
            "name": self.name,
            "kind": self.kind,
            "read_only": self.read_only,
            "roots": self.roots_info(),
            "exec_enabled": self.exec_enabled,
        }
        if self.kind == "bridge":
            data["url"] = self.env.url
        return data

    # ---------------------------------------------------------------- files
    def list_dir(self, root: Optional[str], path: str, **kwargs: Any) -> Dict[str, Any]:
        return self.impl.list_dir(root, path, **kwargs)

    def stat(self, root: Optional[str], path: str, **kwargs: Any) -> Dict[str, Any]:
        return self.impl.stat(root, path, **kwargs)

    def read_bytes(self, root: Optional[str], path: str, **kwargs: Any) -> Any:
        return self.impl.read_bytes(root, path, **kwargs)

    def open_read(self, root: Optional[str], path: str) -> Any:
        return self.impl.open_read(root, path)

    def write_bytes(self, root: Optional[str], path: str, data: bytes, **kwargs: Any) -> Dict[str, Any]:
        return self.impl.write_bytes(root, path, data, **kwargs)

    def write_stream(self, root: Optional[str], path: str, stream: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.impl.write_stream(root, path, stream, **kwargs)

    def mkdir(self, root: Optional[str], path: str, **kwargs: Any) -> Dict[str, Any]:
        return self.impl.mkdir(root, path, **kwargs)

    def move(
        self,
        root: Optional[str],
        src: str,
        dst_root: Optional[str],
        dst: str,
        **kwargs: Any
    ) -> Dict[str, Any]:
        return self.impl.move(root, src, dst_root, dst, **kwargs)

    def remove(self, root: Optional[str], path: str, **kwargs: Any) -> Dict[str, Any]:
        return self.impl.remove(root, path, **kwargs)

    def search(self, root: Optional[str], path: str, **kwargs: Any) -> Dict[str, Any]:
        return self.impl.search(root, path, **kwargs)

    def tree(self, root: Optional[str], path: str, **kwargs: Any) -> Dict[str, Any]:
        return self.impl.tree(root, path, **kwargs)

    def exec_command(self, root: Optional[str], path: str, **kwargs: Any) -> Dict[str, Any]:
        return self.impl.exec_command(root, path, **kwargs)

    def close(self) -> None:
        return None


def open_backend(env: Environment) -> Backend:
    return Backend(env)
