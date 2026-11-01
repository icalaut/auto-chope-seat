import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import server
from app import Controller


class ServerTests(unittest.TestCase):
    TOKEN = "unit-test-token-123"

    def setUp(self):
        server.ApiHandler.controller = Controller()
        server.ApiHandler.token = self.TOKEN
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.ApiHandler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.shutdown)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def request(self, path, body=None, token=None):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {token or self.TOKEN}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.status, json.loads(error.read())

    def test_state_requires_token(self):
        status, body = self.request("/api/state", token="wrong-token")
        self.assertEqual(status, 401)
        self.assertIn("token", body["error"])

    def test_state_reports_idle_countdown(self):
        status, body = self.request("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "idle")
        self.assertEqual(body["status_label"], "Ready to schedule")
        self.assertEqual(body["headline"], "--:--:--")
        self.assertIn("No release scheduled", body["stage_text"])
        self.assertEqual(len(body["steps"]), 6)
        self.assertFalse(body["active"])
        self.assertFalse(body["can_cancel"])

    def test_stations_listed_with_timezones(self):
        status, body = self.request("/api/stations")
        self.assertEqual(status, 200)
        singapore = next(s for s in body["stations"] if s["code"] == "SIN")
        self.assertEqual(singapore["timezone"], "Asia/Singapore")
        self.assertIn("(SIN)", singapore["label"])

    def test_schedule_rejects_invalid_pnr(self):
        status, body = self.request("/api/schedule", body={
            "pnr": "nope", "departure": "Singapore", "timezone": "Asia/Singapore",
            "date": "2030-01-01", "time": "08:00:00.000"})
        self.assertEqual(status, 400)
        self.assertIn("PNR", body["error"])

    def test_cancel_without_schedule_fails(self):
        status, body = self.request("/api/cancel", body={})
        self.assertEqual(status, 400)
        self.assertIn("no scheduled retrieval", body["error"].lower())

    def test_serves_mobile_page_without_token(self):
        with urllib.request.urlopen(self.base + "/") as response:
            page = response.read().decode()
        self.assertEqual(response.status, 200)
        self.assertIn("RELEASE COUNTDOWN", page)
        self.assertIn("apple-mobile-web-app-capable", page)


if __name__ == "__main__":
    unittest.main()
