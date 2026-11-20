import unittest

import dotenv

# Import app without touching production credentials.
dotenv.load_dotenv = lambda *args, **kwargs: False
import app  # noqa: E402


class HostAndOriginTests(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()

    def test_foreign_host_is_rejected_for_reads(self):
        response = self.client.get("/api/analytics/fees", base_url="http://evil.example:5000")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json["error"], "untrusted host")

    def test_dns_rebinding_write_is_rejected_even_with_matching_origin(self):
        response = self.client.post("/api/sync_transactions", base_url="http://evil.example:5000",
                                    headers={"Origin": "http://evil.example:5000"})
        self.assertEqual(response.status_code, 403)

    def test_loopback_hosts_are_served(self):
        for base in ("http://localhost:5000", "http://127.0.0.1:5000", "http://[::1]:5000"):
            with self.subTest(base=base):
                self.assertNotEqual(self.client.get("/link", base_url=base).status_code, 403)

    def test_cross_origin_write_on_localhost_is_rejected(self):
        response = self.client.post("/api/sync_transactions", base_url="http://localhost:5000",
                                    headers={"Origin": "http://evil.example"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json["error"], "cross-origin mutation rejected")

    def test_hostname_parsing(self):
        self.assertEqual(app._hostname("LOCALHOST:5000"), "localhost")
        self.assertEqual(app._hostname("[::1]:5000"), "[::1]")
        self.assertEqual(app._hostname("127.0.0.1"), "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
