"""CLI smoke tests (in-process, no network)."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

from lfb.cli import main


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "project")
        self.other = os.path.join(self.tmp.name, "other")
        os.makedirs(os.path.join(self.root, "sub"))
        os.makedirs(self.other)
        with open(os.path.join(self.root, "a.txt"), "w") as handle:
            handle.write("alpha\n")
        with open(os.path.join(self.root, "sub", "b.py"), "w") as handle:
            handle.write("print('b')\n")
        self.config = os.path.join(self.tmp.name, "config.json")

    def run_cli(self, *args, stdin=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        old_stdin = sys.stdin
        if stdin is not None:
            sys.stdin = type("FakeStdin", (), {"buffer": io.BytesIO(stdin), "read": lambda self: stdin.decode()})()
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = main(list(args))
        finally:
            sys.stdin = old_stdin
        return code, stdout.getvalue(), stderr.getvalue()

    def test_init_and_commands(self):
        code, out, _ = self.run_cli(
            "init",
            "--config",
            self.config,
            "--name",
            "test",
            "--root",
            "project=%s" % self.root,
        )
        self.assertEqual(code, 0)
        self.assertIn("test (local)", out)
        self.assertTrue(os.path.exists(self.config))

        code, out, _ = self.run_cli("--config", self.config, "env", "list")
        self.assertEqual(code, 0)
        self.assertIn("test", out)

        code, out, _ = self.run_cli("--config", self.config, "ls", "project", "-l")
        self.assertEqual(code, 0)
        self.assertIn("a.txt", out)

        code, out, _ = self.run_cli("--config", self.config, "cat", "project/a.txt")
        self.assertEqual(code, 0)
        self.assertEqual(out, "alpha\n")

        code, out, _ = self.run_cli("--config", self.config, "stat", "project/a.txt", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["type"], "file")

        code, out, _ = self.run_cli("--config", self.config, "find", "project", "--glob", "*.py")
        self.assertEqual(code, 0)
        self.assertIn("sub/b.py", out)

        code, out, _ = self.run_cli("--config", self.config, "mkdir", "project/newdir")
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isdir(os.path.join(self.root, "newdir")))

        code, _, _ = self.run_cli(
            "--config", self.config, "mv", "project/a.txt", "project/newdir/a2.txt"
        )
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(os.path.join(self.root, "newdir", "a2.txt")))

        code, _, _ = self.run_cli(
            "--config", self.config, "rm", "project/newdir", "--recursive"
        )
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(os.path.join(self.root, "newdir")))

    def test_put_get_and_sync(self):
        self.run_cli(
            "init",
            "--config",
            self.config,
            "--name",
            "test",
            "--root",
            "project=%s" % self.root,
        )
        self.run_cli(
            "--config",
            self.config,
            "env",
            "add",
            "other",
            "--root",
            "project=%s" % self.other,
        )
        source = os.path.join(self.tmp.name, "upload.txt")
        with open(source, "w") as handle:
            handle.write("uploaded\n")
        code, out, err = self.run_cli(
            "--config", self.config, "put", source, "test:project/upload.txt"
        )
        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.exists(os.path.join(self.root, "upload.txt")))

        destination = os.path.join(self.tmp.name, "download.txt")
        code, out, err = self.run_cli(
            "--config", self.config, "get", "test:project/upload.txt", destination
        )
        self.assertEqual(code, 0, err)
        with open(destination) as handle:
            self.assertEqual(handle.read(), "uploaded\n")

        code, out, err = self.run_cli(
            "--config", self.config, "sync", "test:project", "other:project"
        )
        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.exists(os.path.join(self.other, "upload.txt")))
        self.assertTrue(os.path.exists(os.path.join(self.other, "sub", "b.py")))

        # The copy/cp aliases share the sync implementation.
        code, out, err = self.run_cli(
            "--config", self.config, "cp", "test:project/a.txt", "other:project/a.txt"
        )
        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.exists(os.path.join(self.other, "a.txt")))

    def test_doctor_and_errors(self):
        self.run_cli(
            "init",
            "--config",
            self.config,
            "--name",
            "test",
            "--root",
            "project=%s" % self.root,
        )
        code, out, _ = self.run_cli("--config", self.config, "doctor")
        self.assertEqual(code, 0)
        self.assertIn("OK", out)

        code, _, err = self.run_cli("--config", self.config, "cat", "project/missing.txt")
        self.assertEqual(code, 1)
        self.assertIn("not_found", err)

        code, _, err = self.run_cli(
            "--config", self.config, "ls", "--root", "nope", "project"
        )
        self.assertEqual(code, 1)

    def test_mcp_subprocess(self):
        self.run_cli(
            "init",
            "--config",
            self.config,
            "--name",
            "test",
            "--root",
            "project=%s" % self.root,
        )
        process = subprocess.Popen(
            [sys.executable, "-m", "lfb", "--config", self.config, "mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        )
        try:
            process.stdin.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {"protocolVersion": "2024-11-05"},
                    }
                )
                + "\n"
            )
            process.stdin.flush()
            response = json.loads(process.stdout.readline())
            self.assertEqual(response["result"]["serverInfo"]["name"], "linux-file-bridge")
            process.stdin.close()
            process.wait(timeout=10)
        finally:
            for stream in (process.stdout, process.stderr):
                try:
                    stream.close()
                except Exception:
                    pass
            if process.poll() is None:
                process.kill()


if __name__ == "__main__":
    unittest.main()
