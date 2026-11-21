"""Unit tests for the shared error types in lfb.errors.

The ``code`` of every error is part of the HTTP API / MCP contract, and
``from_payload`` is how the client rebuilds exceptions from a remote
bridge's response — a silent change here breaks error handling in every
consumer, so both directions are pinned down.
"""

import unittest

from lfb.errors import (
    BadRequest,
    ConfigError,
    Conflict,
    Forbidden,
    LfbError,
    NotFound,
    PathEscape,
    ReadOnly,
    RemoteError,
    TimeoutError_,
    TooLarge,
    Unauthorized,
    Unsupported,
    from_payload,
)


class ErrorContractTests(unittest.TestCase):
    def test_codes_and_statuses(self):
        cases = [
            (LfbError, "internal_error", 500),
            (ConfigError, "config_error", 400),
            (BadRequest, "bad_request", 400),
            (Unauthorized, "unauthorized", 401),
            (Forbidden, "forbidden", 403),
            (PathEscape, "path_escape", 403),
            (ReadOnly, "read_only", 403),
            (NotFound, "not_found", 404),
            (Conflict, "conflict", 409),
            (TooLarge, "too_large", 413),
            (Unsupported, "unsupported", 415),
            (RemoteError, "remote_error", 502),
            (TimeoutError_, "timeout", 504),
        ]
        for cls, code, status in cases:
            err = cls("boom")
            self.assertEqual(err.code, code, cls.__name__)
            self.assertEqual(err.status, status, cls.__name__)
            self.assertEqual(err.message, "boom")
            self.assertEqual(str(err), "boom")

    def test_forbidden_subclasses_inherit_status(self):
        self.assertTrue(issubclass(PathEscape, Forbidden))
        self.assertTrue(issubclass(ReadOnly, Forbidden))

    def test_code_and_status_override(self):
        err = BadRequest("nope", code="path_escape", status=403)
        self.assertEqual(err.code, "path_escape")
        self.assertEqual(err.status, 403)

    def test_to_dict_without_details(self):
        payload = NotFound("gone").to_dict()
        self.assertEqual(
            payload,
            {"ok": False, "error": {"code": "not_found", "message": "gone"}},
        )

    def test_to_dict_with_details(self):
        payload = TooLarge("big", details={"limit": 10}).to_dict()
        self.assertEqual(payload["error"]["details"], {"limit": 10})


class FromPayloadTests(unittest.TestCase):
    def test_round_trip_every_mapped_code(self):
        classes = [
            BadRequest, Unauthorized, Forbidden, PathEscape, ReadOnly,
            NotFound, Conflict, TooLarge, Unsupported, ConfigError,
            TimeoutError_, RemoteError,
        ]
        for cls in classes:
            original = cls("round trip", details={"k": "v"})
            rebuilt = from_payload(original.to_dict(), status=original.status)
            self.assertIsInstance(rebuilt, cls, cls.__name__)
            self.assertEqual(rebuilt.code, original.code)
            self.assertEqual(rebuilt.message, "round trip")
            self.assertEqual(rebuilt.details, {"k": "v"})

    def test_unknown_code_falls_back_to_base_error(self):
        payload = {"error": {"code": "brand_new_code", "message": "?"}}
        err = from_payload(payload, status=500)
        self.assertEqual(type(err), LfbError)
        self.assertEqual(err.code, "brand_new_code")
        self.assertEqual(err.status, 500)

    def test_missing_code_defaults_to_remote_error(self):
        err = from_payload({"error": {"message": "oops"}}, status=502)
        self.assertIsInstance(err, RemoteError)

    def test_malformed_payload(self):
        for payload in ({}, {"error": "nope"}, {"error": None}):
            err = from_payload(payload, status=503)
            self.assertEqual(type(err), LfbError)
            self.assertEqual(err.message, "unexpected response from bridge")
            self.assertEqual(err.status, 503)


if __name__ == "__main__":
    unittest.main()
