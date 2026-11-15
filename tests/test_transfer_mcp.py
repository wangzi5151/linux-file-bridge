"""Tests for transfer, mirroring and MCP tool dispatch."""

import io
import json
import os
import tempfile
import unittest

from lfb.config import Config
from lfb.mcp import McpServer
from lfb.targets import TargetResolver
from lfb import transfer


def _make_config(root, tmp_name):
    raw = {
        "version": 1,
        "default_env": "a",
        "environments": {
            "a": {"kind": "local", "roots": {"project": root + "/a"}, "token": "a" * 32},
            "b": {"kind": "local", "roots": {"project": root + "/b"}, "token": "b" * 32},
        },
        "gateway": {"token": "g" * 32, "default_env": "a"},
    }
    return Config(os.path.join(tmp_name, "config.json"), raw)


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.a = os.path.join(self.tmp.name, "a")
        self.b = os.path.join(self.tmp.name, "b")
        os.makedirs(os.path.join(self.a, "sub"))
        os.makedirs(self.b)
        with open(os.path.join(self.a, "one.txt"), "w") as handle:
            handle.write("one\n")
        with open(os.path.join(self.a, "sub", "two.txt"), "w") as handle:
            handle.write("two\n")
        os.utime(os.path.join(self.a, "one.txt"), (1000000000, 1000000000))
        self.config = _make_config(self.tmp.name, self.tmp.name)
        self.resolver = TargetResolver(self.config)

    def test_sync_copies_and_skips(self):
        src = self.resolver.parse("a:project")
        dst = self.resolver.parse("b:project")
        stats = transfer.sync(src, dst)
        self.assertEqual(stats["files_copied"], 2)
        self.assertTrue(os.path.exists(os.path.join(self.b, "one.txt")))
        self.assertTrue(os.path.exists(os.path.join(self.b, "sub", "two.txt")))
        self.assertEqual(
            int(os.path.getmtime(os.path.join(self.b, "one.txt"))), 1000000000
        )
        stats = transfer.sync(src, dst)
        self.assertEqual(stats["files_copied"], 0)
        self.assertEqual(stats["files_skipped"], 2)

    def test_sync_delete(self):
        dst = self.resolver.parse("b:project")
        src = self.resolver.parse("a:project")
        with open(os.path.join(self.b, "extra.txt"), "w") as handle:
            handle.write("extra\n")
        os.makedirs(os.path.join(self.b, "extradir"))
        # Deleting at a root is refused unless explicitly allowed.
        with self.assertRaises(Exception):
            transfer.sync(src, dst, delete=True)
        stats = transfer.sync(src, dst, delete=True, allow_root_delete=True)
        self.assertEqual(stats["deleted"], 2)
        self.assertFalse(os.path.exists(os.path.join(self.b, "extra.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.b, "extradir")))

    def test_sync_dry_run_and_checksum(self):
        src = self.resolver.parse("a:project")
        dst = self.resolver.parse("b:project")
        stats = transfer.sync(src, dst, dry_run=True)
        self.assertEqual(stats["files_copied"], 2)
        self.assertFalse(os.path.exists(os.path.join(self.b, "one.txt")))
        transfer.sync(src, dst)
        stats = transfer.sync(src, dst, checksum=True)
        self.assertEqual(stats["files_copied"], 0)

    def test_download_and_upload(self):
        src = self.resolver.parse("a:project")
        local = os.path.join(self.tmp.name, "download")
        stats = transfer.download(src, local, recursive=True)
        self.assertEqual(stats["files_copied"], 2)
        with open(os.path.join(local, "one.txt")) as handle:
            self.assertEqual(handle.read(), "one\n")
        dst = self.resolver.parse("b:project")
        stats = transfer.upload(local, dst, recursive=True)
        self.assertEqual(stats["files_copied"], 2)
        with open(os.path.join(self.b, "sub", "two.txt")) as handle:
            self.assertEqual(handle.read(), "two\n")

    def test_file_copy_to_directory(self):
        src = self.resolver.parse("a:project/one.txt")
        dst = self.resolver.parse("b:project")
        stats = transfer.sync(src, dst)
        self.assertEqual(stats["files_copied"], 1)
        self.assertTrue(os.path.exists(os.path.join(self.b, "one.txt")))

    def test_delete_respects_excludes(self):
        os.makedirs(os.path.join(self.a, ".git"))
        with open(os.path.join(self.a, ".git", "config"), "w") as handle:
            handle.write("source")
        src = self.resolver.parse("a:project")
        dst = self.resolver.parse("b:project")
        transfer.sync(src, dst)
        with open(os.path.join(self.b, ".git", "extra"), "w") as handle:
            handle.write("extra")
        transfer.sync(src, dst, delete=True, allow_root_delete=True, excludes=[".git"])
        self.assertTrue(os.path.exists(os.path.join(self.b, ".git", "extra")))

    def test_symlinks_are_skipped(self):
        try:
            os.symlink("/etc/passwd", os.path.join(self.a, "escape"))
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available")
        src = self.resolver.parse("a:project")
        dst = self.resolver.parse("b:project")
        stats = transfer.sync(src, dst)
        self.assertGreaterEqual(stats["files_skipped"], 1)
        self.assertFalse(os.path.exists(os.path.join(self.b, "escape")))


class McpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "a")
        os.makedirs(os.path.join(self.root, "sub"))
        self.broot = os.path.join(self.tmp.name, "b")
        os.makedirs(self.broot)
        with open(os.path.join(self.root, "hello.txt"), "w") as handle:
            handle.write("hello mcp\n")
        self.config = _make_config(self.tmp.name, self.tmp.name)
        self.server = McpServer(self.config, quiet=True)

    def call(self, name, arguments=None):
        response = self.server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            }
        )
        return response["result"]

    def test_initialize_and_tools(self):
        response = self.server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05"},
            }
        )
        self.assertEqual(response["result"]["protocolVersion"], "2024-11-05")
        response = self.server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = [tool["name"] for tool in response["result"]["tools"]]
        self.assertIn("lfb_read", names)
        self.assertIn("lfb_write", names)

    def test_list_read_write(self):
        result = self.call("lfb_list", {"env": "a", "root": "project", "path": ""})
        self.assertFalse(result["isError"])
        data = json.loads(result["content"][0]["text"])
        self.assertIn("hello.txt", [entry["name"] for entry in data["entries"]])

        result = self.call("lfb_read", {"env": "a", "root": "project", "path": "hello.txt"})
        self.assertEqual(json.loads(result["content"][0]["text"])["content"], "hello mcp\n")

        result = self.call(
            "lfb_write",
            {"env": "a", "root": "project", "path": "new.txt", "content": "new\n"},
        )
        self.assertFalse(result["isError"])
        result = self.call("lfb_read", {"env": "a", "root": "project", "path": "new.txt"})
        self.assertEqual(json.loads(result["content"][0]["text"])["content"], "new\n")

    def test_search_stat_delete(self):
        result = self.call(
            "lfb_search", {"env": "a", "root": "project", "path": "", "glob": "*.txt"}
        )
        data = json.loads(result["content"][0]["text"])
        self.assertTrue(data["results"])
        result = self.call("lfb_stat", {"env": "a", "root": "project", "path": "hello.txt"})
        self.assertEqual(json.loads(result["content"][0]["text"])["type"], "file")
        with open(os.path.join(self.root, "delete-me.txt"), "w") as handle:
            handle.write("delete me")
        result = self.call(
            "lfb_delete", {"env": "a", "root": "project", "path": "delete-me.txt"}
        )
        self.assertFalse(result["isError"])

    def test_traversal_tool_error(self):
        result = self.call(
            "lfb_read", {"env": "a", "root": "project", "path": "../../etc/passwd"}
        )
        self.assertTrue(result["isError"])
        self.assertIn("path", result["content"][0]["text"])

    def test_run_transport(self):
        stdin = io.StringIO(
            "\n".join(
                [
                    json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
                    json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}),
                ]
            )
            + "\n"
        )
        stdout = io.StringIO()
        self.assertEqual(self.server.run(stdin=stdin, stdout=stdout), 0)
        lines = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[1]["result"], {})


if __name__ == "__main__":
    unittest.main()
