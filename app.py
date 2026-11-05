import calendar
import json
import os
import plistlib
import queue
import re
import subprocess
import sys
import threading
import time
import tempfile
import tkinter as tk
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

import airportsdata
from playwright.sync_api import BrowserContext, Page, Route, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeout


SITE = "https://chopemyseat.flyscoot.com/home"
ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
if getattr(sys, "frozen", False):
    BROWSER_CACHE = Path(sys.executable).resolve().parents[1] / "Resources" / "ms-playwright"
    if not BROWSER_CACHE.is_dir():
        raise RuntimeError("The bundled Chromium browser is missing. Reinstall the application.")
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(BROWSER_CACHE)
PREPARE_LEAD = 720
PAGE_LOAD_TIMEOUT_MS = 120000
FOREGROUND_INTERVAL = 1
RETRY_INTERVAL = 30
RETRY_CUTOFF = 60
MAX_RELEASE_LATENESS_MS = 50
REQUEST_PREPARE_LEAD = 15
FINAL_CHECK_LEAD = 2
RELEASE_POLICY = ("Retrieve Booking clicked once at check-in minus one second. No automatic retries. "
                  "A missed local dispatch window aborts. Click timing is not proof of server receipt.")
DISPATCH_LEAD = 0.25
MAX_REQUEST_TRACE = 100
BOOKING_REQUEST_SCHEME = "https"
BOOKING_REQUEST_HOST = "apigw.singaporeair.com"
BOOKING_REQUEST_PATH = "/chopemyseat/booking/retrieval"
RESULT_WATCH = 30
CAPTURE_ROOT = Path.home() / "Library/Application Support/auto-chope-seat/captures"
PNR_NOT_FOUND = re.compile(r"online\s+check[- ]?in\s+is\s+currently\s+not\s+available\s+for\s+your\s+booking", re.I)
CHECKIN_CLOSED = re.compile(r"online\s+check[- ]?in\s+is\s+already\s+closed\s+for\s+this\s+flight", re.I)
NOT_OPEN_YET = re.compile(r"online\s+check[- ]?in\s+is\s+not\s+open\s+yet", re.I)
NOT_OPEN_DETAIL = re.compile(r"for\s+your\s+flight\s+(?P<flight>[A-Z0-9]{2}\s*\d{1,4})\s+at\s+(?P<when>\d{1,2}-\d{1,2}-\d{4}\s+\d{1,2}:\d{2})(?:\s*\((?P<zone>[A-Z]{2,5})\))?", re.I)
DEPARTURE_MISMATCH = re.compile(r"ensure\s+your\s+booking\s+reference\s*\(PNR\)\s+and\s+departing\s+station\s+are\s+entered\s+correctly", re.I)
MIN_SCHEDULE_LEAD = 90
# ACS_HEADLESS=1 runs Chromium headless (required for the cloud server); the desktop app shows it.
HEADLESS = os.environ.get("ACS_HEADLESS", "") == "1"
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
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d{3})?", text):
        raise ValueError("Enter a calendar date and time in HH:mm:ss.SSS format (three millisecond digits).")
    try:
        local = datetime.strptime(text, "%Y-%m-%d %H:%M:%S.%f" if "." in text else "%Y-%m-%d %H:%M:%S")
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


def wait_until(target_ns: int, stop: threading.Event) -> bool:
    while not stop.is_set():
        remaining = target_ns - time.time_ns()
        if remaining <= 0:
            return True
        if remaining > 5_000_000:
            stop.wait(min(1, (remaining - 2_000_000) / 1_000_000_000))
        else:
            end = time.monotonic_ns() + remaining
            while time.monotonic_ns() < end and time.time_ns() < target_ns:
                if stop.is_set():
                    return False
    return False

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
            if self.page.get_by_text(re.compile(r"access denied|verify you are human", re.I)).filter(visible=True).count():
                raise ValueError("Scoot denied access or requires manual verification. No retrieval was made.")
            if button.filter(visible=True).count() == 1:
                pnr = self.unique(self.page.get_by_role("textbox", name=re.compile("Booking Reference|PNR", re.I)))
                departure = self.unique(
                    self.page.get_by_role("combobox", name=re.compile("Departing", re.I)),
                    self.page.get_by_role("textbox", name=re.compile("departing", re.I)),
                    self.page.get_by_role("combobox"),
                )
                if (pnr is not None and departure is not None and pnr.is_editable() and
                        departure.is_enabled()):
                    return
            if time.monotonic() >= deadline:
                raise TimeoutError(f"The booking form did not become ready within {timeout:.0f} seconds. No retrieval was made.")
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
            # Wait for the actual requested station, including asynchronously loaded native options.
            option = departure.locator("option").filter(has_text=re.compile(r"^\s*" + re.escape(name) + r"\s*$"))
            option.wait_for(state="attached", timeout=5000)
            if option.count() != 1:
                raise ValueError("The departure station is ambiguous. Choose a unique airport.")
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
        booking["site_departure"] = self.select_departure(departure, booking["departure"], booking.get("code"))
        pnr.fill(booking["pnr"])
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
        button.evaluate("""(button, targetLabel) => {
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
                targetLabel + ". Check-in minus one second.";
            const state = Symbol.for("auto-chope-seat.target-marker");
            if (!button[state]) {
                button[state] = true;
                button.addEventListener("click", () => {
                    root.getElementById("label").textContent =
                        "Retrieve Booking clicked. Continue manually; do not click again.";
                    root.getElementById("ring").style.borderColor = "#23834b";
                }, {once:true});
            }
        }""", display_time(target, booking["timezone"]))

    def arm_click(self, booking: dict) -> dict:
        button = self.verify(booking)
        button.scroll_into_view_if_needed(timeout=1000)
        pnr, departure, _ = self.fields()
        return {"pnr": pnr.element_handle(), "departure": departure.element_handle(),
                "button": button.element_handle()}

    def dispatch_click(self, fields: dict, booking: dict, target: float) -> dict:
        # Keep verification and the single click atomic in the page; timing is handled locally.
        return self.page.evaluate("""({pnr, departure, button, booking, target}) => {
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
            if (booking.timed_dispatch && (Date.now() < target || Date.now() - target > 50))
                return refuse("Missed the single-click dispatch window.");
            const dispatched = Symbol.for("auto-chope-seat.dispatched");
            if (button[dispatched])
                return refuse("A click was already dispatched for this form.");
            const dispatchedAt = Date.now();
            const lateness = dispatchedAt - target;
            button[dispatched] = true;
            let eventAt = null;
            const guard = event => {
                if (event.target !== button && !button.contains(event.target)) return;
                if (eventAt !== null) {
                    event.preventDefault();
                    event.stopImmediatePropagation();
                    return;
                }
                eventAt = Date.now();
                if (typeof window.reportChopeClick === "function")
                    window.reportChopeClick(eventAt);
            };
            window.addEventListener("click", guard, {capture: true});
            try {
                button.click();
            } finally {
                window.removeEventListener("click", guard, {capture: true});
            }
            if (eventAt === null)
                return {clicked: false,
                    reason: "No click event was observed. Review the browser; no retry will be made.",
                    lateness_ms: lateness};
            return {clicked: true, dispatched_at: dispatchedAt / 1000, lateness_ms: lateness,
                event_at: eventAt / 1000,
                handler_returned_at: Date.now() / 1000};
        }""", {**fields, "booking": booking, "target": target * 1000})

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


class BookingRequestGate:
    def __init__(self, target: float, stop: threading.Event,
                 report: Callable[[dict], None] | None = None):
        self.target_ns = round(target * 1_000_000_000)
        self.stop = stop
        self.report = report
        self.claimed = False
        self.expired = False
        self.finished = False
        self.timing = {}

    def publish(self):
        if self.report is not None:
            self.report(dict(self.timing))

    def install(self, context: BrowserContext):
        endpoint = f"https://{BOOKING_REQUEST_HOST}{BOOKING_REQUEST_PATH}"
        context.route(re.compile(r"^" + re.escape(endpoint) + r"(?:\?.*)?$"), self.handle)

    def handle(self, route: Route):
        if route.request.method != "POST":
            route.continue_()
            return
        if self.claimed or self.expired:
            route.abort("blockedbyclient")
            return
        # Claim before any browser command, including an uncertain continuation.
        self.claimed = True
        try:
            self.release(route)
        finally:
            self.finished = True

    def release(self, route: Route):
        intercepted_ns = time.time_ns()
        self.timing["request_held_at"] = intercepted_ns / 1_000_000_000
        self.timing["request_intercept_offset_ms"] = (intercepted_ns - self.target_ns) / 1_000_000
        ready = not self.stop.is_set()
        if ready and intercepted_ns < self.target_ns:
            self.publish()
            ready = wait_until(self.target_ns, self.stop)
        checked_ns = time.time_ns()
        offset = (checked_ns - self.target_ns) / 1_000_000
        self.timing["request_release_offset_ms"] = offset
        if not ready or self.stop.is_set() or not 0 <= offset <= MAX_RELEASE_LATENESS_MS:
            self.timing["request_release_blocked"] = True
            if not ready or self.stop.is_set():
                reason = "Stopped before release"
            elif intercepted_ns > self.target_ns + MAX_RELEASE_LATENESS_MS * 1_000_000:
                reason = ("POST reached the gate after the "
                          f"0-{MAX_RELEASE_LATENESS_MS} ms local release window "
                          f"({self.timing['request_intercept_offset_ms']:+.3f} ms at interception)")
            else:
                reason = f"Local wait/check missed 0-{MAX_RELEASE_LATENESS_MS} ms release window ({offset:+.3f} ms)"
            self.timing["request_release_reason"] = reason
            route.abort("blockedbyclient")
            self.publish()
            return
        # This timestamp measures the local continuation call, not network transmission.
        self.timing["request_release_called_at"] = checked_ns / 1_000_000_000
        route.continue_()
        self.timing["request_release_returned_at"] = time.time_ns() / 1_000_000_000
        self.publish()


class Controller:
    def __init__(self):
        self.lock = threading.Lock()
        self.commands = queue.Queue()
        self.stop = threading.Event()
        self.state = {"status": "idle", "message": "Enter the booking and airport-local check-in time.", "target": None}
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
        self.trace_sessions = []
        self.network_records = {}
        self.trace_pnr = ""
        self.request_mode = False
        self.history = []
        self.schedule_zone = ""
        self.next_foreground = 0

    def trace_request(self, source: int, event: dict):
        if event["type"] not in {"Fetch", "XHR", "Document"}:
            return
        with self.lock:
            timing = self.click_timing
            if timing is None or event["wallTime"] < timing["target"] - FINAL_CHECK_LEAD - 1:
                return
            request = event["request"]
            url = urlsplit(request["url"])
            if url.scheme not in {"http", "https"}:
                return
            key = (source, event["requestId"])
            previous = self.network_records.pop(key, None)
            if previous is not None and "redirectResponse" in event:
                previous["status"] = event["redirectResponse"]["status"]
                previous["response_at"] = previous["started_at"] + event["timestamp"] - previous["monotonic_start"]
                previous["redirected"] = True
            records = timing.setdefault("requests", [])
            if len(records) >= MAX_REQUEST_TRACE:
                timing["requests_dropped"] = timing.get("requests_dropped", 0) + 1
                return
            endpoint = f"{url.scheme}://{url.netloc.rsplit('@', 1)[-1]}{unquote(url.path)}"
            if self.trace_pnr:
                endpoint = re.sub(re.escape(self.trace_pnr), "[PNR]", endpoint, flags=re.I)
            record = {"endpoint": endpoint, "method": request["method"], "type": event["type"],
                      "initiator": event.get("initiator", {}).get("type", "unknown"),
                      "started_at": event["wallTime"], "monotonic_start": event["timestamp"],
                      "booking_request": (url.scheme == "https" and url.hostname == BOOKING_REQUEST_HOST and
                                          url.path == BOOKING_REQUEST_PATH and request["method"] == "POST")}
            records.append(record)
            self.network_records[key] = record

    def trace_response(self, source: int, event: dict):
        with self.lock:
            record = self.network_records.get((source, event["requestId"]))
            if record is not None:
                record["response_at"] = record["started_at"] + event["timestamp"] - record["monotonic_start"]
                record["status"] = event["response"]["status"]
                record["from_cache"] = bool(event["response"].get("fromDiskCache") or
                                           event["response"].get("fromServiceWorker"))
                if record.get("booking_request"):
                    self.history.append({"at": record["response_at"],
                                         "action": f"Booking endpoint response headers observed: HTTP {record['status']}; not proof of check-in"})

    def trace_failure(self, source: int, event: dict):
        with self.lock:
            record = self.network_records.get((source, event["requestId"]))
            if record is not None:
                record["failure"] = event["errorText"]

    def attach_request_trace(self, page: Page):
        session = page.context.new_cdp_session(page)
        source = len(self.trace_sessions)
        self.trace_sessions.append(session)
        session.on("Network.requestWillBeSent", lambda event: self.trace_request(source, event))
        session.on("Network.responseReceived", lambda event: self.trace_response(source, event))
        session.on("Network.loadingFailed", lambda event: self.trace_failure(source, event))
        session.send("Network.enable")

    def click_observed(self, source, stamp: float):
        with self.lock:
            if self.click_timing is None:
                return
            self.click_timing.update(clicked=True, event_at=stamp / 1000)
            self.history.append({"at": stamp / 1000, "action": "Retrieve Booking click observed; no retry"})

    def request_observed(self, request):
        if not (request.method == "POST" and urlsplit(request.url).scheme == BOOKING_REQUEST_SCHEME
                and urlsplit(request.url).hostname == BOOKING_REQUEST_HOST
                and urlsplit(request.url).path == BOOKING_REQUEST_PATH):
            return
        with self.lock:
            if self.click_timing is None or self.observed_request is not None:
                return
            observed = time.time()
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
            self.history.append({"at": time.time(), "action": message})

    def gate_progress(self, timing: dict):
        with self.lock:
            if self.click_timing is None:
                return
            self.click_timing.update(timing)
            if timing.get("request_release_blocked"):
                message = timing["request_release_reason"] + ". POST aborted; no retry."
            elif "request_release_returned_at" in timing:
                message = "Local POST release call completed; transmission and server receipt unverified."
            elif timing["request_held_at"] < self.click_timing["target"]:
                message = "Booking POST held locally. Armed for API release at the entered check-in time."
            else:
                message = "Booking POST intercepted at or after the target; release is not yet confirmed."
            if not timing.get("request_release_blocked"):
                self.history.append({"at": time.time(), "action": message})
            self.state = {"status": "submitting", "message": message, "target": self.click_timing["target"]}

    def snapshot(self) -> dict:
        with self.lock:
            target = self.state["target"]
            return {**self.state, "now": time.time(), "browser_open": self.browser_open,
                    "request_mode": self.request_mode,
                    "timezone": self.schedule_zone,
                    "history": [dict(event) for event in self.history],
                    "browser_name": self.browser_info["name"], "browser_note": self.browser_info["note"],
                    "click_timing": ({**self.click_timing,
                                      "requests": [dict(record) for record in self.click_timing.get("requests", [])]}
                                     if self.click_timing is not None else None),
                    "prepare_at": None}

    def submit(self, action: str, data: dict):
        if action not in {"schedule", "cancel", "show_browser"}:
            raise ValueError("Unknown action.")
        with self.lock:
            status = self.state["status"]
            if action == "show_browser":
                if not self.browser_open:
                    raise ValueError("The Scoot browser opens when preparation starts.")
                if status in {"working", "preparing", "submitting", "observing"}:
                    raise ValueError("Wait until the current browser action completes.")
                self.commands.put((action, {}))
                return
            if action == "schedule":
                if status in ACTIVE_STATUSES:
                    raise ValueError("Cancel the current schedule before scheduling another booking.")
                payload = booking_input(data)
                checkin = scheduled_timestamp({**data, "timezone": payload["timezone"]})
                payload = {"booking": payload, "checkin": checkin,
                           "target": checkin - 1, "request_mode": False}
                if payload["target"] - time.time() < MIN_SCHEDULE_LEAD:
                    raise ValueError(f"Schedule at least {MIN_SCHEDULE_LEAD} seconds ahead so the browser can be prepared.")
            else:
                if status in {"submitting", "observing"}:
                    raise ValueError("Retrieval has begun. A dispatched click cannot be undone.")
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
        context = self.browser.new_context(viewport={"width": 1280, "height": 900},
                                          service_workers="block")
        context.on("page", self.attach_request_trace)
        self.page = context.new_page()
        self.page.bring_to_front()
        self.page.expose_binding("reportChopeClick", self.click_observed)
        self.page.on("request", self.request_observed)
        self.page.on("response", self.response_observed)
        self.result_pages = []
        self.page.on("popup", lambda popup: self.result_pages.append(popup))
        with self.lock:
            self.browser_open = True

    def close_browser(self):
        browser, self.browser, self.page, self.result_pages = self.browser, None, None, []
        self.trace_sessions = []
        self.network_records = {}
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
        budget = lambda limit: max(1000, min(limit, (target - RETRY_CUTOFF - time.time()) * 1000))
        self.page.bring_to_front()
        response = self.page.goto(SITE, wait_until="commit", timeout=budget(45000))
        if response is not None and response.status >= 400:
            raise ValueError(f"Scoot page navigation failed (HTTP {response.status}). No retrieval was made.")
        self.page.wait_for_load_state("load", timeout=budget(PAGE_LOAD_TIMEOUT_MS))
        form = BookingForm(self.page)
        form.wait_until_ready(budget(15000) / 1000)
        form.prepare(booking)
        self.page.bring_to_front()

    def advance(self, job: dict) -> dict | None:
        now = time.time()
        target = job["target"]
        if job["ready"]:
            if target - now > DISPATCH_LEAD and now >= self.next_foreground:
                self.page.bring_to_front()
                self.next_foreground = time.time() + FOREGROUND_INTERVAL
            if now >= target - DISPATCH_LEAD:
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
        message = "Armed. Entries verified; Retrieve Booking will be clicked once at check-in minus one second."
        self.update("ready", message, target)
        return job

    def click(self, job: dict):
        due = job["target"]
        with self.lock:
            if self.state["status"] != "ready":
                return
            self.state = {"status": "submitting", "message": "Waiting for the single retrieval click.", "target": due}
        if "click_fields" not in job:
            raise ValueError("Final verification was not completed before the deadline. No click was made.")
        with self.lock:
            self.click_timing = {"target": due, "checkin": job.get("checkin", due + 1), "request_mode": False}
            self.observed_request = None
            self.network_records = {}
            self.trace_pnr = job["booking"]["pnr"]
        # Enter the final local wait before the target. A missed deadline never causes a late fallback.
        if time.time() > due or not wait_until(round(due * 1e9), self.stop) or self.stop.is_set():
            self.update("error", "Stopped or missed the dispatch deadline. No click or retry was made.")
            return
        if time.time() - due > MAX_RELEASE_LATENESS_MS / 1000:
            self.update("error", "Missed the dispatch deadline. No click or retry was made.")
            return
        try:
            result = BookingForm(self.page).dispatch_click(job["click_fields"], {**job["booking"], "timed_dispatch": True}, due)
            with self.lock:
                self.click_timing.update(result)
            if not result["clicked"]:
                self.update("error", result["reason"] + " No retry will be made.")
                return
            self.update("observing", "Retrieve Booking clicked once. Waiting for the booking API response; no retry.")
            deadline = time.monotonic() + RESULT_WATCH
            while not self.stop.is_set() and time.monotonic() < deadline:
                with self.lock:
                    received = self.click_timing.get("first_response_status")
                if received is not None:
                    self.update("submitted", f"Booking retrieval API returned HTTP {received}. Review the same Scoot browser and continue manually. Check-in and seat availability are not confirmed.")
                    return
                self.page.wait_for_timeout(50)
            self.update("uncertain", "Booking retrieval response unconfirmed (timeout or stopped). Review the same browser; no retry will be made.")
        except Exception as error:
            self.update("uncertain", f"Retrieval outcome unconfirmed ({type(error).__name__}). Review the same browser; no retry will be made.")

    def scheduled_message(self, job: dict) -> str:
        zone = job["booking"]["timezone"]
        return (f"Check-in: {display_time(job['checkin'], zone)}. "
                f"Retrieve Booking dispatch target: {display_time(job['target'], zone)} ({zone}), exactly one second earlier. "
                "Not armed until the loaded form is verified. Keep the app running, Mac awake and lid open. "
                "Use Cancel schedule before dispatch. One click; no retries; subsequent check-in steps are manual.")

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
                    self.request_mode = False
                    with self.lock:
                        self.click_timing = None
                        self.observed_request = None
                        self.trace_pnr = ""
                        self.history = []
                        self.schedule_zone = payload["booking"]["timezone"]
                    self.browser_info = default_browser()
                    job = {**payload, "ready": False, "attempts": 0, "last_error": "",
                           "next_attempt": time.time()}
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


STATUS_LABELS = {
    "idle": "Ready to schedule", "working": "Processing action", "scheduled": "Scheduled",
    "preparing": "Preparing browser", "ready": "Armed - waiting for retrieval click",
    "submitting": "Dispatching retrieval click", "observing": "Reviewing Scoot response",
    "submitted": "Review your result", "airport_queue": "Airport counter required",
    "unsuccessful": "Check-in unsuccessful", "not_open_yet": "Check-in opens later",
    "checkin_closed": "Check-in closed", "pnr_not_found": "Booking not found",
    "departure_mismatch": "Check departing airport", "error": "Needs attention",
    "uncertain": "Review your result", "passenger_details": "Passenger page captured",
    "capture_incomplete": "Page capture needs review",
}
PROGRESS_STEPS = (
    "Schedule confirmed",
    "Open browser & fully load Scoot",
    "Fill booking & keep page active",
    "Verify form & arm single click",
    "Click at check-in minus one second",
    "Review result in Scoot browser (no seat confirmed)",
)
FINAL_STEP = len(PROGRESS_STEPS) - 1


def display_time(stamp: float, timezone_name: str = "") -> str:
    zone = ZoneInfo(timezone_name) if timezone_name else None
    return datetime.fromtimestamp(stamp, zone).isoformat(sep=" ", timespec="milliseconds")


def status_label(state: dict) -> str:
    if state.get("request_mode") is False and state["status"] == "submitting":
        return "Dispatching single retrieval click"
    if state["status"] == "submitting":
        timing = state.get("click_timing") or {}
        if timing.get("request_release_blocked"):
            return "POST aborted - no retry"
        if timing.get("request_release_returned_at") is not None:
            return "Local release acknowledged"
        if timing.get("request_release_called_at") is not None:
            return "Release attempted - unconfirmed"
        if timing.get("request_held_at") is not None:
            return "POST intercepted - awaiting release"
    return STATUS_LABELS.get(state["status"], state["status"])


def progress_state(state: dict) -> int:
    """Index of the current step; FINAL_STEP + 1 means all done, -1 means stopped or idle."""
    status = state["status"]
    if status == "submitting":
        return 4 if (state.get("click_timing") or {}).get("request_held_at") is not None else 3
    steps = {"scheduled": 0, "preparing": 1, "ready": 2, "observing": FINAL_STEP}
    if status in steps:
        return steps[status]
    if status in {"idle", "working", "error", "uncertain"}:
        return -1
    return FINAL_STEP + 1


def stage_countdown(state: dict) -> str:
    target = state.get("target")
    timing = state.get("click_timing") or {}
    if target is None:
        target = timing.get("target")
    if target is None:
        return "No retrieval scheduled.\nEnter the booking and local check-in time, then select Confirm schedule."
    if state.get("request_mode") is False:
        zone = state.get("timezone") or ""
        checkin = timing.get("checkin", target + 1)
        return (f"Check-in time: {display_time(checkin, zone)}\n"
                f"Retrieve Booking dispatch target: {display_time(target, zone)} (check-in minus one second)\n"
                f"{status_label(state)}. Keep the app and browser open; no automatic retry.")
    status = state["status"]
    active = status in {"scheduled", "preparing", "ready", "submitting"}
    now = state["now"]
    def clock(stamp):
        return display_time(stamp, state.get("timezone") or "")

    def stage(label, stamp, done_text):
        remaining = stamp - now
        when = f"in {remaining:.3f} s" if remaining >= 0 else (done_text if not active or status != "scheduled" else "starting")
        return f"{label:<28}{clock(stamp)}   {when}"

    lines = [f"API release target: {clock(target)}  ({state.get('timezone') or 'computer local time'}; entered check-in time)", ""]
    lines.append(stage("1  Open browser & load", target - PREPARE_LEAD, "planned time passed; see activity log"))
    if timing.get("request_held_at") is not None:
        lines.append(f"{'2  Construct & hold POST':<28}{clock(timing['request_held_at'])}   intercepted locally")
    else:
        lines.append(stage("2  Construct & hold POST", target - REQUEST_PREPARE_LEAD, "not intercepted"))
    if timing.get("request_release_blocked"):
        lines.append(f"3  API release aborted: {timing['request_release_reason']}")
    elif timing.get("request_release_returned_at") is not None:
        offset = timing["request_release_offset_ms"]
        lines.append(f"3  Local API release call: {offset:+.3f} ms; transmission unverified")
    elif timing.get("request_release_called_at") is not None:
        lines.append("3  API release attempted; completion unconfirmed")
    else:
        lines.append(stage("3  Release original POST", target, "no release confirmed"))
    lines.append("")
    if active:
        lines.append("Waiting for Scoot's response..." if timing.get("request_release_returned_at") is not None
                     else "Keep this app and the Scoot browser open.")
    elif status == "observing":
        lines.append("Waiting for Scoot's response...")
    else:
        lines.append(f"Result: {STATUS_LABELS.get(status, status)}. No automatic retry.")
    return "\n".join(lines)


def booking_request_summary(timing: dict) -> list[str]:
    requests = [record for record in timing.get("requests", []) if record.get("booking_request")]
    if not requests:
        return ["Booking endpoint request not observed; unrelated requests do not establish booking timing."]
    lines = []
    for record in requests:
        offset = (record["started_at"] - timing["target"]) * 1000
        lines.append(f"Booking request initiation: {display_time(record['started_at'])} "
                     f"({offset:+.1f} ms from dispatch target; diagnostic only). "
                     "Chromium initiation is NOT transmission and may precede local interception.")
        if timing.get("request_release_called_at") is not None:
            offset = (timing["request_release_called_at"] - timing["target"]) * 1000
            lines.append(f"Local release call offset: {offset:+.3f} ms; transmission/server receipt unverified.")
        if timing.get("request_release_blocked"):
            lines.append("Release aborted: " + timing["request_release_reason"])
        if timing.get("event_at") is not None:
            click_offset = (timing["event_at"] - timing["target"]) * 1000
            delay = (record["started_at"] - timing["event_at"]) * 1000
            lines.append(f"Construction click from dispatch target: {click_offset:+.1f} ms; "
                         f"click-to-booking-request: {delay:+.1f} ms (page request delay)")
    return lines


def request_trace_summary(timing: dict | None) -> str:
    lines = ["Passive Chromium network trace. Booking requests matched by exact POST endpoint; other entries are candidates.",
             "Start is Chromium's request-initiation timestamp, not server receipt.",
             "Only requests observed during browser automation are included.",
             "Query strings, fragments, URL credentials and the entered PNR are omitted.",
             "Request/response bodies and headers are not captured.", ""]
    if not timing or not timing.get("requests"):
        return "\n".join(lines + ["No network candidates recorded."])
    lines.extend(booking_request_summary(timing))
    lines.append("")
    for index, record in enumerate(timing["requests"], 1):
        started = display_time(record["started_at"])
        offset = (record["started_at"] - timing["target"]) * 1000
        lines.append(f"{index}. {record['method']} {record['endpoint']}")
        if record.get("booking_request"):
            lines.append("   MATCHED BOOKING ENDPOINT (does not prove server receipt or queue position)")
        lines.append(f"   {record['type']}; initiator: {record['initiator']}; initiation: {started}; API target offset: {offset:+.1f} ms")
        if timing.get("event_at") is not None:
            lines.append(f"   Click-to-request: {(record['started_at'] - timing['event_at']) * 1000:+.1f} ms")
        if "status" in record:
            elapsed = (record["response_at"] - record["started_at"]) * 1000
            lines.append(f"   HTTP {record['status']}; response headers after {elapsed:.1f} ms"
                         + (" (cache/service worker)" if record.get("from_cache") else "")
                         + (" (redirect)" if record.get("redirected") else ""))
        elif "failure" not in record:
            lines.append("   Response headers not observed.")
        if "failure" in record:
            lines.append(f"   Failed: {record['failure']}")
        lines.append("")
    if timing.get("requests_dropped"):
        lines.append(f"Trace limit reached: {timing['requests_dropped']} additional requests omitted.")
    return "\n".join(lines)


def headline_countdown(state: dict) -> tuple[str, str]:
    """Large timer text and its one-line caption for the hero countdown card."""
    target = state.get("target")
    timing = state.get("click_timing") or {}
    if target is None:
        target = timing.get("target")
    status = state["status"]
    if target is None:
        return "--:--:--", "No retrieval scheduled"
    if state.get("request_mode") is False:
        remaining = max(0, target - state["now"])
        hours, rest = divmod(int(remaining), 3600)
        minutes, seconds = divmod(rest, 60)
        return (f"{hours:02d}:{minutes:02d}:{seconds:02d}",
                f"{status_label(state)} — click target {display_time(target, state.get('timezone') or '')}")
    clock = display_time(target, state.get("timezone") or "")
    remaining = target - state["now"]
    if status in {"scheduled", "preparing", "ready", "submitting"} and remaining > 0:
        days, rest = divmod(int(remaining), 86400)
        hours, rest = divmod(rest, 3600)
        minutes, seconds = divmod(rest, 60)
        text = f"{days}d {hours:02d}:{minutes:02d}:{seconds:02d}" if days else f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return text, f"until API release at {clock} (entered check-in time)"
    if status in {"submitting", "observing"}:
        return "00:00:00", ("POST released locally - waiting for Scoot" if timing.get("request_release_returned_at") is not None
                             else "API release not yet confirmed")
    if status in ACTIVE_STATUSES:
        return "00:00:00", f"API release target {clock}"
    return "--:--:--", f"{status_label(state)} - API target was {clock}"


def timing_table_rows(timing: dict | None, timezone_name: str = "") -> list[tuple[str, str, str, str]]:
    if not timing:
        return []
    rows = []

    def add(label, stamp, meaning):
        if stamp is not None:
            rows.append((label, display_time(stamp, timezone_name),
                         f"{(stamp - timing['target']) * 1000:+.1f} ms", meaning))

    if timing.get("request_mode") is False:
        add("Check-in time", timing.get("checkin"), "Entered airport-local check-in time")
        add("Click dispatch target", timing.get("target"), "Check-in minus exactly one second")
        add("Retrieve Booking click", timing.get("event_at"), "Local click; server receipt unverified")
        add("Booking API response", timing.get("first_response_at"), f"HTTP {timing.get('first_response_status', 'unconfirmed')}")
    else:
        add("API release target", timing.get("target"), "Entered check-in time; no subtraction")
    if timing.get("request_mode") is not False:
        add("Construction click", timing.get("event_at"), "Builds POST early; NOT API transmission")
    for record in timing.get("requests", []):
        if record.get("booking_request"):
            add("Booking request initiation", record["started_at"], "Can precede interception; NOT transmission")
    add("POST intercepted", timing.get("request_held_at"), "Gate entry; may already be late, not proof of arming")
    add("Local release call", timing.get("request_release_called_at"), "NOT proof of transmission/server receipt")
    add("Release call returned", timing.get("request_release_returned_at"), "Browser protocol acknowledged")
    if timing.get("request_release_blocked"):
        rows.append(("Release aborted", "-", f"{timing['request_release_offset_ms']:+.3f} ms",
                     timing["request_release_reason"]))
    for record in timing.get("requests", []):
        if record.get("booking_request"):
            if "status" in record:
                add("Booking response", record["response_at"], f"HTTP {record['status']}")
            if "failure" in record:
                rows.append(("Booking request failed", "-", "-", record["failure"]))
    return rows


def request_table_rows(timing: dict | None, timezone_name: str = "") -> list[tuple[str, ...]]:
    if not timing:
        return []
    rows = []
    for record in timing.get("requests", []):
        response = "Not observed"
        if "status" in record:
            elapsed = (record["response_at"] - record["started_at"]) * 1000
            response = f"HTTP {record['status']}; {elapsed:.1f} ms to headers"
        if record.get("from_cache"):
            response += "; cache/service worker"
        if record.get("redirected"):
            response += "; redirect"
        if "failure" in record:
            response += f"; failed: {record['failure']}"
        rows.append((
            "Booking" if record.get("booking_request") else "Other",
            record["method"], record["endpoint"], record["type"], record["initiator"],
            display_time(record["started_at"], timezone_name),
            f"{(record['started_at'] - timing['target']) * 1000:+.1f} ms",
            f"{(record['started_at'] - timing['event_at']) * 1000:+.1f} ms" if timing.get("event_at") is not None else "-",
            response,
        ))
    return rows


INK = "#161a22"
PANEL_DARK = "#1d2330"
CARD = "#ffffff"
BG = "#f2f4f7"
BORDER = "#dfe3ea"
MUTED = "#5f6774"
ACCENT = "#ffd200"
ACCENT_DARK = "#e8bf00"
ACCENT_SOFT = "#fff3bd"
GOOD = "#1d7a34"
BAD = "#b00020"


def apply_theme(root: tk.Tk) -> ttk.Style:
    style = ttk.Style(root)
    style.theme_use("clam")
    base_font = ("Helvetica Neue", 13)
    style.configure(".", background=BG, foreground=INK, bordercolor=BORDER,
                    focuscolor=ACCENT, font=base_font)
    style.configure("TFrame", background=BG)
    style.configure("Card.TFrame", background=CARD)
    style.configure("Hero.TFrame", background=PANEL_DARK)
    style.configure("TLabel", background=CARD, foreground=INK)
    style.configure("Bg.TLabel", background=BG)
    style.configure("Title.TLabel", background=BG, font=("Helvetica Neue", 26, "bold"))
    style.configure("Subtitle.TLabel", background=BG, foreground=MUTED)
    style.configure("HeroEyebrow.TLabel", background=PANEL_DARK, foreground=ACCENT,
                    font=("Helvetica Neue", 12, "bold"))
    style.configure("HeroTimer.TLabel", background=PANEL_DARK, foreground="#ffffff",
                    font=("Menlo", 40, "bold"))
    style.configure("HeroCaption.TLabel", background=PANEL_DARK, foreground="#aab3c2")
    style.configure("HeroDetail.TLabel", background=PANEL_DARK, foreground="#8d97a8",
                    font=("Menlo", 11))
    style.configure("Heading.TLabel", font=("Helvetica Neue", 17, "bold"))
    style.configure("Muted.TLabel", foreground=MUTED)
    style.configure("Section.TLabel", foreground=MUTED, font=("Helvetica Neue", 12, "bold"))
    style.configure("Error.TLabel", foreground=BAD)
    style.configure("Step.TLabel", padding=8, foreground=MUTED, background=CARD)
    style.configure("Current.TLabel", padding=8, background=ACCENT_SOFT, foreground=INK,
                    font=("Helvetica Neue", 13, "bold"))
    style.configure("Done.TLabel", padding=8, foreground=GOOD, background=CARD)
    style.configure("TLabelframe", background=CARD, bordercolor=BORDER, borderwidth=1,
                    relief="solid")
    style.configure("TLabelframe.Label", background=CARD, foreground=MUTED,
                    font=("Helvetica Neue", 12, "bold"))
    style.configure("TButton", background=CARD, padding=(14, 8))
    style.map("TButton", background=[("active", "#eef0f4"), ("disabled", "#f5f6f8")],
              foreground=[("disabled", "#9aa1ac")])
    style.configure("Accent.TButton", background=ACCENT, foreground=INK, bordercolor=ACCENT_DARK,
                    font=("Helvetica Neue", 14, "bold"), padding=(14, 10))
    style.map("Accent.TButton", background=[("active", ACCENT_DARK), ("disabled", "#f5f6f8")],
              foreground=[("disabled", "#9aa1ac")])
    style.configure("TEntry", fieldbackground="#ffffff", padding=6)
    style.configure("TCombobox", fieldbackground="#ffffff", padding=6, arrowsize=16)
    style.map("TEntry", fieldbackground=[("disabled", "#f0f1f4")])
    style.map("TCombobox", fieldbackground=[("disabled", "#f0f1f4")])
    style.configure("Treeview", background="#ffffff", fieldbackground="#ffffff", rowheight=26)
    style.configure("Treeview.Heading", background="#f3f4f6", foreground=MUTED,
                    font=("Helvetica Neue", 12, "bold"))
    root.configure(bg=BG)
    return style


class DesktopApp:
    def __init__(self, root: tk.Tk, controller: Controller):
        self.root, self.controller = root, controller
        self.closing = False
        self.updating = False
        self.date_edited = self.time_edited = False
        self.state = controller.snapshot()
        self.station_labels = {
            f'{s["name"]} ({s["code"]}) - {s["country"]}': s
            for s in departure_stations()["stations"]
        }
        self.station_names = {s["name"].casefold(): s for s in self.station_labels.values()}
        self.station_names.update({s["code"].casefold(): s for s in self.station_labels.values()})
        root.title("auto-chope-seat")
        root.geometry("1100x900")
        root.minsize(720, 600)
        root.protocol("WM_DELETE_WINDOW", self.close)
        apply_theme(root)
        shell = ttk.Frame(root, padding=20)
        shell.pack(fill="both", expand=True)
        header = ttk.Frame(shell)
        header.pack(fill="x", pady=(0, 14))
        ttk.Button(header, text="Quit app", command=self.close).pack(side="right", anchor="n")
        ttk.Label(header, text="auto-chope-seat", style="Title.TLabel").pack(anchor="w")
        ttk.Label(header, text="Follows SKILL.md: clicks Retrieve Booking once, one second before airport-local check-in time.",
                  style="Subtitle.TLabel").pack(anchor="w")

        self.countdown = tk.StringVar()
        self.countdown_big = tk.StringVar(value="--:--:--")
        self.countdown_caption = tk.StringVar(value="No retrieval scheduled")
        self.countdown_panel = ttk.Frame(shell, style="Hero.TFrame", padding=(20, 16))
        self.countdown_panel.pack(fill="x", pady=(6, 14))
        ttk.Label(self.countdown_panel, text="RETRIEVAL COUNTDOWN", style="HeroEyebrow.TLabel").pack(anchor="w")
        self.timer_label = ttk.Label(self.countdown_panel, textvariable=self.countdown_big,
                                     style="HeroTimer.TLabel")
        self.timer_label.pack(anchor="w")
        ttk.Label(self.countdown_panel, textvariable=self.countdown_caption,
                  style="HeroCaption.TLabel").pack(anchor="w", pady=(0, 10))
        self.countdown_label = ttk.Label(self.countdown_panel, textvariable=self.countdown,
                                          style="HeroDetail.TLabel", justify="left")
        self.countdown_label.pack(fill="x")
        self.countdown_panel.bind("<Configure>", lambda event: self.countdown_label.configure(
            wraplength=max(200, event.width - 40)))
        canvas = tk.Canvas(shell, highlightthickness=0, bg=BG)
        scrollbar = ttk.Scrollbar(shell, orient="vertical", command=canvas.yview)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(fill="both", expand=True)
        canvas.configure(yscrollcommand=scrollbar.set)
        content = ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=content, anchor="nw")
        content.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        content.columnconfigure(0, weight=1)
        content.columnconfigure(1, weight=1)
        form = ttk.Frame(content, padding=(0, 0, 15, 0))
        form.grid(row=0, column=0, sticky="new")
        status_panel = ttk.LabelFrame(content, text="3 · Progress", padding=16)
        status_panel.grid(row=0, column=1, sticky="new")
        self.pnr, self.departure, self.timezone, self.date, self.clock = (tk.StringVar() for _ in range(5))
        self.timezone_note = tk.StringVar(value="Choose an airport to set its timezone automatically.")
        self.error, self.status, self.message = (tk.StringVar() for _ in range(3))
        self.inputs = []
        booking = ttk.LabelFrame(form, text="1 · Booking", padding=16)
        booking.pack(fill="x")
        self.field(booking, "Booking reference (PNR)", ttk.Entry(booking, textvariable=self.pnr))
        self.station_box = ttk.Combobox(booking, textvariable=self.departure, values=tuple(self.station_labels))
        self.field(booking, "Departing airport", self.station_box)
        self.station_box.bind("<<ComboboxSelected>>", self.select_departure)
        self.station_box.bind("<KeyRelease>", self.filter_departures)
        self.field(booking, "Airport timezone", ttk.Combobox(
            booking, textvariable=self.timezone, values=tuple(sorted(available_timezones()))))
        ttk.Label(booking, textvariable=self.timezone_note, wraplength=400, style="Muted.TLabel").pack(anchor="w", pady=6)
        schedule = ttk.LabelFrame(form, text="2 · Schedule", padding=16)
        schedule.pack(fill="x", pady=14)
        ttk.Label(schedule, text="Enter your airport-local check-in time. Retrieve Booking is clicked one second earlier.",
                  wraplength=400, style="Muted.TLabel").pack(anchor="w", pady=(0, 10))
        ttk.Label(schedule, text="Check-in date (YYYY-MM-DD)").pack(anchor="w")
        date_row = ttk.Frame(schedule)
        date_row.pack(fill="x", pady=(3, 10))
        date_entry = ttk.Entry(date_row, textvariable=self.date)
        date_entry.pack(side="left", fill="x", expand=True)
        self.calendar_icon = tk.PhotoImage(master=self.root, width=24, height=24)
        self.calendar_icon.put("#1d2330", to=(2, 4, 22, 23))
        self.calendar_icon.put("#ffffff", to=(4, 10, 20, 21))
        for x in (6, 16):
            self.calendar_icon.put("#ffd200", to=(x, 1, x + 2, 7))
        for x in (6, 11, 16):
            for y in (12, 16):
                self.calendar_icon.put("#1d2330", to=(x, y, x + 2, y + 2))
        self.calendar_button = ttk.Button(date_row, image=self.calendar_icon, text="Choose date from calendar",
                                          command=self.choose_date, padding=4)
        self.calendar_button.pack(side="left", padx=(6, 0))
        self.inputs.extend((date_entry, self.calendar_button))
        self.field(schedule, "Check-in time (HH:mm:ss.SSS, e.g. 08:00:00.000)", ttk.Entry(schedule, textvariable=self.clock))
        self.schedule_button = ttk.Button(schedule, text="Confirm schedule", style="Accent.TButton", command=self.schedule)
        self.schedule_button.pack(fill="x", pady=(8, 4))
        self.cancel_button = ttk.Button(schedule, text="Cancel schedule", command=lambda: self.send("cancel"))
        self.cancel_button.pack(fill="x", pady=4)
        ttk.Label(status_panel, textvariable=self.status, style="Heading.TLabel", wraplength=400).pack(anchor="w")
        ttk.Label(status_panel, textvariable=self.message, wraplength=400, style="Muted.TLabel").pack(anchor="w", pady=10)
        self.steps = []
        for index, name in enumerate(PROGRESS_STEPS):
            label = ttk.Label(status_panel, text=f"{index + 1}  {name}", style="Step.TLabel")
            label.pack(fill="x")
            self.steps.append(label)
        ttk.Label(status_panel, text="Activity log (departure airport time)", style="Section.TLabel").pack(anchor="w", pady=(12, 0))
        self.history_log = self.make_log(status_panel, height=7)
        self.history_rows = []
        ttk.Label(status_panel, text="Retrieval timing (departure airport time)", style="Section.TLabel").pack(anchor="w", pady=(8, 0))
        self.timing_table = self.make_table(status_panel, [
            ("Event", 170), ("Airport-local time (UTC offset)", 275), ("From click target", 125), ("Meaning", 300),
        ], height=6)
        self.timing_rows = []
        ttk.Label(status_panel, textvariable=self.error, style="Error.TLabel", wraplength=400).pack(anchor="w", pady=4)
        actions = ttk.Frame(status_panel)
        actions.pack(fill="x")
        actions.columnconfigure((0, 1), weight=1)
        self.browser_button = ttk.Button(actions, text="Show Scoot browser",
                                         command=lambda: self.send("show_browser"))
        self.browser_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        ttk.Button(actions, text="Network details", command=self.show_request_trace).grid(
            row=0, column=1, sticky="ew", padx=(4, 0))
        notes = ttk.LabelFrame(content, text="ⓘ Before you start", padding=14)
        notes.grid(row=1, column=0, columnspan=2, sticky="ew", pady=14)
        ttk.Label(notes, wraplength=820, style="Muted.TLabel", text=(
            "Keep this app open, the Mac plugged in and the lid open. "
            "Retrieve Booking is clicked once at check-in minus one second. Cancel before dispatch. "
            "No automatic retries. Continue passenger details, baggage, seats and boarding passes manually. "
            "Timing and HTTP success do not confirm check-in or a seat.")).pack(anchor="w")
        self.pnr.trace_add("write", self.normalize_pnr)
        self.departure.trace_add("write", self.departure_changed)
        self.timezone.trace_add("write", self.timezone_changed)
        self.date.trace_add("write", lambda *_: self.edited("date"))
        self.clock.trace_add("write", lambda *_: self.edited("time"))
        self.prefill()
        self.refresh()

    def choose_date(self):
        if self.controller.snapshot()["status"] in ACTIVE_STATUSES:
            self.error.set("Cancel the active schedule before changing its date.")
            return
        timezone_name = self.timezone.get().strip()
        try:
            zone = ZoneInfo(timezone_name) if timezone_name else None
        except (ZoneInfoNotFoundError, ValueError):
            messagebox.showerror("Invalid airport timezone", "Select a valid departure airport timezone.",
                                 parent=self.root)
            return
        today = datetime.now(zone).date()
        try:
            selected = datetime.strptime(self.date.get(), "%Y-%m-%d").date()
        except ValueError:
            selected = today
        selected = max(today, selected)
        window = tk.Toplevel(self.root)
        window.configure(bg=BG)
        window.title(f"Check-in date - {zone.key if zone else 'choose departure airport next'}")
        window.transient(self.root)
        ttk.Label(window, text=f"Departure timezone: {zone.key}" if zone else
                  "Preview uses the computer's date. Choose the departure airport before scheduling.",
                  wraplength=300, padding=8, style="Bg.TLabel").pack()
        month = [selected.year, selected.month]
        heading = ttk.Frame(window, padding=8)
        heading.pack(fill="x")
        title = tk.StringVar()
        days = ttk.Frame(window, padding=8)
        days.pack()

        def select(day):
            if self.controller.snapshot()["status"] in ACTIVE_STATUSES:
                self.error.set("Cancel the active schedule before changing its date.")
            elif day < datetime.now(zone).date():
                self.error.set("Choose today or a later date in the departure timezone.")
                return
            else:
                self.date.set(day.isoformat())
            window.destroy()

        def draw():
            for child in days.winfo_children():
                child.destroy()
            year, number = month
            title.set(f"{calendar.month_name[number]} {year}")
            for column, name in enumerate(calendar.day_abbr):
                ttk.Label(days, text=name, width=5, anchor="center", style="Bg.TLabel").grid(row=0, column=column)
            for row, week in enumerate(calendar.Calendar().monthdayscalendar(year, number), 1):
                for column, number_day in enumerate(week):
                    if number_day:
                        day = datetime(year, number, number_day).date()
                        button = ttk.Button(days, text=str(number_day), width=4, command=lambda day=day: select(day))
                        button.grid(row=row, column=column, padx=2, pady=2)
                        if day < datetime.now(zone).date():
                            button.state(["disabled"])

        def move(delta):
            index = month[0] * 12 + month[1] - 1 + delta
            year, zero_month = divmod(index, 12)
            if 1 <= year <= 9999:
                month[:] = [year, zero_month + 1]
                draw()

        ttk.Button(heading, text="<", width=4, command=lambda: move(-1)).pack(side="left")
        ttk.Button(heading, text=">", width=4, command=lambda: move(1)).pack(side="right")
        ttk.Label(heading, textvariable=title, anchor="center", style="Bg.TLabel").pack(fill="x")
        ttk.Button(window, text="Today", command=lambda: select(datetime.now(zone).date())).pack(fill="x", padx=8, pady=8)
        draw()
        window.update_idletasks()
        window.geometry(f"+{self.calendar_button.winfo_rootx()}+{self.calendar_button.winfo_rooty() + self.calendar_button.winfo_height()}")
        window.wait_visibility()
        window.grab_set()
        window.lift()
        window.focus_set()

    def show_request_trace(self):
        window = tk.Toplevel(self.root)
        window.configure(bg=BG)
        window.title("Network details - booking endpoint and other requests")
        window.geometry("1150x550")
        ttk.Label(window, text="Browser initiation may precede interception; it is NOT transmission or server receipt. "
                  "All timestamps use the scheduled departure airport timezone with UTC offset. "
                  "Booking labels match the exact POST endpoint, not check-in success.",
                  wraplength=1000, padding=10, style="Bg.TLabel").pack(anchor="w")
        table = self.make_table(window, [
            ("Kind", 80), ("Method", 65), ("Endpoint", 400), ("Type", 80), ("Initiator", 90),
            ("Browser initiation (airport time)", 275), ("From click target", 125), ("From retrieval click", 150), ("Response", 300),
        ], height=14)
        note = tk.StringVar()
        ttk.Label(window, textvariable=note, padding=10, style="Bg.TLabel").pack(anchor="w")

        def update():
            state = self.controller.snapshot()
            timing = state.get("click_timing")
            self.fill_table(table, request_table_rows(timing, state.get("timezone") or ""))
            count = len(timing.get("requests", [])) if timing else 0
            dropped = timing.get("requests_dropped", 0) if timing else 0
            note.set(f"{count} requests; {dropped} omitted. Snapshot only: select Refresh to update. "
                     "Query strings, credentials, entered PNR, bodies and headers are omitted.")

        ttk.Button(window, text="Refresh", command=update).pack(anchor="e", padx=10, pady=5)
        update()

    @staticmethod
    def make_log(parent, height):
        """Read-only word-wrapping log so long entries stay fully readable in the window."""
        frame = ttk.Frame(parent, style="Card.TFrame")
        frame.pack(fill="both", expand=True, pady=8)
        text = tk.Text(frame, height=height, wrap="word", font=("Helvetica Neue", 12),
                       background="#ffffff", foreground=INK, relief="flat", borderwidth=0,
                       highlightthickness=1, highlightbackground=BORDER, highlightcolor=BORDER,
                       padx=8, pady=6, cursor="arrow", takefocus=0)
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scrollbar.set, state="disabled")
        text.tag_configure("time", font=("Menlo", 11), foreground=MUTED, spacing1=4)
        text.tag_configure("action", lmargin1=0, lmargin2=16, spacing3=4)
        scrollbar.pack(side="right", fill="y")
        text.pack(side="left", fill="both", expand=True)
        return text

    @staticmethod
    def fill_log(log, entries):
        log.configure(state="normal")
        log.delete("1.0", "end")
        for stamp, action in entries:
            log.insert("end", f"{stamp}\n", "time")
            log.insert("end", f"{action}\n", "action")
        log.configure(state="disabled")
        log.see("end")

    @staticmethod
    def make_table(parent, columns, height):
        frame = ttk.Frame(parent, width=400, height=height * 26 + 50, style="Card.TFrame")
        frame.pack(fill="both", expand=True, pady=8)
        frame.grid_propagate(False)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        names = tuple(name for name, _ in columns)
        table = ttk.Treeview(frame, columns=names, show="headings", height=height)
        for name, width in columns:
            table.heading(name, text=name)
            table.column(name, width=width, minwidth=width, stretch=False)
        vertical = ttk.Scrollbar(frame, orient="vertical", command=table.yview)
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=table.xview)
        table.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        table.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        return table

    @staticmethod
    def fill_table(table, rows):
        children = table.get_children()
        if children:
            table.delete(*children)
        for row in rows:
            table.insert("", "end", values=row)

    def field(self, parent, label, widget):
        ttk.Label(parent, text=label).pack(anchor="w")
        widget.pack(fill="x", pady=(3, 10))
        self.inputs.append(widget)

    def normalize_pnr(self, *_):
        self.pnr.set(self.pnr.get().upper())

    def edited(self, field):
        if not self.updating:
            if field == "date":
                self.date_edited = True
            else:
                self.time_edited = True

    def prefill(self):
        try:
            zone = ZoneInfo(self.timezone.get()) if self.timezone.get() else None
        except (ZoneInfoNotFoundError, ValueError):
            self.timezone_note.set("Select a valid IANA airport timezone.")
            return
        now = datetime.now(zone)
        today = now.strftime("%Y-%m-%d")
        self.updating = True
        try:
            if not self.date_edited or self.date.get() < today:
                self.date.set(today)
            if not self.time_edited:
                self.clock.set(now.strftime("%H:%M:%S") + ".000")
        finally:
            self.updating = False

    def departure_changed(self, *_):
        if self.updating:
            return
        station = self.station_labels.get(self.departure.get()) or self.station_names.get(self.departure.get().strip().casefold())
        self.timezone.set(station["timezone"] if station else "")
        self.timezone_note.set(f'Automatically selected {station["timezone"]}.' if station
                               else "Airport not recognised. Select its timezone manually.")
        self.prefill()

    def timezone_changed(self, *_):
        if not self.updating:
            self.prefill()

    def select_departure(self, _event=None):
        station = self.station_labels.get(self.departure.get())
        if station:
            self.departure.set(station["name"])
        self.station_box.configure(values=tuple(self.station_labels))

    def filter_departures(self, _event=None):
        query = self.departure.get().strip().casefold()
        self.station_box.configure(values=tuple(label for label in self.station_labels if query in label.casefold()))

    def schedule(self):
        self.send("schedule", {"pnr": self.pnr.get(), "departure": self.departure.get(),
                              "timezone": self.timezone.get(), "date": self.date.get(), "time": self.clock.get()})

    def send(self, action, data=None):
        self.error.set("")
        try:
            if self.controller.stop.is_set():
                raise ValueError("Browser worker is unavailable. Restart the app.")
            self.controller.submit(action, data or {})
        except ValueError as error:
            self.error.set(str(error))

    def refresh(self):
        if self.closing:
            if self.controller.worker.is_alive():
                self.root.after(100, self.refresh)
            else:
                self.root.destroy()
            return
        state = self.controller.snapshot()
        self.state = state
        status = state["status"]
        self.status.set(status_label(state))
        self.message.set(state["message"])
        step = progress_state(state)
        for index, label in enumerate(self.steps):
            if index == step:
                style, marker = "Current.TLabel", "->"
            elif 0 <= index < step:
                style, marker = "Done.TLabel", "\u2713"
            else:
                style, marker = "Step.TLabel", str(index + 1)
            label.configure(style=style, text=f"{marker}  {PROGRESS_STEPS[index]}")
        self.countdown.set(stage_countdown(state))
        big, caption = headline_countdown(state)
        self.countdown_big.set(big)
        self.countdown_caption.set(caption)
        if status in ACTIVE_STATUSES:
            timer_color = ACCENT
        elif status in {"submitted", "passenger_details"}:
            timer_color = "#6fdc8c"
        elif status in {"error", "unsuccessful", "uncertain", "capture_incomplete"}:
            timer_color = "#ff9b9b"
        else:
            timer_color = "#ffffff"
        self.timer_label.configure(foreground=timer_color)
        # The stage breakdown only adds detail once an API target exists.
        has_target = state.get("target") is not None or (state.get("click_timing") or {}).get("target") is not None
        if has_target and not self.countdown_label.winfo_manager():
            self.countdown_label.pack(fill="x")
        elif not has_target and self.countdown_label.winfo_manager():
            self.countdown_label.pack_forget()
        history = [(display_time(event["at"], state.get("timezone") or ""), event["action"])
                   for event in state.get("history", [])]
        if history != self.history_rows:
            self.fill_log(self.history_log, history)
            self.history_rows = history
        rows = timing_table_rows(state.get("click_timing"), state.get("timezone") or "")
        if rows != self.timing_rows:
            self.fill_table(self.timing_table, rows)
            self.timing_rows = rows
        active = status in ACTIVE_STATUSES
        for widget in self.inputs:
            widget.configure(state="disabled" if active else "normal")
        self.schedule_button.configure(state="disabled" if active else "normal")
        self.cancel_button.configure(state="normal" if status in {"scheduled", "preparing", "ready"} else "disabled")
        self.browser_button.configure(state="normal" if state["browser_open"] and status not in {
            "working", "preparing", "submitting", "observing"} else "disabled")
        self.root.after(50, self.refresh)

    def close(self):
        status = self.controller.snapshot()["status"]
        if status in {"submitting", "observing"}:
            messagebox.showwarning("Retrieval in progress", "Wait for result observation to finish before quitting.", parent=self.root)
            return
        if status in ACTIVE_STATUSES and not messagebox.askyesno(
                "Cancel retrieval?", "Quit and cancel the pending retrieval?", parent=self.root):
            return
        self.closing = True
        self.controller.stop.set()
        self.refresh()


def main():
    root = tk.Tk()
    controller = Controller()
    DesktopApp(root, controller)
    controller.worker.start()
    root.mainloop()
    controller.stop.set()
    controller.worker.join(timeout=35)


if __name__ == "__main__":
    main()
