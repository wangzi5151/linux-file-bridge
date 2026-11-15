"""Tests for the secure local filesystem layer."""

import os
import stat
import tempfile
import unittest

from lfb.config import Environment, build_local_env
from lfb.errors import BadRequest, Conflict, PathEscape, ReadOnly, TooLarge
from lfb.fsops import LocalFS


class LocalFSTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "project")
        os.mkdir(self.root)
        os.mkdir(os.path.join(self.root, "sub"))
        with open(os.path.join(self.root, "hello.txt"), "w", encoding="utf-8") as handle:
            handle.write("hello world\nline2\n")
        with open(os.path.join(self.root, "sub", "note.md"), "w", encoding="utf-8") as handle:
            handle.write("# note\nneedle here\n")
        with open(os.path.join(self.root, "bin.dat"), "wb") as handle:
            handle.write(b"\x00\x01\x02")
        self.env = build_local_env("test", {"project": self.root}, token="t" * 32)
        self.fs = LocalFS(self.env)

    def test_read_write_stat(self):
        data, entry, truncated = self.fs.read_bytes("project", "hello.txt")
        self.assertEqual(data, b"hello world\nline2\n")
        self.assertFalse(truncated)
        written = self.fs.write_bytes("project", "new.txt", b"abc")
        self.assertEqual(written["size"], 3)
        self.assertEqual(self.fs.read_bytes("project", "new.txt")[0], b"abc")
        self.assertEqual(self.fs.stat("project", "new.txt", want_hash=True)["sha256"],
                         "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")

    def test_read_offset_truncated(self):
        data, entry, truncated = self.fs.read_bytes("project", "hello.txt", offset=6, max_bytes=5)
        self.assertEqual(data, b"world")
        self.assertTrue(truncated)
        self.assertEqual(entry["size"], 18)

    def test_list_and_hidden(self):
        with open(os.path.join(self.root, ".hidden"), "w") as handle:
            handle.write("x")
        listing = self.fs.list_dir("project", "")
        paths = [e["path"] for e in listing["entries"]]
        self.assertIn("sub", paths)
        self.assertIn("sub/note.md", [e["path"] for e in self.fs.list_dir("project", "sub")["entries"]])
        self.assertIn(".hidden", paths)
        self.assertNotIn(
            ".hidden",
            [e["path"] for e in self.fs.list_dir("project", "", show_hidden=False)["entries"]],
        )

    def test_traversal_rejected(self):
        for path in ("../outside", "sub/../../outside"):
            with self.assertRaises(BadRequest) as ctx:
                self.fs.read_bytes("project", path)
            self.assertEqual(ctx.exception.code, "path_escape")
        with self.assertRaises(BadRequest):
            self.fs.write_bytes("project", "../escape.txt", b"x")

    def test_symlink_escape_rejected(self):
        link = os.path.join(self.root, "escape")
        try:
            os.symlink("/etc/passwd", link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available")
        with self.assertRaises(PathEscape):
            self.fs.read_bytes("project", "escape")
        with self.assertRaises(PathEscape):
            self.fs.write_bytes("project", "escape", b"overwrite")
        listing = self.fs.list_dir("project", "")
        entry = [e for e in listing["entries"] if e["name"] == "escape"][0]
        self.assertTrue(entry.get("blocked"))

    def test_symlink_inside_allowed(self):
        try:
            os.symlink("hello.txt", os.path.join(self.root, "inside"))
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available")
        data, entry, _ = self.fs.read_bytes("project", "inside")
        self.assertEqual(data, b"hello world\nline2\n")
        self.assertTrue(entry["symlink"])
        self.assertEqual(entry["symlink_target"], "hello.txt")

    def test_read_only(self):
        env = build_local_env("ro", {"project": self.root}, token="x" * 32, read_only=True)
        fs = LocalFS(env)
        with self.assertRaises(ReadOnly):
            fs.write_bytes("project", "nope.txt", b"x")
        self.assertEqual(fs.read_bytes("project", "hello.txt")[0], b"hello world\nline2\n")

    def test_mkdir_move_remove(self):
        self.fs.mkdir("project", "a/b/c", parents=True)
        self.fs.write_bytes("project", "a/b/c/file.txt", b"x")
        moved = self.fs.move("project", "a/b/c/file.txt", "project", "a/b/renamed.txt")
        self.assertEqual(moved["path"], "a/b/renamed.txt")
        with self.assertRaises(Conflict):
            self.fs.remove("project", "a", recursive=False)
        self.fs.remove("project", "a", recursive=True)
        self.assertFalse(os.path.exists(os.path.join(self.root, "a")))
        with self.assertRaises(BadRequest):
            self.fs.remove("project", "", recursive=True)

    def test_search_and_tree(self):
        result = self.fs.search("project", "", glob="*.txt")
        self.assertEqual([e["path"] for e in result["results"]], ["hello.txt"])
        result = self.fs.search("project", "", contains="needle")
        self.assertEqual(result["results"][0]["path"], "sub/note.md")
        self.assertEqual(result["results"][0]["line"], 2)
        result = self.fs.search("project", "", regex=r"sub/.*\.md")
        self.assertEqual(result["count"], 1)
        tree = self.fs.tree("project", "", depth=2)
        names = [e["name"] for e in tree["entries"]]
        self.assertIn("sub", names)
        self.assertIn("hello.txt", names)

    def test_binary_and_limit(self):
        data, entry, _ = self.fs.read_bytes("project", "bin.dat", max_bytes=2)
        self.assertEqual(data, b"\x00\x01")
        env = build_local_env("small", {"project": self.root}, token="s" * 32)
        env.raw["limits"] = {"max_read": 1024, "max_upload": 1024, "max_search_results": 10}
        small = LocalFS(Environment("small", env.raw))
        with self.assertRaises(TooLarge):
            small.write_bytes("project", "big.bin", b"x" * 2048)

    def test_exec_disabled(self):
        with self.assertRaises(Exception) as ctx:
            self.fs.exec_command("project", "", argv=["echo", "hi"])
        self.assertEqual(ctx.exception.code, "forbidden")

    def test_exec_enabled(self):
        env = build_local_env(
            "exec",
            {"project": self.root},
            token="e" * 32,
            exec_enabled=True,
            allow_shell=True,
        )
        fs = LocalFS(env)
        result = fs.exec_command("project", "", argv=["printf", "hello"])
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["stdout"], "hello")
        result = fs.exec_command("project", "", command="pwd", shell=True)
        self.assertEqual(result["stdout"].strip(), self.root)
        result = fs.exec_command("project", "", command="sleep 2", shell=True, timeout=0.2)
        self.assertTrue(result["timed_out"])


if __name__ == "__main__":
    unittest.main()
