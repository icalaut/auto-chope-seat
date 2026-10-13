import json
import queue
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

import airportsdata
from playwright.sync_api import Page, sync_playwright


SITE = "https://chopemyseat.flyscoot.com/"
HOST = "127.0.0.1"
PORT = 8766
ORIGIN = f"http://{HOST}:{PORT}"
TOKEN = secrets.token_urlsafe(32)
ROOT = Path(__file__).parent
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
        self.preview_requested = threading.Event()
        self.preview_image = None
        self.preview_time = None
        self.preview_error = ""

    def update(self, status: str, message: str, target: float | None = None):
        with self.lock:
            self.state = {"status": status, "message": message, "target": target}

    def snapshot(self) -> dict:
        with self.lock:
            return {**self.state, "now": time.time(), "preview_time": self.preview_time,
                    "preview_error": self.preview_error}

    def capture_preview(self, page: Page):
        try:
            image = page.screenshot(type="jpeg", quality=65, timeout=1000)
            with self.lock:
                self.preview_image = image
                self.preview_time = time.time()
                self.preview_error = ""
        except Exception as error:
            with self.lock:
                self.preview_error = f"Browser preview unavailable: {type(error).__name__}: {error}"
        finally:
            self.preview_requested.clear()

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
                    self.capture_preview(page)
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
                                with self.lock:
                                    self.preview_image = None
                                    self.preview_time = None
                                page.goto(SITE, wait_until="domcontentloaded", timeout=30000)
                                page.get_by_role("button", name=re.compile(r"^Retrieve Booking\s*$", re.I)).wait_for(timeout=15000)
                                form.prepare(payload)
                                booking = payload
                                self.capture_preview(page)
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
                                self.capture_preview(visible_page)
                                self.update(outcome["status"], outcome["message"])
                            if self.preview_requested.is_set() and (target is None or target - time.time() > 3):
                                self.capture_preview(result_pages[-1] if result_pages else page)
                        except Exception as error:
                            target = None
                            booking = None
                            self.update("error", f"{type(error).__name__}: {error} No automatic retry will be attempted.")
                finally:
                    browser.close()
        except Exception as error:
            self.update("error", f"Browser worker stopped: {type(error).__name__}: {error}. Restart the app after resolving this error.")
            self.stop.set()


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
            self.json_response(200, {"token": TOKEN, "timezones": sorted(available_timezones())})
        elif self.path == "/api/stations":
            self.json_response(200, departure_stations())
        elif self.path == "/api/preview":
            if self.headers.get("X-App-Token") != TOKEN:
                self.json_response(403, {"error": "Unauthorized preview request."})
                return
            controller.preview_requested.set()
            with controller.lock:
                image = controller.preview_image
                message = controller.preview_error
            if image is None:
                self.json_response(503, {"error": message or "Waiting for the controlled browser preview."})
            else:
                self.send(200, image, "image/jpeg")
        elif self.path in {"/", "/app.js", "/style.css"}:
            filename = "index.html" if self.path == "/" else self.path[1:]
            content_type = {"/": "text/html; charset=utf-8", "/app.js": "text/javascript", "/style.css": "text/css"}[self.path]
            self.send(200, (ROOT / filename).read_bytes(), content_type)
        else:
            self.json_response(404, {"error": "Not found."})

    def do_POST(self):
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
            controller.submit(data.get("action"), data)
            self.json_response(202, {"accepted": True})
        except (ValueError, ZoneInfoNotFoundError) as error:
            self.json_response(400, {"error": str(error)})


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    controller.worker.start()
    print(f"auto-chope-seat: {ORIGIN} — Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping; pending retrievals are cancelled.", flush=True)
    finally:
        controller.stop.set()
        controller.worker.join(timeout=35)
        server.server_close()


if __name__ == "__main__":
    main()
