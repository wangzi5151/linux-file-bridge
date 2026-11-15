"""HTTP bridge and gateway integration tests."""

import json
import os
import threading
import time
import unittest
import urllib.error
import urllib.request

from lfb.api import make_server, server_url
from lfb.client import BridgeClient
from lfb.config import Config, build_local_env
from lfb.errors import PathEscape, Unauthorized
from lfb.fsops import LocalFS
from lfb.gateway import make_gateway_server


def _request(url, token=None, method="GET", data=None, headers=None):
    request_headers = dict(headers or {})
    if token:
        request_headers["Authorization"] = "Bearer %s" % token
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        response = urllib.request.urlopen(request, timeout=10)
        return response.status, dict(response.getheaders()), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


class BridgeAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tempfile

        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = os.path.join(cls.tmp.name, "project")
        os.mkdir(cls.root)
        os.mkdir(os.path.join(cls.root, "sub"))
        with open(os.path.join(cls.root, "hello.txt"), "w", encoding="utf-8") as handle:
            handle.write("hello world\nline2\n")
        with open(os.path.join(cls.root, "bin.dat"), "wb") as handle:
            handle.write(b"\x00\x01\x02")
        cls.token = "test-token-" + "x" * 24
        env = build_local_env("api", {"project": cls.root}, token=cls.token)
        cls.fs = LocalFS(env)
        cls.httpd = make_server(cls.fs, cls.token, host="127.0.0.1", port=0, quiet=True)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = server_url(cls.httpd, "127.0.0.1")
        cls.client = BridgeClient(cls.base, cls.token, name="api")

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        cls.tmp.cleanup()

    def test_health_without_auth(self):
        status, _headers, body = _request(self.base + "/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["data"]["environment"], "api")

    def test_authentication(self):
        status, _headers, _body = _request(self.base + "/v1/roots")
        self.assertEqual(status, 401)
        with self.assertRaises(Unauthorized):
            BridgeClient(self.base, "wrong-token", name="bad").public_roots()

    def test_roots_and_list(self):
        roots = self.client.public_roots()
        self.assertEqual(roots["project"]["path"], self.root)
        listing = self.client.list_dir("project", "")
        paths = [entry["path"] for entry in listing["entries"]]
        self.assertIn("hello.txt", paths)
        self.assertIn("sub", paths)

    def test_read_write_and_stat(self):
        data, entry, truncated = self.client.read_bytes("project", "hello.txt")
        self.assertEqual(data, b"hello world\nline2\n")
        self.assertFalse(truncated)
        written = self.client.write_bytes("project", "new.txt", b"new content\n")
        self.assertEqual(written["size"], 12)
        self.assertEqual(self.client.read_bytes("project", "new.txt")[0], b"new content\n")
        self.assertEqual(self.client.stat("project", "new.txt")["type"], "file")

    def test_binary_roundtrip(self):
        data, _entry, _ = self.client.read_bytes("project", "bin.dat")
        self.assertEqual(data, b"\x00\x01\x02")
        self.client.write_bytes("project", "copy.dat", data)
        self.assertEqual(self.client.read_bytes("project", "copy.dat")[0], b"\x00\x01\x02")

    def test_mkdir_move_remove(self):
        self.client.mkdir("project", "a/b", parents=True)
        self.client.write_bytes("project", "a/b/x.txt", b"x")
        self.client.move("project", "a/b/x.txt", None, "a/y.txt")
        self.assertEqual(self.client.read_bytes("project", "a/y.txt")[0], b"x")
        self.client.remove("project", "a", recursive=True)
        self.assertFalse(os.path.exists(os.path.join(self.root, "a")))

    def test_search_tree_and_hash(self):
        results = self.client.search("project", "", glob="*.txt")
        self.assertTrue(any(entry["path"] == "hello.txt" for entry in results["results"]))
        tree = self.client.tree("project", "", depth=2)
        self.assertTrue(any(entry["name"] == "sub" for entry in tree["entries"]))
        entry = self.client.stat("project", "hello.txt", want_hash=True)
        self.assertEqual(len(entry["sha256"]), 64)

    def test_range_download(self):
        status, headers, body = _request(
            self.base + "/v1/download?root=project&path=hello.txt",
            token=self.token,
            headers={"Range": "bytes=0-4"},
        )
        self.assertEqual(status, 206)
        self.assertEqual(body, b"hello")
        self.assertEqual(headers["Content-Range"], "bytes 0-4/18")

    def test_traversal_and_symlink(self):
        with self.assertRaises(PathEscape):
            self.client.read_bytes("project", "../../etc/passwd")
        try:
            os.symlink("/etc/passwd", os.path.join(self.root, "escape"))
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available")
        self.addCleanup(lambda: os.path.exists(os.path.join(self.root, "escape")) and os.unlink(os.path.join(self.root, "escape")))
        with self.assertRaises(PathEscape):
            self.client.stat("project", "escape")

    def test_mtime_preserved(self):
        self.client.write_bytes("project", "mtime.txt", b"x", mtime=1000000000.0)
        self.assertEqual(int(self.client.stat("project", "mtime.txt")["mtime"]), 1000000000)

    def test_large_upload(self):
        data = os.urandom(256 * 1024)
        self.client.write_bytes("project", "large.bin", data)
        self.assertEqual(self.client.read_bytes("project", "large.bin")[0], data)


class GatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tempfile

        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = os.path.join(cls.tmp.name, "project")
        cls.root2 = os.path.join(cls.tmp.name, "project2")
        os.mkdir(cls.root)
        os.mkdir(cls.root2)
        with open(os.path.join(cls.root, "a.txt"), "w") as handle:
            handle.write("local a\n")
        with open(os.path.join(cls.root2, "b.txt"), "w") as handle:
            handle.write("remote b\n")
        cls.bridge_token = "bridge-" + "b" * 24
        cls.gateway_token = "gateway-" + "g" * 24
        env = build_local_env("remote", {"project": cls.root2}, token=cls.bridge_token)
        cls.bridge_httpd = make_server(LocalFS(env), cls.bridge_token, host="127.0.0.1", port=0, quiet=True)
        cls.bridge_thread = threading.Thread(target=cls.bridge_httpd.serve_forever, daemon=True)
        cls.bridge_thread.start()
        cls.bridge_url = server_url(cls.bridge_httpd, "127.0.0.1")

        raw = {
            "version": 1,
            "default_env": "local",
            "environments": {
                "local": {"kind": "local", "roots": {"project": cls.root}, "token": "l" * 32},
                "remote": {
                    "kind": "bridge",
                    "url": cls.bridge_url,
                    "token": cls.bridge_token,
                },
            },
            "gateway": {
                "host": "127.0.0.1",
                "port": 0,
                "token": cls.gateway_token,
                "default_env": "local",
            },
        }
        cls.config = Config(os.path.join(cls.tmp.name, "config.json"), raw)
        cls.gateway_httpd = make_gateway_server(
            cls.config, host="127.0.0.1", port=0, quiet=True
        )
        cls.gateway_thread = threading.Thread(target=cls.gateway_httpd.serve_forever, daemon=True)
        cls.gateway_thread.start()
        cls.gateway_url = server_url(cls.gateway_httpd, "127.0.0.1")

    @classmethod
    def tearDownClass(cls):
        for httpd, thread in (
            (cls.gateway_httpd, cls.gateway_thread),
            (cls.bridge_httpd, cls.bridge_thread),
        ):
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)
        cls.tmp.cleanup()

    def test_envs(self):
        status, _headers, body = _request(
            self.gateway_url + "/v1/envs", token=self.gateway_token
        )
        self.assertEqual(status, 200)
        data = json.loads(body)["data"]
        names = [item["name"] for item in data["environments"]]
        self.assertEqual(sorted(names), ["local", "remote"])
        self.assertEqual(data["default_env"], "local")

    def test_local_and_remote_read(self):
        status, _headers, body = _request(
            self.gateway_url + "/v1/read?env=local&root=project&path=a.txt",
            token=self.gateway_token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["data"]["content"], "local a\n")
        status, _headers, body = _request(
            self.gateway_url + "/v1/read?env=remote&root=project&path=b.txt",
            token=self.gateway_token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["data"]["content"], "remote b\n")

    def test_remote_env_header_and_write(self):
        status, _headers, body = _request(
            self.gateway_url + "/v1/write?root=project&path=written.txt",
            token=self.gateway_token,
            method="PUT",
            data=b"through gateway\n",
            headers={"X-LFB-Env": "remote", "Content-Type": "application/octet-stream"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(os.path.exists(os.path.join(self.root2, "written.txt")))
        status, _headers, body = _request(
            self.gateway_url + "/v1/read?root=project&path=written.txt",
            token=self.gateway_token,
            headers={"X-LFB-Env": "remote"},
        )
        self.assertEqual(json.loads(body)["data"]["content"], "through gateway\n")

    def test_default_env_and_unknown_env(self):
        status, _headers, body = _request(
            self.gateway_url + "/v1/read?root=project&path=a.txt", token=self.gateway_token
        )
        self.assertEqual(status, 200)
        status, _headers, _body = _request(
            self.gateway_url + "/v1/list?env=missing&root=project", token=self.gateway_token
        )
        self.assertEqual(status, 404)

    def test_gateway_requires_auth(self):
        status, _headers, _body = _request(self.gateway_url + "/v1/envs")
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()
