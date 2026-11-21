"""Unit tests for ``env:root/path`` target parsing in lfb.targets.

Target parsing decides which environment and root every file operation
lands on, and it is the first line of defence against ``..`` escapes —
it had no tests before this file.
"""

import os
import tempfile
import unittest

from lfb.config import Config
from lfb.errors import BadRequest, ConfigError
from lfb.targets import Target, TargetResolver


class TargetResolverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = self.tmp.name
        self.project = os.path.join(base, "project")
        self.data = os.path.join(base, "data")
        self.home = os.path.join(base, "home")
        for path in (self.project, self.data, self.home):
            os.makedirs(path)
        raw = {
            "environments": {
                "ubuntu": {
                    "kind": "local",
                    "roots": {"project": self.project, "data": self.data},
                },
                "termux": {
                    "kind": "local",
                    "roots": {"home": self.home},
                },
            },
            "default_env": "ubuntu",
        }
        self.config = Config(os.path.join(base, "config.json"), raw)
        self.resolver = TargetResolver(self.config)

    def tearDown(self):
        self.resolver.close()
        self.tmp.cleanup()

    def test_parse_none_uses_default_env_and_root(self):
        target = self.resolver.parse(None)
        self.assertEqual(target.env, "ubuntu")
        self.assertEqual(target.root, self.resolver.backend("ubuntu").default_root)
        self.assertEqual(target.path, "")

    def test_parse_root_and_path(self):
        target = self.resolver.parse("project/src/main.py")
        self.assertEqual((target.env, target.root, target.path),
                         ("ubuntu", "project", "src/main.py"))

    def test_parse_env_prefix(self):
        target = self.resolver.parse("termux:home/notes.txt")
        self.assertEqual((target.env, target.root, target.path),
                         ("termux", "home", "notes.txt"))

    def test_parse_unknown_prefix_is_a_path_not_an_env(self):
        # "docs" is not an environment, so the whole spec stays a path
        # under the default environment/root.
        target = self.resolver.parse("docs:readme.md")
        self.assertEqual(target.env, "ubuntu")
        self.assertEqual(target.path, "docs:readme.md")

    def test_parse_explicit_env_argument(self):
        target = self.resolver.parse("home/a.txt", env="termux")
        self.assertEqual((target.env, target.root, target.path),
                         ("termux", "home", "a.txt"))

    def test_parse_explicit_root_argument(self):
        target = self.resolver.parse("data/file.bin", root="data")
        self.assertEqual((target.root, target.path), ("data", "file.bin"))
        # The root name may also be repeated as the first path segment.
        target = self.resolver.parse("file.bin", root="data")
        self.assertEqual((target.root, target.path), ("data", "file.bin"))

    def test_parse_normalizes_separators_and_dots(self):
        target = self.resolver.parse("project//a/./b/")
        self.assertEqual(target.path, "a/b")

    def test_parse_rejects_parent_escape(self):
        with self.assertRaises(BadRequest) as caught:
            self.resolver.parse("project/../secret.txt")
        self.assertEqual(caught.exception.code, "path_escape")
        self.assertEqual(caught.exception.status, 403)

    def test_parse_unknown_root_argument(self):
        with self.assertRaises(BadRequest):
            self.resolver.parse("x.txt", root="nope")

    def test_backend_unknown_env(self):
        with self.assertRaises(ConfigError):
            self.resolver.backend("nope")

    def test_backend_is_cached(self):
        self.assertIs(self.resolver.backend("ubuntu"),
                      self.resolver.backend("ubuntu"))

    def test_env_names(self):
        self.assertEqual(self.resolver.env_names(), ["ubuntu", "termux"])

    def test_label_and_repr(self):
        target = self.resolver.parse("project/src/main.py")
        self.assertEqual(target.label(), "ubuntu:project/src/main.py")
        self.assertIn("project", repr(target))
        bare = Target("ubuntu", self.resolver.backend("ubuntu"), "", "")
        self.assertEqual(bare.label(), "ubuntu")


if __name__ == "__main__":
    unittest.main()
