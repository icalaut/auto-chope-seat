import json
import os
import plistlib
import queue
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import tempfile
import urllib.request
from datetime import datetime, timezone
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import airportsdata
from playwright.sync_api import Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeout


SITE = "https://chopemyseat.flyscoot.com/home"
HOST = "127.0.0.1"
PORT = 8766
ORIGIN = f"http://{HOST}:{PORT}"
TOKEN = secrets.token_urlsafe(32)
ROOT = Path(__file__).parent
PREPARE_LEAD = 300
RETRY_INTERVAL = 30
RETRY_CUTOFF = 60
MAX_CLICK_LATENESS_MS = 50
FINAL_CHECK_LEAD = 2
RESULT_WATCH = 30
CAPTURE_ROOT = Path.home() / "Library/Application Support/auto-chope-seat/captures"
PNR_NOT_FOUND = re.compile(r"online\s+check[- ]?in\s+is\s+currently\s+not\s+available\s+for\s+your\s+booking", re.I)
CHECKIN_CLOSED = re.compile(r"online\s+check[- ]?in\s+is\s+already\s+closed\s+for\s+this\s+flight", re.I)
NOT_OPEN_YET = re.compile(r"online\s+check[- ]?in\s+is\s+not\s+open\s+yet", re.I)
NOT_OPEN_DETAIL = re.compile(r"for\s+your\s+flight\s+(?P<flight>[A-Z0-9]{2}\s*\d{1,4})\s+at\s+(?P<when>\d{1,2}-\d{1,2}-\d{4}\s+\d{1,2}:\d{2})(?:\s*\((?P<zone>[A-Z]{2,5})\))?", re.I)
DEPARTURE_MISMATCH = re.compile(r"ensure\s+your\s+booking\s+reference\s*\(PNR\)\s+and\s+departing\s+station\s+are\s+entered\s+correctly", re.I)
MIN_SCHEDULE_LEAD = 90
HEADLESS = False
KEEP_AWAKE = True
USE_DEFAULT_BROWSER = True
# Playwright can only drive Chromium-based browsers through their installed executable.
CHROMIUM_BROWSERS = {
    "com.google.chrome", "com.google.chrome.beta", "com.google.chrome.dev", "com.google.chrome.canary",
    "com.microsoft.edgemac", "com.microsoft.edgemac.beta", "com.microsoft.edgemac.dev",
    "com.brave.browser", "org.chromium.chromium", "com.vivaldi.vivaldi", "com.operasoftware.opera",
    "company.thebrowser.browser",
}
ACTIVE_STATUSES = {"working", "scheduled", "preparing", "ready", "submitting", "observing"}
WINDOW = None
RETRIEVE_BUTTON = re.compile(r"^Retrieve Booking\b", re.I)
STATION_TIMEZONE_OVERRIDES = {
    "TYO": "Asia/Tokyo",
    "PNH": "Asia/Phnom_Penh",
    "BWX": "Asia/Jakarta",
    "TRT": "Asia/Makassar",
}


@lru_cache(maxsize=1)
def airport_data() -> dict:
    return airportsdata.load("IATA")


def default_browser() -> dict:
    """Return the OS default browser's name and, if it can be automated, its executable."""
    fallback = {"name": "Chromium (bundled)", "executable": None, "note": ""}
    if not USE_DEFAULT_BROWSER or sys.platform != "darwin":
        return fallback
    try:
        prefs = Path.home() / "Library/Preferences/com.apple.LaunchServices/com.apple.launchservices.secure.plist"
        raw = subprocess.run(["plutil", "-convert", "json", "-o", "-", str(prefs)],
                             capture_output=True, check=True, timeout=5).stdout
        handlers = json.loads(raw).get("LSHandlers", [])
        bundle_id = next((h.get("LSHandlerRoleAll") for h in handlers if h.get("LSHandlerURLScheme") == "https"), None)
    except Exception:
        bundle_id = None
    bundle_id = (bundle_id or "com.apple.safari").lower()
    app_path = None
    try:
        found = subprocess.run(["mdfind", f"kMDItemCFBundleIdentifier == '{bundle_id}'c"],
                               capture_output=True, text=True, timeout=5).stdout.split("\n")
        app_path = next((Path(p) for p in found if p.endswith(".app") and Path(p).exists()), None)
    except Exception:
        pass
    name = bundle_id
    executable = None
    if app_path is not None:
        try:
            with open(app_path / "Contents/Info.plist", "rb") as handle:
                info = plistlib.load(handle)
            name = info.get("CFBundleDisplayName") or info.get("CFBundleName") or app_path.stem
            candidate = app_path / "Contents/MacOS" / info.get("CFBundleExecutable", "")
            executable = str(candidate) if candidate.is_file() else None
        except Exception:
            name = app_path.stem
    if bundle_id in CHROMIUM_BROWSERS and executable:
        return {"name": name, "executable": executable, "note": ""}
    return {**fallback, "note": f"{name} cannot be automated, so the bundled Chromium is used instead."}


def departure_stations() -> dict:
    data = json.loads((ROOT / "stations.json").read_text())
    airports = airport_data()
    for station in data["stations"]:
        code = station["code"]
        station["timezone"] = STATION_TIMEZONE_OVERRIDES.get(code) or airports.get(code, {}).get("tz")
        if station["timezone"]:
            ZoneInfo(station["timezone"])
    return data


def station_timezone(departure: str) -> str | None:
    matches = [station for station in departure_stations()["stations"]
               if station["name"].casefold() == departure.casefold()
               or station["code"].casefold() == departure.casefold()]
    zones = {station["timezone"] for station in matches}
    return zones.pop() if len(zones) == 1 else None


def station_code(departure: str) -> str | None:
    codes = {station["code"] for station in departure_stations()["stations"]
             if station["name"].casefold() == departure.casefold() or station["code"].casefold() == departure.casefold()}
    return codes.pop() if len(codes) == 1 else None


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
    return {"pnr": pnr, "departure": departure, "timezone": zone, "code": station_code(departure)}


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
    current = time.time() if now is None else now
    if local.date() < datetime.fromtimestamp(current, zone).date():
        raise ValueError(f"The retrieval date must be today ({zone.key}) or later.")
    if stamp <= current:
        raise ValueError("The scheduled date/time must be in the future.")
    return stamp


DEPARTURE_OPTION = '[aria-label^="Dropdown option for"][aria-label*="departing"]'



def not_open_message(text: str) -> str:
    detail = NOT_OPEN_DETAIL.search(text)
    if not detail:
        opens = "Scoot did not state the opening time"
    else:
        flight = re.sub(r"\s+", " ", detail["flight"].upper())
        try:
            when = datetime.strptime(re.sub(r"\s+", " ", detail["when"]), "%m-%d-%Y %H:%M").strftime("%Y-%m-%d %H:%M")
        except ValueError:
            when = detail["when"]
        zone = f" ({detail['zone'].upper()})" if detail["zone"] else ""
        opens = f"Check-in for flight {flight} opens at {when}{zone}"
    return (f"Online check-in is not open yet. {opens}. Web check-in is available between 3 hours and 65 minutes before departure; "
            "schedule a new retrieval for the opening time. No further actions or retries will be made.")

class BookingForm:
    def __init__(self, page: Page):
        self.page = page

    def unique(self, *locators):
        for locator in locators:
            visible = locator.filter(visible=True)
            if visible.count() == 1:
                return visible
        return None

    def wait_until_ready(self, timeout: float):
        # Polls with non-waiting checks; avoids Locator.wait_for stalls seen on the live Angular page.
        deadline = time.monotonic() + timeout
        button = self.page.get_by_role("button", name=RETRIEVE_BUTTON)
        while True:
            if self.page.get_by_text(re.compile("temporarily unavailable|maintenance", re.I)).count():
                raise ValueError("Scoot is unavailable or under maintenance.")
            if button.filter(visible=True).count():
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(f"The Retrieve Booking button did not appear within {timeout:.0f} seconds.")
            self.page.wait_for_timeout(250)

    def fields(self):
        if self.page.get_by_text(re.compile("temporarily unavailable|maintenance", re.I)).count():
            raise ValueError("Scoot is unavailable or under maintenance. Nothing has been scheduled.")
        pnr = self.unique(self.page.get_by_role("textbox", name=re.compile("Booking Reference|PNR", re.I)))
        departure = self.unique(
            self.page.get_by_role("combobox", name=re.compile("Departing", re.I)),
            self.page.get_by_role("textbox", name=re.compile("departing", re.I)),
            self.page.get_by_role("combobox"),
        )
        button = self.unique(self.page.get_by_role("button", name=RETRIEVE_BUTTON))
        if pnr is None or departure is None or button is None:
            raise ValueError("The booking form could not be uniquely identified. Live-site integration needs review; no click was made.")
        return pnr, departure, button

    def select_departure(self, departure, name: str, code: str | None = None) -> str:
        """Selects the station and returns the label ChopeMySeat shows for it."""
        tag = departure.evaluate("(element) => element.tagName")
        if tag == "SELECT":
            departure.select_option(label=name)
            return name
        departure.click()
        options = self.page.locator(DEPARTURE_OPTION).filter(visible=True)
        if tag != "INPUT":
            options = self.page.get_by_role("option").filter(visible=True)
        deadline = time.monotonic() + 5
        while True:
            texts = [self.option_lines(options.nth(i)) for i in range(options.count())]
            # Prefer the exact station name; ChopeMySeat labels can differ from the app list, so fall back to the airport code.
            matches = [i for i, lines in enumerate(texts) if lines and lines[0] == name]
            if not matches and code:
                matches = [i for i, lines in enumerate(texts) if len(lines) >= 2 and lines[-1] == code]
            if len(matches) == 1:
                options.nth(matches[0]).click()
                return texts[matches[0]][0]
            if time.monotonic() >= deadline:
                listed = f" ({code})" if code else ""
                raise ValueError(f"The departure station '{name}'{listed} is not offered by ChopeMySeat or could not be uniquely identified. No retrieval was made.")
            self.page.wait_for_timeout(200)

    @staticmethod
    def option_lines(option) -> list[str]:
        # Live options show the station name, country and IATA code on separate lines.
        return [line.strip() for line in option.inner_text().splitlines() if line.strip()]

    def prepare(self, booking: dict):
        pnr, departure, _ = self.fields()
        pnr.fill(booking["pnr"])
        booking["site_departure"] = self.select_departure(departure, booking["departure"], booking.get("code"))
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
        if selected.strip() != booking.get("site_departure", booking["departure"]):
            raise ValueError("The departure station changed or could not be verified. Prepare the form again.")
        if not button.is_visible() or not button.is_enabled():
            raise ValueError("Retrieve Booking is not available. No click was made.")
        return button

    def show_click_target(self, booking: dict, target: float):
        button = self.verify(booking)
        button.evaluate("""(button, target) => {
            let host = document.getElementById("auto-chope-seat-target");
            if (!host) {
                host = document.createElement("div");
                host.id = "auto-chope-seat-target";
                host.style.cssText = "position:fixed;inset:0;pointer-events:none;z-index:2147483647";
                const root = host.attachShadow({mode:"open"});
                root.innerHTML = `<style>
                    * {box-sizing:border-box;pointer-events:none}
                    #ring {position:fixed;border:3px solid #177bce;border-radius:12px}
                    #label {position:fixed;top:12px;right:12px;max-width:340px;padding:12px 16px;
                        border:2px solid #177bce;border-radius:10px;background:#fff;color:#152333;
                        font:600 14px/1.5 system-ui;box-shadow:0 2px 12px #0002}
                </style><div id="ring"></div><div id="label" role="status"></div>`;
                document.documentElement.append(host);
            }
            const root = host.shadowRoot;
            host.updatePosition = () => {
                const rect = button.getBoundingClientRect();
                Object.assign(root.getElementById("ring").style, {
                    left: `${rect.left - 4}px`, top: `${rect.top - 4}px`,
                    width: `${rect.width + 8}px`, height: `${rect.height + 8}px`
                });
            };
            host.updatePosition();
            if (!host.tracking) {
                host.tracking = true;
                addEventListener("scroll", () => host.updatePosition(), {passive:true, capture:true});
                addEventListener("resize", () => host.updatePosition(), {passive:true});
            }
            root.getElementById("label").textContent = "NEXT: Retrieve Booking once at " +
                new Date(target * 1000).toLocaleString([], {hour12:false}) +
                " (computer time). Check app status for aborts.";
            const state = Symbol.for("auto-chope-seat.target-marker");
            if (!button[state]) {
                button[state] = true;
                button.addEventListener("click", () => {
                    root.getElementById("label").textContent =
                        "Click event observed. Waiting for Scoot; do not click again. No seat is confirmed.";
                    root.getElementById("ring").style.borderColor = "#23834b";
                }, {once:true});
            }
        }""", target)

    def arm_click(self, booking: dict) -> dict:
        button = self.verify(booking)
        button.scroll_into_view_if_needed(timeout=1000)
        pnr, departure, _ = self.fields()
        button.evaluate("""button => {
            button.addEventListener("click", () => {
                if (typeof window.reportChopeClick === "function")
                    window.reportChopeClick(Date.now());
            }, {capture: true, once: true});
        }""")
        return {"pnr": pnr.element_handle(), "departure": departure.element_handle(),
                "button": button.element_handle()}

    def dispatch_click(self, fields: dict, booking: dict, target: float) -> dict:
        # One browser task checks the actual dispatch deadline, then invokes the button
        # without Playwright's actionability waits or a second protocol round-trip.
        return self.page.evaluate("""({pnr, departure, button, booking, target, tolerance}) => {
            const refuse = reason => ({clicked: false, reason});
            if (![pnr, departure, button].every(element => element && element.isConnected))
                return refuse("The prepared form was replaced or removed.");
            if (pnr.value.trim().toUpperCase() !== booking.pnr)
                return refuse("The browser PNR changed.");
            const selected = departure.tagName === "SELECT"
                ? departure.selectedOptions[0]?.textContent
                : departure.tagName === "INPUT" ? departure.value : departure.innerText;
            if (selected?.trim() !== (booking.site_departure || booking.departure))
                return refuse("The departure station changed.");
            const visible = element => {
                const rect = element.getBoundingClientRect();
                const style = getComputedStyle(element);
                return rect.width > 0 && rect.height > 0 &&
                    style.visibility === "visible" && style.display !== "none";
            };
            const buttons = [...document.querySelectorAll("button, [role=button]")]
                .filter(element => visible(element) &&
                    /^Retrieve Booking\\b/i.test(element.getAttribute("aria-label") || element.innerText || ""));
            if (buttons.length !== 1 || buttons[0] !== button ||
                button.matches(":disabled") || button.getAttribute("aria-disabled") === "true")
                return refuse("Retrieve Booking is unavailable or ambiguous.");
            if (!visible(pnr) || !visible(departure))
                return refuse("The booking fields are no longer visible.");
            const rect = button.getBoundingClientRect();
            const hit = document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2);
            if (!hit || !button.contains(hit))
                return refuse("Retrieve Booking is covered or outside the viewport.");
            const dispatched = Symbol.for("auto-chope-seat.dispatched");
            if (button[dispatched])
                return refuse("A click was already dispatched for this form.");
            const dispatchedAt = Date.now();
            const lateness = dispatchedAt - target;
            if (lateness < 0 || lateness > tolerance)
                return {clicked: false, reason: lateness < 0
                    ? "The clock is earlier than the scheduled time."
                    : "The 50 ms dispatch deadline was missed.", lateness_ms: lateness};
            button[dispatched] = true;
            let eventAt = null;
            button.addEventListener("click", () => { eventAt = Date.now(); }, {capture: true, once: true});
            button.click();
            return {clicked: true, dispatched_at: dispatchedAt / 1000, lateness_ms: lateness,
                event_at: eventAt === null ? null : eventAt / 1000,
                handler_returned_at: Date.now() / 1000};
        }""", {**fields, "booking": booking, "target": target * 1000,
               "tolerance": MAX_CLICK_LATENESS_MS})

    def passenger_form_visible(self) -> bool:
        for frame in self.page.frames:
            fields = frame.locator("input:visible, select:visible, [role=combobox]:visible")
            names = fields.evaluate_all("""elements => elements.map(element => [
                element.id, element.name, element.placeholder, element.getAttribute("aria-label"),
                ...[...(element.labels || [])].map(label => label.innerText),
                ...((element.getAttribute("aria-labelledby") || "").split(/\\s+/)
                    .map(id => document.getElementById(id)?.textContent || ""))
            ].filter(Boolean).join(" ")).join("\\n")""")
            if re.search(r"passport", names, re.I) and re.search(r"nationality|citizenship", names, re.I):
                return True
        return False

    def capture_passenger_form(self) -> dict:
        CAPTURE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
        folder = Path(tempfile.mkdtemp(prefix=datetime.now().strftime("%Y%m%d-%H%M%S-"), dir=CAPTURE_ROOT))
        report = {"captured_at": datetime.now(timezone.utc).isoformat(),
                  "note": "Passenger form detected, not confirmation of a seat. No fields entered or submitted.",
                  "limitations": "Only currently loaded DOM options are available. Virtualized, dependent, or remote lists may be incomplete.",
                  "frames": [], "errors": []}

        def save_part(name: str, action: Callable):
            try:
                action()
            except Exception as error:
                report["errors"].append(f"{name}: {type(error).__name__}: {error}")

        def save_archive():
            session = self.page.context.new_cdp_session(self.page)
            try:
                snapshot = session.send("Page.captureSnapshot", {"format": "mhtml"})
                (folder / "page.mhtml").write_text(snapshot["data"], encoding="utf-8")
            finally:
                session.detach()

        try:
            save_part("Screenshot", lambda: self.page.screenshot(
                path=str(folder / "page.png"), full_page=True, timeout=10000))
            save_part("HTML", lambda: (folder / "page.html").write_text(self.page.content(), encoding="utf-8"))
            save_part("MHTML", save_archive)
            for frame_index, frame in enumerate(self.page.frames):
                if frame != self.page.main_frame:
                    save_part(f"Frame {frame_index} HTML", lambda: (folder / f"frame-{frame_index}.html").write_text(
                        frame.content(), encoding="utf-8"))
                data = frame.evaluate("""() => {
                    const label = element => element.getAttribute("aria-label") ||
                        [...(element.labels || [])].map(label => label.innerText).join(" ") ||
                        (element.getAttribute("aria-labelledby") || "").split(/\\s+/)
                            .map(id => document.getElementById(id)?.textContent || "").join(" ") ||
                        element.name || element.id;
                    return {
                        fields: [...document.querySelectorAll("input, select, textarea, [role=combobox]")].map(element => ({
                            tag: element.tagName, type: element.type || null, id: element.id,
                            name: element.name || null, label: label(element),
                            placeholder: element.getAttribute("placeholder"), required: element.required ||
                                element.getAttribute("aria-required") === "true",
                            disabled: element.disabled || element.getAttribute("aria-disabled") === "true",
                            options: element.tagName === "SELECT" ? [...element.options].map(option => ({
                                label: option.textContent.trim(), value: option.value, disabled: option.disabled
                            })) : null
                        })),
                        loaded_options: [...document.querySelectorAll('[role=option], .dropdown-option, [aria-label^="Dropdown option"]')]
                            .map(option => ({label: option.textContent.trim(), value: option.getAttribute("data-value")}))
                    };
                }""")
                data["expanded_dropdowns"] = []
                # Expand only list widgets, never options or submit/continue buttons.
                dropdowns = frame.locator(
                    '[role=combobox]:not(select):visible, input[aria-haspopup="listbox"]:visible, '
                    'input[aria-label^="Dropdown input"]:visible')
                for index in range(dropdowns.count()):
                    dropdown = dropdowns.nth(index)
                    entry = {"index": index, "options": []}
                    data["expanded_dropdowns"].append(entry)
                    if not dropdown.is_enabled():
                        entry["note"] = "Disabled; not opened."
                        continue
                    try:
                        dropdown.click(timeout=2000)
                        options = frame.locator('[role=option], .dropdown-option, [aria-label^="Dropdown option"]')
                        options.filter(visible=True).first.wait_for(timeout=2000)
                        entry["options"] = options.evaluate_all(
                            "elements => elements.map(element => ({label: element.textContent.trim(), value: element.getAttribute('data-value')}))")
                        dropdown.press("Escape", timeout=2000)
                    except PlaywrightTimeout as error:
                        entry["error"] = str(error)
                        report["errors"].append(f"Dropdown {index} could not be fully read: {error}")
                report["frames"].append(data)
        except Exception as error:
            report["errors"].append(f"{type(error).__name__}: {error}")
        finally:
            (folder / "fields.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            for file in folder.iterdir():
                file.chmod(0o600)
        incomplete = bool(report["errors"])
        return {"status": "capture_incomplete" if incomplete else "passenger_details",
                "message": f"Passport/nationality page detected. {'Capture incomplete' if incomplete else 'Capture saved'} at {folder}.\n"
                           + ("\n".join(report["errors"]) + "\n" if incomplete else "")
                           + "Screenshot, saved page and field inventory are for local review. Custom dropdown lists may be incomplete; see fields.json. "
                           "No passenger information was entered or submitted. A seat is not confirmed; continue manually in Scoot's browser."}

    def observe_result(self, timeout: float | None = None, stop: threading.Event | None = None,
                       page_provider: Callable[[], Page] | None = None) -> dict:
        deadline = time.monotonic() + (RESULT_WATCH if timeout is None else timeout)
        while True:
            if page_provider is not None:
                self.page = page_provider()
            try:
                text = self.page.locator("body").inner_text(timeout=2000)
            except Exception:
                # The page may still be busy or navigating after the click; keep reading until the deadline.
                text = ""
            queues = list(dict.fromkeys(re.findall(r"\bQueue\s*:\s*([A-Z]+-\d+|\d+)\b", text, re.I)))
            if queues:
                return {"status": "airport_queue",
                        "message": f"Scoot assigned queue: {', '.join(queues)}. Stop here and attend the airport staff-travel counter in person to queue and wait. No further actions or retries will be made; this is not a confirmed seat."}
            if re.search(r"^\s*(?:unsuccessful\s+(?:staff\s+)?web\s+check[- ]?in|(?:staff\s+)?web\s+check[- ]?in\s+(?:is\s+|was\s+)?unsuccessful)\b", text, re.I | re.M):
                return {"status": "unsuccessful",
                        "message": "Scoot reports unsuccessful web check-in. Automation has stopped. Follow the airline's airport counter instructions; no further actions or retries will be made."}
            if CHECKIN_CLOSED.search(text):
                return {"status": "checkin_closed",
                        "message": "Scoot says online check-in is already closed for this flight (PNR and departure matched, but the flight is in the past). Check the booking and date. No further actions or retries will be made."}
            if NOT_OPEN_YET.search(text):
                return {"status": "not_open_yet", "message": not_open_message(text)}
            if PNR_NOT_FOUND.search(text):
                return {"status": "pnr_not_found",
                        "message": "Scoot says online check-in is not available for this booking (Booking Reference/PNR not found). Please go to the airport counter. No further actions or retries will be made."}
            if DEPARTURE_MISMATCH.search(text):
                return {"status": "departure_mismatch",
                        "message": "Scoot found the Booking Reference (PNR) but the Departing station does not match it. Check the departure station for this booking. No further actions or retries will be made."}
            if self.passenger_form_visible():
                return self.capture_passenger_form()
            if time.monotonic() >= deadline or (stop is not None and stop.is_set()):
                return {"status": "submitted",
                        "message": "Retrieve Booking was clicked once. No recognized queue or failure result was detected during the brief read-only check. Review the Scoot browser yourself; no further actions or retries will be made, and no seat/check-in success is inferred."}
            time.sleep(0.1)


class Controller:
    def __init__(self):
        self.lock = threading.Lock()
        self.commands = queue.Queue()
        self.stop = threading.Event()
        self.state = {"status": "idle", "message": "Enter the booking and schedule the retrieval time.", "target": None}
        self.worker = threading.Thread(target=self.run, daemon=True)
        self.playwright = None
        self.browser = None
        self.page = None
        self.result_pages = []
        self.browser_open = False
        self.caffeinate = None
        self.browser_info = {"name": "", "executable": None, "note": ""}
        self.click_timing = None
        self.observed_request = None

    def click_observed(self, source, stamp: float):
        with self.lock:
            if self.click_timing is None:
                return
            self.click_timing.update(clicked=True, event_at=stamp / 1000)
            self.state = {"status": "observing",
                          "message": "Retrieve Booking's click event was observed. Waiting for Scoot's request and result; no second click will be made.",
                          "target": None}

    def request_observed(self, request):
        if request.resource_type not in {"xhr", "fetch"}:
            return
        with self.lock:
            if self.click_timing is None or self.observed_request is not None:
                return
            observed = time.time()
            if observed < self.click_timing["target"]:
                return
            self.observed_request = request
            self.click_timing["first_request_at"] = observed

    def response_observed(self, response):
        if response.request != self.observed_request:
            return
        with self.lock:
            if self.click_timing is not None:
                self.click_timing["first_response_at"] = time.time()
                self.click_timing["first_response_status"] = response.status
                started = response.request.timing["startTime"]
                if started > 0:
                    self.click_timing["first_request_started_at"] = started / 1000

    def update(self, status: str, message: str, target: float | None = None):
        with self.lock:
            self.state = {"status": status, "message": message, "target": target}

    def snapshot(self) -> dict:
        with self.lock:
            target = self.state["target"]
            return {**self.state, "now": time.time(), "browser_open": self.browser_open,
                    "browser_name": self.browser_info["name"], "browser_note": self.browser_info["note"],
                    "click_timing": self.click_timing,
                    "prepare_at": target - PREPARE_LEAD if target else None}

    def submit(self, action: str, data: dict):
        if action not in {"schedule", "cancel", "show_browser"}:
            raise ValueError("Unknown action.")
        with self.lock:
            status = self.state["status"]
            if action == "show_browser":
                if not self.browser_open:
                    raise ValueError(f"The Scoot browser opens {PREPARE_LEAD // 60} minutes before the retrieval time.")
                if status in {"working", "preparing", "submitting", "observing"}:
                    raise ValueError("Wait until the current browser action completes.")
                self.commands.put((action, {}))
                return
            if action == "schedule":
                if status in ACTIVE_STATUSES:
                    raise ValueError("Cancel the current schedule before scheduling another booking.")
                payload = booking_input(data)
                payload = {"booking": payload, "target": scheduled_timestamp({**data, "timezone": payload["timezone"]})}
                if payload["target"] - time.time() < MIN_SCHEDULE_LEAD:
                    raise ValueError(f"Schedule at least {MIN_SCHEDULE_LEAD} seconds ahead so the browser can be prepared.")
            else:
                if status in {"submitting", "observing"}:
                    raise ValueError("The click is already in progress and cannot be undone.")
                if status not in {"scheduled", "preparing", "ready"}:
                    raise ValueError("There is no scheduled retrieval to cancel.")
                payload = {}
            self.state = {"status": "working", "message": f"Processing {action}…", "target": None}
            self.commands.put((action, payload))

    def keep_awake(self, enabled: bool):
        # Prevents idle/system sleep while a retrieval is pending (macOS only; closing the lid can still sleep).
        if enabled and self.caffeinate is None and sys.platform == "darwin" and KEEP_AWAKE:
            try:
                self.caffeinate = subprocess.Popen(["caffeinate", "-dims", "-w", str(os.getpid())])
            except OSError:
                self.caffeinate = None
        elif not enabled and self.caffeinate is not None:
            self.caffeinate.terminate()
            self.caffeinate = None

    def open_browser(self):
        self.close_browser()
        self.browser = self.playwright.chromium.launch(
            headless=HEADLESS, executable_path=self.browser_info["executable"],
            args=["--disable-background-timer-throttling", "--disable-renderer-backgrounding",
                  "--disable-backgrounding-occluded-windows"])
        context = self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.page = context.new_page()
        self.page.expose_binding("reportChopeClick", self.click_observed)
        self.page.on("request", self.request_observed)
        self.page.on("response", self.response_observed)
        self.result_pages = []
        self.page.on("popup", lambda popup: self.result_pages.append(popup))
        with self.lock:
            self.browser_open = True

    def close_browser(self):
        browser, self.browser, self.page, self.result_pages = self.browser, None, None, []
        with self.lock:
            self.browser_open = False
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass

    def visible_page(self) -> Page:
        open_pages = [page for page in self.result_pages if not page.is_closed()]
        return open_pages[-1] if open_pages else self.page

    def prepare_page(self, booking: dict, target: float):
        if self.page is None or self.page.is_closed() or not self.browser.is_connected():
            self.open_browser()
        self.result_pages.clear()
        budget = lambda limit: max(1000, min(limit, (target - 10 - time.time()) * 1000))
        self.page.goto(SITE, wait_until="domcontentloaded", timeout=budget(45000))
        try:
            # Slow third-party assets can delay "load" indefinitely; the form check below is authoritative.
            self.page.wait_for_load_state("load", timeout=budget(15000))
        except PlaywrightTimeout:
            pass
        form = BookingForm(self.page)
        form.wait_until_ready(budget(15000) / 1000)
        form.prepare(booking)

    def advance(self, job: dict) -> dict | None:
        now = time.time()
        target = job["target"]
        if job["ready"]:
            if now >= target:
                self.click(job)
                return None
            if target - now <= FINAL_CHECK_LEAD and "click_fields" not in job:
                self.page.bring_to_front()
                job["click_fields"] = BookingForm(self.page).arm_click(job["booking"])
                BookingForm(self.page).show_click_target(job["booking"], target)
                return job
            if now >= job["next_check"] and target - now > RETRY_CUTOFF:
                try:
                    BookingForm(self.page).verify(job["booking"])
                    job["next_check"] = now + RETRY_INTERVAL
                except Exception as error:
                    job["ready"] = False
                    job["next_attempt"] = now
                    job["last_error"] = f"{type(error).__name__}: {error}"
            return job
        if now < job["next_attempt"]:
            return job
        if target - now < RETRY_CUTOFF:
            reason = f" Last error: {job['last_error']}" if job["last_error"] else ""
            raise ValueError(f"The booking form was not ready {RETRY_CUTOFF} seconds before retrieval, so nothing was clicked.{reason}")
        job["attempts"] += 1
        self.update("preparing", f"Opening {self.browser_info['name']} on Scoot and filling entries (attempt {job['attempts']}). Nothing will be clicked yet.", target)
        try:
            self.prepare_page(job["booking"], target)
        except Exception as error:
            job["last_error"] = f"{type(error).__name__}: {error}"
            job["next_attempt"] = time.time() + RETRY_INTERVAL
            if target - job["next_attempt"] < RETRY_CUTOFF:
                raise ValueError(f"Preparation failed and no retry fits before the {RETRY_CUTOFF}-second cutoff, so nothing was clicked. Last error: {job['last_error']}")
            retry_at = datetime.fromtimestamp(job["next_attempt"], ZoneInfo(job["booking"]["timezone"])).strftime("%H:%M:%S")
            self.update("preparing", f"Preparation attempt {job['attempts']} failed: {job['last_error']}\nRetrying at {retry_at} (airport time). Nothing has been clicked.", target)
            return job
        job["ready"] = True
        BookingForm(self.page).show_click_target(job["booking"], target)
        job["next_check"] = time.time() + RETRY_INTERVAL
        self.update("ready", "Entries filled and verified. Standing by to click Retrieve Booking once at the scheduled time.", target)
        return job

    def click(self, job: dict):
        due = job["target"]
        with self.lock:
            if self.state["status"] != "ready":
                return
            self.state = {"status": "submitting", "message": "Checking the 50 ms dispatch deadline…", "target": due}
        if time.time() > due + MAX_CLICK_LATENESS_MS / 1000:
            raise ValueError("The 50 ms dispatch deadline was missed. No click was made.")
        if "click_fields" not in job:
            raise ValueError("Final verification was not completed before the deadline. No click was made.")
        with self.lock:
            self.click_timing = {"target": due}
            self.observed_request = None
        try:
            result = BookingForm(self.page).dispatch_click(job["click_fields"], job["booking"], due)
        except Exception as error:
            self.update("uncertain", f"Click dispatch could not be confirmed ({type(error).__name__}: {error}). "
                        "A click may have been sent. Review the Scoot browser; no retry will be made.")
            return
        with self.lock:
            self.click_timing.update(result)
        if not result["clicked"]:
            raise ValueError(f"{result['reason']} No click was made.")
        self.update("observing", "Retrieve Booking was clicked once. Watching the result and capturing the passenger form if shown. No second retrieval click will be made.")
        try:
            self.page.wait_for_timeout(100)
            page = self.visible_page()
            page.bring_to_front()
            outcome = BookingForm(page).observe_result(stop=self.stop, page_provider=self.visible_page)
        except Exception as error:
            outcome = {"status": "submitted",
                       "message": f"Retrieve Booking was clicked once, but the result could not be read ({type(error).__name__}). "
                                  "Review the Scoot browser yourself; no further actions or retries will be made."}
        self.update(outcome["status"], outcome["message"])

    def scheduled_message(self, job: dict) -> str:
        zone = ZoneInfo(job["booking"]["timezone"])
        at = datetime.fromtimestamp(job["target"], zone).strftime("%Y-%m-%d %H:%M:%S")
        opens = datetime.fromtimestamp(job["target"] - PREPARE_LEAD, zone).strftime("%H:%M:%S")
        note = f" {self.browser_info['note']}" if self.browser_info["note"] else ""
        return (f"Scheduled for {at} ({zone.key}). {self.browser_info['name']} launches at {opens} to load Scoot and fill the entries, "
                f"then attempts one Retrieve Booking click within 50 ms of the scheduled time, otherwise aborts.{note} "
                "This is a local dispatch limit, not a guarantee of Scoot's receipt time. Keep the laptop awake, plugged in and open.")

    def run(self):
        try:
            with sync_playwright() as playwright:
                self.playwright = playwright
                try:
                    self.loop()
                finally:
                    self.close_browser()
                    self.keep_awake(False)
        except Exception as error:
            self.update("error", f"Browser worker stopped: {type(error).__name__}: {error}. Restart the app after resolving this error.")
            self.stop.set()

    def loop(self):
        job = None
        while not self.stop.is_set():
            wait = 0.2
            if job is not None:
                wait = min(wait, max(0.001, (job["target"] - time.time()) / 2))
            try:
                action, payload = self.commands.get(timeout=wait)
            except queue.Empty:
                action = None
            try:
                if action == "schedule":
                    self.close_browser()
                    with self.lock:
                        self.click_timing = None
                        self.observed_request = None
                    self.browser_info = default_browser()
                    job = {**payload, "ready": False, "attempts": 0, "last_error": "",
                           "next_attempt": payload["target"] - PREPARE_LEAD}
                    self.keep_awake(True)
                    self.update("scheduled", self.scheduled_message(job), job["target"])
                elif action == "cancel":
                    job = None
                    self.close_browser()
                    self.keep_awake(False)
                    self.update("idle", "Cancelled. No retrieval will be made.")
                elif action == "show_browser" and self.page is not None:
                    self.visible_page().bring_to_front()
                if job is not None:
                    job = self.advance(job)
                    if job is None:
                        self.keep_awake(False)
                    elif not job["ready"] and self.state["status"] == "ready":
                        self.update("preparing", f"Entries could not be re-verified ({job['last_error']}). Preparing again; nothing was clicked.", job["target"])
            except Exception as error:
                job = None
                self.keep_awake(False)
                self.update("error", f"{type(error).__name__}: {error} No automatic retry will be attempted.")


controller = Controller()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def send(self, status: int, body: bytes, content_type: str):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def json_response(self, status: int, value: dict):
        self.send(status, json.dumps(value).encode(), "application/json")

    def valid_host(self):
        return self.headers.get("Host") == f"{HOST}:{PORT}"

    def do_GET(self):
        if not self.valid_host():
            self.json_response(403, {"error": "Invalid host."})
        elif self.path == "/api/status":
            self.json_response(200, controller.snapshot())
        elif self.path == "/api/config":
            self.json_response(200, {"token": TOKEN})
        elif self.path == "/api/stations":
            self.json_response(200, departure_stations())
        elif self.path in {"/", "/app.js", "/style.css", "/icon.png", "/stations.json", "/timezones.json"}:
            filename = "index.html" if self.path == "/" else self.path[1:]
            content_type = {"/": "text/html; charset=utf-8", "/app.js": "text/javascript", "/style.css": "text/css",
                            "/icon.png": "image/png", "/stations.json": "application/json",
                            "/timezones.json": "application/json"}[self.path]
            self.send(200, (ROOT / filename).read_bytes(), content_type)
        else:
            self.json_response(404, {"error": "Not found."})

    def do_POST(self):
        if self.path == "/api/focus" and self.valid_host():
            # Lets a second launch bring the existing window forward; it exposes no data or actions.
            if WINDOW is not None:
                WINDOW.restore()
                WINDOW.show()
            self.json_response(202 if WINDOW is not None else 404, {"window": WINDOW is not None})
            return
        if not self.valid_host() or self.headers.get("Origin") != ORIGIN or self.headers.get("X-App-Token") != TOKEN:
            self.json_response(403, {"error": "Unauthorized local request."})
            return
        if self.path != "/api/action":
            self.json_response(404, {"error": "Not found."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise ValueError("Invalid request size.")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("Expected an object.")
            if controller.stop.is_set():
                raise ValueError("Browser worker unavailable. Restart the app.")
            if data.get("action") == "quit":
                if controller.snapshot()["status"] in {"submitting", "observing"}:
                    raise ValueError("The click is in progress. Quit after it completes.")
                self.json_response(202, {"accepted": True})
                if WINDOW is not None:
                    WINDOW.destroy()
                else:
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            controller.submit(data.get("action"), data)
            self.json_response(202, {"accepted": True})
        except (ValueError, ZoneInfoNotFoundError) as error:
            self.json_response(400, {"error": str(error)})


def stop_on_terminate(*_):
    if WINDOW is not None:
        WINDOW.destroy()
    else:
        raise KeyboardInterrupt


def window_closing(window):
    status = controller.snapshot()["status"]
    if status in {"submitting", "observing"}:
        return False
    window.confirm_close = status in {"scheduled", "preparing", "ready"}
    window.localization["global.quitConfirmation"] = "Quit auto-chope-seat and cancel the scheduled retrieval?"


def focus_running_window() -> bool:
    try:
        request = urllib.request.Request(f"{ORIGIN}/api/focus", data=b"", method="POST")
        with urllib.request.urlopen(request, timeout=2):
            return True
    except Exception:
        return False


def run_window(server: ThreadingHTTPServer):
    global WINDOW
    import webview

    if sys.platform == "darwin":
        try:
            from Foundation import NSBundle
            NSBundle.mainBundle().infoDictionary()["CFBundleName"] = "auto-chope-seat"
        except Exception:
            pass
    threading.Thread(target=server.serve_forever, daemon=True).start()
    WINDOW = webview.create_window("auto-chope-seat", ORIGIN, width=1100, height=900, min_size=(640, 560))
    WINDOW.events.closing += window_closing
    try:
        webview.start()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()


def main():
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        if focus_running_window():
            sys.exit(0)
        print(f"auto-chope-seat could not start: port {PORT} is in use (is it already running?).", flush=True)
        sys.exit(1)
    signal.signal(signal.SIGTERM, stop_on_terminate)
    controller.worker.start()
    print(f"auto-chope-seat: {ORIGIN} (native window)", flush=True)
    try:
        run_window(server)
    except KeyboardInterrupt:
        pass
    finally:
        print("Stopping; pending retrievals are cancelled.", flush=True)
        controller.stop.set()
        controller.worker.join(timeout=35)
        server.server_close()


if __name__ == "__main__":
    main()
