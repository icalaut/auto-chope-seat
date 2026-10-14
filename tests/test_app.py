import sys
from pathlib import Path
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
        self.assertEqual(data["source"], "https://chopemyseat.flyscoot.com/home")
        self.assertEqual(len(data["stations"]), 109)
        self.assertEqual(len({station["code"] for station in data["stations"]}), 109)
        self.assertEqual(next(station["name"] for station in data["stations"] if station["code"] == "SIN"), "Singapore")
        self.assertEqual(next(station["name"] for station in data["stations"] if station["code"] == "BKK"), "Bangkok (Suvarnabhumi)")
        self.assertEqual(next(station["name"] for station in data["stations"] if station["code"] == "SYD"), "Sydney(Kingsford Smith)")
        for code in ["BTU", "MPH", "DLI", "HEL", "TNA", "KHN", "NGB", "WUX"]:
            self.assertIn(code, {station["code"] for station in data["stations"]})
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
                              ("Sydney(Kingsford Smith)", "Australia/Sydney"), ("Perth", "Australia/Perth"),
                              ("TYO", "Asia/Tokyo"), ("HEL", "Europe/Helsinki"),
                              ("Bintulu", "Asia/Kuching"), ("Wuxi / Suzhou", "Asia/Shanghai"),
                              ("Dalat", "Asia/Ho_Chi_Minh")]:
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

    def test_date_must_be_airport_today_or_later(self):
        # 2027-01-02 00:30 in Sydney is still 2027-01-01 in Singapore.
        now = datetime(2027, 1, 1, 13, 30, tzinfo=timezone.utc).timestamp()
        with self.assertRaisesRegex(ValueError, r"today \(Australia/Sydney\) or later"):
            scheduled_timestamp({"date": "2027-01-01", "time": "23:59:59", "timezone": "Australia/Sydney"}, now=now)
        self.assertGreater(scheduled_timestamp({"date": "2027-01-01", "time": "23:59:59", "timezone": "Asia/Singapore"}, now=now), now)

    def test_frontend_date_minimum_is_today_onward(self):
        script = (Path(app.ROOT) / "app.js").read_text()
        self.assertIn("dateInput.min = today", script)
        self.assertIn("dateInput.value < today", script)

    def test_schedule_requires_preparation_lead(self):
        from zoneinfo import ZoneInfo
        soon = datetime.fromtimestamp(int(time.time()) + 30, ZoneInfo("Asia/Singapore"))
        with self.assertRaisesRegex(ValueError, "at least"):
            Controller().submit("schedule", {"pnr": "ABC123", "departure": "Singapore", "timezone": "Asia/Singapore",
                                             "date": soon.strftime("%Y-%m-%d"), "time": soon.strftime("%H:%M:%S")})

    def test_prepare_action_removed_and_show_browser_needs_open_browser(self):
        for action in ["prepare", "show_browser"]:
            with self.subTest(action=action), self.assertRaises(ValueError):
                Controller().submit(action, {})

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


LIVE_FORM = """
<form aria-label="Staff travel Web Check-in Form">
  <p>Booking Reference (PNR)</p>
  <input aria-label="Booking Reference field" type="text" name="bookingReference" placeholder="Enter Your Booking Reference">
  <p>Departing</p>
  <input type="text" class="dropdown-input" aria-label="Dropdown input for home.form-departing" placeholder="Select Airport"
         onclick="document.querySelector('.dropdown-option-container').hidden=false"
         oninput="for (const o of document.querySelectorAll('.dropdown-option')) o.hidden = !o.dataset.name.toLowerCase().includes(this.value.toLowerCase())">
  <div class="dropdown-option-container" aria-label="Dropdown options container for home.form-departing" hidden>
    <div class="dropdown-option" data-name="Tokyo (All Airports)" aria-label="Dropdown option for home.form-departing"><div><div> Tokyo (All Airports) </div><div> Japan </div></div><div>TYO</div></div>
    <div class="dropdown-option" data-name="Tokyo (Haneda)" aria-label="Dropdown option for home.form-departing"><div><div> Tokyo (Haneda) </div><div> Japan </div></div><div>HND</div></div>
    <div class="dropdown-option" data-name="Singapore" aria-label="Dropdown option for home.form-departing"><div><div> Singapore </div><div> Singapore </div></div><div>SIN</div></div>
    <div class="dropdown-option" data-name="Sydney(Kingsford Smith)" aria-label="Dropdown option for home.form-departing"><div><div> Sydney(Kingsford Smith) </div><div> Australia </div></div><div>SYD</div></div>
  </div>
  <input type="text" aria-label="Dropdown input for home.form-arriving" placeholder="Select Airport" style="display:none">
  <button type="submit" aria-label="Retrieve Booking button" onclick="event.preventDefault();window.clicks=(window.clicks||0)+1">Retrieve Booking <img alt="Path icon"></button>
</form>
<script>
for (const o of document.querySelectorAll('.dropdown-option')) o.addEventListener('click', () => {
  document.querySelector('.dropdown-input').value = o.dataset.name;
  document.querySelector('.dropdown-option-container').hidden = true;
});
</script>
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

    def test_live_chopemyseat_dropdown(self):
        # Mirrors the live Angular markup observed on 2026-10-14.
        self.page.set_content(LIVE_FORM)
        self.form.prepare({**self.booking, "departure": "Tokyo (All Airports)"})
        self.assertEqual(self.page.get_by_role("textbox", name="Dropdown input for home.form-departing").input_value(), "Tokyo (All Airports)")
        self.assertEqual(self.page.evaluate("window.clicks||0"), 0)
        self.form.verify({**self.booking, "departure": "Tokyo (All Airports)"}).click()
        self.assertEqual(self.page.evaluate("window.clicks"), 1)

    def test_live_dropdown_matches_by_airport_code_when_label_differs(self):
        self.page.set_content(LIVE_FORM)
        booking = {**self.booking, "departure": "Sydney", "code": "SYD"}
        self.form.prepare(booking)
        self.assertEqual(booking["site_departure"], "Sydney(Kingsford Smith)")
        self.assertTrue(self.form.verify(booking).is_enabled())
        self.assertEqual(self.page.evaluate("window.clicks||0"), 0)

    def test_booking_input_includes_station_code(self):
        self.assertEqual(booking_input({"pnr": "ABC123", "departure": "Sydney(Kingsford Smith)", "timezone": "Australia/Sydney"})["code"], "SYD")
        self.assertIsNone(booking_input({"pnr": "ABC123", "departure": "Custom station", "timezone": "UTC"})["code"])

    def test_live_dropdown_unknown_station_fails_without_click(self):
        self.page.set_content(LIVE_FORM)
        with self.assertRaisesRegex(ValueError, "not offered by ChopeMySeat"):
            self.form.prepare({**self.booking, "departure": "Adelaide"})
        self.assertEqual(self.page.evaluate("window.clicks||0"), 0)

    def test_hidden_duplicate_button_ignored(self):
        self.page.set_content(FORM + '<button style="display:none">Retrieve Booking</button>')
        self.form.prepare(self.booking)
        self.assertTrue(self.form.verify(self.booking).is_visible())

    def test_wait_until_ready_polls_for_late_button(self):
        self.page.set_content("<p>Loading…</p>")
        self.page.evaluate("""setTimeout(() => document.body.innerHTML = '<button aria-label="Retrieve Booking button">Retrieve Booking</button>', 600)""")
        started = time.monotonic()
        self.form.wait_until_ready(5)
        self.assertLess(time.monotonic() - started, 3)

    def test_wait_until_ready_times_out(self):
        self.page.set_content("<p>Loading…</p>")
        with self.assertRaisesRegex(TimeoutError, "did not appear"):
            self.form.wait_until_ready(0.6)

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

    def test_checkin_closed_message(self):
        self.page.set_content("<p>Online check-in is already closed for this flight.</p>" + FORM)
        outcome = self.form.observe_result(timeout=0)
        self.assertEqual(outcome["status"], "checkin_closed")
        self.assertIn("flight is in the past", outcome["message"])
        self.assertEqual(self.page.evaluate("window.clicks||0"), 0)

    def test_not_open_yet_reports_flight_and_opening_time(self):
        self.page.set_content("<p>Online check-in is not open yet! You can start checking in for your flight <b>TR 866</b> at <b>10-13-2026 22:00 (SGT)</b></p>"
                              "<p>*Subject to seats availability.</p>" + FORM)
        outcome = self.form.observe_result(timeout=0)
        self.assertEqual(outcome["status"], "not_open_yet")
        self.assertIn("flight TR 866 opens at 2026-10-13 22:00 (SGT)", outcome["message"])
        self.assertEqual(self.page.evaluate("window.clicks||0"), 0)

    def test_not_open_yet_without_detail(self):
        self.page.set_content("<p>Online check-in is not open yet!</p>")
        outcome = self.form.observe_result(timeout=0)
        self.assertEqual(outcome["status"], "not_open_yet")
        self.assertIn("did not state the opening time", outcome["message"])

    def test_pnr_not_found_message(self):
        self.page.set_content("<div role='alert'>Online check-in is currently not available for your booking. Please go to the counter.</div>" + FORM)
        outcome = self.form.observe_result(timeout=0)
        self.assertEqual(outcome["status"], "pnr_not_found")
        self.assertIn("PNR not found", outcome["message"])
        self.assertEqual(self.page.evaluate("window.clicks||0"), 0)

    def test_departure_mismatch_message(self):
        self.page.set_content("<div>Please ensure your Booking Reference (PNR) and Departing station are entered correctly. </div>" + FORM)
        outcome = self.form.observe_result(timeout=0)
        self.assertEqual(outcome["status"], "departure_mismatch")
        self.assertIn("Departing station does not match", outcome["message"])

    def test_delayed_departure_mismatch_recognized(self):
        self.page.evaluate("setTimeout(()=>document.body.append('Please ensure your Booking Reference (PNR) and Departing station are entered correctly.'), 150)")
        self.assertEqual(self.form.observe_result(timeout=1)["status"], "departure_mismatch")

    def test_generic_instructions_not_failure(self):
        self.page.set_content("<p>If your web check-in is unsuccessful</p><p>Please queue at the airport</p>")
        # The conditional help text is not an actual result.
        self.assertEqual(self.form.observe_result(timeout=0)["status"], "submitted")

    def test_unknown_result_no_success_inferred(self):
        self.assertEqual(self.form.observe_result(timeout=0)["status"], "submitted")

    def test_delayed_queue_recognized(self):
        self.page.evaluate("setTimeout(()=>document.body.innerHTML='<p>Queue: TRY-3</p>', 150)")
        self.assertEqual(self.form.observe_result(timeout=1)["status"], "airport_queue")


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
        self.page.wait_for_function("document.querySelectorAll('#stations option').length === 109")
        self.page.wait_for_function("document.querySelector('#status').textContent === 'Hosted automation unavailable'")
        self.assertGreater(self.page.locator("#timezones option").count(), 100)
        self.page.locator("[name=departure]").fill("Singapore")
        self.assertEqual(self.page.locator("[name=timezone]").input_value(), "Asia/Singapore")
        self.page.locator("[name=departure]").fill("Sydney(Kingsford Smith) (SYD) — Australia")
        self.assertEqual(self.page.locator("[name=departure]").input_value(), "Sydney(Kingsford Smith)")
        self.assertEqual(self.page.locator("[name=timezone]").input_value(), "Australia/Sydney")
        self.assertTrue(self.page.locator("#schedule").is_disabled())
        self.assertEqual([path for path in requests if path.startswith("/api/")], ["/api/config"])

    def test_cloud_frontend_preview_and_manual_controls(self):
        image = self.page.screenshot(type="jpeg")
        actions = []
        status = {"status": "submitted", "message": "Manual review required.", "mode": "cloud",
                  "manual_available": True, "preview_available": True,
                  "session_expires": time.time() + 600, "now": time.time(), "target": None}

        def serve(route):
            from urllib.parse import urlparse
            path = urlparse(route.request.url).path
            if path == "/api/config":
                route.fulfill(json={"token": "fixture-token", "mode": "cloud"})
            elif path == "/api/status":
                route.fulfill(json=status)
            elif path == "/api/preview":
                self.assertEqual(route.request.headers["x-app-token"], "fixture-token")
                route.fulfill(body=image, content_type="image/jpeg")
            elif path == "/api/action":
                actions.append(route.request.post_data_json)
                route.fulfill(json=status)
            else:
                filename = "index.html" if path == "/" else path.lstrip("/")
                types = {".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".json": "application/json"}
                file = app.ROOT / filename
                if filename in {"index.html", "app.js", "style.css", "stations.json", "timezones.json"}:
                    route.fulfill(body=file.read_bytes(), content_type=types[file.suffix])
                else:
                    route.fulfill(status=404)

        self.page.route("https://fixture.workers.dev/**", serve)
        self.page.goto("https://fixture.workers.dev/")
        self.page.wait_for_function("!document.querySelector('#browser-preview').hidden && !document.querySelector('#remote-controls').disabled")
        self.assertTrue(self.page.locator("#show-browser").is_hidden())
        self.assertTrue(self.page.locator("#remote-controls").is_visible())
        self.assertIn("encrypted", self.page.locator("#privacy-note").inner_text())
        self.page.locator("[name=departure]").fill("Kochi (KCZ) — Japan")
        self.assertEqual(self.page.locator("[name=timezone]").input_value(), "Asia/Tokyo")
        self.page.locator("[name=departure]").fill("Unlisted station")
        self.assertEqual(self.page.locator("[name=timezone]").input_value(), "")
        self.page.locator("#browser-preview").click(position={"x": 20, "y": 20})
        self.assertEqual(actions, [])
        self.page.locator("#enable-clicks").check()
        self.page.locator("#browser-preview").click(position={"x": 20, "y": 20})
        self.page.wait_for_function("!document.querySelector('#remote-controls').disabled")
        self.assertEqual(actions[-1]["kind"], "click")
        self.assertGreater(actions[-1]["x"], 0)
        self.assertNotIn("pnr", actions[-1])
        self.page.locator("#remote-text").fill("Synthetic test text")
        self.page.locator("#send-text").click()
        self.page.wait_for_function("!document.querySelector('#remote-controls').disabled")
        self.assertEqual(actions[-1], {"action": "manual", "kind": "type", "text": "Synthetic test text"})
        self.assertEqual(self.page.locator("#remote-text").input_value(), "")
        self.page.locator("[data-key=Tab]").click()
        self.page.wait_for_function("!document.querySelector('#remote-controls').disabled")
        self.assertEqual(actions[-1]["key"], "Tab")



MAINTENANCE = "<p>ChopeMySeat is currently under maintenance.</p>"


class WindowTests(unittest.TestCase):
    class FakeWindow:
        def __init__(self):
            self.confirm_close = False
            self.localization = {}

    def closing(self, status):
        window = self.FakeWindow()
        with patch.object(app.controller, "state", {"status": status, "message": "", "target": None}):
            return app.window_closing(window), window

    def test_close_confirms_only_when_retrieval_pending(self):
        for status in ("scheduled", "preparing", "ready"):
            result, window = self.closing(status)
            self.assertIsNone(result)
            self.assertTrue(window.confirm_close)
            self.assertIn("cancel the scheduled retrieval", window.localization["global.quitConfirmation"])
        result, window = self.closing("idle")
        self.assertIsNone(result)
        self.assertFalse(window.confirm_close)

    def test_close_refused_while_clicking(self):
        for status in ("submitting", "observing"):
            self.assertIs(self.closing(status)[0], False)

    def test_focus_without_window_reports_absent(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_port
            with patch.object(app, "PORT", port), patch.object(app, "HOST", "127.0.0.1"):
                import http.client
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                connection.request("POST", "/api/focus", body=b"", headers={"Host": f"127.0.0.1:{app.PORT}"})
                response = connection.getresponse()
                body = json.loads(response.read())
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(response.status, 404)
        self.assertEqual(body, {"window": False})


class DefaultBrowserTests(unittest.TestCase):
    def test_disabled_uses_bundled_chromium(self):
        with patch.object(app, "USE_DEFAULT_BROWSER", False):
            self.assertEqual(app.default_browser(), {"name": "Chromium (bundled)", "executable": None, "note": ""})

    @unittest.skipUnless(sys.platform == "darwin", "macOS LaunchServices lookup")
    def test_detects_macos_default_browser(self):
        info = app.default_browser()
        self.assertTrue(info["name"])
        if info["executable"]:
            self.assertTrue(Path(info["executable"]).is_file())
            self.assertEqual(info["note"], "")
        else:
            self.assertIn("cannot be automated", info["note"])


class SchedulerTests(unittest.TestCase):
    LEAD = 4

    def setUp(self):
        self.clicks = []
        self.loads = []
        self.maintenance_loads = 0
        self.hang_assets = False
        self.busy_click = False
        self.busy_result = False
        self.release = threading.Event()
        test = self

        class FixtureHandler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                if self.path == "/hang":
                    test.release.wait(30)
                    return
                if self.path == "/clicked":
                    test.clicks.append(time.time())
                    content = b"clicked"
                else:
                    test.loads.append(time.time())
                    if len(test.loads) <= test.maintenance_loads:
                        content = MAINTENANCE.encode()
                    else:
                        content = FORM.replace('window.clicks=(window.clicks||0)+1', "fetch('/clicked')")
                        if test.busy_click:
                            content = content.replace("fetch('/clicked')", "fetch('/clicked');const t=Date.now();while(Date.now()-t<2500);")
                        if test.busy_result:
                            content = content.replace("fetch('/clicked')", "fetch('/clicked');const t=Date.now();while(Date.now()-t<4000);document.body.append('Queue: TRY-1');")
                        if test.hang_assets:
                            content += '<img src="/hang">'
                        content = content.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(content)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.patches = [
            patch.object(app, "SITE", f"http://127.0.0.1:{self.server.server_port}/"),
            patch.object(app, "PREPARE_LEAD", self.LEAD),
            patch.object(app, "RETRY_INTERVAL", 0.5),
            patch.object(app, "RETRY_CUTOFF", 1),
            patch.object(app, "MIN_SCHEDULE_LEAD", 1),
            patch.object(app, "HEADLESS", True),
            patch.object(app, "KEEP_AWAKE", False),
            patch.object(app, "USE_DEFAULT_BROWSER", False),
            patch.object(app, "RESULT_WATCH", 8),
        ]
        for item in self.patches:
            item.start()
        self.controller = Controller()
        self.controller.worker.start()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.release.set()
        self.controller.stop.set()
        self.controller.worker.join(timeout=10)
        for item in reversed(self.patches):
            item.stop()
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)

    def test_slow_click_acknowledgement_is_treated_as_clicked_once(self):
        self.busy_click = True
        with patch.object(app, "CLICK_TIMEOUT_MS", 1000):
            due = self.schedule()
            self.wait_status("submitted", timeout=25)
        time.sleep(0.3)
        self.assertEqual(len(self.clicks), 1)
        self.assertGreaterEqual(self.clicks[0], due)

    def test_busy_result_page_is_reread_until_readable(self):
        self.busy_result = True
        with patch.object(app, "CLICK_TIMEOUT_MS", 1000):
            self.schedule()
            state = self.wait_status("airport_queue", timeout=30)
        self.assertIn("TRY-1", state["message"])
        self.assertEqual(len(self.clicks), 1)

    def test_slow_page_load_event_does_not_block_preparation(self):
        self.hang_assets = True
        due = self.schedule(ahead=8)
        self.wait_status("submitted", timeout=25)
        time.sleep(0.3)
        self.assertEqual(len(self.clicks), 1)
        self.assertGreaterEqual(self.clicks[0], due)

    def wait_status(self, expected, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.controller.snapshot()
            if state["status"] == expected:
                return state
            if state["status"] == "error" and expected != "error":
                self.fail(state["message"])
            time.sleep(0.02)
        self.fail(f"Expected {expected}; got {self.controller.snapshot()}")

    def schedule(self, ahead=7):
        due = int(time.time()) + ahead
        from zoneinfo import ZoneInfo
        local = datetime.fromtimestamp(due, ZoneInfo("Asia/Singapore"))
        self.controller.submit("schedule", {"pnr": "abc123", "departure": "Singapore", "timezone": "Asia/Singapore",
                                            "date": local.strftime("%Y-%m-%d"), "time": local.strftime("%H:%M:%S")})
        self.wait_status("scheduled")
        return due

    def test_browser_opens_at_lead_fills_entries_then_clicks_once_on_time(self):
        due = self.schedule()
        self.assertFalse(self.controller.snapshot()["browser_open"])
        self.assertEqual(self.controller.snapshot()["prepare_at"], due - self.LEAD)
        self.assertEqual(self.controller.snapshot()["browser_name"], "Chromium (bundled)")
        self.assertIn("Chromium (bundled) launches at", self.controller.snapshot()["message"])
        self.wait_status("ready")
        self.assertGreaterEqual(self.loads[0], due - self.LEAD)
        self.assertTrue(self.controller.snapshot()["browser_open"])
        self.assertEqual(self.clicks, [])
        self.wait_status("submitted")
        time.sleep(0.3)
        self.assertEqual(len(self.clicks), 1)
        self.assertGreaterEqual(self.clicks[0], due)
        self.assertLess(self.clicks[0] - due, 2)
        self.assertTrue(self.controller.snapshot()["browser_open"])

    def test_preparation_retries_until_form_available(self):
        self.maintenance_loads = 1
        due = self.schedule(ahead=8)
        self.wait_status("submitted")
        time.sleep(0.3)
        self.assertGreaterEqual(len(self.loads), 2)
        self.assertEqual(len(self.clicks), 1)
        self.assertGreaterEqual(self.clicks[0], due)

    def test_preparation_stops_before_cutoff_without_click(self):
        self.maintenance_loads = 1000
        due = self.schedule()
        state = self.wait_status("error")
        self.assertIn("nothing was clicked", state["message"])
        self.assertGreaterEqual(len(self.loads), 2)
        time.sleep(max(0, due - time.time()) + 0.5)
        self.assertEqual(self.clicks, [])

    def test_cancel_before_browser_opens(self):
        due = self.schedule()
        self.controller.submit("cancel", {})
        self.wait_status("idle")
        time.sleep(max(0, due - time.time()) + 0.3)
        self.assertEqual(self.loads, [])
        self.assertEqual(self.clicks, [])

    def test_cancel_while_ready_prevents_click_and_closes_browser(self):
        due = self.schedule()
        self.wait_status("ready")
        self.controller.submit("cancel", {})
        self.wait_status("idle")
        self.assertFalse(self.controller.snapshot()["browser_open"])
        time.sleep(max(0, due - time.time()) + 0.3)
        self.assertEqual(self.clicks, [])

    def test_show_browser_does_not_submit(self):
        self.schedule()
        self.wait_status("ready")
        self.controller.submit("show_browser", {})
        time.sleep(0.2)
        self.assertEqual(self.controller.snapshot()["status"], "ready")
        self.assertEqual(self.clicks, [])



if __name__ == "__main__":
    unittest.main()
