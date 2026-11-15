"""Unit tests for helpers and configuration."""

import os
import tempfile
import unittest

from lfb.config import Config, Environment, build_local_env, create_config, default_config_path
from lfb.errors import BadRequest, ConfigError
from lfb.util import human_size, is_text_bytes, parse_size, split_rel_path


class UtilTests(unittest.TestCase):
    def test_parse_size(self):
        self.assertEqual(parse_size("1024"), 1024)
        self.assertEqual(parse_size("2K"), 2048)
        self.assertEqual(parse_size("1.5MiB"), int(1.5 * 1024 * 1024))
        self.assertEqual(parse_size("3G"), 3 * 1024 ** 3)
        self.assertIsNone(parse_size("none"))
        with self.assertRaises(BadRequest):
            parse_size("12wat")

    def test_human_size(self):
        self.assertEqual(human_size(0), "0 B")
        self.assertEqual(human_size(1536), "1.5 KiB")

    def test_split_rel_path(self):
        self.assertEqual(split_rel_path("a/b"), ["a", "b"])
        self.assertEqual(split_rel_path("/a//b/"), ["a", "b"])
        with self.assertRaises(BadRequest):
            split_rel_path("../etc/passwd")
        with self.assertRaises(BadRequest):
            split_rel_path("a\x00b")

    def test_text_detection(self):
        self.assertTrue(is_text_bytes(b"hello\n"))
        self.assertFalse(is_text_bytes(b"\x00\x01"))
        self.assertFalse(is_text_bytes(b"\xff\xfe"))


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "config.json")
        self.root = os.path.join(self.tmp.name, "project")
        os.mkdir(self.root)

    def test_create_and_load(self):
        config = create_config(self.path, "ubuntu", {"project": self.root})
        self.assertTrue(os.path.exists(self.path))
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        loaded = Config.load(self.path)
        env = loaded.get_env("ubuntu")
        self.assertEqual(env.kind, "local")
        self.assertEqual(env.roots["project"]["path"], os.path.abspath(self.root))
        self.assertTrue(env.token)
        self.assertTrue(loaded.gateway["token"])
        self.assertEqual(loaded.gateway_token(), loaded.gateway["token"])

    def test_create_refuses_overwrite(self):
        create_config(self.path, "ubuntu", {"project": self.root})
        with self.assertRaises(ConfigError):
            create_config(self.path, "ubuntu", {"project": self.root})

    def test_environment_modes(self):
        env = Environment(
            "a",
            {
                "kind": "local",
                "roots": {"project": {"path": self.root, "mode": "ro"}},
                "read_only": False,
            },
        )
        self.assertEqual(env.roots["project"]["mode"], "ro")
        public = env.to_public_dict()
        self.assertEqual(public["roots"]["project"]["mode"], "ro")

    def test_bridge_requires_url(self):
        with self.assertRaises(ConfigError):
            Environment("b", {"kind": "bridge", "url": ""})

    def test_default_env(self):
        raw = {
            "version": 1,
            "default_env": "a",
            "environments": {
                "a": {"kind": "local", "roots": {"r": self.root}, "token": "x"},
                "b": {"kind": "bridge", "url": "http://127.0.0.1:1", "token": "y"},
            },
        }
        config = Config(self.path, raw)
        self.assertEqual(config.default_env, "a")
        config.set_default("b")
        self.assertEqual(config.default_env, "b")
        with self.assertRaises(ConfigError):
            config.get_env("missing")


if __name__ == "__main__":
    unittest.main()
