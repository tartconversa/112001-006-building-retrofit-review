import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from retrofit.server import make_server

ROOT = Path(__file__).resolve().parents[1]


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.server = make_server(Path(cls.tmp.name) / "ws.json", ROOT / "demo_data", port=0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def request(self, path, method="GET", body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            method=method,
            data=data,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_index_and_static_assets(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/") as response:
            html = response.read().decode("utf-8")
        self.assertIn("公共建筑改造复核", html)
        # every module the page imports must be served
        for asset in ("/app.js", "/charts.js", "/app.css"):
            with self.subTest(asset=asset):
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{asset}") as response:
                    self.assertEqual(response.status, 200)
                    self.assertTrue(response.read())

    def test_analysis_payload_has_no_object_key_marks(self):
        status, payload = self.request("/api/analysis")
        self.assertEqual(status, 200)
        self.assertIn("analysis", payload)
        self.assertGreater(payload["analysis"]["summary"]["projects"], 0)
        for issue in payload["analysis"]["data_issues"]:
            self.assertNotIn("'", issue["message"])

    def test_unknown_endpoint_returns_json_error(self):
        status, payload = self.request("/api/nope", "POST", {})
        self.assertEqual(status, 400)
        self.assertIn("error", payload)

    def test_reject_bad_as_of(self):
        status, payload = self.request("/api/as-of", "POST", {"as_of": "not-a-date"})
        self.assertEqual(status, 400)

    def test_import_records_merge(self):
        status, payload = self.request(
            "/api/import-records",
            "POST",
            {
                "records": {
                    "comfort": [
                        {
                            "record_id": "C99",
                            "building_id": "BL04",
                            "date": "2026-06-01",
                            "comfort_sample_pct": "88",
                            "sample_count": "40",
                            "stage": "accepted",
                        }
                    ]
                },
                "note": "补录抽测",
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(any(row["record_id"] == "C99" for row in payload["payload"]["records"]["comfort"]))


if __name__ == "__main__":
    unittest.main()
