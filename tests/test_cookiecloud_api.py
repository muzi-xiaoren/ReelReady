import gzip
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from reelready.web.app import create_app


class CookieCloudUploadTests(unittest.TestCase):
    def setUp(self):
        # Do not start the scheduler or write real cookies while testing the API.
        self.client = TestClient(create_app())
        self.settings = SimpleNamespace(cookiecloud=SimpleNamespace(uuid="test-user"))

    def test_compressed_and_plain_uploads(self):
        payload = {"uuid": "test-user", "encrypted": "test-data", "crypto_type": "legacy"}
        for compressed in (False, True):
            with self.subTest(compressed=compressed):
                content = json.dumps(payload).encode()
                headers = {"Content-Type": "application/json"}
                if compressed:
                    content = gzip.compress(content)
                    headers["Content-Encoding"] = "gzip"
                with (
                    patch("reelready.web.cookiecloud_api.load_settings", return_value=self.settings),
                    patch("reelready.web.cookiecloud_api.store_cookiecloud", return_value=0) as store,
                ):
                    response = self.client.post("/cookiecloud/update", content=content, headers=headers)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json(), {"action": "done"})
                    store.assert_called_once_with(self.settings, "test-user", "test-data", "legacy")

    def test_gzip_preflight(self):
        response = self.client.options("/cookiecloud/update", headers={
            "Origin": "chrome-extension://test",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,content-encoding",
        })
        self.assertEqual(response.status_code, 204)
        self.assertIn("Content-Encoding", response.headers["Access-Control-Allow-Headers"])

    def test_malformed_compressed_upload(self):
        for content in (b"not-gzip", b"\x1f\x8b", gzip.compress(b"not-json")):
            with self.subTest(content=content):
                response = self.client.post("/cookiecloud/update", content=content, headers={
                    "Content-Type": "application/json", "Content-Encoding": "gzip",
                })
                self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
