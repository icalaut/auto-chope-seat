"""Refresh stations.json from the live ChopeMySeat Departing dropdown.

Usage: .venv/bin/python scripts/fetch_stations.py
"""
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
from app import SITE, BookingForm, default_browser  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402


def main():
    browser_info = default_browser() if app.USE_DEFAULT_BROWSER else {"executable": None}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False, executable_path=browser_info["executable"])
        try:
            page = browser.new_page()
            page.goto(SITE, wait_until="domcontentloaded", timeout=60000)
            form = BookingForm(page)
            form.wait_until_ready(30)
            _, departure, _ = form.fields()
            departure.click()
            page.locator(app.DEPARTURE_OPTION).first.wait_for(timeout=10000)
            rows = page.locator(app.DEPARTURE_OPTION).evaluate_all(
                "options => options.map(option => option.innerText.split('\\n').map(line => line.trim()).filter(Boolean))")
        finally:
            browser.close()

    stations, seen = [], set()
    for lines in rows:
        if len(lines) != 3 or len(lines[2]) != 3 or not lines[2].isalpha():
            continue
        name, country, code = lines[0], lines[1], lines[2].upper()
        if code not in seen:
            seen.add(code)
            zone = app.STATION_TIMEZONE_OVERRIDES.get(code) or app.AIRPORTS.get(code, {}).get("tz")
            if not zone:
                raise SystemExit(f"No timezone known for {name} ({code}); add it to STATION_TIMEZONE_OVERRIDES.")
            stations.append({"name": name, "country": country, "code": code, "timezone": zone})
    if len(stations) < 50:
        raise SystemExit(f"Only {len(stations)} stations found; stations.json left unchanged.")

    data = {"source": SITE, "source_field": "Departing dropdown (Staff travel Web Check-in Form)",
            "extracted_on": date.today().isoformat(), "stations": stations}
    (ROOT / "stations.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    print(f"Wrote {len(stations)} stations.")


if __name__ == "__main__":
    main()
