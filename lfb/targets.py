"""Parsing of ``env:root/path`` target specifications."""

from __future__ import annotations

from typing import Dict, Optional

from .backends import Backend, open_backend
from .config import Config
from .errors import BadRequest, ConfigError


class Target(object):
    __slots__ = ("env", "backend", "root", "path")

    def __init__(self, env: str, backend: Backend, root: str, path: str) -> None:
        self.env = env
        self.backend = backend
        self.root = root
        self.path = path

    def label(self) -> str:
        suffix = "/".join(part for part in (self.root, self.path) if part)
        return "%s:%s" % (self.env, suffix) if suffix else self.env

    def __repr__(self) -> str:
        return "Target(%r, root=%r, path=%r)" % (self.env, self.root, self.path)


class TargetResolver(object):
    def __init__(self, config: Config) -> None:
        self.config = config
        self._backends: Dict[str, Backend] = {}

    def backend(self, name: Optional[str] = None) -> Backend:
        env_name = name or self.config.default_env
        if env_name not in self.config.environments:
            raise ConfigError(
                "unknown environment %r (available: %s)"
                % (env_name, ", ".join(self.config.environments.keys()))
            )
        if env_name not in self._backends:
            self._backends[env_name] = open_backend(self.config.environments[env_name])
        return self._backends[env_name]

    def env_names(self) -> list:
        return list(self.config.environments.keys())

    def parse(
        self,
        spec: Optional[str],
        *,
        env: Optional[str] = None,
        root: Optional[str] = None,
    ) -> Target:
        raw = "" if spec is None else str(spec)
        env_name = env or self.config.default_env
        rest = raw

        # An explicit env argument wins.  Otherwise accept env:root/path.
        if env is None and ":" in raw:
            prefix, after = raw.split(":", 1)
            if prefix in self.config.environments:
                env_name = prefix
                rest = after

        backend = self.backend(env_name)
        rest = rest.lstrip("/")
        roots = backend.roots_info()
        if not roots:
            raise BadRequest("environment %r exposes no roots" % env_name)
        if root is None:
            first, separator, tail = rest.partition("/")
            if first and first in roots:
                chosen_root = first
                rest = tail
            else:
                chosen_root = backend.default_root
        else:
            if root not in roots:
                raise BadRequest(
                    "environment %r has no root %r (available: %s)"
                    % (env_name, root, ", ".join(roots.keys()))
                )
            chosen_root = root
            prefix, separator, tail = rest.partition("/")
            if prefix == chosen_root:
                rest = tail
        # Keep empty segments out, but preserve the intent of a directory path.
        rest = "/".join(part for part in rest.split("/") if part not in ("", "."))
        if any(part == ".." for part in rest.split("/") if part):
            raise BadRequest("path must not contain '..'", code="path_escape", status=403)
        return Target(env_name, backend, chosen_root, rest)

    def describe_all(self) -> Dict[str, object]:
        result: Dict[str, object] = {}
        for name in self.config.environments:
            backend = self.backend(name)
            result[name] = backend.describe()
        return result

    def close(self) -> None:
        for backend in self._backends.values():
            backend.close()
