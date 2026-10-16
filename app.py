import json
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

import airportsdata
from playwright.sync_api import Page, sync_playwright


SITE = "https://chopemyseat.flyscoot.com/"
ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
if getattr(sys, "frozen", False):
    BROWSER_CACHE = Path(sys.executable).resolve().parents[1] / "Resources" / "ms-playwright"
    if not BROWSER_CACHE.is_dir():
        raise RuntimeError("The bundled Chromium browser is missing. Reinstall the application.")
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(BROWSER_CACHE)
AIRPORTS = airportsdata.load("IATA")
STATION_TIMEZONE_OVERRIDES = {
    "TYO": "Asia/Tokyo",
    "PNH": "Asia/Phnom_Penh",
    "BWX": "Asia/Jakarta",
    "TRT": "Asia/Makassar",
}


def departure_stations() -> dict:
    data = json.loads((ROOT / "stations.json").read_text())
    for station in data["stations"]:
        code = station["code"]
        station["timezone"] = STATION_TIMEZONE_OVERRIDES.get(code) or AIRPORTS.get(code, {}).get("tz")
        if station["timezone"]:
            ZoneInfo(station["timezone"])
    return data


def station_timezone(departure: str) -> str | None:
    matches = [station for station in departure_stations()["stations"]
               if station["name"].casefold() == departure.casefold()
               or station["code"].casefold() == departure.casefold()]
    zones = {station["timezone"] for station in matches}
    return zones.pop() if len(zones) == 1 else None


def booking_input(data: dict) -> dict:
    pnr = str(data.get("pnr", "")).strip().upper()
    departure = str(data.get("departure", "")).strip()
    zone = str(data.get("timezone", "")).strip()
    if not re.fullmatch(r"[A-Z0-9]{6}", pnr):
        raise ValueError("Enter a six-character alphanumeric PNR.")
    if not departure or len(departure) > 100:
        raise ValueError("Enter the departure station exactly as listed by Scoot.")
    expected_zone = station_timezone(departure)
    if expected_zone and zone != expected_zone:
        raise ValueError(f"The departure station uses {expected_zone}. Select that timezone before preparing.")
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError("Select a valid IANA timezone for the departure airport.") from error
    return {"pnr": pnr, "departure": departure, "timezone": zone}


def scheduled_timestamp(data: dict, now: float | None = None) -> float:
    zone = ZoneInfo(str(data["timezone"]))
    text = f'{data.get("date", "")} {data.get("time", "")}'
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", text):
        raise ValueError("Enter a calendar date and time in HH:mm:ss format.")
    try:
        local = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError as error:
        raise ValueError("The date or time is invalid.") from error
    possibilities = set()
    for fold in (0, 1):
        aware = local.replace(tzinfo=zone, fold=fold)
        stamp = aware.timestamp()
        if datetime.fromtimestamp(stamp, zone).replace(tzinfo=None) == local:
            possibilities.add(stamp)
    if not possibilities:
        raise ValueError("That local time does not exist because of a timezone clock change.")
    if len(possibilities) != 1:
        raise ValueError("That local time occurs twice because of a timezone clock change. Choose an unambiguous time.")
    stamp = possibilities.pop()
    if stamp <= (time.time() if now is None else now):
        raise ValueError("The scheduled date/time must be in the future.")
    return stamp


class BookingForm:
    def __init__(self, page: Page):
        self.page = page

    def fields(self):
        if self.page.get_by_text(re.compile("temporarily unavailable|maintenance", re.I)).count():
            raise ValueError("Scoot is unavailable or under maintenance. Nothing has been scheduled.")
        pnr = self.page.get_by_role("textbox", name=re.compile("Booking Reference|PNR", re.I))
        if pnr.count() != 1:
            pnr = self.page.get_by_role("textbox")
        departure = self.page.get_by_role("combobox", name=re.compile("Departing", re.I))
        if departure.count() != 1:
            departure = self.page.get_by_role("combobox")
        button = self.page.get_by_role("button", name=re.compile(r"^Retrieve Booking\s*$", re.I))
        if pnr.count() != 1 or departure.count() != 1 or button.count() != 1:
            raise ValueError("The booking form could not be uniquely identified. Live-site integration needs review; no click was made.")
        return pnr, departure, button

    def prepare(self, booking: dict):
        pnr, departure, _ = self.fields()
        pnr.fill(booking["pnr"])
        if departure.evaluate("(element) => element.tagName") == "SELECT":
            departure.select_option(label=booking["departure"])
        else:
            departure.click()
            option = self.page.get_by_role("option", name=booking["departure"], exact=True)
            if option.count() != 1:
                raise ValueError("The departure option could not be uniquely identified. Selectors need review; no retrieval was made.")
            option.click()
        self.verify(booking)

    def verify(self, booking: dict):
        pnr, departure, button = self.fields()
        if pnr.input_value().strip().upper() != booking["pnr"]:
            raise ValueError("The browser PNR changed. Prepare the form again.")
        if departure.evaluate("(element) => element.tagName") == "SELECT":
            selected = departure.locator("option:checked").inner_text()
        elif departure.evaluate("(element) => element.tagName") == "INPUT":
            selected = departure.input_value()
        else:
            selected = departure.inner_text()
        if selected.strip() != booking["departure"]:
            raise ValueError("The departure station changed or could not be verified. Prepare the form again.")
        if not button.is_visible() or not button.is_enabled():
            raise ValueError("Retrieve Booking is not available. No click was made.")
        return button

    def observe_result(self, timeout: float = 5.0, stop: threading.Event | None = None) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            text = self.page.locator("body").inner_text(timeout=1500)
            queues = list(dict.fromkeys(re.findall(r"\bQueue\s*:\s*([A-Z]+-\d+|\d+)\b", text, re.I)))
            if queues:
                return {"status": "airport_queue",
                        "message": f"Scoot assigned queue: {', '.join(queues)}. Stop here and attend the airport staff-travel counter in person to queue and wait. No further actions or retries will be made; this is not a confirmed seat."}
            if re.search(r"^\s*(?:unsuccessful\s+(?:staff\s+)?web\s+check[- ]?in|(?:staff\s+)?web\s+check[- ]?in\s+(?:is\s+|was\s+)?unsuccessful)\b", text, re.I | re.M):
                return {"status": "unsuccessful",
                        "message": "Scoot reports unsuccessful web check-in. Automation has stopped. Follow the airline's airport counter instructions; no further actions or retries will be made."}
            if time.monotonic() >= deadline or (stop is not None and stop.is_set()):
                return {"status": "submitted",
                        "message": "Retrieve Booking was clicked once. No recognized queue or failure result was detected during the brief read-only check. Review the Scoot browser yourself; no further actions or retries will be made, and no seat/check-in success is inferred."}
            self.page.wait_for_timeout(100)


class Controller:
    def __init__(self):
        self.lock = threading.Lock()
        self.commands = queue.Queue()
        self.stop = threading.Event()
        self.state = {"status": "idle", "message": "Prepare the booking form before scheduling.", "target": None}
        self.worker = threading.Thread(target=self.run, daemon=True)

    def update(self, status: str, message: str, target: float | None = None):
        with self.lock:
            self.state = {"status": status, "message": message, "target": target}

    def snapshot(self) -> dict:
        with self.lock:
            return {**self.state, "now": time.time()}

    def submit(self, action: str, data: dict):
        if action not in {"prepare", "schedule", "cancel", "show_browser"}:
            raise ValueError("Unknown action.")
        with self.lock:
            status = self.state["status"]
            if action == "show_browser":
                if status in {"working", "submitting", "observing"}:
                    raise ValueError("Wait until the current browser action completes.")
                self.commands.put((action, {}))
                return
            if action == "prepare":
                if status in {"working", "scheduled", "submitting", "observing"}:
                    raise ValueError("Cancel the current schedule before preparing another booking.")
                payload = booking_input(data)
            elif action == "schedule":
                if status != "prepared":
                    raise ValueError("Prepare and verify the booking form first.")
                payload = {"target": scheduled_timestamp(data), "timezone": data["timezone"]}
            else:
                if status in {"submitting", "observing"}:
                    raise ValueError("The click is already in progress and cannot be undone.")
                if status not in {"prepared", "scheduled"}:
                    raise ValueError("There is no prepared or scheduled booking to cancel.")
                payload = {}
            self.state = {"status": "working", "message": f"Processing {action}…", "target": None}
            self.commands.put((action, payload))

    def run(self):
        booking = None
        target = None
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=False)
                context = browser.new_context(viewport={"width": 1280, "height": 900})
                page = context.new_page()
                form = BookingForm(page)
                result_pages = []
                page.on("popup", lambda popup: result_pages.append(popup))
                try:
                    try:
                        page.goto(SITE, wait_until="domcontentloaded", timeout=15000)
                    except Exception as error:
                        self.update("error", f"Initial Scoot page load failed: {type(error).__name__}: {error}. You can try Prepare when the site is available.")
                    while not self.stop.is_set():
                        try:
                            action, payload = self.commands.get(timeout=0.02 if target else 0.2)
                        except queue.Empty:
                            action = None
                        try:
                            if action == "prepare":
                                target = None
                                booking = None
                                result_pages.clear()
                                page.goto(SITE, wait_until="domcontentloaded", timeout=30000)
                                page.get_by_role("button", name=re.compile(r"^Retrieve Booking\s*$", re.I)).wait_for(timeout=15000)
                                form.prepare(payload)
                                booking = payload
                                self.update("prepared", "Form filled and verified. Review the browser, then schedule one retrieval.")
                            elif action == "schedule":
                                if booking is None:
                                    raise ValueError("No prepared booking.")
                                if payload["timezone"] != booking["timezone"]:
                                    raise ValueError("The timezone changed. Prepare again before scheduling.")
                                form.verify(booking)
                                if payload["target"] <= time.time():
                                    raise ValueError("The scheduled time passed during verification. Nothing was clicked.")
                                target = payload["target"]
                                self.update("scheduled", "Waiting for the scheduled time. Keep this app, browser, and computer awake.", target)
                            elif action == "cancel":
                                target = None
                                booking = None
                                self.update("idle", "Cancelled. No future retrieval will be made.")
                            elif action == "show_browser":
                                visible_page = result_pages[-1] if result_pages else page
                                visible_page.bring_to_front()
                            if target is not None and time.time() >= target:
                                due = target
                                target = None
                                with self.lock:
                                    if self.state["status"] != "scheduled":
                                        continue
                                    self.state = {"status": "submitting", "message": "Verifying form and clicking once…", "target": due}
                                if time.time() - due > 2:
                                    raise ValueError("The scheduled time was missed by more than two seconds. No late click was made.")
                                button = form.verify(booking)
                                if time.time() - due > 2:
                                    raise ValueError("Verification took too long. No late click was made.")
                                button.click(timeout=1500, no_wait_after=True)
                                booking = None
                                self.update("observing", "Retrieve Booking was clicked once. Reading the result only; no further clicks will be made.")
                                page.wait_for_timeout(100)
                                visible_page = result_pages[-1] if result_pages else page
                                visible_page.bring_to_front()
                                outcome = BookingForm(visible_page).observe_result(stop=self.stop)
                                self.update(outcome["status"], outcome["message"])
                        except Exception as error:
                            target = None
                            booking = None
                            self.update("error", f"{type(error).__name__}: {error} No automatic retry will be attempted.")
                finally:
                    browser.close()
        except Exception as error:
            self.update("error", f"Browser worker stopped: {type(error).__name__}: {error}. Restart the app after resolving this error.")
            self.stop.set()


class DesktopApp:
    def __init__(self, root: tk.Tk, controller: Controller):
        self.root = root
        self.controller = controller
        self.state = controller.snapshot()
        self.edited = False
        self.closing = False
        self.updating_fields = False
        self.date_was_edited = False
        self.time_was_edited = False
        self.station_data = departure_stations()["stations"]
        self.station_labels = {
            f'{station["name"]} ({station["code"]}) — {station["country"]}': station
            for station in self.station_data
        }
        self.station_names = {}
        for station in self.station_data:
            key = station["name"].casefold()
            self.station_names[key] = station if key not in self.station_names else None
            self.station_names[station["code"].casefold()] = station

        root.title("auto-chope-seat")
        root.minsize(620, 640)
        root.geometry("760x760")
        root.protocol("WM_DELETE_WINDOW", self.close)

        content = ttk.Frame(root, padding=20)
        content.pack(fill="both", expand=True)
        ttk.Label(content, text="auto-chope-seat", font=("TkDefaultFont", 18, "bold")).pack(anchor="w")
        ttk.Label(
            content,
            text="Prepare your Scoot booking and schedule one Retrieve Booking click.",
            wraplength=700,
        ).pack(anchor="w", pady=(4, 14))
        ttk.Label(
            content,
            text="Live integration is not yet verified: Scoot was under maintenance during development. "
                 "The app stops if it cannot identify or verify the form. It does not bypass maintenance, "
                 "CAPTCHA, queues, or check-in opening times.",
            wraplength=700,
            justify="left",
        ).pack(anchor="w", pady=(0, 14))

        self.pnr = tk.StringVar()
        self.departure = tk.StringVar()
        self.timezone = tk.StringVar()
        self.date = tk.StringVar()
        self.clock = tk.StringVar()
        self.timezone_note = tk.StringVar(value="Select a departure station to choose its airport timezone.")
        self.error = tk.StringVar()
        self.status = tk.StringVar(value=self.state["status"])
        self.message = tk.StringVar(value=self.state["message"])
        self.countdown = tk.StringVar()

        form = ttk.Frame(content)
        form.pack(fill="x")
        self._field(form, "Booking reference (PNR)", ttk.Entry(form, textvariable=self.pnr, width=40))
        self.pnr.trace_add("write", self._field_changed)

        station_box = ttk.Combobox(
            form,
            textvariable=self.departure,
            values=tuple(self.station_labels),
            width=55,
        )
        self.station_box = station_box
        self._field(form, "Departing airport / station", station_box)
        station_box.bind("<<ComboboxSelected>>", self._select_departure)
        station_box.bind("<KeyRelease>", self._filter_departures, add="+")
        self.departure.trace_add("write", self._departure_changed)

        timezone_box = ttk.Combobox(
            form,
            textvariable=self.timezone,
            values=tuple(sorted(available_timezones())),
            width=55,
        )
        self._field(form, "Departure airport timezone", timezone_box)
        self.timezone.trace_add("write", self._timezone_changed)
        ttk.Label(form, textvariable=self.timezone_note, wraplength=700, justify="left").pack(
            anchor="w", pady=(0, 10)
        )

        self._field(form, "Retrieval date (airport local time, YYYY-MM-DD)",
                    ttk.Entry(form, textvariable=self.date, width=24))
        self._field(form, "Retrieval time (HH:mm:ss)",
                    ttk.Entry(form, textvariable=self.clock, width=24))
        self.date.trace_add("write", self._date_changed)
        self.clock.trace_add("write", self._time_changed)
        self.prefill_today()

        actions = ttk.Frame(content)
        actions.pack(fill="x", pady=(8, 10))
        self.prepare_button = ttk.Button(actions, text="1. Prepare browser", command=self.prepare)
        self.prepare_button.pack(side="left", padx=(0, 8))
        self.schedule_button = ttk.Button(actions, text="2. Schedule retrieval", command=self.schedule)
        self.schedule_button.pack(side="left", padx=(0, 8))
        self.cancel_button = ttk.Button(actions, text="Cancel", command=self.cancel)
        self.cancel_button.pack(side="left", padx=(0, 8))
        self.browser_button = ttk.Button(actions, text="Show Scoot browser / result", command=self.show_browser)
        self.browser_button.pack(side="left")

        ttk.Separator(content).pack(fill="x", pady=10)
        ttk.Label(content, textvariable=self.status, font=("TkDefaultFont", 12, "bold")).pack(anchor="w")
        ttk.Label(content, textvariable=self.message, wraplength=700, justify="left").pack(
            anchor="w", pady=(4, 2)
        )
        ttk.Label(content, textvariable=self.countdown).pack(anchor="w")
        ttk.Label(content, textvariable=self.error, foreground="#b00020", wraplength=700,
                  justify="left").pack(anchor="w", pady=(4, 8))

        notes = (
            "The Scoot browser is a separate window. Review the PNR and departure there before scheduling.",
            "Use one device for this booking. Keep this app, the browser and computer awake. Exact delivery "
            "time is not guaranteed; a click more than two seconds late is stopped.",
            "If Scoot assigns a queue or reports unsuccessful web check-in, automation stops. Attend the "
            "airport staff-travel counter in person as instructed. The app never retries or refreshes.",
            "PNR is held in memory only and sent to Scoot when you prepare the browser. No booking data is "
            "saved to files. Retrieve Booking does not reserve or confirm a seat; review the result yourself.",
        )
        for note in notes:
            ttk.Label(content, text=note, wraplength=700, justify="left").pack(anchor="w", pady=(5, 0))

        self.refresh()

    @staticmethod
    def _field(parent: ttk.Frame, label: str, widget: ttk.Widget):
        ttk.Label(parent, text=label).pack(anchor="w")
        widget.pack(fill="x", anchor="w", pady=(2, 8))

    def _field_changed(self, *_):
        if not self.updating_fields:
            self.mark_edited()

    def _date_changed(self, *_):
        if not self.updating_fields:
            self.date_was_edited = True
            self.mark_edited()

    def _time_changed(self, *_):
        if not self.updating_fields:
            self.time_was_edited = True
            self.mark_edited()

    def _departure_changed(self, *_):
        if self.updating_fields:
            return
        station = self.station_labels.get(self.departure.get())
        if station is None:
            station = self.station_names.get(self.departure.get().strip().casefold())
        self.updating_fields = True
        try:
            self.timezone.set(station["timezone"] if station else "")
        finally:
            self.updating_fields = False
        self._set_timezone_note(station)
        self.prefill_today(station["timezone"] if station else None)
        self.mark_edited()

    def _select_departure(self, _event=None):
        station = self.station_labels.get(self.departure.get())
        if station is None:
            return
        self.station_box.configure(values=tuple(self.station_labels))
        self.updating_fields = True
        try:
            self.departure.set(station["name"])
            self.timezone.set(station["timezone"])
        finally:
            self.updating_fields = False
        self._set_timezone_note(station)
        self.prefill_today(station["timezone"])
        self.mark_edited()

    def _filter_departures(self, _event=None):
        query = self.departure.get().strip().casefold()
        if not query:
            self.station_box.configure(values=tuple(self.station_labels))
            return
        matches = (
            label for label, station in self.station_labels.items()
            if query in f'{station["name"]} {station["country"]} {station["code"]}'.casefold()
        )
        self.station_box.configure(values=tuple(matches))

    def _timezone_changed(self, *_):
        if self.updating_fields:
            return
        try:
            ZoneInfo(self.timezone.get())
            self.prefill_today(self.timezone.get())
            self.timezone_note.set(f"Using {self.timezone.get()} for the retrieval date and time.")
        except (ZoneInfoNotFoundError, ValueError):
            self.timezone_note.set("Enter or select a valid IANA timezone for the departure airport.")
        self.mark_edited()

    def _set_timezone_note(self, station):
        if station and station["timezone"]:
            self.timezone_note.set(
                f'Automatically selected {station["timezone"]} for {station["name"]} '
                f'({station["code"]}). Daylight saving is applied for the scheduled date.'
            )
        else:
            self.timezone_note.set(
                "Station not recognized or its name is ambiguous. Select a full station suggestion "
                "or enter its airport timezone manually; no timezone is assumed."
            )

    def prefill_today(self, timezone_name: str | None = None):
        zone = ZoneInfo(timezone_name) if timezone_name else None
        now = datetime.now(zone).astimezone() if zone is None else datetime.now(zone)
        self.updating_fields = True
        try:
            if not self.date_was_edited:
                self.date.set(now.strftime("%Y-%m-%d"))
            if not self.time_was_edited:
                self.clock.set(now.strftime("%H:%M:%S"))
        finally:
            self.updating_fields = False

    def mark_edited(self):
        if self.state["status"] == "prepared":
            self.edited = True
            self.message.set("Inputs changed. Prepare the browser again before scheduling.")
            self.refresh_buttons()

    def prepare(self):
        self.error.set("")
        try:
            self.controller.submit("prepare", {
                "pnr": self.pnr.get(),
                "departure": self.departure.get(),
                "timezone": self.timezone.get(),
            })
        except ValueError as error:
            self.error.set(str(error))

    def schedule(self):
        self.error.set("")
        if self.edited:
            self.error.set("Inputs changed. Prepare the browser again before scheduling.")
            return
        try:
            self.controller.submit("schedule", {
                "date": self.date.get(),
                "time": self.clock.get(),
                "timezone": self.timezone.get(),
            })
        except (ValueError, KeyError) as error:
            self.error.set(str(error))

    def cancel(self):
        self.error.set("")
        try:
            self.controller.submit("cancel", {})
        except ValueError as error:
            self.error.set(str(error))

    def show_browser(self):
        self.error.set("")
        try:
            self.controller.submit("show_browser", {})
        except ValueError as error:
            self.error.set(str(error))

    def refresh_buttons(self):
        status = self.state["status"]
        busy = status in {"working", "submitting", "observing"}
        self.prepare_button.configure(state="disabled" if busy or status == "scheduled" else "normal")
        self.schedule_button.configure(
            state="normal" if status == "prepared" and not self.edited else "disabled"
        )
        self.cancel_button.configure(
            state="normal" if status in {"prepared", "scheduled"} else "disabled"
        )
        self.browser_button.configure(state="disabled" if busy else "normal")

    def refresh(self):
        if self.closing:
            if not self.controller.worker.is_alive():
                self.root.destroy()
            else:
                self.root.after(100, self.refresh)
            return
        current = self.controller.snapshot()
        if current["status"] != self.state["status"] and current["status"] == "prepared":
            self.edited = False
        self.state = current
        self.status.set(current["status"])
        self.message.set(
            "Inputs changed. Prepare the browser again before scheduling."
            if self.edited and current["status"] == "prepared" else current["message"]
        )
        target = current.get("target")
        self.countdown.set(
            f'{max(0, target - current["now"]):.1f} seconds remaining'
            if target and current["status"] == "scheduled" else ""
        )
        self.refresh_buttons()
        self.root.after(250, self.refresh)

    def close(self):
        status = self.controller.snapshot()["status"]
        if status == "scheduled" and not messagebox.askyesno(
            "Cancel scheduled retrieval?",
            "Closing the app cancels the pending retrieval. Close anyway?",
            parent=self.root,
        ):
            return
        if status in {"submitting", "observing"} and not messagebox.askyesno(
            "Retrieval in progress",
            "The click cannot be undone. Closing may interrupt result review. Close anyway?",
            parent=self.root,
        ):
            return
        self.closing = True
        self.controller.stop.set()
        self.prepare_button.configure(state="disabled")
        self.schedule_button.configure(state="disabled")
        self.cancel_button.configure(state="disabled")
        self.browser_button.configure(state="disabled")
        self.refresh()


def main():
    root = tk.Tk()
    controller = Controller()
    DesktopApp(root, controller)
    controller.worker.start()
    root.mainloop()


if __name__ == "__main__":
    main()
