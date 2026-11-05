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
        self.assertIn("No retrieval scheduled", self.ui.countdown.get())

    def test_progress_marks_completed_steps(self):
        self.controller.update("ready", "Standing by", time.time() + 600)
        self.ui.refresh()
        self.assertTrue(self.ui.steps[0].cget("text").startswith("\u2713"))
        self.assertEqual(self.ui.steps[2].cget("style"), "Current.TLabel")
        self.assertTrue(self.ui.steps[3].cget("text").startswith("4"))

    def test_ui_shares_airport_timezone_and_live_phase_label(self):
        target = datetime(2027, 1, 2, tzinfo=timezone.utc).timestamp()
        self.controller.schedule_zone = "Asia/Singapore"
        self.controller.click_timing = {"target": target, "request_held_at": target - 10}
        self.controller.update("submitting", "Holding", target)
        self.ui.refresh()
        self.assertEqual(self.ui.status.get(), "Dispatching single retrieval click")
        first = self.ui.timing_table.get_children()[0]
        self.assertEqual(self.ui.timing_table.item(first, "values")[1],
                         "2027-01-02 08:00:00.000+08:00")
        self.assertIn("+08:00", self.ui.history_log.get("1.0", "end"))
        self.assertNotIn("Response received", " ".join(app.PROGRESS_STEPS))

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

    def test_schedules_skill_click_one_second_before_checkin_even_with_legacy_mode(self):
        for data in [{}, {"request_mode": False}, {"request_mode": True}]:
            checkin = time.time() + 600
            with self.subTest(data=data), patch.object(app, "scheduled_timestamp", return_value=checkin):
                controller = Controller()
                controller.submit("schedule", {"pnr": "ABC123", "departure": "Singapore",
                                              "timezone": "Asia/Singapore", **data})
                action, payload = controller.commands.get_nowait()
                self.assertEqual(action, "schedule")
                self.assertFalse(payload["request_mode"])
                self.assertEqual(payload["checkin"], checkin)
                self.assertEqual(payload["target"], checkin - 1)

    def test_schedule_lead_applies_to_api_target(self):
        with patch.object(app.time, "time", return_value=100), \
                patch.object(app, "scheduled_timestamp", return_value=100 + app.MIN_SCHEDULE_LEAD - 0.001):
            with self.assertRaisesRegex(ValueError, "Schedule at least"):
                Controller().submit("schedule", {"pnr": "ABC123", "departure": "Singapore",
                                                  "timezone": "Asia/Singapore"})

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


class StandbyTimingTests(unittest.TestCase):
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

    def test_wait_sleeps_then_uses_monotonic_final_wait(self):
        stop = Mock()
        stop.is_set.return_value = False
        with patch.object(app.time, "time_ns", side_effect=[0, 9_000_000, 10_000_000, 10_000_000]), \
                patch.object(app.time, "monotonic_ns", side_effect=[0, 0, 1_000_000]):
            self.assertTrue(app.wait_until(10_000_000, stop))
        stop.wait.assert_called_once_with(0.008)

    def test_wait_rechecks_wall_clock_after_backward_change(self):
        stop = Mock()
        stop.is_set.return_value = False
        with patch.object(app.time, "time_ns", side_effect=[9_000_000, 5_000_000, 10_000_000]), \
                patch.object(app.time, "monotonic_ns", side_effect=[0, 1_000_000, 0, 5_000_000]):
            self.assertTrue(app.wait_until(10_000_000, stop))

    def test_wait_stops_on_shutdown(self):
        stop = threading.Event()
        stop.set()
        self.assertFalse(app.wait_until(time.time_ns() + 1_000_000, stop))

    def test_release_tolerance_boundaries(self):
        self.assertEqual(app.MAX_RELEASE_LATENESS_MS, 50)
        for offset, allowed in [(-1, False), (0, True), (5, True), (6, True),
                                (50, True), (50.001, False), (1000, False)]:
            with self.subTest(offset=offset):
                gate = app.BookingRequestGate(100, threading.Event())
                route = Mock()
                route.request.method = "POST"
                with patch.object(app, "wait_until", return_value=True), \
                        patch.object(app.time, "time_ns", return_value=100_000_000_000 + round(offset * 1_000_000)):
                    gate.handle(route)
                self.assertEqual(route.continue_.call_count, int(allowed))
                self.assertEqual(route.abort.call_count, int(not allowed))
                self.assertEqual("request_release_called_at" in gate.timing, allowed)
                self.assertAlmostEqual(gate.timing["request_release_offset_ms"], offset)

    def test_post_intercepted_760_ms_late_never_reports_armed(self):
        controller = Controller()
        controller.click_timing = {"target": 100, "request_mode": True}
        gate = app.BookingRequestGate(100, threading.Event(), controller.gate_progress)
        route = Mock()
        route.request.method = "POST"
        with patch.object(app.time, "time_ns", return_value=100_760_523_000), \
                patch.object(app, "wait_until") as wait:
            gate.handle(route)
        self.assertFalse(any("Armed" in event["action"] for event in controller.history))
        wait.assert_not_called()
        route.abort.assert_called_once_with("blockedbyclient")
        route.continue_.assert_not_called()
        self.assertIn("POST reached the gate after", gate.timing["request_release_reason"])
        self.assertIn("+760.523 ms", gate.timing["request_release_reason"])

    def test_post_intercepted_after_target_within_tolerance_is_not_reported_armed(self):
        reports = []
        gate = app.BookingRequestGate(100, threading.Event(), reports.append)
        route = Mock()
        route.request.method = "POST"
        with patch.object(app.time, "time_ns", return_value=100_006_000_000), \
                patch.object(app, "wait_until") as wait:
            gate.handle(route)
        wait.assert_not_called()
        route.continue_.assert_called_once_with()
        self.assertEqual(len(reports), 1)
        self.assertIn("request_release_returned_at", reports[0])

    def test_early_interception_followed_by_late_wakeup_reports_wait_deadline(self):
        gate = app.BookingRequestGate(100, threading.Event())
        route = Mock()
        route.request.method = "POST"
        with patch.object(app.time, "time_ns", side_effect=[99_000_000_000, 100_760_523_000]), \
                patch.object(app, "wait_until", return_value=True):
            gate.handle(route)
        self.assertIn("Local wait/check missed", gate.timing["request_release_reason"])
        self.assertNotIn("POST reached the gate after", gate.timing["request_release_reason"])
        route.abort.assert_called_once_with("blockedbyclient")

    def test_stopped_wait_aborts_post(self):
        gate = app.BookingRequestGate(100, threading.Event())
        route = Mock()
        route.request.method = "POST"
        with patch.object(app, "wait_until", return_value=False), \
                patch.object(app.time, "time_ns", return_value=99_000_000_000):
            gate.handle(route)
        route.continue_.assert_not_called()
        route.abort.assert_called_once_with("blockedbyclient")
        self.assertEqual(gate.timing["request_release_reason"], "Stopped before release")

    def test_already_stopped_gate_never_reports_armed(self):
        reports = []
        stop = threading.Event()
        stop.set()
        gate = app.BookingRequestGate(100, stop, reports.append)
        route = Mock()
        route.request.method = "POST"
        with patch.object(app.time, "time_ns", return_value=99_000_000_000), \
                patch.object(app, "wait_until") as wait:
            gate.handle(route)
        wait.assert_not_called()
        self.assertEqual(len(reports), 1)
        self.assertTrue(reports[0]["request_release_blocked"])
        route.abort.assert_called_once_with("blockedbyclient")
        route.continue_.assert_not_called()

    def test_shutdown_before_construction_does_not_click(self):
        controller = Controller()
        controller.update("ready", "Standing by", 100)
        controller.stop.set()
        with patch.object(BookingForm, "dispatch_click") as dispatch:
            controller.click({"target": 100, "booking": {"pnr": "ABC123"}, "click_fields": {}})
        dispatch.assert_not_called()
        self.assertEqual(controller.snapshot()["status"], "error")

    def test_gate_preserves_request_and_blocks_duplicates_after_uncertain_release(self):
        gate = app.BookingRequestGate(100, threading.Event())
        route = Mock()
        route.request.method = "POST"
        route.continue_.side_effect = RuntimeError("Connection lost")
        with patch.object(app, "wait_until", return_value=True), \
                patch.object(app.time, "time_ns", return_value=100_000_000_000):
            with self.assertRaisesRegex(RuntimeError, "Connection lost"):
                gate.handle(route)
        route.continue_.assert_called_once_with()
        self.assertNotIn("request_release_returned_at", gate.timing)
        duplicate = Mock()
        duplicate.request.method = "POST"
        gate.handle(duplicate)
        duplicate.abort.assert_called_once_with("blockedbyclient")
        duplicate.continue_.assert_not_called()

    def test_completed_release_keeps_duplicate_posts_blocked(self):
        reports = []
        gate = app.BookingRequestGate(100, threading.Event(), reports.append)
        route = Mock()
        route.request.method = "POST"
        with patch.object(app, "wait_until", return_value=True), \
                patch.object(app.time, "time_ns", side_effect=[99_000_000_000, 100_006_000_000, 100_007_000_000]):
            gate.handle(route)
        route.continue_.assert_called_once_with()
        self.assertTrue(gate.finished)
        self.assertEqual(len(reports), 2)
        self.assertIn("request_held_at", reports[0])
        self.assertNotIn("request_release_called_at", reports[0])
        self.assertIn("request_release_returned_at", reports[1])
        duplicate = Mock()
        duplicate.request.method = "POST"
        gate.handle(duplicate)
        duplicate.abort.assert_called_once_with("blockedbyclient")
        duplicate.continue_.assert_not_called()

    def test_shutdown_after_wait_before_release_aborts(self):
        stop = threading.Event()
        gate = app.BookingRequestGate(100, stop)
        route = Mock()
        route.request.method = "POST"

        def finish_wait(target_ns, event):
            event.set()
            return True

        with patch.object(app, "wait_until", side_effect=finish_wait), \
                patch.object(app.time, "time_ns", side_effect=[99_000_000_000, 100_000_000_000]):
            gate.handle(route)
        route.continue_.assert_not_called()
        route.abort.assert_called_once_with("blockedbyclient")

    def test_expired_gate_blocks_late_post_but_not_options(self):
        gate = app.BookingRequestGate(100, threading.Event())
        gate.expired = True
        route = Mock()
        route.request.method = "POST"
        gate.handle(route)
        route.abort.assert_called_once_with("blockedbyclient")
        route.continue_.assert_not_called()
        options = Mock()
        options.request.method = "OPTIONS"
        gate.handle(options)
        options.continue_.assert_called_once_with()
        options.abort.assert_not_called()

    def test_gate_routes_only_exact_booking_endpoint(self):
        context = Mock()
        app.BookingRequestGate(100, threading.Event()).install(context)
        pattern, _ = context.route.call_args.args
        endpoint = f"https://{app.BOOKING_REQUEST_HOST}{app.BOOKING_REQUEST_PATH}"
        self.assertIsNotNone(pattern.fullmatch(endpoint))
        self.assertIsNotNone(pattern.fullmatch(endpoint + "?fixture=1"))
        for url in [endpoint + "/other", endpoint.replace("https:", "http:"),
                    endpoint.replace(app.BOOKING_REQUEST_HOST, "example.test")]:
            self.assertIsNone(pattern.fullmatch(url))

    def test_hold_keeps_countdown_and_does_not_report_release_on_click(self):
        controller = Controller()
        controller.request_mode = True
        controller.click_timing = {"target": 100, "request_mode": True}
        controller.update("submitting", "Constructing", 100)
        controller.click_observed(None, 85000)
        self.assertEqual(controller.snapshot()["status"], "submitting")
        controller.gate_progress({"request_held_at": 90})
        state = controller.snapshot()
        self.assertEqual(state["target"], 100)
        self.assertIn("Armed", state["message"])
        state["now"] = 95
        self.assertIn("until API release", app.headline_countdown(state)[1])

    def test_stopped_click_does_not_install_a_gate(self):
        controller = Controller()
        controller.update("ready", "Armed", 100)
        controller.stop.set()
        with patch.object(app.BookingRequestGate, "install") as install:
            controller.click({"target": 100, "booking": {"pnr": "ABC123"}, "click_fields": {}})
        install.assert_not_called()
        self.assertEqual(controller.snapshot()["status"], "error")

class TimingTableTests(unittest.TestCase):
    def test_all_timing_surfaces_use_airport_date_and_utc_offset(self):
        target = datetime(2027, 1, 2, 0, 0, 0, tzinfo=timezone.utc).timestamp()
        timing = {"target": target, "event_at": target - 15, "requests": [{
            "booking_request": True, "started_at": target - 10, "method": "POST",
            "endpoint": "https://example.test/booking", "type": "Fetch", "initiator": "script",
        }]}
        state = {"status": "ready", "timezone": "America/Los_Angeles", "target": target,
                 "now": target - 20, "click_timing": timing}
        self.assertIn("2027-01-01 16:00:00.000-08:00", app.stage_countdown(state))
        self.assertIn("2027-01-01 16:00:00.000-08:00", app.headline_countdown(state)[1])
        self.assertEqual(app.timing_table_rows(timing, state["timezone"])[0][1],
                         "2027-01-01 16:00:00.000-08:00")
        self.assertEqual(app.request_table_rows(timing, state["timezone"])[0][5],
                         "2027-01-01 15:59:50.000-08:00")

    def test_status_distinguishes_form_construction_hold_and_release(self):
        state = {"status": "ready", "click_timing": None}
        self.assertIn("Armed", app.status_label(state))
        state["status"] = "submitting"
        self.assertEqual(app.status_label(state), "Dispatching retrieval click")
        state["click_timing"] = {"request_held_at": 90}
        self.assertEqual(app.status_label(state), "POST intercepted - awaiting release")
        state["click_timing"]["request_release_called_at"] = 100
        self.assertIn("unconfirmed", app.status_label(state))
        state["click_timing"]["request_release_returned_at"] = 100.002
        self.assertEqual(app.status_label(state), "Local release acknowledged")
        state["click_timing"]["request_release_blocked"] = True
        self.assertEqual(app.status_label(state), "POST aborted - no retry")

    def test_elapsed_browser_plan_does_not_claim_successful_load(self):
        summary = app.stage_countdown({"status": "error", "target": 1000, "now": 1001,
                                       "timezone": "UTC", "click_timing": None})
        self.assertIn("planned time passed; see activity log", summary)
        self.assertNotIn("   done", summary)

    def test_countdown_retains_target_and_result_after_release(self):
        state = {"status": "submitted", "target": None, "timezone": "Asia/Taipei", "now": 105,
                 "click_timing": {"target": 100, "request_release_returned_at": 100.004,
                                  "request_release_offset_ms": 2}}
        summary = app.stage_countdown(state)
        self.assertIn("Local API release call: +2.000 ms", summary)
        self.assertIn("API release target:", summary)
        self.assertIn("entered check-in time", summary)
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
        self.assertIn("intercepted locally", app.stage_countdown(state))
        self.assertEqual(app.progress_state(state), 4)
        state["click_timing"].update(request_release_returned_at=target + 0.004,
                                     request_release_offset_ms=2)
        self.assertIn("Waiting for Scoot's response", app.stage_countdown(state))

    def test_event_table_separates_construction_initiation_from_release(self):
        rows = app.timing_table_rows({
            "target": 100, "checkin": 100, "event_at": 85,
            "request_held_at": 89.38, "request_release_called_at": 100.0017,
            "requests": [{"booking_request": True, "started_at": 89.37}],
        })
        requested = next(row for row in rows if row[0] == "Local release call")
        self.assertEqual(requested[2], "+1.7 ms")
        self.assertIn("NOT proof", requested[3])
        click = next(row for row in rows if row[0] == "Construction click")
        self.assertEqual(click[2], "-15000.0 ms")
        request = next(row for row in rows if row[0] == "Booking request initiation")
        self.assertIn("NOT transmission", request[3])
        self.assertEqual(app.timing_table_rows(None), [])

    def test_initiation_is_not_reported_as_api_transmission(self):
        summary = "\n".join(app.booking_request_summary({
            "target": 100, "request_mode": True, "request_release_called_at": 100.002,
            "requests": [{"booking_request": True, "started_at": 89.38}]}))
        self.assertIn("NOT transmission", summary)
        self.assertIn("+2.000 ms", summary)
        self.assertNotIn("Schedule-to-click", summary)

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
        self.assertIn("API target offset: +125.0 ms", summary)
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
        self.controller.trace_request(0, self.request(started=1799999900))
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
        self.assertIn("+5965.4 ms from dispatch target", summary)
        self.assertIn("diagnostic only", summary)
        self.assertIn("Construction click from dispatch target: +1013.0 ms", summary)
        self.assertIn("click-to-booking-request: +4952.4 ms", summary)

    def test_booking_request_offset_is_diagnostic_not_a_dispatch_gate(self):
        for offset in [-0.001, 0, 0.005, 0.006, 0.050, 1]:
            with self.subTest(offset=offset):
                timing = {"target": 0, "requests": [{"booking_request": True, "started_at": offset}]}
                summary = "\n".join(app.booking_request_summary(timing))
                self.assertIn(f"{offset * 1000:+.1f} ms from dispatch target", summary)
                self.assertNotIn("WITHIN", summary)
                self.assertNotIn("OUTSIDE", summary)


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

    def test_browser_delay_does_not_abort_or_duplicate_a_dispatched_click(self):
        fixed_now = 1800000000000
        for lateness in [0, 4, 5, 6, 50, 569, 1500]:
            with self.subTest(lateness=lateness):
                self.page.set_content(FORM)
                self.page.evaluate("window.clicks = 0")
                self.form.prepare(self.booking)
                fields = self.form.arm_click(self.booking)
                self.page.evaluate("(now) => { Date.now = () => now; }", fixed_now)
                result = self.form.dispatch_click(fields, self.booking, (fixed_now - lateness) / 1000)
                self.assertTrue(result["clicked"])
                self.assertEqual(result["lateness_ms"], lateness)
                self.assertEqual(self.page.evaluate("window.clicks||0"), 1)
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

    def test_dispatch_still_checks_form_and_prevents_duplicates(self):
        self.form.prepare(self.booking)
        fields = self.form.arm_click(self.booking)
        self.page.evaluate("Date.now = () => 1800000000569")
        self.page.locator("#pnr").fill("WRONG1")
        rejected = self.form.dispatch_click(fields, self.booking, 1800000000)
        self.assertFalse(rejected["clicked"])
        self.assertEqual(self.page.evaluate("window.clicks||0"), 0)
        self.page.locator("#pnr").fill(self.booking["pnr"])
        result = self.form.dispatch_click(fields, self.booking, 1800000000)
        self.assertTrue(result["clicked"])
        self.assertEqual(result["lateness_ms"], 569)
        self.assertFalse(self.form.dispatch_click(fields, self.booking, 1800000000)["clicked"])
        self.assertEqual(self.page.evaluate("window.clicks"), 1)

    def test_missing_event_does_not_retry(self):
        self.form.prepare(self.booking)
        fields = self.form.arm_click(self.booking)
        self.page.evaluate("document.querySelector('button').click = () => {}")
        result = self.form.dispatch_click(fields, self.booking, 1800000000)
        self.assertFalse(result["clicked"])
        self.assertIn("No click event was observed", result["reason"])
        self.assertFalse(self.form.dispatch_click(fields, self.booking, 1800000000)["clicked"])
        self.assertEqual(self.page.evaluate("window.clicks||0"), 0)

    def test_skill_dispatch_rejects_early_and_late_browser_execution(self):
        self.form.prepare(self.booking)
        fields = self.form.arm_click(self.booking)
        booking = {**self.booking, "timed_dispatch": True}
        for milliseconds in [-1, 51]:
            self.page.evaluate(f"Date.now = () => {1800000000000 + milliseconds}")
            result = self.form.dispatch_click(fields, booking, 1800000000)
            self.assertFalse(result["clicked"])
            self.assertEqual(self.page.evaluate("window.clicks||0"), 0)
        self.page.evaluate("Date.now = () => 1800000000000")
        self.assertTrue(self.form.dispatch_click(fields, booking, 1800000000)["clicked"])
        self.assertEqual(self.page.evaluate("window.clicks"), 1)

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
        self.assertIn("Retrieve Booking clicked", self.page.locator("#auto-chope-seat-target #label").inner_text())

    def test_local_wait_never_returns_before_target(self):
        self.form.prepare(self.booking)
        fields = self.form.arm_click(self.booking)
        target_ns = time.time_ns() + 100_000_000
        self.assertTrue(app.wait_until(target_ns, threading.Event()))
        self.assertGreaterEqual(time.time_ns(), target_ns)
        result = self.form.dispatch_click(fields, self.booking, target_ns / 1e9)
        self.assertTrue(result["clicked"])
        self.assertEqual(self.page.evaluate("window.clicks"), 1)

    def test_delay_inside_button_click_is_recorded_not_aborted(self):
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
        target = time.time()
        result = self.form.dispatch_click(fields, self.booking, target)
        self.assertTrue(result["clicked"])
        self.assertGreater((result["event_at"] - target) * 1000, app.MAX_RELEASE_LATENESS_MS)
        self.assertEqual(self.page.evaluate("window.clicks||0"), 1)

    def test_event_time_is_captured_before_slow_button_listeners(self):
        self.form.prepare(self.booking)
        self.page.evaluate("""() => {
            document.querySelector("button").addEventListener("click", () => {
                const start = performance.now();
                while (performance.now() - start < 100) {}
            }, {capture: true});
        }""")
        fields = self.form.arm_click(self.booking)
        target = time.time()
        result = self.form.dispatch_click(fields, self.booking, target)
        self.assertTrue(result["clicked"], result)
        self.assertGreater((result["handler_returned_at"] - result["event_at"]) * 1000, 90)
        self.assertEqual(self.page.evaluate("window.clicks"), 1)

    def test_dispatch_rechecks_fields_changed_during_local_wait(self):
        self.form.prepare(self.booking)
        fields = self.form.arm_click(self.booking)
        target = self.page.evaluate("""() => {
            setTimeout(() => document.querySelector("#pnr").value = "WRONG1", 25);
            return Date.now() + 100;
        }""") / 1000
        self.assertTrue(app.wait_until(round(target * 1e9), threading.Event()))
        result = self.form.dispatch_click(fields, self.booking, target)
        self.assertFalse(result["clicked"])
        self.assertIn("PNR changed", result["reason"])
        self.assertEqual(self.page.evaluate("window.clicks||0"), 0)

    def test_late_event_timing_is_visible_without_claiming_release(self):
        rows = app.timing_table_rows({"target": 1800000000, "event_at": 1800000000.569,
                                      "lateness_ms": 12, "clicked": True})
        click = next(row for row in rows if row[0] == "Construction click")
        self.assertEqual(click[2], "+569.0 ms")
        self.assertIn("NOT API transmission", click[3])

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
        self.request_delay_ms = 0
        self.post_bodies = []
        self.continuations = []
        self.release = threading.Event()
        test = self

        class FixtureHandler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_POST(self):
                test.post_bodies.append(self.rfile.read(int(self.headers.get("Content-Length", 0))))
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
                        request = "fetch('/booking', {method:'POST', body:'fixture'})"
                        action = request
                        if test.request_journey:
                            action += ".then(r=>r.json()).then(data=>fetch(data.next))"
                        if test.request_delay_ms:
                            action = f"setTimeout(() => {action}, {test.request_delay_ms})"
                        if test.busy_click:
                            action += ";const t=Date.now();while(Date.now()-t<2500);"
                        if test.busy_result:
                            action += ";const t=Date.now();while(Date.now()-t<4000);document.body.append('Queue: TRY-1');"
                        content = FORM.replace('window.clicks=(window.clicks||0)+1', action)
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
        endpoint = f"http://127.0.0.1:{self.server.server_port}/booking"

        def install_fixture_gate(gate, context):
            context.route(endpoint, gate.handle)

        self.patches = [
            patch.object(app, "SITE", f"http://127.0.0.1:{self.server.server_port}/"),
            patch.object(app, "BOOKING_REQUEST_SCHEME", "http"),
            patch.object(app, "BOOKING_REQUEST_HOST", "127.0.0.1"),
            patch.object(app, "BOOKING_REQUEST_PATH", "/booking"),
            patch.object(app, "PREPARE_LEAD", self.LEAD),
            patch.object(app, "REQUEST_PREPARE_LEAD", 2),
            patch.object(app, "FINAL_CHECK_LEAD", 3),
            patch.object(app.BookingRequestGate, "install", install_fixture_gate),
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
        with patch.object(app, "PREPARE_LEAD", 6), \
                patch.object(app, "REQUEST_PREPARE_LEAD", 4), \
                patch.object(app, "FINAL_CHECK_LEAD", 5):
            due = self.schedule(ahead=9)
            self.wait_status("submitted", timeout=25)
        time.sleep(0.3)
        self.assertEqual(len(self.clicks), 1)
        self.assertGreaterEqual(self.clicks[0], due)

    def test_busy_page_keeps_one_click_and_reports_api_response(self):
        self.busy_result = True
        self.schedule(ahead=8)
        state = self.wait_status("submitted", timeout=20)
        self.assertEqual(len(self.clicks), 1)
        self.assertEqual(state["click_timing"]["first_response_status"], 200)

    def test_browser_protocol_delay_aborts_late_click(self):
        dispatch = BookingForm.dispatch_click
        def delayed(form, fields, booking, target):
            time.sleep(0.2)
            return dispatch(form, fields, booking, target)
        with patch.object(BookingForm, "dispatch_click", delayed):
            self.schedule()
            state = self.wait_status("error")
        self.assertIn("dispatch window", state["message"])
        self.assertEqual(self.clicks, [])

    def test_missed_click_deadline_aborts_without_retry(self):
        click = self.controller.click
        def delayed(job):
            time.sleep(0.4)
            return click(job)
        with patch.object(self.controller, "click", delayed):
            self.schedule()
            state = self.wait_status("error")
        self.assertIn("missed", state["message"])
        self.assertEqual(self.clicks, [])

    def test_uncertain_dispatch_is_never_retried(self):
        with patch.object(BookingForm, "dispatch_click", side_effect=RuntimeError("Browser disconnected")) as dispatch:
            self.schedule()
            state = self.wait_status("uncertain")
            time.sleep(0.2)
            self.assertEqual(dispatch.call_count, 1)
        self.assertIn("unconfirmed", state["message"])
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

    def schedule(self, ahead=7, milliseconds=0):
        due = int(time.time()) + ahead + milliseconds / 1000
        from zoneinfo import ZoneInfo
        local = datetime.fromtimestamp(due, ZoneInfo("Asia/Singapore"))
        data = {"pnr": "abc123", "departure": "Singapore", "timezone": "Asia/Singapore",
                "date": local.strftime("%Y-%m-%d"), "time": local.strftime("%H:%M:%S.%f")[:-3]}
        self.controller.submit("schedule", data)
        self.wait_status(("scheduled", "preparing", "error"))
        return due - 1

    def test_skill_click_sends_one_post_and_preserves_continuation(self):
        self.request_journey = True
        due = self.schedule(milliseconds=123)
        state = self.wait_status("submitted")
        timing = state["click_timing"]
        self.assertAlmostEqual(timing["target"], due)
        self.assertEqual(timing["checkin"], due + 1)
        self.assertFalse(timing["request_mode"])
        self.assertNotIn("request_held_at", timing)
        time.sleep(0.2)
        self.assertEqual(len(self.clicks), 1)
        self.assertGreaterEqual(self.clicks[0], due)
        self.assertEqual(self.post_bodies, [b"fixture"])
        self.assertEqual(len(self.continuations), 1)
        self.assertTrue(self.controller.snapshot()["browser_open"])

    def test_page_request_delay_remains_observable_after_timed_click(self):
        self.request_delay_ms = 1200
        due = self.schedule(milliseconds=123)
        state = self.wait_status("submitted")
        timing = state["click_timing"]
        self.assertGreaterEqual(timing["event_at"], due)
        self.assertGreater(timing["first_request_at"] - timing["event_at"], 1)
        self.assertNotIn("request_release_called_at", timing)
        self.assertEqual(len(self.clicks), 1)

    def test_response_timeout_leaves_browser_open_without_retry(self):
        self.request_delay_ms = 2000
        with patch.object(app, "RESULT_WATCH", 0.2):
            self.schedule()
            state = self.wait_status("uncertain")
        self.assertIn("unconfirmed", state["message"])
        self.assertTrue(state["browser_open"])
        self.assertEqual(self.post_bodies, [])

    def test_browser_prepares_immediately_and_clicks_at_skill_target(self):
        due = self.schedule()
        self.wait_status("ready")
        self.assertTrue(self.controller.snapshot()["browser_open"])
        self.assertEqual(self.clicks, [])
        state = self.wait_status("submitted")
        self.assertEqual(len(self.clicks), 1)
        self.assertGreaterEqual(self.clicks[0], due)
        self.assertTrue(state["click_timing"]["clicked"])

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

    def test_cancel_pending_schedule_prevents_click(self):
        self.schedule()
        self.controller.submit("cancel", {})
        self.wait_status("idle")
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
