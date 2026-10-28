import argparse
import importlib.util
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "yuanbao_search.py"
spec = importlib.util.spec_from_file_location("yuanbao_search", SCRIPT)
search = importlib.util.module_from_spec(spec)
spec.loader.exec_module(search)


class SearchTests(unittest.TestCase):
    def test_normalizes_provider_json_strings_and_compacts_content(self):
        body = {"Response": {"Pages": [json.dumps({"title": "Test", "url": "https://example.org", "passage": "x" * 800, "content": "long"})], "Version": "standard", "RequestId": "id"}}
        result = search.normalize_response(body)
        self.assertEqual(result["result_count"], 1)
        self.assertEqual(result["results"][0]["content"], "long")
        compact = search.compact_results(result)["results"][0]
        self.assertNotIn("content", compact)
        self.assertTrue(compact["passage_truncated"])
        self.assertEqual(compact["url"], "https://example.org")

    def test_bad_response_and_invalid_time_are_rejected(self):
        with self.assertRaises(ValueError):
            search.normalize_response({"Response": {"Pages": "bad"}})
        for value in ("d0", "d31", "m13", "y6", "not-a-date"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                search.freshness_value(value)
        self.assertEqual(search.freshness_value("d7"), "d7")

    def test_credential_echoed_in_error_is_redacted(self):
        fake = "dummy-credential-for-test"
        body = {"Response": {"Error": {"Code": "Denied", "Message": fake}}}

        class FakeResponse(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        class FakeOpener:
            def open(self, request, timeout):
                return FakeResponse(json.dumps(body).encode())

        with patch.object(search.urllib.request, "build_opener", return_value=FakeOpener()):
            with self.assertRaises(RuntimeError) as caught:
                search.search({"Query": "test"}, fake)
        self.assertNotIn(fake, str(caught.exception))
        self.assertIn("[REDACTED]", str(caught.exception))

    def test_no_credential_forwarding_on_redirect(self):
        handler = search.NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "", {}, "https://example.org"))

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI only")
    def test_windows_encryption_round_trip(self):
        message = b"temporary-example-credential"
        encrypted = search._windows_dpapi(message, protect=True)
        self.assertNotIn(message, encrypted)
        self.assertEqual(search._windows_dpapi(encrypted, protect=False), message)


if __name__ == "__main__":
    unittest.main()
