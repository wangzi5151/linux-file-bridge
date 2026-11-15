"""Configuration loading, validation and creation.

A single JSON file describes every environment the agent may talk to::

    {
      "version": 1,
      "default_env": "ubuntu",
      "environments": {
        "ubuntu": {
          "kind": "local",
          "roots": {"project": {"path": "/workspace/linux", "mode": "rw"}},
          "token": "...",
          "serve": {"host": "127.0.0.1", "port": 8790}
        },
        "ubuntu-dev": {
          "kind": "bridge",
          "url": "http://127.0.0.1:8791",
          "token": "..."
        }
      },
      "gateway": {"host": "127.0.0.1", "port": 8787, "token": "..."}
    }

The file contains bearer tokens, so it is written with mode ``0600``.
"""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import time
from typing import Any, Dict, List, Optional

from .errors import ConfigError
from .util import generate_token, parse_size

CONFIG_VERSION = 1
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
DEFAULT_HOST = "127.0.0.1"
DEFAULT_SERVE_PORT = 8790
DEFAULT_GATEWAY_PORT = 8787
DEFAULT_MAX_READ = 1024 * 1024
DEFAULT_MAX_UPLOAD = 512 * 1024 * 1024
DEFAULT_MAX_SEARCH = 200
DEFAULT_EXEC_TIMEOUT = 30
DEFAULT_EXEC_OUTPUT = 1024 * 1024


def default_config_path() -> str:
    override = os.environ.get("LFB_CONFIG")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = xdg if xdg else os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "lfb", "config.json")


def validate_name(value: str, what: str = "name") -> str:
    value = str(value or "").strip()
    if not NAME_RE.match(value):
        raise ConfigError(
            "invalid %s %r: use letters, digits, '.', '_' or '-' (max 64 chars)" % (what, value)
        )
    return value


def _normalize_root(name: str, raw: Any, default_mode: str = "rw") -> Dict[str, Any]:
    name = validate_name(name, "root name")
    if isinstance(raw, str):
        raw = {"path": raw}
    if not isinstance(raw, dict):
        raise ConfigError("root %r must be a string or an object" % name)
    path = raw.get("path")
    if not path:
        raise ConfigError("root %r is missing 'path'" % name)
    path = os.path.abspath(os.path.expanduser(str(path)))
    mode = str(raw.get("mode") or default_mode).lower()
    if mode not in ("ro", "rw"):
        raise ConfigError("root %r mode must be 'ro' or 'rw'" % name)
    result: Dict[str, Any] = {"path": path, "mode": mode}
    if raw.get("description"):
        result["description"] = str(raw["description"])
    if raw.get("max_read") is not None:
        result["max_read"] = parse_size(raw["max_read"], name="max_read")
    return result


def _normalize_exec(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}
    timeout = raw.get("timeout", DEFAULT_EXEC_TIMEOUT)
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        raise ConfigError("exec.timeout must be a number")
    if timeout <= 0 or timeout > 3600:
        raise ConfigError("exec.timeout must be between 0 and 3600 seconds")
    max_output = parse_size(raw.get("max_output", DEFAULT_EXEC_OUTPUT), name="exec.max_output")
    env = raw.get("env") or {}
    if not isinstance(env, dict):
        raise ConfigError("exec.env must be an object")
    return {
        "enabled": bool(raw.get("enabled", False)),
        "allow_shell": bool(raw.get("allow_shell", False)),
        "timeout": timeout,
        "max_output": int(max_output or DEFAULT_EXEC_OUTPUT),
        "env": {str(k): str(v) for k, v in env.items()},
    }


def _normalize_limits(raw: Any) -> Dict[str, int]:
    if not isinstance(raw, dict):
        raw = {}
    max_read = int(parse_size(raw.get("max_read", DEFAULT_MAX_READ), name="max_read") or DEFAULT_MAX_READ)
    max_upload = int(
        parse_size(raw.get("max_upload", DEFAULT_MAX_UPLOAD), name="max_upload")
        or DEFAULT_MAX_UPLOAD
    )
    try:
        max_search = int(raw.get("max_search_results", DEFAULT_MAX_SEARCH))
    except (TypeError, ValueError):
        raise ConfigError("limits.max_search_results must be an integer")
    if max_read <= 0 or max_upload <= 0 or max_search <= 0:
        raise ConfigError("limits must be positive")
    if max_search > 100000:
        raise ConfigError("limits.max_search_results is unreasonably large")
    return {
        "max_read": max_read,
        "max_upload": max_upload,
        "max_search_results": max_search,
    }


def _normalize_serve(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}
    host = str(raw.get("host") or DEFAULT_HOST)
    try:
        port = int(raw.get("port", DEFAULT_SERVE_PORT))
    except (TypeError, ValueError):
        raise ConfigError("serve.port must be an integer")
    if not 0 <= port <= 65535:
        raise ConfigError("serve.port must be between 0 and 65535")
    result: Dict[str, Any] = {"host": host, "port": port}
    for key in ("tls_cert", "tls_key"):
        if raw.get(key):
            result[key] = os.path.abspath(os.path.expanduser(str(raw[key])))
    return result


class Environment(object):
    """A named environment: either a local filesystem or a remote bridge."""

    def __init__(self, name: str, raw: Dict[str, Any]) -> None:
        self.name = validate_name(name, "environment")
        if not isinstance(raw, dict):
            raise ConfigError("environment %r must be an object" % name)
        kind = str(raw.get("kind") or ("bridge" if raw.get("url") else "local")).lower()
        if kind not in ("local", "bridge"):
            raise ConfigError("environment %r has unknown kind %r" % (name, kind))
        self.kind = kind
        self.raw = raw
        self.token = str(raw.get("token") or "")
        self.read_only = bool(raw.get("read_only", False))
        self.limits = _normalize_limits(raw.get("limits"))
        self.exec = _normalize_exec(raw.get("exec"))
        self.serve = _normalize_serve(raw.get("serve"))

        if kind == "local":
            roots_raw = raw.get("roots") or {}
            if isinstance(roots_raw, list):
                roots_raw = {os.path.basename(str(p).rstrip("/")) or "root": p for p in roots_raw}
            if not isinstance(roots_raw, dict) or not roots_raw:
                raise ConfigError("environment %r must define at least one root" % name)
            roots: Dict[str, Dict[str, Any]] = {}
            for root_name, root_raw in roots_raw.items():
                roots[root_name] = _normalize_root(root_name, root_raw)
            self.roots = roots
            self.url = ""
            self.verify_tls: Any = True
            self.timeout = 30.0
        else:
            url = str(raw.get("url") or "").strip()
            if not url:
                raise ConfigError("bridge environment %r is missing 'url'" % name)
            if not re.match(r"^https?://", url, re.IGNORECASE):
                raise ConfigError("bridge environment %r url must start with http:// or https://" % name)
            self.roots = {}
            self.url = url.rstrip("/")
            self.verify_tls = raw.get("verify_tls", True)
            try:
                self.timeout = float(raw.get("timeout", 30))
            except (TypeError, ValueError):
                raise ConfigError("bridge environment %r timeout must be a number" % name)

        # Keep a canonical copy for saving: absolute roots, normalized modes,
        # explicit kind and normalized limits.  Unknown keys are preserved.
        self.raw = copy.deepcopy(raw)
        self.raw["kind"] = kind
        self.raw["token"] = self.token
        self.raw["read_only"] = self.read_only
        self.raw["limits"] = self.limits
        self.raw["exec"] = self.exec
        if kind == "local":
            self.raw["roots"] = copy.deepcopy(self.roots)
            self.raw["serve"] = self.serve
        else:
            self.raw["url"] = self.url
            self.raw["verify_tls"] = self.verify_tls
            self.raw["timeout"] = self.timeout

    @property
    def root_names(self) -> List[str]:
        return list(self.roots.keys())

    @property
    def default_root(self) -> str:
        if not self.roots:
            return ""
        return next(iter(self.roots))

    def root(self, name: str) -> Dict[str, Any]:
        try:
            return self.roots[name]
        except KeyError:
            raise ConfigError(
                "environment %r has no root %r (available: %s)"
                % (self.name, name, ", ".join(self.root_names) or "none")
            )

    def to_public_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "name": self.name,
            "kind": self.kind,
            "read_only": self.read_only,
            "limits": self.limits,
            "exec": {
                "enabled": self.exec["enabled"],
                "allow_shell": self.exec["allow_shell"],
            },
        }
        if self.kind == "local":
            data["roots"] = {
                name: {
                    "path": value["path"],
                    "mode": "ro" if self.read_only else value["mode"],
                    "description": value.get("description", ""),
                }
                for name, value in self.roots.items()
            }
            data["serve"] = {"host": self.serve["host"], "port": self.serve["port"]}
        else:
            data["url"] = self.url
        return data


class Config(object):
    def __init__(self, path: str, raw: Dict[str, Any]) -> None:
        self.path = os.path.abspath(os.path.expanduser(path))
        if not isinstance(raw, dict):
            raise ConfigError("config root must be a JSON object")
        version = raw.get("version", CONFIG_VERSION)
        try:
            version_number = int(version)
        except (TypeError, ValueError):
            raise ConfigError("invalid config version %r" % (version,))
        if version_number != CONFIG_VERSION:
            raise ConfigError("unsupported config version %r (expected %d)" % (version, CONFIG_VERSION))
        envs_raw = raw.get("environments")
        if not isinstance(envs_raw, dict) or not envs_raw:
            raise ConfigError("config must define at least one environment")
        self.environments: Dict[str, Environment] = {}
        for name, env_raw in envs_raw.items():
            env = Environment(name, env_raw)
            self.environments[name] = env
        default_env = raw.get("default_env")
        if default_env:
            if default_env not in self.environments:
                raise ConfigError("default_env %r is not a configured environment" % default_env)
            self.default_env = str(default_env)
        else:
            self.default_env = next(iter(self.environments))
        gateway_raw = raw.get("gateway") or {}
        if not isinstance(gateway_raw, dict):
            raise ConfigError("gateway must be an object")
        try:
            gateway_port = int(gateway_raw.get("port", DEFAULT_GATEWAY_PORT))
        except (TypeError, ValueError):
            raise ConfigError("gateway.port must be an integer")
        if not 0 <= gateway_port <= 65535:
            raise ConfigError("gateway.port must be between 0 and 65535")
        self.gateway = {
            "host": str(gateway_raw.get("host") or DEFAULT_HOST),
            "port": gateway_port,
            "token": str(gateway_raw.get("token") or ""),
            "default_env": str(gateway_raw.get("default_env") or self.default_env),
        }
        for key in ("tls_cert", "tls_key"):
            if gateway_raw.get(key):
                self.gateway[key] = os.path.abspath(os.path.expanduser(str(gateway_raw[key])))
        self._raw_extra = {
            key: value
            for key, value in raw.items()
            if key not in ("version", "default_env", "environments", "gateway")
        }

    # ------------------------------------------------------------------ load
    @classmethod
    def load(cls, path: Optional[str] = None, must_exist: bool = True) -> "Config":
        target = os.path.abspath(os.path.expanduser(path or default_config_path()))
        if not os.path.exists(target):
            if must_exist:
                raise ConfigError(
                    "config not found: %s\nRun 'lfb init' to create one, or pass --config." % target
                )
            raise ConfigError("config not found: %s" % target)
        try:
            with open(target, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except json.JSONDecodeError as exc:
            raise ConfigError("invalid JSON in %s: %s" % (target, exc))
        except OSError as exc:
            raise ConfigError("cannot read %s: %s" % (target, exc))
        return cls(target, raw)

    # ------------------------------------------------------------------ save
    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "version": CONFIG_VERSION,
            "default_env": self.default_env,
            "environments": {name: env.raw for name, env in self.environments.items()},
            "gateway": self.gateway,
        }
        data.update(self._raw_extra)
        return data

    def save(self) -> None:
        directory = os.path.dirname(self.path) or "."
        created_directory = not os.path.isdir(directory)
        try:
            os.makedirs(directory, mode=0o700, exist_ok=True)
        except OSError as exc:
            raise ConfigError("cannot create config directory %s: %s" % (directory, exc))
        payload = json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n"
        fd, tmp = tempfile.mkstemp(prefix=".lfb-config-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        if created_directory:
            try:
                os.chmod(directory, 0o700)
            except OSError:
                pass

    # -------------------------------------------------------------- env mgmt
    def get_env(self, name: Optional[str] = None) -> Environment:
        target = name or self.default_env
        try:
            return self.environments[target]
        except KeyError:
            raise ConfigError(
                "unknown environment %r (available: %s)"
                % (target, ", ".join(self.environments.keys()))
            )

    def add_env(self, env: Environment, make_default: bool = False) -> None:
        if env.name in self.environments:
            raise ConfigError("environment %r already exists (use --force to replace)" % env.name)
        self.environments[env.name] = env
        if make_default or len(self.environments) == 1:
            self.default_env = env.name
            if not self.gateway.get("default_env") or self.gateway["default_env"] not in self.environments:
                self.gateway["default_env"] = env.name

    def remove_env(self, name: str) -> None:
        if name not in self.environments:
            raise ConfigError("unknown environment %r" % name)
        if len(self.environments) == 1:
            raise ConfigError("cannot remove the last environment")
        del self.environments[name]
        if self.default_env == name:
            self.default_env = next(iter(self.environments))
        if self.gateway.get("default_env") == name:
            self.gateway["default_env"] = self.default_env

    def set_default(self, name: str) -> None:
        self.get_env(name)
        self.default_env = name
        self.gateway["default_env"] = name

    def gateway_token(self) -> str:
        token = self.gateway.get("token") or ""
        if token:
            return token
        return self.get_env().token


def build_local_env(
    name: str,
    roots: Dict[str, Any],
    *,
    token: Optional[str] = None,
    read_only: bool = False,
    exec_enabled: bool = False,
    allow_shell: bool = False,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_SERVE_PORT,
) -> Environment:
    raw = {
        "kind": "local",
        "roots": roots,
        "token": token or generate_token(),
        "read_only": bool(read_only),
        "exec": {"enabled": bool(exec_enabled), "allow_shell": bool(allow_shell)},
        "serve": {"host": host, "port": port},
    }
    return Environment(name, raw)


def build_bridge_env(name: str, url: str, token: str, verify_tls: Any = True, timeout: float = 30) -> Environment:
    raw = {
        "kind": "bridge",
        "url": url,
        "token": token,
        "verify_tls": verify_tls,
        "timeout": timeout,
    }
    return Environment(name, raw)


def create_config(
    path: Optional[str],
    env_name: str,
    roots: Optional[Dict[str, Any]] = None,
    *,
    force: bool = False,
    token: Optional[str] = None,
    gateway_token: Optional[str] = None,
    gateway_port: int = DEFAULT_GATEWAY_PORT,
    exec_enabled: bool = False,
    allow_shell: bool = False,
) -> Config:
    target = os.path.abspath(os.path.expanduser(path or default_config_path()))
    if os.path.exists(target) and not force:
        raise ConfigError("config already exists: %s (use --force to overwrite)" % target)
    env_name = validate_name(env_name, "environment name")
    if roots is None:
        roots = {"project": {"path": os.getcwd()}}
    env = build_local_env(
        env_name,
        roots,
        token=token,
        exec_enabled=exec_enabled,
        allow_shell=allow_shell,
    )
    raw: Dict[str, Any] = {
        "version": CONFIG_VERSION,
        "default_env": env.name,
        "environments": {env.name: env.raw},
        "gateway": {
            "host": DEFAULT_HOST,
            "port": int(gateway_port),
            "token": gateway_token or generate_token(),
            "default_env": env.name,
        },
    }
    config = Config(target, raw)
    config.save()
    return config


def now_stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")
