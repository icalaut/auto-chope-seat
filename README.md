# auto-chope-seat

Native macOS desktop window for scheduling one Scoot staff-travel booking
retrieval. The app interface uses Python's built-in Tkinter; it does not run a
local web server or include a hosted web application. Playwright opens a
separate Chromium window to interact with Scoot's website.

## Setup and run (macOS)

Python 3.11+ with Tkinter is required. Install the app dependencies and
Playwright's Chromium browser:

```sh
cd ~/Documents/auto-chope-seat
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
.venv/bin/python app.py
```

The `auto-chope-seat` window and Scoot browser open on your desktop. Close the
app window to stop it; closing cancels a pending retrieval. If your Python
installation does not include Tkinter, install a Python distribution that
includes Tcl/Tk.

## Use

1. Enter your six-character booking reference (PNR) and select or enter the
   exact departure station label used by Scoot. The PNR is normalized to
   uppercase. Suggestions contain 274 stations extracted from the
   [official Scoot homepage](https://www.flyscoot.com/en)'s From picker on
   2026-10-13, stored in `stations.json` with source metadata. Search by
   station name, country, or IATA code. Choosing a suggestion fills its
   original    station name; suggestions can be filtered by station name, country, or
   IATA code. Manual station entry remains available.
2. Confirm the departure airport timezone. It is selected automatically for
   known stations, using the IATA airport dataset and explicit mappings for
   grouped or ambiguous entries. For an unrecognized or ambiguous station,
   enter its IANA timezone manually. The app rejects a mismatched timezone for
   known stations and handles daylight saving time for the scheduled date.
3. Enter a retrieval **date and HH:mm:ss**, not the flight departure time. The
   date and time are initially filled using the selected airport timezone.
   Choose a future time; past or elapsed times are rejected.
4. Select **Prepare browser**. Review the PNR and departure in the separate
   Scoot browser window.
5. Select **Schedule retrieval**. Keep the app, browser, and computer awake.
   **Cancel** prevents a click before submission begins.

At the scheduled time, the app verifies the form again and clicks **Retrieve
Booking once**. It then reads visible result text briefly without clicking or
refreshing. A queue number or unsuccessful web check-in message stops the
automation; attend the airport staff-travel counter in person as instructed.
No automatic retries or further check-in actions are made, and no seat or
check-in success is inferred.

The live airline integration is not yet verified; Scoot was under maintenance
during development. The app stops if it cannot identify or verify the form. It
does not bypass maintenance, CAPTCHA, queues, or check-in opening times.

Scheduling uses airport-local time. Ambiguous or nonexistent daylight-saving
times and past times are rejected. There are no automatic submission retries.
A click already dispatched cannot be undone; a click more than two seconds
late is rejected, including after system sleep. Exact delivery time is not
guaranteed.

## Privacy and testing

The PNR is held in memory only and sent to Scoot when you prepare the browser.
Booking data is not saved to files. The app does not confirm or reserve a seat;
review Scoot's response yourself.

Run the local tests with:

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Browser tests require the Playwright Chromium browser. Tests do not deploy
anything or submit a live airline booking.
