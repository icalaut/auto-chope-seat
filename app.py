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
MAX_CLICK_LATENESS_MS = 5
FINAL_CHECK_LEAD = 2
DISPATCH_LEAD = 0.25
MAX_REQUEST_TRACE = 100
BOOKING_REQUEST_HOST = "apigw.singaporeair.com"
BOOKING_REQUEST_PATH = "/chopemyseat/booking/retrieval"
REQUEST_PREPARE_LEAD = 15
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
        return {"pnr": pnr.element_handle(), "departure": departure.element_handle(),
                "button": button.element_handle()}

    def dispatch_click(self, fields: dict, booking: dict, target: float) -> dict:
        # Enter the browser before the deadline, rather than send a protocol command at it.
        return self.page.evaluate("""async ({pnr, departure, button, booking, target, tolerance,
                                             wait_until_target = false, allow_late_retry = false}) => {
            const refuse = reason => ({clicked: false, reason});
            if (wait_until_target) {
                if (target - Date.now() > 1000)
                    return refuse("Browser dispatch was requested too early.");
                while (Date.now() < target)
                    await new Promise(resolve => setTimeout(resolve, Math.max(0, target - Date.now() - 2)));
            }
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
            const lateFallback = lateness > tolerance && allow_late_retry;
            if (lateness < 0 || (lateness > tolerance && !lateFallback))
                return {clicked: false, reason: lateness < 0
                    ? "The clock is earlier than the scheduled time."
                    : `The ${tolerance} ms dispatch deadline was missed.`, lateness_ms: lateness};
            button[dispatched] = true;
            let eventAt = null;
            let blocked = false;
            const guard = event => {
                if (event.target !== button && !button.contains(event.target)) return;
                eventAt = Date.now();
                if (eventAt < target || (!lateFallback && eventAt - target > tolerance)) {
                    event.preventDefault();
                    event.stopImmediatePropagation();
                    blocked = true;
                    return;
                }
                if (typeof window.reportChopeClick === "function")
                    window.reportChopeClick(eventAt);
            };
            window.addEventListener("click", guard, {capture: true});
            try {
                button.click();
            } finally {
                window.removeEventListener("click", guard, {capture: true});
            }
            if (blocked || eventAt === null)
                return {clicked: false, reason: blocked
                    ? `The ${tolerance} ms click-event deadline was missed; the event was blocked.`
                    : "No click event was observed. Review the browser; no retry will be made.",
                    lateness_ms: lateness, blocked_event_at: eventAt === null ? null : eventAt / 1000};
            return {clicked: true, dispatched_at: dispatchedAt / 1000, lateness_ms: lateness,
                late_fallback: lateFallback,
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


class BookingRequestGate:
    def __init__(self, target: float, stop: threading.Event, report: Callable[[dict], None] | None = None,
                 keep_active: Callable[[], None] | None = None):
        self.target = target
        self.stop = stop
        self.claimed = False
        self.expired = False
        self.timing = {}
        self.report = report
        self.keep_active = keep_active

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
        self.claimed = True
        self.timing["request_held_at"] = time.time()
        self.publish()
        next_focus = time.monotonic()
        while not self.stop.is_set():
            remaining = self.target - time.time()
            if remaining <= 0:
                break
            if self.keep_active is not None and remaining > DISPATCH_LEAD and time.monotonic() >= next_focus:
                self.keep_active()
                next_focus = time.monotonic() + FOREGROUND_INTERVAL
            self.stop.wait(min(0.001, remaining))
        checked = time.time()
        offset = (checked - self.target) * 1000
        if self.stop.is_set() or not 0 <= offset <= MAX_CLICK_LATENESS_MS:
            self.timing["request_release_blocked"] = True
            self.timing["request_release_offset_ms"] = offset
            route.abort("blockedbyclient")
            self.publish()
            return
        # This is the protocol-call time, not evidence of network transmission.
        self.timing["request_release_called_at"] = checked
        route.continue_()
        self.timing["request_release_returned_at"] = time.time()
        self.publish()


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
        self.trace_sessions = []
        self.network_records = {}
        self.trace_pnr = ""
        self.request_mode = True
        self.history = []
        self.schedule_zone = ""
        self.next_foreground = 0

    def trace_request(self, source: int, event: dict):
        if event["type"] not in {"Fetch", "XHR", "Document"}:
            return
        with self.lock:
            timing = self.click_timing
            lead = REQUEST_PREPARE_LEAD + 1 if timing and timing.get("request_mode") else DISPATCH_LEAD
            if timing is None or event["wallTime"] < timing["target"] - lead:
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
            self.history.append({"at": stamp / 1000, "action": "Click event observed; request construction"})
            if self.click_timing.get("request_mode"):
                return
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
            self.history.append({"at": time.time(), "action": message})

    def gate_progress(self, timing: dict):
        with self.lock:
            if self.click_timing is None:
                return
            self.click_timing.update(timing)
            if timing.get("request_release_blocked"):
                action = "Local release blocked: cancelled or outside 0-5 ms window"
                stamp = time.time()
            elif "request_release_called_at" in timing:
                action = "Local release call completed; waiting for response (transmission time unverified)"
                stamp = timing["request_release_called_at"]
            else:
                action = "Booking POST held locally; counting down to scheduled release"
                stamp = timing["request_held_at"]
            self.history.append({"at": stamp, "action": action})
            self.state = {"status": "submitting", "message": action, "target": self.click_timing["target"]}

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
                payload = {"booking": payload, "target": scheduled_timestamp({**data, "timezone": payload["timezone"]}),
                           "request_mode": True}
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
        context = self.browser.new_context(viewport={"width": 1280, "height": 900},
                                          service_workers="block" if self.request_mode else "allow")
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
            if job.get("request_mode") and target - now <= REQUEST_PREPARE_LEAD:
                job["click_fields"] = BookingForm(self.page).arm_click(job["booking"])
                self.click(job)
                return None
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
        BookingForm(self.page).show_click_target(
            job["booking"], target - REQUEST_PREPARE_LEAD if job.get("request_mode") else target)
        job["next_check"] = time.time() + RETRY_INTERVAL
        message = (f"Page loaded and booking filled. Standing by with Scoot kept active; the request is built "
                   f"and held {REQUEST_PREPARE_LEAD} s before release."
                   if job.get("request_mode") else
                   "Entries filled and verified. Standing by to click Retrieve Booking once at the scheduled time.")
        self.update("ready", message, target)
        return job

    def click(self, job: dict):
        due = job["target"]
        with self.lock:
            if self.state["status"] != "ready":
                return
            self.state = {"status": "submitting", "message": f"Checking the {MAX_CLICK_LATENESS_MS} ms dispatch deadline…", "target": due}
        if "click_fields" not in job:
            raise ValueError("Final verification was not completed before the deadline. No click was made.")
        with self.lock:
            self.click_timing = {"target": due, "request_mode": bool(job.get("request_mode"))}
            self.observed_request = None
            self.network_records = {}
            self.trace_pnr = job["booking"]["pnr"]
        gate = None
        try:
            if job.get("request_mode"):
                gate = BookingRequestGate(due, self.stop, self.gate_progress, self.page.bring_to_front)
                gate.install(self.page.context)
                self.update("submitting", "Constructing Scoot's request early, holding it locally until the schedule. No cancellation once submission begins.", due)
            result = BookingForm(self.page).dispatch_click(
                {**job["click_fields"], "wait_until_target": gate is None, "allow_late_retry": True},
                job["booking"], time.time() if gate is not None else due)
            if gate is not None:
                while not gate.claimed and time.time() <= due + MAX_CLICK_LATENESS_MS / 1000 and not self.stop.is_set():
                    if not result["clicked"]:
                        break
                    self.page.wait_for_timeout(5)
                if not gate.claimed:
                    gate.expired = True
                    raise ValueError("The booking request was not intercepted before the release deadline. Later matching requests will be blocked.")
        except Exception as error:
            if gate is not None:
                gate.expired = True
                with self.lock:
                    self.click_timing.update(gate.timing)
            self.update("uncertain", f"Click dispatch could not be confirmed ({type(error).__name__}: {error}). "
                        "A click may have been sent. Review the Scoot browser; no retry will be made.")
            return
        with self.lock:
            self.click_timing.update(result)
            if gate is not None:
                self.click_timing.update(gate.timing)
                self.click_timing["request_mode"] = True
        if gate is not None and gate.timing.get("request_release_blocked"):
            self.update("error", "Request release was cancelled or missed the 5 ms local window. The intercepted POST was aborted; no retry will be made.")
            return
        if not result["clicked"]:
            raise ValueError(f"{result['reason']} No retry will be made.")
        note = ("The held request was released to the browser. Actual network timing must be checked separately. "
                if gate is not None else
                f"The {MAX_CLICK_LATENESS_MS} ms deadline was missed before dispatch; one immediate late fallback click was made. "
                if result.get("late_fallback") else "Retrieve Booking was clicked once. ")
        self.update("observing", note + "Watching the result and capturing the passenger form if shown. No second retrieval click will be made.")
        try:
            self.page.wait_for_timeout(100)
            page = self.visible_page()
            page.bring_to_front()
            outcome = BookingForm(page).observe_result(stop=self.stop, page_provider=self.visible_page)
        except Exception as error:
            outcome = {"status": "submitted",
                       "message": f"Retrieve Booking was clicked once, but the result could not be read ({type(error).__name__}). "
                                  "Review the Scoot browser yourself; no further actions or retries will be made."}
        self.update(outcome["status"], (note if gate is not None or result.get("late_fallback") else "") + outcome["message"])

    def scheduled_message(self, job: dict) -> str:
        zone = ZoneInfo(job["booking"]["timezone"])
        at = datetime.fromtimestamp(job["target"], zone).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        opens = datetime.fromtimestamp(job["target"] - PREPARE_LEAD, zone).strftime("%H:%M:%S")
        note = f" {self.browser_info['note']}" if self.browser_info["note"] else ""
        if job.get("request_mode"):
            return (f"Request release scheduled for {at} ({zone.key}). {self.browser_info['name']} opens at {opens}. "
                    f"The button is clicked {REQUEST_PREPARE_LEAD} seconds early to construct the normal request; "
                    "the matching POST is held locally and released only if its local check is within 0-5 ms. "
                    "Network/server timing is not guaranteed. No late fallback or resubmission. "
                    "Tokens may expire; successful passenger-page continuation is unverified.")
        return (f"Scheduled for {at} ({zone.key}). {self.browser_info['name']} launches at {opens} to load Scoot and fill the entries, "
                f"then targets a Retrieve Booking click within {MAX_CLICK_LATENESS_MS} ms of the scheduled time.{note} "
                "If the pre-click deadline is missed, one immediate late fallback is allowed only before any click is dispatched. "
                "No retry follows a sent, blocked, or uncertain click. Scoot's receipt time is not guaranteed. Keep the laptop awake, plugged in and open.")

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
                    self.request_mode = payload.get("request_mode", True)
                    with self.lock:
                        self.click_timing = None
                        self.observed_request = None
                        self.trace_pnr = ""
                        self.history = []
                        self.schedule_zone = payload["booking"]["timezone"]
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


STATUS_LABELS = {
    "idle": "Ready to schedule", "working": "Processing action", "scheduled": "Scheduled",
    "preparing": "Preparing browser", "ready": "Ready and standing by",
    "submitting": "Retrieving booking", "observing": "Reading the result",
    "submitted": "Review your result", "airport_queue": "Airport counter required",
    "unsuccessful": "Check-in unsuccessful", "not_open_yet": "Check-in opens later",
    "checkin_closed": "Check-in closed", "pnr_not_found": "Booking not found",
    "departure_mismatch": "Check departing airport", "error": "Needs attention",
    "uncertain": "Review your result", "passenger_details": "Passenger page captured",
    "capture_incomplete": "Page capture needs review",
}
PROGRESS_STEPS = (
    "Schedule confirmed",
    f"Open browser & fully load Scoot (T-{PREPARE_LEAD // 60} min)",
    "Fill booking & keep page active",
    f"Construct & hold request (T-{REQUEST_PREPARE_LEAD} s)",
    "Release request at scheduled time",
    "Response received - continue in Scoot browser",
)
FINAL_STEP = len(PROGRESS_STEPS) - 1


def progress_state(state: dict) -> int:
    """Index of the current step; FINAL_STEP + 1 means all done, -1 means stopped or idle."""
    status = state["status"]
    timing = state.get("click_timing") or {}
    if status == "submitting":
        return 4 if timing.get("request_held_at") is not None else 3
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
        return "No release scheduled.\nEnter the booking, choose the date and time, then select Confirm schedule."
    status = state["status"]
    active = status in {"scheduled", "preparing", "ready", "submitting"}
    now = state["now"]
    zone = ZoneInfo(state["timezone"]) if state.get("timezone") else None

    def clock(stamp):
        return datetime.fromtimestamp(stamp, zone).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    def stage(label, stamp, done_text):
        remaining = stamp - now
        when = f"in {remaining:.3f} s" if remaining >= 0 else (done_text if not active or status != "scheduled" else "starting")
        return f"{label:<28}{clock(stamp)}   {when}"

    lines = [f"Release target: {clock(target)}  ({state.get('timezone') or 'computer local time'})", ""]
    lines.append(stage("1  Open browser & load", target - PREPARE_LEAD, "done"))
    if timing.get("request_held_at") is not None:
        lines.append(f"{'2  Construct & hold POST':<28}{clock(timing['request_held_at'])}   held")
    else:
        lines.append(stage("2  Construct & hold POST", target - REQUEST_PREPARE_LEAD,
                           "not held" if not active else "in progress"))
    if timing.get("request_release_called_at") is not None:
        offset = (timing["request_release_called_at"] - target) * 1000
        lines.append(f"{'3  Release request':<28}{clock(target)}   released {offset:+.1f} ms (local call)")
    elif timing.get("request_release_blocked"):
        lines.append(f"{'3  Release request':<28}{clock(target)}   blocked - missed 0-{MAX_CLICK_LATENESS_MS} ms window")
    else:
        lines.append(stage("3  Release request", target, "not released"))
    lines.append("")
    if active:
        lines.append("Waiting for Scoot's response..." if timing.get("request_release_called_at") is not None
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
        if timing.get("request_mode"):
            lines.append("Booking request constructed/intercepted. Chromium's request-start event may precede the hold; "
                         "it must NOT be treated as transmission time or evaluated against the release target.")
            if timing.get("request_release_called_at"):
                offset = (timing["request_release_called_at"] - timing["target"]) * 1000
                lines.append(f"Local release call offset: {offset:+.1f} ms; actual transmission/server receipt unverified.")
            continue
        offset = (record["started_at"] - timing["target"]) * 1000
        within = 0 <= offset <= MAX_CLICK_LATENESS_MS
        lines.append(f"Booking request start: {datetime.fromtimestamp(record['started_at']).strftime('%H:%M:%S.%f')[:-3]} "
                     f"({offset:+.1f} ms from schedule; {'WITHIN' if within else 'OUTSIDE'} 0-{MAX_CLICK_LATENESS_MS} ms target)")
        if timing.get("event_at") is not None:
            click_offset = (timing["event_at"] - timing["target"]) * 1000
            delay = (record["started_at"] - timing["event_at"]) * 1000
            lines.append(f"Schedule-to-click: {click_offset:+.1f} ms; click-to-booking-request: {delay:+.1f} ms")
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
        started = datetime.fromtimestamp(record["started_at"]).strftime("%H:%M:%S.%f")[:-3]
        offset = (record["started_at"] - timing["target"]) * 1000
        lines.append(f"{index}. {record['method']} {record['endpoint']}")
        if record.get("booking_request"):
            lines.append("   MATCHED BOOKING ENDPOINT (does not prove server receipt or queue position)")
        lines.append(f"   {record['type']}; initiator: {record['initiator']}; start: {started}; scheduled offset: {offset:+.1f} ms")
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
        return "--:--:--", "No release scheduled"
    zone = ZoneInfo(state["timezone"]) if state.get("timezone") else None
    clock = datetime.fromtimestamp(target, zone).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    remaining = target - state["now"]
    if status in {"scheduled", "preparing", "ready"} and remaining > 0:
        days, rest = divmod(int(remaining), 86400)
        hours, rest = divmod(rest, 3600)
        minutes, seconds = divmod(rest, 60)
        text = f"{days}d {hours:02d}:{minutes:02d}:{seconds:02d}" if days else f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return text, f"until release at {clock}"
    if status in {"submitting", "observing"}:
        return "00:00:00", "Released - waiting for Scoot's response"
    if status in ACTIVE_STATUSES:
        return "00:00:00", f"Release at {clock}"
    return "--:--:--", f"{STATUS_LABELS.get(status, status)} - release was {clock}"


def timing_table_rows(timing: dict | None) -> list[tuple[str, str, str, str]]:
    if not timing:
        return []
    rows = []

    def add(label, stamp, meaning):
        if stamp is not None:
            rows.append((label, datetime.fromtimestamp(stamp).strftime("%H:%M:%S.%f")[:-3],
                         f"{(stamp - timing['target']) * 1000:+.1f} ms", meaning))

    add("Scheduled release", timing.get("target"), "Target")
    add("Construction click", timing.get("event_at"), "Builds the request early; not sent")
    add("Blocked click", timing.get("blocked_event_at"), "Event blocked; no retry")
    add("Request held", timing.get("request_held_at"), "POST paused locally")
    add("Local release call", timing.get("request_release_called_at"), "NOT proof of transmission")
    if timing.get("request_release_blocked"):
        offset = timing.get("request_release_offset_ms")
        rows.append(("Release blocked", "-", f"{offset:+.1f} ms" if offset is not None else "-",
                     "Deadline missed or cancelled; no retry"))
    for record in timing.get("requests", []):
        if record.get("booking_request"):
            add("Booking request start", record["started_at"],
                "May precede hold; NOT transmission" if timing.get("request_mode") else "Browser request start")
            if "status" in record:
                add("Booking response", record["response_at"], f"HTTP {record['status']}")
            if "failure" in record:
                rows.append(("Booking request failed", "-", "-", record["failure"]))
    return rows


def request_table_rows(timing: dict | None) -> list[tuple[str, ...]]:
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
            datetime.fromtimestamp(record["started_at"]).strftime("%H:%M:%S.%f")[:-3],
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
        ttk.Label(header, text="Releases Scoot's booking retrieval at the exact departure-airport time you set.",
                  style="Subtitle.TLabel").pack(anchor="w")

        self.countdown = tk.StringVar()
        self.countdown_big = tk.StringVar(value="--:--:--")
        self.countdown_caption = tk.StringVar(value="No release scheduled")
        self.countdown_panel = ttk.Frame(shell, style="Hero.TFrame", padding=(20, 16))
        self.countdown_panel.pack(fill="x", pady=(6, 14))
        ttk.Label(self.countdown_panel, text="RELEASE COUNTDOWN", style="HeroEyebrow.TLabel").pack(anchor="w")
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
        ttk.Label(schedule, text="Use the departure airport's local time (not the flight time).",
                  wraplength=400, style="Muted.TLabel").pack(anchor="w", pady=(0, 10))
        ttk.Label(schedule, text="Release date (YYYY-MM-DD)").pack(anchor="w")
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
        self.field(schedule, "Release time (HH:mm:ss.SSS, e.g. 08:00:00.000)", ttk.Entry(schedule, textvariable=self.clock))
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
        self.history_table = self.make_table(status_panel, [("Time", 200), ("Action", 650)], height=4)
        self.history_rows = []
        ttk.Label(status_panel, text="Release timing (computer time)", style="Section.TLabel").pack(anchor="w", pady=(8, 0))
        self.timing_table = self.make_table(status_panel, [
            ("Event", 160), ("Time", 115), ("From schedule", 115), ("Meaning", 300),
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
        ttk.Label(notes, wraplength=820, style="Muted.TLabel", text="Keep this app open, the Mac plugged in and the lid open; quitting cancels the schedule. "
                  "One release only, no retries. The recorded release is the local call time, not Scoot's receipt time, "
                  "and a passenger page does not confirm a seat. Passenger pages are saved locally and may contain personal data.").pack(anchor="w")
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
        window.title(f"Release date - {zone.key if zone else 'choose departure airport next'}")
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
        ttk.Label(window, text="Request-start timestamps are NOT server receipt. With request holding they may precede "
                  "the hold and are NOT transmission times. Booking labels match the exact POST endpoint, not check-in success.",
                  wraplength=1000, padding=10, style="Bg.TLabel").pack(anchor="w")
        table = self.make_table(window, [
            ("Kind", 80), ("Method", 65), ("Endpoint", 400), ("Type", 80), ("Initiator", 90),
            ("Browser start", 115), ("From schedule", 115), ("From click", 110), ("Response", 300),
        ], height=14)
        note = tk.StringVar()
        ttk.Label(window, textvariable=note, padding=10, style="Bg.TLabel").pack(anchor="w")

        def update():
            timing = self.controller.snapshot().get("click_timing")
            self.fill_table(table, request_table_rows(timing))
            count = len(timing.get("requests", [])) if timing else 0
            dropped = timing.get("requests_dropped", 0) if timing else 0
            note.set(f"{count} requests; {dropped} omitted. Snapshot only: select Refresh to update. "
                     "Query strings, credentials, entered PNR, bodies and headers are omitted.")

        ttk.Button(window, text="Refresh", command=update).pack(anchor="e", padx=10, pady=5)
        update()

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
        self.status.set(STATUS_LABELS.get(status, status))
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
        # The stage breakdown only adds detail once a release target exists.
        has_target = state.get("target") is not None or (state.get("click_timing") or {}).get("target") is not None
        if has_target and not self.countdown_label.winfo_manager():
            self.countdown_label.pack(fill="x")
        elif not has_target and self.countdown_label.winfo_manager():
            self.countdown_label.pack_forget()
        zone = ZoneInfo(state["timezone"]) if state.get("timezone") else None
        history = [(datetime.fromtimestamp(event["at"], zone).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3], event["action"])
                   for event in state.get("history", [])]
        if history != self.history_rows:
            self.fill_table(self.history_table, history)
            self.history_table.yview_moveto(1)
            self.history_rows = history
        rows = timing_table_rows(state.get("click_timing"))
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
