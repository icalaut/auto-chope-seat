# auto-chope-seat

Local, single-device Scoot staff-travel booking retrieval scheduler.
This is independent of the auspicious-date application.

## Setup and run (macOS)

Python 3.11+ is required.

```sh
cd ~/Documents/auto-chope-seat
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
.venv/bin/python app.py
```

Open http://127.0.0.1:8766. Stop with Ctrl+C in the terminal.

1. Enter your PNR and the exact departure station label used by Scoot.
   PNR letters are automatically converted to uppercase when typed or pasted;
   the server also normalizes them before filling Scoot's form.
   Departure suggestions contain 274 unique stations extracted from the
   [official Scoot homepage](https://www.flyscoot.com/en)'s From picker on
   2026-10-13, stored in `stations.json` with source metadata.
   Search by station name, country or IATA code. Selecting a suggestion passes
   its original station name, without the display code/country, to ChopeMySeat.
   The public list includes connecting stations and grouped cities; it does not
   establish eligibility in the staff-travel system. Exact live label matching
   still applies. Manual station entry remains available for label differences.
   Source metadata remains in the dataset and documentation, not in the form.
2. The departure airport's IANA timezone is automatically selected, e.g.
   `Asia/Singapore`, `Asia/Taipei`, `Asia/Bangkok`, or `Australia/Sydney`.
   Mapping uses the `airportsdata` IATA dataset, not the public site's country
   label (some labels are inconsistent). Explicit mappings cover the Tokyo
   city group (`TYO`, Asia/Tokyo), Phnom Penh (`PNH`, Asia/Phnom_Penh),
   Banyuwangi (`BWX`, Asia/Jakarta), and Toraja (`TRT`, Asia/Makassar).
   All 274 current entries have a timezone. Changing to an unrecognized station
   clears the previous timezone and requires manual selection; the server
   rejects a mismatched timezone for known stations. DST is resolved at the
   scheduled date, not as a fixed UTC offset.
3. Supply a retrieval **date and HH:mm:ss**, not the flight departure time.
   The date starts as today in the computer's timezone, then updates to today
   in the selected departure airport's timezone. Manually edited dates are
   preserved when switching stations or timezones.
   Retrieval time is prefilled with the current `HH:mm:ss` on load and updates
   to the airport timezone when selected, unless manually edited. This is an
   editable initial value, not a ticking clock; choose a future time before
   scheduling, as past/current elapsed times are rejected.
4. Prepare the browser. Review the PNR and departure on the official site.
5. Schedule. Keep the computer awake, app running and browser open.
6. The app clicks **Retrieve Booking once**, then reads visible result text for
   up to five seconds without clicking or refreshing. A displayed queue number
   (e.g. `Queue: TRY-1`) produces an `airport_queue` status: stop and attend the
   airport staff-travel counter in person to queue and wait. An explicit
   unsuccessful web check-in message also stops automation. No retries or further
   check-in actions occur in any outcome. Inspect the airline's response yourself;
   no seat/check-in success is inferred.

The schedule is airport-local time with daylight saving accounted for.
Ambiguous/nonexistent DST times and past times are rejected. A calendar day is
always required. There are no automatic submission retries or refresh storms.
Cancel is effective before submission begins; a dispatched click cannot be undone.
Missed clicks over two seconds late are rejected, including after system sleep.
Timing depends on the OS/browser/network; exact delivery to Scoot is not guaranteed.

## Current integration limitation

### Static Cloudflare hosting

Airport suggestions and timezone options load independently from `stations.json`
and `timezones.json`, with airport timezone mappings bundled in the station data.
Include both JSON files alongside `index.html`, `app.js`, and `style.css` in the
published static directory. No Python API is needed for these dropdowns.

A static Cloudflare deployment does **not** run the Python/Playwright scheduler.
The hosted page explicitly displays "Hosted form only" and disables automation
controls instead of suggesting that retrieval is available. Use the local
Python app for scheduled clicks and the controlled-browser preview. A remote
automation backend would require a separate architecture, authentication, and
secure handling of booking data; it is not provided by static hosting.

The live site redirected to maintenance during development. Form selectors are
based on the supplied screenshot (PNR, Departing, Retrieve Booking), with local
fixture tests, not a verified live DOM. Uniqueness, selected station, PNR and
button readiness are checked before proceeding. If those checks fail, the app
shows an error and does not retrieve. Once maintenance ends, live-site verification
and possibly selector adjustments are still required. No booking credentials
were used during development.
Result recognition is likewise based on the supplied queue screenshot and local
fixtures, not a verified live result page. Unknown or later-arriving results are
left for manual review; they never trigger another submission.

Use only one device per booking as advised by Scoot. The app does not bypass
maintenance, access controls, CAPTCHA, queues, or opening-time restrictions.
It does not automatically select seats, pay, or complete check-in.

## Privacy

The panel below the form shows JPEG captures of the actual controlled browser,
including filled entries and the result, refreshed about once per second.
This is read-only, not a cross-origin iframe or a second Scoot session. Captures
remain in memory, use a token-protected uncached local endpoint, and may contain
personal information; do not share your app screen. Preview work pauses in the
last three seconds before submission to prioritize the timed click.

After retrieval the controlled browser's result tab is brought to the front.
If Scoot opens a popup, that tab is used; otherwise its existing tab is kept.
The app does not copy result URLs into another browser or duplicate submissions,
because that can lose the booking session. Use **Show Scoot browser / result tab**
for manual continuation. It does not interact with passenger/passport forms.

PNR stays in memory and is filled into the official Scoot website only when
Prepare is requested. No booking data is written to files or browser storage
by this app; the airline controls its own website's storage.
The server binds to loopback only, rejects other Host/Origin values, and requires
a session token for mutations. API responses are not cached. Inputs and actions
are not written to HTTP access logs.

## Tests

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Tests use a local synthetic booking form, not the live airline site.
