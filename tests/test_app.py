import time
import json
import threading
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from playwright.sync_api import sync_playwright

import app
from app import BookingForm, Controller, booking_input, scheduled_timestamp, departure_stations, station_timezone


class InputTests(unittest.TestCase):
    def test_departure_dataset(self):
        data = json.loads((app.ROOT / "stations.json").read_text())
        self.assertEqual(data["source"], "https://www.flyscoot.com/en")
        self.assertEqual(len(data["stations"]), 274)
        self.assertEqual(len({station["code"] for station in data["stations"]}), 274)
        self.assertEqual(next(station["name"] for station in data["stations"] if station["code"] == "SIN"), "Singapore")
        self.assertEqual(next(station["name"] for station in data["stations"] if station["code"] == "BKK"), "Bangkok (Suvarnabhumi)")
        self.assertTrue(all(station.get("timezone") for station in data["stations"]))
        zones = json.loads((app.ROOT / "timezones.json").read_text())
        self.assertTrue(all(station["timezone"] in zones for station in data["stations"]))
    def test_booking(self):
        self.assertEqual(booking_input({"pnr": " abc123 ", "departure": " Singapore ", "timezone": "Asia/Singapore"})["pnr"], "ABC123")

    def test_all_departures_have_valid_timezones(self):
        from zoneinfo import ZoneInfo
        for station in departure_stations()["stations"]:
            with self.subTest(code=station["code"]):
                self.assertIsInstance(ZoneInfo(station["timezone"]), ZoneInfo)

    def test_departure_timezone_mapping(self):
        for station, zone in [("Singapore", "Asia/Singapore"), ("tpe", "Asia/Taipei"),
                              ("Sydney", "Australia/Sydney"), ("Perth", "Australia/Perth"),
                              ("JFK", "America/New_York"), ("TYO", "Asia/Tokyo"),
                              ("PNH", "Asia/Phnom_Penh"), ("BWX", "Asia/Jakarta"),
                              ("TRT", "Asia/Makassar"), ("CNN", "Asia/Kolkata")]:
            with self.subTest(station=station):
                self.assertEqual(station_timezone(station), zone)
        self.assertIsNone(station_timezone("Unknown station"))

    def test_known_station_wrong_timezone_rejected(self):
        with self.assertRaises(ValueError):
            booking_input({"pnr": "ABC123", "departure": "Singapore", "timezone": "Asia/Taipei"})

    def test_unknown_station_manual_timezone(self):
        self.assertEqual(booking_input({"pnr": "ABC123", "departure": "Custom station", "timezone": "UTC"})["timezone"], "UTC")

    def test_sydney_seasonal_offsets(self):
        january = scheduled_timestamp({"date": "2027-01-02", "time": "12:00:00", "timezone": "Australia/Sydney"}, now=0)
        july = scheduled_timestamp({"date": "2027-07-02", "time": "12:00:00", "timezone": "Australia/Sydney"}, now=0)
        self.assertEqual(datetime.fromtimestamp(january, timezone.utc).hour, 1)
        self.assertEqual(datetime.fromtimestamp(july, timezone.utc).hour, 2)

    def test_bad_input(self):
        for overrides in [{"pnr": "bad"}, {"departure": ""}, {"timezone": "Unknown/Timezone"}]:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                booking_input({"pnr": "ABC123", "departure": "Singapore", "timezone": "Asia/Singapore", **overrides})

    def test_airport_time(self):
        stamp = scheduled_timestamp({"date": "2027-01-02", "time": "22:03:04", "timezone": "Asia/Singapore"}, now=0)
        self.assertEqual(datetime.fromtimestamp(stamp, timezone.utc).isoformat(), "2027-01-02T14:03:04+00:00")

    def test_dst_rejected(self):
        for date, clock in [("2027-03-14", "02:30:00"), ("2027-11-07", "01:30:00")]:
            with self.subTest(date=date), self.assertRaises(ValueError):
                scheduled_timestamp({"date": date, "time": clock, "timezone": "America/New_York"}, now=0)

    def test_invalid_or_past(self):
        for date, clock in [("2000-01-01", "22:00:00"), ("2027-02-30", "12:00:00"), ("2027-02-01", "24:00:00"), ("2027-02-01", "2:00:00")]:
            with self.subTest(date=date, clock=clock), self.assertRaises(ValueError):
                scheduled_timestamp({"date": date, "time": clock, "timezone": "Asia/Singapore"})

    def test_cannot_schedule_before_prepare(self):
        with self.assertRaises(ValueError):
            Controller().submit("schedule", {})

    def test_cancel_queued(self):
        controller = Controller()
        controller.update("scheduled", "Waiting", time.time() + 30)
        controller.submit("cancel", {})
        self.assertEqual(controller.snapshot()["status"], "working")
        self.assertEqual(controller.commands.get_nowait()[0], "cancel")


FORM = """
<label>Booking Reference (PNR)<input id="pnr"></label>
<label>Departing<select><option>Choose</option><option>Singapore</option><option>Taipei</option></select></label>
<button onclick="window.clicks=(window.clicks||0)+1">Retrieve Booking</button>
"""


class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.page.set_content(FORM)
        self.booking = {"pnr": "ABC123", "departure": "Singapore", "timezone": "Asia/Singapore"}
        self.form = BookingForm(self.page)

    def tearDown(self):
        self.page.close()

    def test_fill_verify_click_once(self):
        self.form.prepare(self.booking)
        self.assertEqual(self.page.evaluate("window.clicks||0"), 0)
        self.form.verify(self.booking).click()
        self.assertEqual(self.page.evaluate("window.clicks"), 1)

    def test_changed_pnr_blocked(self):
        self.form.prepare(self.booking)
        self.page.locator("#pnr").fill("DEF456")
        with self.assertRaises(ValueError):
            self.form.verify(self.booking)

    def test_changed_station_blocked(self):
        self.form.prepare(self.booking)
        self.page.locator("select").select_option(label="Taipei")
        with self.assertRaises(ValueError):
            self.form.verify(self.booking)

    def test_maintenance_blocked(self):
        self.page.set_content("<p>ChopeMySeat is temporarily unavailable.</p>")
        with self.assertRaises(ValueError):
            self.form.fields()

    def test_duplicate_button_blocked(self):
        self.page.set_content(FORM + "<button>Retrieve Booking</button>")
        with self.assertRaises(ValueError):
            self.form.fields()

    def test_disabled_button_blocked(self):
        self.form.prepare(self.booking)
        self.page.get_by_role("button").evaluate("(button)=>button.disabled=true")
        with self.assertRaises(ValueError):
            self.form.verify(self.booking)

    def test_custom_departure_dropdown(self):
        self.page.set_content("""
        <label>Booking Reference (PNR)<input></label>
        <button role="combobox" aria-label="Departing" onclick="document.querySelector('[role=option]').hidden=false">Choose</button>
        <div role="option" hidden onclick="document.querySelector('[role=combobox]').textContent='Singapore';this.hidden=true">Singapore</div>
        <button>Retrieve Booking</button>
        """)
        self.form.prepare(self.booking)
        self.assertTrue(self.form.verify(self.booking).is_enabled())

    def test_queue_stops_without_action(self):
        self.page.set_content("<h2>Passenger(s) Check-in Status</h2><p>Queue: TRY-1 Economy</p>" + FORM)
        outcome = self.form.observe_result(timeout=0)
        self.assertEqual(outcome["status"], "airport_queue")
        self.assertIn("TRY-1", outcome["message"])
        self.assertIn("in person", outcome["message"])
        self.assertEqual(self.page.evaluate("window.clicks||0"), 0)

    def test_multiple_queue_numbers(self):
        self.page.set_content("<p>Queue: TRY-1</p><p>Queue: TRY-2</p>")
        self.assertIn("TRY-1, TRY-2", self.form.observe_result(timeout=0)["message"])

    def test_unsuccessful_stops(self):
        self.page.set_content("<h1>Unsuccessful staff web check-in</h1>")
        self.assertEqual(self.form.observe_result(timeout=0)["status"], "unsuccessful")

    def test_generic_instructions_not_failure(self):
        self.page.set_content("<p>If your web check-in is unsuccessful</p><p>Please queue at the airport</p>")
        # The conditional help text is not an actual result.
        self.assertEqual(self.form.observe_result(timeout=0)["status"], "submitted")

    def test_unknown_result_no_success_inferred(self):
        self.assertEqual(self.form.observe_result(timeout=0)["status"], "submitted")

    def test_delayed_queue_recognized(self):
        self.page.evaluate("setTimeout(()=>document.body.innerHTML='<p>Queue: TRY-3</p>', 150)")
        self.assertEqual(self.form.observe_result(timeout=1)["status"], "airport_queue")

    def test_preview_captures_controlled_entries(self):
        self.form.prepare(self.booking)
        controller = Controller()
        controller.capture_preview(self.page)
        self.assertTrue(controller.preview_image.startswith(b"\xff\xd8"))
        self.assertIsNotNone(controller.snapshot()["preview_time"])
        self.assertEqual(controller.snapshot()["preview_error"], "")
        self.assertEqual(self.page.locator("#pnr").input_value(), "ABC123")
        self.assertEqual(self.page.evaluate("window.clicks||0"), 0)

    def test_static_host_dropdowns_without_python_api(self):
        requests = []

        def serve(route):
            from urllib.parse import urlparse
            path = urlparse(route.request.url).path
            requests.append(path)
            filename = "index.html" if path == "/" else path.lstrip("/")
            file = app.ROOT / filename
            if filename in {"index.html", "app.js", "style.css", "stations.json", "timezones.json"}:
                types = {".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".json": "application/json"}
                route.fulfill(body=file.read_bytes(), content_type=types[file.suffix])
            else:
                route.fulfill(status=404, body="Not found")

        self.page.route("https://fixture.pages.dev/**", serve)
        self.page.goto("https://fixture.pages.dev/")
        self.page.wait_for_function("document.querySelectorAll('#stations option').length === 274")
        self.page.wait_for_function("document.querySelector('#status').textContent === 'Hosted form only'")
        self.assertGreater(self.page.locator("#timezones option").count(), 100)
        self.page.locator("[name=departure]").fill("Singapore")
        self.assertEqual(self.page.locator("[name=timezone]").input_value(), "Asia/Singapore")
        self.page.locator("[name=departure]").fill("Sydney (SYD) — Australia")
        self.assertEqual(self.page.locator("[name=departure]").input_value(), "Sydney")
        self.assertEqual(self.page.locator("[name=timezone]").input_value(), "Australia/Sydney")
        self.assertTrue(self.page.locator("#schedule").is_disabled())
        self.assertFalse(any(path.startswith("/api/") for path in requests))

    def test_closed_preview_reports_error_without_submission(self):
        self.page.close()
        controller = Controller()
        controller.capture_preview(self.page)
        self.assertIn("preview unavailable", controller.snapshot()["preview_error"])
        self.assertEqual(controller.snapshot()["status"], "idle")


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.clicks = []
        clicks = self.clicks

        class FixtureHandler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                if self.path == "/clicked":
                    clicks.append(time.time())
                    content = b"clicked"
                else:
                    content = FORM.replace('window.clicks=(window.clicks||0)+1', "fetch('/clicked')").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(content)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.site_patch = patch.object(app, "SITE", f"http://127.0.0.1:{self.server.server_port}/")
        self.site_patch.start()
        self.controller = Controller()
        self.controller.worker.start()
        self.addCleanup(self.cleanup)
        self.controller.submit("prepare", {"pnr": "ABC123", "departure": "Singapore", "timezone": "Asia/Singapore"})
        self.wait_status("prepared")

    def cleanup(self):
        self.controller.stop.set()
        self.controller.worker.join(timeout=10)
        self.site_patch.stop()
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)

    def wait_status(self, expected):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            state = self.controller.snapshot()
            if state["status"] == expected:
                return state
            if state["status"] == "error":
                self.fail(state["message"])
            time.sleep(0.02)
        self.fail(f"Expected {expected}; got {self.controller.snapshot()}")

    def schedule(self):
        due = int(time.time()) + 3
        from zoneinfo import ZoneInfo
        local = datetime.fromtimestamp(due, ZoneInfo("Asia/Singapore"))
        self.controller.submit("schedule", {"date": local.strftime("%Y-%m-%d"), "time": local.strftime("%H:%M:%S"), "timezone": "Asia/Singapore"})
        self.wait_status("scheduled")
        return due

    def test_exactly_one_scheduled_click_not_early(self):
        due = self.schedule()
        self.assertEqual(self.clicks, [])
        self.wait_status("submitted")
        time.sleep(0.3)
        self.assertEqual(len(self.clicks), 1)
        self.assertGreaterEqual(self.clicks[0], due)
        self.assertLess(self.clicks[0] - due, 2)

    def test_cancel_prevents_click(self):
        due = self.schedule()
        self.controller.submit("cancel", {})
        self.wait_status("idle")
        time.sleep(max(0, due - time.time()) + 0.3)
        self.assertEqual(self.clicks, [])

    def test_show_browser_does_not_submit(self):
        self.controller.submit("show_browser", {})
        time.sleep(0.2)
        self.assertEqual(self.controller.snapshot()["status"], "prepared")
        self.assertEqual(self.clicks, [])

    def test_preview_does_not_repeat_retrieval(self):
        self.schedule()
        self.controller.preview_requested.set()
        self.wait_status("submitted")
        self.controller.preview_requested.set()
        time.sleep(0.3)
        self.assertEqual(len(self.clicks), 1)
        self.assertIsNotNone(self.controller.preview_image)


if __name__ == "__main__":
    unittest.main()
