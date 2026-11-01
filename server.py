"""HTTP control server for auto-chope-seat.

Runs the existing scheduler/browser Controller headlessly on a server and
exposes a token-protected JSON API plus a mobile-friendly web page, so the
schedule can be created and monitored from a phone. Start with:

    ACS_TOKEN=<secret> ACS_HEADLESS=1 python server.py
"""
import hmac
import json
import os
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

from app import (ACTIVE_STATUSES, PROGRESS_STEPS, STATUS_LABELS, Controller,
                 departure_stations, headline_countdown, progress_state,
                 stage_countdown, timing_table_rows)

WEB_ROOT = Path(__file__).parent / "web"
STATIC_FILES = {
    "/": (WEB_ROOT / "index.html", "text/html; charset=utf-8"),
    "/index.html": (WEB_ROOT / "index.html", "text/html; charset=utf-8"),
    "/manifest.webmanifest": (WEB_ROOT / "manifest.webmanifest", "application/manifest+json"),
    "/icon.png": (Path(__file__).parent / "icon.png", "image/png"),
}
MIN_TOKEN_LENGTH = 12


def state_payload(controller: Controller) -> dict:
    state = controller.snapshot()
    zone = ZoneInfo(state["timezone"]) if state.get("timezone") else None
    headline, caption = headline_countdown(state)
    status = state["status"]
    return {
        "status": status,
        "status_label": STATUS_LABELS.get(status, status),
        "message": state["message"],
        "headline": headline,
        "caption": caption,
        "stage_text": stage_countdown(state),
        "steps": list(PROGRESS_STEPS),
        "step_index": progress_state(state),
        "active": status in ACTIVE_STATUSES,
        "can_cancel": status in {"scheduled", "preparing", "ready"},
        "target": state["target"] or (state.get("click_timing") or {}).get("target"),
        "server_now": state["now"],
        "timezone": state.get("timezone") or "",
        "history": [{"time": datetime.fromtimestamp(event["at"], zone).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                     "action": event["action"]} for event in state.get("history", [])],
        "timing_rows": [list(row) for row in timing_table_rows(state.get("click_timing"))],
    }


def stations_payload() -> dict:
    return {"stations": [{"label": f'{s["name"]} ({s["code"]}) - {s["country"]}',
                          "name": s["name"], "code": s["code"], "timezone": s["timezone"]}
                         for s in departure_stations()["stations"]]}


class ApiHandler(BaseHTTPRequestHandler):
    controller: Controller = None
    token = ""
    protocol_version = "HTTP/1.1"

    def authorised(self) -> bool:
        supplied = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        return bool(self.token) and hmac.compare_digest(supplied, self.token)

    def send_json(self, payload: dict, status: int = 200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: Path, content_type: str):
        try:
            body = path.read_bytes()
        except OSError:
            self.send_json({"error": "Not found."}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def guard(self) -> bool:
        if self.authorised():
            return True
        self.send_json({"error": "Missing or invalid access token."}, 401)
        return False

    def do_GET(self):
        if self.path in STATIC_FILES:
            self.send_file(*STATIC_FILES[self.path])
        elif self.path == "/api/state":
            if self.guard():
                self.send_json(state_payload(self.controller))
        elif self.path == "/api/stations":
            if self.guard():
                self.send_json(stations_payload())
        else:
            self.send_json({"error": "Not found."}, 404)

    def do_POST(self):
        if self.path not in {"/api/schedule", "/api/cancel"}:
            self.send_json({"error": "Not found."}, 404)
            return
        if not self.guard():
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(data, dict):
                raise ValueError("Send a JSON object.")
        except (ValueError, json.JSONDecodeError):
            self.send_json({"error": "Invalid JSON body."}, 400)
            return
        try:
            if self.controller.stop.is_set():
                raise ValueError("Browser worker is unavailable. Restart the server.")
            action = "schedule" if self.path == "/api/schedule" else "cancel"
            self.controller.submit(action, data)
        except ValueError as error:
            self.send_json({"error": str(error)}, 400)
            return
        self.send_json({"ok": True})

    def log_message(self, format, *args):  # noqa: A002 - BaseHTTPRequestHandler API
        # Suppress default per-request logging; request paths never include the PNR.
        pass


def main():
    token = os.environ.get("ACS_TOKEN", "")
    if len(token) < MIN_TOKEN_LENGTH:
        sys.exit(f"Set ACS_TOKEN to a secret of at least {MIN_TOKEN_LENGTH} characters before starting the server.")
    controller = Controller()
    controller.worker.start()
    ApiHandler.controller = controller
    ApiHandler.token = token
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), ApiHandler)
    print(f"auto-chope-seat server listening on port {port} (headless={os.environ.get('ACS_HEADLESS', '') == '1'})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        controller.stop.set()
        controller.worker.join(timeout=35)


if __name__ == "__main__":
    main()
