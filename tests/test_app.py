import sys
from pathlib import Path
import time
import json
import threading
import tempfile
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

from playwright.sync_api import sync_playwright

import app
from app import BookingForm, Controller, booking_input, scheduled_timestamp, departure_stations, station_timezone


class CalendarUITests(unittest.TestCase):
    def test_countdown_panel_remains_outside_scrolling_content(self):
        self.assertEqual(self.ui.countdown_panel.master, self.root.winfo_children()[0])
        self.assertEqual(self.ui.countdown_label.master, self.ui.countdown_panel)
        self.assertTrue(self.ui.countdown_panel.winfo_viewable())
        self.assertIn("No release scheduled", self.ui.countdown.get())

    def test_progress_marks_completed_steps(self):
        self.controller.update("ready", "Standing by", time.time() + 600)
        self.ui.refresh()
        self.assertTrue(self.ui.steps[0].cget("text").startswith("\u2713"))
        self.assertEqual(self.ui.steps[2].cget("style"), "Current.TLabel")
        self.assertTrue(self.ui.steps[3].cget("text").startswith("4"))

    def setUp(self):
        self.root = app.tk.Tk()
        self.addCleanup(self.root.destroy)
        self.controller = Controller()
        self.ui = app.DesktopApp(self.root, self.controller)
        self.root.update()

    def popup(self):
        self.ui.calendar_button.invoke()
        return next(child for child in self.root.winfo_children() if isinstance(child, app.tk.Toplevel))

    def test_icon_opens_calendar_before_airport_selection(self):
        self.assertTrue(self.ui.calendar_button.cget("image"))
        window = self.popup()
        self.assertTrue(window.winfo_viewable())
        self.assertIn("choose departure airport next", window.title())
        self.assertEqual(self.root.grab_current(), window)

    def test_calendar_today_sets_date_in_departure_timezone(self):
        self.ui.timezone.set("Asia/Taipei")
        window = self.popup()
        today_button = next(child for child in window.winfo_children()
                            if isinstance(child, app.ttk.Button) and child.cget("text") == "Today")
        today_button.invoke()
        from zoneinfo import ZoneInfo
        self.assertEqual(self.ui.date.get(), datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d"))
        self.assertFalse(window.winfo_exists())

    def test_active_schedule_disables_calendar_button(self):
        self.controller.update("scheduled", "Waiting", time.time() + 600)
        self.ui.refresh()
        self.assertTrue(self.ui.calendar_button.instate(["disabled"]))


class InputTests(unittest.TestCase):
    def test_schedule_preserves_departure_local_milliseconds(self):
        data = {"date": "2027-01-02", "time": "13:14:59.123", "timezone": "Asia/Taipei"}
        stamp = scheduled_timestamp(data, now=0)
        self.assertEqual(datetime.fromtimestamp(stamp, timezone.utc).isoformat(), "2027-01-02T05:14:59.123000+00:00")
        for value in ["13:14:59.1", "13:14:59.12", "13:14:59.1234", "13:14:59.abc"]:
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "millisecond"):
                scheduled_timestamp({**data, "time": value}, now=0)

    def test_millisecond_schedule_rejects_elapsed_and_dst_ambiguity(self):
        with self.assertRaisesRegex(ValueError, "future"):
            scheduled_timestamp({"date": "2027-01-02", "time": "13:14:59.123", "timezone": "UTC"},
                                now=datetime(2027, 1, 2, 13, 14, 59, 124000, timezone.utc).timestamp())
        with self.assertRaisesRegex(ValueError, "twice"):
            scheduled_timestamp({"date": "2027-11-07", "time": "01:30:00.123", "timezone": "America/New_York"}, now=0)

    def test_schedules_always_use_request_gate_even_with_legacy_false(self):
        for data in [{}, {"request_mode": False}, {"request_mode": True}]:
            with self.subTest(data=data), patch.object(app, "scheduled_timestamp", return_value=time.time() + 600):
                controller = Controller()
                controller.submit("schedule", {"pnr": "ABC123", "departure": "Singapore",
                                              "timezone": "Asia/Singapore", **data})
                action, payload = controller.commands.get_nowait()
                self.assertEqual(action, "schedule")
                self.assertTrue(payload["request_mode"])

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


class RequestGateTests(unittest.TestCase):
    def test_browser_launch_lead_is_twelve_minutes_and_ready_page_is_foregrounded(self):
        self.assertEqual(app.PREPARE_LEAD, 12 * 60)
        controller = Controller()
        controller.page = Mock()
        now = time.time()
        job = {"ready": True, "target": now + 600, "next_check": now + 30}
        self.assertIs(controller.advance(job), job)
        controller.page.bring_to_front.assert_called_once()
        controller.advance(job)
        controller.page.bring_to_front.assert_called_once()

    def test_held_request_keeps_page_active_before_final_release_window(self):
        keep_active = Mock()
        gate = app.BookingRequestGate(100, threading.Event(), keep_active=keep_active)
        route = Mock()
        route.request.method = "POST"
        with patch.object(app.time, "time", side_effect=[98, 99, 100, 100.002, 100.003]):
            gate.handle(route)
        keep_active.assert_called_once()
        route.continue_.assert_called_once()

    def test_gate_reports_hold_and_release_as_they_happen(self):
        reports = []
        gate = app.BookingRequestGate(100, threading.Event(), reports.append)
        route = Mock()
        route.request.method = "POST"
        with patch.object(app.time, "time", side_effect=[99, 100, 100.003, 100.004]):
            gate.handle(route)
        self.assertEqual(len(reports), 2)
        self.assertEqual(reports[0], {"request_held_at": 99})
        self.assertEqual(reports[1]["request_release_called_at"], 100.003)

    def test_controller_retains_countdown_during_early_click_and_hold(self):
        controller = Controller()
        controller.schedule_zone = "Asia/Taipei"
        controller.click_timing = {"target": 100, "request_mode": True}
        controller.update("submitting", "Constructing", 100)
        controller.click_observed(None, 85000)
        self.assertEqual(controller.snapshot()["status"], "submitting")
        self.assertEqual(controller.snapshot()["target"], 100)
        controller.gate_progress({"request_held_at": 90})
        state = controller.snapshot()
        self.assertEqual(state["click_timing"]["request_held_at"], 90)
        self.assertEqual(state["target"], 100)
        self.assertIn("held locally", state["history"][-1]["action"])

    def test_release_once_with_original_request(self):
        gate = app.BookingRequestGate(100, threading.Event())
        route = Mock()
        route.request.method = "POST"
        with patch.object(app.time, "time", side_effect=[99, 100, 100.003, 100.004]):
            gate.handle(route)
        route.continue_.assert_called_once_with()
        route.abort.assert_not_called()
        self.assertEqual(gate.timing["request_release_called_at"], 100.003)
        duplicate = Mock()
        duplicate.request.method = "POST"
        gate.handle(duplicate)
        duplicate.abort.assert_called_once_with("blockedbyclient")
        duplicate.continue_.assert_not_called()

    def test_late_cancelled_and_expired_requests_are_aborted(self):
        for mode in ["late", "cancelled", "expired"]:
            with self.subTest(mode=mode):
                stop = threading.Event()
                gate = app.BookingRequestGate(100, stop)
                if mode == "cancelled":
                    stop.set()
                gate.expired = mode == "expired"
                route = Mock()
                route.request.method = "POST"
                with patch.object(app.time, "time", return_value=100.006):
                    gate.handle(route)
                route.abort.assert_called_once_with("blockedbyclient")
                route.continue_.assert_not_called()

    def test_uncertain_release_is_never_repeated(self):
        gate = app.BookingRequestGate(100, threading.Event())
        route = Mock()
        route.request.method = "POST"
        route.continue_.side_effect = RuntimeError("Connection lost")
        with patch.object(app.time, "time", return_value=100):
            with self.assertRaisesRegex(RuntimeError, "Connection lost"):
                gate.handle(route)
        gate.handle(route)
        self.assertEqual(route.continue_.call_count, 1)
        route.abort.assert_called_once()

    def test_interception_timestamp_is_not_reported_as_transmission(self):
        summary = "\n".join(app.booking_request_summary({
            "target": 100, "request_mode": True, "request_release_called_at": 100.002,
            "requests": [{"booking_request": True, "started_at": 90}]}))
        self.assertIn("must NOT be treated as transmission", summary)
        self.assertIn("+2.0 ms", summary)
        self.assertNotIn("WITHIN", summary)

class TimingTableTests(unittest.TestCase):
    def test_countdown_retains_target_and_result_after_release(self):
        state = {"status": "submitted", "target": None, "timezone": "Asia/Taipei", "now": 105,
                 "click_timing": {"target": 100, "request_release_called_at": 100.002}}
        summary = app.stage_countdown(state)
        self.assertIn("released +2.0 ms", summary)
        self.assertIn("Release target:", summary)
        self.assertIn("Review your result", summary)
        self.assertNotIn("Waiting for Scoot's response", summary)

    def test_stage_countdown_uses_airport_zone_and_millisecond_target(self):
        target = datetime(2027, 1, 2, 5, 14, 59, 123000, timezone.utc).timestamp()
        state = {"status": "ready", "timezone": "Asia/Taipei", "target": target,
                 "now": target - 20, "click_timing": None}
        summary = app.stage_countdown(state)
        self.assertIn("2027-01-02 13:14:59.123", summary)
        self.assertIn("20.000 s", summary)
        self.assertIn("2  Construct & hold POST", summary)
        self.assertEqual(app.progress_state(state), 2)
        state.update(status="submitting", click_timing={"request_held_at": target - 5})
        self.assertIn("held", app.stage_countdown(state))
        self.assertEqual(app.progress_state(state), 4)
        state["click_timing"]["request_release_called_at"] = target + 0.002
        self.assertIn("Waiting for Scoot's response", app.stage_countdown(state))

    def test_event_table_separates_release_from_browser_start(self):
        rows = app.timing_table_rows({
            "target": 100, "request_mode": True, "event_at": 87,
            "request_held_at": 94, "request_release_called_at": 100.0017,
            "requests": [{"booking_request": True, "started_at": 94}],
        })
        release = next(row for row in rows if row[0] == "Local release call")
        self.assertEqual(release[2], "+1.7 ms")
        self.assertIn("NOT proof", release[3])
        request = next(row for row in rows if row[0] == "Booking request start")
        self.assertIn("NOT transmission", request[3])
        self.assertEqual(app.timing_table_rows(None), [])

    def test_request_table_preserves_response_and_failure_details(self):
        timing = {"target": 100, "event_at": 99, "requests": [{
            "booking_request": True, "method": "POST", "endpoint": "https://example.test/booking",
            "type": "XHR", "initiator": "script", "started_at": 100.002,
            "response_at": 101.002, "status": 200, "from_cache": True, "redirected": True,
            "failure": "net::ERR_FAILED",
        }]}
        row = app.request_table_rows(timing)[0]
        self.assertEqual(row[0], "Booking")
        self.assertEqual(row[6], "+2.0 ms")
        self.assertEqual(row[7], "+1002.0 ms")
        self.assertIn("1000.0 ms to headers", row[8])
        self.assertIn("cache/service worker", row[8])
        self.assertIn("redirect", row[8])
        self.assertIn("net::ERR_FAILED", row[8])
        self.assertEqual(app.request_table_rows(None), [])


class RequestTraceTests(unittest.TestCase):
    def setUp(self):
        self.controller = Controller()
        self.controller.trace_pnr = "ABC123"
        self.controller.click_timing = {"target": 1800000000, "event_at": 1800000000.001}

    def request(self, request_id="1", started=1800000000.125, **overrides):
        event = {"requestId": request_id, "wallTime": started, "timestamp": 100.125,
                 "type": "Fetch", "initiator": {"type": "script"},
                 "request": {"method": "POST",
                             "url": "https://user:secret@example.test/booking/%41BC123?token=private#hidden",
                             "postData": "private body", "headers": {"Authorization": "private"}}}
        event.update(overrides)
        return event

    def test_browser_timestamps_redaction_and_independent_snapshot(self):
        self.controller.trace_request(0, self.request())
        self.controller.trace_response(0, {"requestId": "1", "timestamp": 100.625,
                                          "response": {"status": 200}})
        record = self.controller.snapshot()["click_timing"]["requests"][0]
        self.assertEqual(record["endpoint"], "https://example.test/booking/[PNR]")
        self.assertEqual(record["started_at"], 1800000000.125)
        self.assertEqual(record["response_at"], 1800000000.625)
        summary = app.request_trace_summary(self.controller.snapshot()["click_timing"])
        self.assertIn("Booking endpoint request not observed", summary)
        self.assertIn("scheduled offset: +125.0 ms", summary)
        self.assertIn("Click-to-request: +124.0 ms", summary)
        self.assertIn("response headers after 500.0 ms", summary)
        for secret in ["ABC123", "secret", "private", "Authorization", "hidden"]:
            self.assertNotIn(secret, summary)
        record["status"] = 999
        self.assertEqual(self.controller.snapshot()["click_timing"]["requests"][0]["status"], 200)

    def test_redirects_and_sources_do_not_overwrite_candidates(self):
        self.controller.trace_request(0, self.request())
        self.controller.trace_request(0, self.request(started=1800000000.25, timestamp=100.25,
                                                     redirectResponse={"status": 302}))
        self.controller.trace_request(1, self.request())
        self.controller.trace_failure(1, {"requestId": "1", "errorText": "net::ERR_FAILED"})
        self.controller.trace_response(0, {"requestId": "1", "timestamp": 100.5,
                                          "response": {"status": 200, "fromServiceWorker": True}})
        records = self.controller.snapshot()["click_timing"]["requests"]
        self.assertEqual(len(records), 3)
        self.assertTrue(records[0]["redirected"])
        self.assertEqual(records[0]["status"], 302)
        self.assertTrue(records[1]["from_cache"])
        self.assertEqual(records[2]["failure"], "net::ERR_FAILED")

    def test_trace_is_bounded_and_excludes_older_requests(self):
        self.controller.trace_request(0, self.request(started=1799999999))
        for index in range(app.MAX_REQUEST_TRACE + 2):
            self.controller.trace_request(0, self.request(request_id=str(index)))
        timing = self.controller.snapshot()["click_timing"]
        self.assertEqual(len(timing["requests"]), app.MAX_REQUEST_TRACE)
        self.assertEqual(timing["requests_dropped"], 2)
        self.assertIn("2 additional requests omitted", app.request_trace_summary(timing))

    def test_no_attempt_means_no_trace(self):
        self.controller.click_timing = None
        self.controller.trace_request(0, self.request())
        self.assertIsNone(self.controller.snapshot()["click_timing"])
        self.assertIn("No network candidates", app.request_trace_summary(None))

    def test_booking_endpoint_match_requires_exact_host_path_and_method(self):
        endpoint = "https://apigw.singaporeair.com/chopemyseat/booking/retrieval"
        for index, (url, method, matched) in enumerate([
            (endpoint, "POST", True),
            (endpoint + "?private=hidden", "POST", True),
            (endpoint, "GET", False),
            (endpoint + "/other", "POST", False),
            (endpoint.replace("apigw.singaporeair.com", "apigw.singaporeair.com.example.test"), "POST", False),
            (endpoint.replace("https:", "http:"), "POST", False),
            ("https://example.test/telemetry", "POST", False),
        ]):
            with self.subTest(url=url, method=method):
                event = self.request(request_id=str(index))
                event["request"] = {"url": url, "method": method}
                self.controller.trace_request(0, event)
                self.assertEqual(self.controller.snapshot()["click_timing"]["requests"][-1]["booking_request"], matched)

    def test_booking_timing_separates_late_click_and_website_delay(self):
        timing = {"target": 1800000000, "event_at": 1800000001.013,
                  "requests": [{"booking_request": True, "started_at": 1800000005.9654}]}
        summary = "\n".join(app.booking_request_summary(timing))
        self.assertIn("+5965.4 ms from schedule", summary)
        self.assertIn("OUTSIDE 0-5 ms target", summary)
        self.assertIn("Schedule-to-click: +1013.0 ms", summary)
        self.assertIn("click-to-booking-request: +4952.4 ms", summary)

    def test_booking_request_target_boundaries(self):
        for offset, within in [(-0.001, False), (0, True), (0.005, True), (0.006, False)]:
            with self.subTest(offset=offset):
                timing = {"target": 0, "requests": [{"booking_request": True, "started_at": offset}]}
                summary = "\n".join(app.booking_request_summary(timing))
                self.assertIn(f"{'WITHIN' if within else 'OUTSIDE'} 0-5 ms target", summary)


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

    def test_preparation_requires_pending_image_to_finish(self):
        self.check_preparation_with_pending_asset('<img src="/pending-asset">')

    def test_preparation_requires_deferred_script_to_finish(self):
        self.check_preparation_with_pending_asset('<script defer src="/pending-asset"></script>',
                                                  expect_domcontentloaded=False)

    def check_preparation_with_pending_asset(self, asset, expect_domcontentloaded=None):
        controller = Controller()
        controller.browser = self.browser
        controller.page = self.page
        self.page.route("https://loading.test/", lambda route: route.fulfill(
            content_type="text/html", body="""<script>
                window.domReady = false;
                document.addEventListener("DOMContentLoaded", () => window.domReady = true);
            </script>""" + FORM + asset))
        pending = []
        self.page.route("https://loading.test/pending-asset", lambda route: pending.append(route))
        try:
            with patch.object(app, "SITE", "https://loading.test/"), patch.object(app, "PAGE_LOAD_TIMEOUT_MS", 100):
                with self.assertRaises(app.PlaywrightTimeout):
                    controller.prepare_page(self.booking, time.time() + 600)
            self.assertEqual(len(pending), 1)
            self.assertEqual(self.page.evaluate("document.readyState"), "interactive")
            if expect_domcontentloaded is not None:
                self.assertEqual(self.page.evaluate("window.domReady"), expect_domcontentloaded)
            self.assertEqual(self.page.locator("#pnr").input_value(), "")
            self.assertEqual(self.page.locator("select").input_value(), "Choose")
            self.assertEqual(self.page.evaluate("window.clicks||0"), 0)
        finally:
            for route in pending:
                route.abort()

    def test_full_load_precedes_populating_booking(self):
        controller = Controller()
        controller.browser = self.browser
        controller.page = self.page
        self.page.route("https://loading.test/", lambda route: route.fulfill(
            content_type="text/html", body=FORM + """<script>
            window.addEventListener("load", () => window.pnrAtLoad = document.querySelector("#pnr").value);
            </script>"""))
        with patch.object(app, "SITE", "https://loading.test/"):
            controller.prepare_page(self.booking, time.time() + 600)
        self.assertEqual(self.page.evaluate("document.readyState"), "complete")
        self.assertEqual(self.page.evaluate("window.pnrAtLoad"), "")
        self.assertEqual(self.page.locator("#pnr").input_value(), self.booking["pnr"])
        self.assertEqual(self.page.evaluate("window.clicks||0"), 0)

    def test_preparation_reports_http_error_without_filling(self):
        controller = Controller()
        controller.browser = self.browser
        controller.page = self.page
        self.page.route("https://loading.test/", lambda route: route.fulfill(
            status=403, content_type="text/html", body=FORM))
        with patch.object(app, "SITE", "https://loading.test/"):
            with self.assertRaisesRegex(ValueError, "HTTP 403"):
                controller.prepare_page(self.booking, time.time() + 60)
        self.assertEqual(self.page.locator("#pnr").input_value(), "")
        self.assertEqual(self.page.evaluate("window.clicks||0"), 0)

    def test_passive_trace_uses_real_chromium_network_events(self):
        controller = Controller()
        controller.trace_pnr = "ABC123"
        controller.click_timing = {"target": time.time() - 0.1}
        controller.attach_request_trace(self.page)
        self.page.route("https://trace.test/**", lambda route: route.fulfill(
            status=200, content_type="text/html", body=FORM))
        self.page.goto("https://trace.test/")
        controller.click_timing["event_at"] = self.page.evaluate("Date.now()") / 1000
        self.page.evaluate("""async () => {
            await fetch("/telemetry?secret=hidden");
            await new Promise(resolve => setTimeout(resolve, 100));
            await fetch("/booking/ABC123?token=hidden", {method:"POST", body:"private"});
        }""")
        self.page.wait_for_timeout(100)
        timing = controller.snapshot()["click_timing"]
        records = timing["requests"]
        self.assertEqual(len(records), 3)
        self.assertEqual(records[0]["type"], "Document")
        self.assertEqual(records[1]["endpoint"], "https://trace.test/telemetry")
        self.assertEqual(records[2]["endpoint"], "https://trace.test/booking/[PNR]")
        self.assertEqual(records[2]["method"], "POST")
        self.assertGreaterEqual((records[2]["started_at"] - timing["event_at"]) * 1000, 90)
        self.assertTrue(all(record["status"] == 200 for record in records))
        self.assertTrue(all(record["response_at"] >= record["started_at"] for record in records))
        self.assertNotIn("hidden", app.request_trace_summary(timing))
        self.assertNotIn("private", app.request_trace_summary(timing))
        self.assertEqual(self.page.evaluate("window.clicks||0"), 0)

    def test_passenger_page_capture_saves_files_and_dropdowns_without_submission(self):
        self.page.set_content("""
            <form onsubmit="event.preventDefault();window.submitted=true">
              <label>Passport number<input name="passportNumber"></label>
              <label>Nationality<select name="nationality"><option value="">Choose</option>
                <option value="SG">Singapore</option><option value="MY">Malaysia</option></select></label>
              <label>Issuing country<input role="combobox" aria-label="Issuing country"
                onclick="document.querySelector('[role=listbox]').hidden=false"
                onkeydown="if(event.key==='Escape')document.querySelector('[role=listbox]').hidden=true"></label>
              <div role="listbox" hidden><div role="option">Singapore</div><div role="option">Malaysia</div></div>
              <button>Continue</button>
            </form>""")
        with tempfile.TemporaryDirectory() as root, patch.object(app, "CAPTURE_ROOT", Path(root)):
            result = self.form.observe_result(timeout=0)
            self.assertEqual(result["status"], "passenger_details", result["message"])
            folder, = Path(root).iterdir()
            self.assertEqual({file.name for file in folder.iterdir()}, {"page.png", "page.html", "page.mhtml", "fields.json"})
            self.assertTrue((folder / "page.png").read_bytes().startswith(b"\x89PNG"))
            self.assertIn("Passport number", (folder / "page.html").read_text())
            self.assertIn("MIME-Version", (folder / "page.mhtml").read_text())
            data = json.loads((folder / "fields.json").read_text())
            fields = data["frames"][0]["fields"]
            nationality = next(field for field in fields if field["name"] == "nationality")
            self.assertEqual([option["value"] for option in nationality["options"]], ["", "SG", "MY"])
            self.assertEqual(len(data["frames"][0]["expanded_dropdowns"][0]["options"]), 2)
            self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
            self.assertTrue(all(file.stat().st_mode & 0o777 == 0o600 for file in folder.iterdir()))
        self.assertEqual(self.page.locator("[name=passportNumber]").input_value(), "")
        self.assertFalse(self.page.evaluate("Boolean(window.submitted)"))

    def test_capture_failure_is_reported_not_success(self):
        self.page.set_content('<label>Passport<input></label><label>Nationality<select><option>Choose</option></select></label>')
        with tempfile.TemporaryDirectory() as root, patch.object(app, "CAPTURE_ROOT", Path(root)):
            with patch.object(self.page, "screenshot", side_effect=RuntimeError("Screenshot unavailable")):
                result = self.form.observe_result(timeout=0)
            self.assertEqual(result["status"], "capture_incomplete")
            self.assertIn("Screenshot unavailable", result["message"])
            folder, = Path(root).iterdir()
            self.assertTrue(json.loads((folder / "fields.json").read_text())["errors"])
            self.assertTrue((folder / "page.html").is_file())
            self.assertTrue((folder / "page.mhtml").is_file())
            self.assertTrue(json.loads((folder / "fields.json").read_text())["frames"])

    def test_passport_help_text_does_not_trigger_capture(self):
        self.page.set_content("<p>Have your passport and nationality ready.</p>" + FORM)
        with patch.object(self.form, "capture_passenger_form") as capture:
            self.assertEqual(self.form.observe_result(timeout=0)["status"], "submitted")
            capture.assert_not_called()

    def test_result_observer_follows_new_popup(self):
        popup = self.browser.new_page()
        try:
            popup.set_content('<label>Passport<input></label><label>Nationality<select><option>Choose</option></select></label>')
            with tempfile.TemporaryDirectory() as root, patch.object(app, "CAPTURE_ROOT", Path(root)):
                outcome = self.form.observe_result(timeout=0, page_provider=lambda: popup)
                self.assertEqual(outcome["status"], "passenger_details", outcome["message"])
        finally:
            popup.close()

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

    def test_strict_dispatch_boundaries_and_no_duplicate(self):
        fixed_now = 1800000000000
        self.assertEqual(app.MAX_CLICK_LATENESS_MS, 5)
        for lateness, allowed in [(-1, False), (0, True), (4, True), (5, True), (6, False), (50, False), (569, False), (1500, False)]:
            with self.subTest(lateness=lateness):
                self.page.set_content(FORM)
                self.page.evaluate("window.clicks = 0")
                self.form.prepare(self.booking)
                fields = self.form.arm_click(self.booking)
                self.page.evaluate("(now) => { Date.now = () => now; }", fixed_now)
                result = self.form.dispatch_click(fields, self.booking, (fixed_now - lateness) / 1000)
                self.assertEqual(result["clicked"], allowed)
                self.assertEqual(result["lateness_ms"], lateness)
                self.assertEqual(self.page.evaluate("window.clicks||0"), int(allowed))
                if allowed:
                    again = self.form.dispatch_click(fields, self.booking, (fixed_now - lateness) / 1000)
                    self.assertFalse(again["clicked"])
                    self.assertEqual(self.page.evaluate("window.clicks"), 1)

    def test_strict_dispatch_rechecks_form_and_button(self):
        for change in [
            "document.querySelector('#pnr').value = 'WRONG1'",
            "document.querySelector('select').value = 'Taipei'",
            "document.querySelector('button').disabled = true",
            "document.querySelector('button').remove()",
            "document.body.insertAdjacentHTML('beforeend', '<button>Retrieve Booking</button>')",
            "document.body.insertAdjacentHTML('beforeend', '<div style=\"position:fixed;inset:0;z-index:99;background:white\"></div>')",
        ]:
            with self.subTest(change=change):
                self.page.set_content(FORM)
                self.page.evaluate("window.clicks = 0")
                self.form.prepare(self.booking)
                fields = self.form.arm_click(self.booking)
                self.page.evaluate(change)
                result = self.form.dispatch_click(fields, self.booking, time.time())
                self.assertFalse(result["clicked"])
                self.assertNotIn("lateness_ms", result)
                self.assertEqual(self.page.evaluate("window.clicks||0"), 0)

    def test_late_fallback_still_checks_form_and_prevents_duplicates(self):
        self.form.prepare(self.booking)
        fields = {**self.form.arm_click(self.booking), "allow_late_retry": True}
        self.page.evaluate("Date.now = () => 1800000000569")
        self.page.locator("#pnr").fill("WRONG1")
        rejected = self.form.dispatch_click(fields, self.booking, 1800000000)
        self.assertFalse(rejected["clicked"])
        self.assertEqual(self.page.evaluate("window.clicks||0"), 0)
        self.page.locator("#pnr").fill(self.booking["pnr"])
        result = self.form.dispatch_click(fields, self.booking, 1800000000)
        self.assertTrue(result["clicked"])
        self.assertTrue(result["late_fallback"])
        self.assertEqual(result["lateness_ms"], 569)
        self.assertFalse(self.form.dispatch_click(fields, self.booking, 1800000000)["clicked"])
        self.assertEqual(self.page.evaluate("window.clicks"), 1)

    def test_late_fallback_does_not_retry_blocked_event(self):
        self.form.prepare(self.booking)
        fields = {**self.form.arm_click(self.booking), "allow_late_retry": True}
        self.page.evaluate("""() => {
            let calls = 0;
            Date.now = () => ++calls === 1 ? 1800000000000 : 1800000000569;
        }""")
        result = self.form.dispatch_click(fields, self.booking, 1800000000)
        self.assertFalse(result["clicked"])
        self.assertIn("event was blocked", result["reason"])
        self.assertFalse(self.form.dispatch_click(fields, self.booking, 1800000000)["clicked"])
        self.assertEqual(self.page.evaluate("window.clicks||0"), 0)

    def test_strict_dispatch_with_live_dropdown_markup(self):
        self.page.set_content(LIVE_FORM)
        self.form.prepare(self.booking)
        self.form.show_click_target(self.booking, 1800000000)
        self.assertIn("NEXT: Retrieve Booking", self.page.locator("#auto-chope-seat-target #label").inner_text())
        self.assertEqual(self.page.evaluate("window.clicks||0"), 0)
        fields = self.form.arm_click(self.booking)
        self.page.evaluate("Date.now = () => 1800000000000")
        result = self.form.dispatch_click(fields, self.booking, 1800000000)
        self.assertTrue(result["clicked"])
        self.assertEqual(result["lateness_ms"], 0)
        self.assertEqual(self.page.evaluate("window.clicks"), 1)
        self.assertIn("Click event observed", self.page.locator("#auto-chope-seat-target #label").inner_text())

    def test_browser_wait_never_clicks_early(self):
        self.form.prepare(self.booking)
        fields = self.form.arm_click(self.booking)
        target = self.page.evaluate("Date.now() + 200") / 1000
        result = self.form.dispatch_click({**fields, "wait_until_target": True}, self.booking, target)
        if result["clicked"]:
            offset = (result["event_at"] - target) * 1000
            self.assertGreaterEqual(offset, 0)
            self.assertLessEqual(offset, app.MAX_CLICK_LATENESS_MS + 0.001)
        else:
            self.assertIn("deadline was missed", result["reason"])
            late_at = result.get("blocked_event_at", target + result["lateness_ms"] / 1000)
            self.assertGreater((late_at - target) * 1000, app.MAX_CLICK_LATENESS_MS)
        self.assertEqual(self.page.evaluate("window.clicks||0"), int(result["clicked"]))

    def test_event_guard_blocks_delay_inside_button_click(self):
        self.form.prepare(self.booking)
        fields = self.form.arm_click(self.booking)
        self.page.evaluate("""() => {
            const button = document.querySelector("button");
            const click = button.click.bind(button);
            button.click = () => {
                const start = performance.now();
                while (performance.now() - start < 100) {}
                click();
            };
        }""")
        target = self.page.evaluate("Date.now() + 100") / 1000
        result = self.form.dispatch_click({**fields, "wait_until_target": True}, self.booking, target)
        self.assertFalse(result["clicked"])
        self.assertIn("event was blocked", result["reason"])
        self.assertGreater((result["blocked_event_at"] - target) * 1000, app.MAX_CLICK_LATENESS_MS)
        self.assertEqual(self.page.evaluate("window.clicks||0"), 0)

    def test_event_time_is_captured_before_slow_button_listeners(self):
        self.form.prepare(self.booking)
        self.page.evaluate("""() => {
            document.querySelector("button").addEventListener("click", () => {
                const start = performance.now();
                while (performance.now() - start < 100) {}
            }, {capture: true});
        }""")
        fields = self.form.arm_click(self.booking)
        target = self.page.evaluate("Date.now() + 100") / 1000
        result = self.form.dispatch_click({**fields, "wait_until_target": True}, self.booking, target)
        self.assertTrue(result["clicked"], result)
        self.assertLessEqual((result["event_at"] - target) * 1000, app.MAX_CLICK_LATENESS_MS + 0.001)
        self.assertGreater((result["handler_returned_at"] - result["event_at"]) * 1000, 90)
        self.assertEqual(self.page.evaluate("window.clicks"), 1)

    def test_browser_wait_rechecks_fields_changed_while_waiting(self):
        self.form.prepare(self.booking)
        fields = self.form.arm_click(self.booking)
        target = self.page.evaluate("""() => {
            setTimeout(() => document.querySelector("#pnr").value = "WRONG1", 25);
            return Date.now() + 100;
        }""") / 1000
        result = self.form.dispatch_click({**fields, "wait_until_target": True}, self.booking, target)
        self.assertFalse(result["clicked"])
        self.assertIn("PNR changed", result["reason"])
        self.assertEqual(self.page.evaluate("window.clicks||0"), 0)

    def test_blocked_event_timing_is_visible(self):
        rows = app.timing_table_rows({"target": 1800000000, "blocked_event_at": 1800000000.569,
                                      "lateness_ms": 12, "clicked": False})
        blocked = next(row for row in rows if row[0] == "Blocked click")
        self.assertEqual(blocked[2], "+569.0 ms")

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
        self.page.evaluate("(form) => setTimeout(() => document.body.innerHTML = form, 600)", FORM)
        started = time.monotonic()
        self.form.wait_until_ready(5)
        self.assertLess(time.monotonic() - started, 3)

    def test_wait_until_ready_times_out(self):
        self.page.set_content("<p>Loading…</p>")
        with self.assertRaisesRegex(TimeoutError, "did not become ready"):
            self.form.wait_until_ready(0.6)

    def test_wait_until_ready_requires_fields_not_only_button(self):
        self.page.set_content("<button>Retrieve Booking</button>")
        with self.assertRaisesRegex(TimeoutError, "did not become ready"):
            self.form.wait_until_ready(0.3)

    def test_wait_until_ready_waits_for_editable_fields(self):
        self.page.set_content(FORM.replace('id="pnr"', 'id="pnr" readonly'))
        self.page.evaluate("setTimeout(() => document.querySelector('#pnr').readOnly = false, 300)")
        self.form.wait_until_ready(3)
        self.assertTrue(self.page.locator("#pnr").is_editable())

    def test_wait_until_ready_reports_access_denied(self):
        self.page.set_content("<h1>Access Denied</h1>")
        with self.assertRaisesRegex(ValueError, "denied access"):
            self.form.wait_until_ready(1)

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







MAINTENANCE = "<p>ChopeMySeat is currently under maintenance.</p>"




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
        self.request_journey = False
        self.continuations = []
        self.release = threading.Event()
        test = self

        class FixtureHandler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                test.clicks.append(time.time())
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"next":"/continuation"}')

            def do_GET(self):
                if self.path == "/continuation":
                    test.continuations.append(time.time())
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"Manual journey ready")
                    return
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
                        if test.request_journey:
                            content = FORM.replace('window.clicks=(window.clicks||0)+1',
                                "fetch('/booking', {method:'POST', body:'fixture'}).then(r=>r.json()).then(data=>fetch(data.next))")
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
        due = self.schedule()
        self.wait_status("submitted", timeout=25)
        time.sleep(0.3)
        self.assertEqual(len(self.clicks), 1)
        self.assertGreaterEqual(self.clicks[0], due)

    def test_busy_result_page_is_reread_until_readable(self):
        self.busy_result = True
        self.schedule()
        observed = self.wait_status("observing", timeout=15)
        self.assertIsNotNone(observed["click_timing"]["event_at"])
        self.assertNotIn("handler_returned_at", observed["click_timing"])
        state = self.wait_status("airport_queue", timeout=30)
        self.assertIn("TRY-1", state["message"])
        self.assertEqual(len(self.clicks), 1)
        timing = state["click_timing"]
        self.assertGreater(timing["handler_returned_at"] - timing["event_at"], 3.5)
        self.assertGreaterEqual(timing["first_request_at"], timing["target"])
        self.assertEqual(timing["first_response_status"], 200)

    def test_browser_side_missed_deadline_allows_one_late_fallback(self):
        dispatch = BookingForm.dispatch_click

        def delayed(form, fields, booking, target):
            time.sleep(app.DISPATCH_LEAD + 0.08)
            return dispatch(form, fields, booking, target)

        with patch.object(BookingForm, "dispatch_click", delayed):
            self.schedule()
            state = self.wait_status("submitted")
        self.assertIn("one immediate late fallback", state["message"])
        self.assertTrue(self.controller.snapshot()["click_timing"]["late_fallback"])
        self.assertGreater(self.controller.snapshot()["click_timing"]["lateness_ms"], app.MAX_CLICK_LATENESS_MS)
        time.sleep(0.2)
        self.assertEqual(len(self.clicks), 1)

    def test_python_side_missed_deadline_allows_one_late_fallback(self):
        click = self.controller.click

        def delayed(job):
            time.sleep(app.DISPATCH_LEAD + 0.08)
            return click(job)

        with patch.object(self.controller, "click", delayed):
            self.schedule()
            state = self.wait_status("submitted")
        self.assertIn("one immediate late fallback", state["message"])
        self.assertTrue(state["click_timing"]["late_fallback"])
        time.sleep(0.2)
        self.assertEqual(len(self.clicks), 1)

    def test_uncertain_dispatch_is_never_retried(self):
        with patch.object(BookingForm, "dispatch_click", side_effect=RuntimeError("Browser disconnected")) as dispatch:
            self.schedule()
            state = self.wait_status("uncertain")
            time.sleep(0.2)
            self.assertEqual(dispatch.call_count, 1)
        self.assertIn("A click may have been sent", state["message"])
        self.assertEqual(self.clicks, [])

    def test_unfinished_page_load_never_becomes_ready_or_clicks(self):
        self.hang_assets = True
        with patch.object(app, "PAGE_LOAD_TIMEOUT_MS", 100):
            self.schedule(ahead=5)
            state = self.wait_status("error", timeout=10)
        self.assertIn("Timeout", state["message"])
        self.assertEqual(self.clicks, [])

    def wait_status(self, expected, timeout=20):
        statuses = (expected,) if isinstance(expected, str) else expected
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.controller.snapshot()
            if state["status"] in statuses:
                return state
            if state["status"] == "error" and "error" not in statuses:
                self.fail(state["message"])
            time.sleep(0.02)
        self.fail(f"Expected {expected}; got {self.controller.snapshot()}")

    def schedule(self, ahead=7, request_mode=False, milliseconds=0):
        due = int(time.time()) + ahead + milliseconds / 1000
        from zoneinfo import ZoneInfo
        local = datetime.fromtimestamp(due, ZoneInfo("Asia/Singapore"))
        data = {"pnr": "abc123", "departure": "Singapore", "timezone": "Asia/Singapore",
                "date": local.strftime("%Y-%m-%d"), "time": local.strftime("%H:%M:%S.%f")[:-3]}
        if request_mode:
            self.controller.submit("schedule", data)
        else:
            # Exercise the retained internal click path without exposing it through the public schedule API.
            self.controller.update("working", "Processing schedule")
            self.controller.commands.put(("schedule", {
                "booking": booking_input(data), "target": scheduled_timestamp(data), "request_mode": False,
            }))
        self.wait_status("scheduled")
        return due

    def test_request_mode_holds_post_and_preserves_page_continuation(self):
        self.request_journey = True
        endpoint = f"http://127.0.0.1:{self.server.server_port}/booking"

        def install(gate, context):
            context.route(endpoint, gate.handle)

        with patch.object(app.BookingRequestGate, "install", install):
            due = self.schedule(request_mode=True, milliseconds=123)
            state = self.wait_status(("submitted", "error", "uncertain"), timeout=25)
        timing = state["click_timing"]
        self.assertAlmostEqual(timing["target"], due, places=6)
        self.assertTrue(timing["request_mode"])
        self.assertLess(timing["request_held_at"], due)
        if timing.get("request_release_blocked"):
            self.assertEqual(state["status"], "error")
            self.assertEqual(self.clicks, [])
            self.assertEqual(self.continuations, [])
        else:
            self.assertEqual(state["status"], "submitted", state["message"])
            self.assertGreaterEqual(timing["request_release_called_at"], due)
            self.assertLessEqual((timing["request_release_called_at"] - due) * 1000, 5.001)
            self.assertEqual(len(self.clicks), 1)
            self.assertGreaterEqual(self.clicks[0], due)
            self.assertEqual(len(self.continuations), 1)

    def test_browser_opens_at_lead_then_clicks_in_window_or_aborts(self):
        due = self.schedule()
        self.assertFalse(self.controller.snapshot()["browser_open"])
        self.assertEqual(self.controller.snapshot()["prepare_at"], due - self.LEAD)
        self.assertEqual(self.controller.snapshot()["browser_name"], "Chromium (bundled)")
        self.assertIn("Chromium (bundled) launches at", self.controller.snapshot()["message"])
        self.wait_status("ready")
        self.assertGreaterEqual(self.loads[0], due - self.LEAD)
        self.assertTrue(self.controller.snapshot()["browser_open"])
        self.assertEqual(self.clicks, [])
        state = self.wait_status(("submitted", "error"))
        time.sleep(0.3)
        timing = state["click_timing"]
        if state["status"] == "error":
            self.assertIn("deadline was missed", state["message"])
            self.assertEqual(self.clicks, [])
            if timing is not None:
                self.assertFalse(timing.get("clicked", False))
                late_at = timing.get("blocked_event_at", due + timing.get("lateness_ms", 0) / 1000)
                self.assertGreater((late_at - due) * 1000, app.MAX_CLICK_LATENESS_MS)
            return
        self.assertEqual(len(self.clicks), 1)
        self.assertGreaterEqual(self.clicks[0], due)
        self.assertTrue(timing["clicked"])
        self.assertGreaterEqual(timing["lateness_ms"], 0)
        if timing.get("late_fallback"):
            self.assertGreater(timing["lateness_ms"], app.MAX_CLICK_LATENESS_MS)
        else:
            self.assertLessEqual(timing["lateness_ms"], app.MAX_CLICK_LATENESS_MS)
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

    def test_cancel_after_final_verification_prevents_dispatch(self):
        armed = threading.Event()
        arm = BookingForm.arm_click

        def capture(form, booking):
            fields = arm(form, booking)
            armed.set()
            return fields

        with patch.object(BookingForm, "arm_click", capture):
            due = self.schedule()
            self.assertTrue(armed.wait(timeout=10))
            self.controller.submit("cancel", {})
            self.wait_status("idle")
        time.sleep(max(0, due - time.time()) + 0.1)
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
