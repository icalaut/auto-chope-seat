# auto-chope-seat

Native macOS desktop window for scheduling one Scoot staff-travel booking
retrieval. The app interface uses Python's built-in Tkinter. Playwright opens a
separate Chromium window to interact with Scoot's website. The same scheduler
can also be hosted on Cloudflare (Workers + Browser Rendering) and controlled
from a phone (see "Hosted Cloudflare application").

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

## Hosted Cloudflare application

The hosted backend uses a Worker, Browser Rendering, and one SQLite Durable
Object per authenticated user, protected by Cloudflare Access. The mobile-first
web frontend works from a phone: open the Access-protected URL in Safari and
sign in. Static assets alone cannot provide the preview or scheduled clicks.
The frontend probes `/api/config`; missing APIs, missing setup, and failed
authentication explicitly disable automation without affecting the
airport/timezone lists.

### Deploy to the Worker

Node.js 22.15+ is required for the hosted test harness; use a current Node 22 LTS.

1. Run `npm ci`, `npm run typecheck`, and `npm test`.
2. Sign in with `npx wrangler login` in your own browser. Do not put Cloudflare
   API tokens, passwords, booking references, or passport data in Git.
3. Enable Cloudflare Access for the Worker's hostname in its domain settings,
   or protect a custom hostname with an Access self-hosted application.
   Create an **Allow** policy limited to your email/approved users, not a public
   Bypass policy. The Worker independently verifies signed Access JWTs.
4. `wrangler.jsonc` includes the owner-supplied `ACCESS_TEAM_DOMAIN` and
   `ACCESS_AUD` for the existing application. These are public identifiers,
   not authentication secrets, and deploy as runtime Text variables.
   For a different Access application, update both values: the team hostname
   has no `https://` or trailing slash, and AUD is the application's Audience
   tag, not its application ID. Unset or mismatched configuration fails closed.
5. Create and upload an encryption secret without printing it or saving it:

   ```sh
   openssl rand -base64 32 | npx wrangler secret put BOOKING_KEY
   ```

   APIs remain disabled without the secret. Do not rotate this key while a
   booking is pending.
6. Confirm your Cloudflare account supports Browser Rendering and SQLite
   Durable Objects and review its quotas/billing. `wrangler.jsonc` supplies the
   `BROWSER`, `BOOKINGS`, SQLite migration, and `ASSETS` bindings.
7. Run `npm run deploy` (or `npx wrangler deploy`). The configured build hook
   runs `npm run build` automatically to create `dist` before uploading the
   five public assets and the Worker entrypoint. Deploying only `dist` is
   insufficient.
8. Open your Access-protected URL and sign in. Prepare a booking only when
   ready to contact Scoot, and verify the browser preview before scheduling.

### Hosted behavior and limits

- Scheduling requires a time more than five seconds and at most 24 hours ahead.
  For schedules over five minutes away, the prepared browser is closed and
  reopened 30 seconds before retrieval; entries are reverified before the click.
- Durable Object alarms are at-least-once, not exact-second guarantees. A
  persisted attempt marker precedes the external click, so replayed alarms
  never click again. Verification or platform delays over two seconds stop
  retrieval; there is no late click or automatic retry.
- Cloud preview updates every five seconds while visible and pauses 15 seconds
  before retrieval. After retrieval, manual controls (clicks, typing, keys,
  scroll) act on the same remote session for the remaining time.
- Sessions expire ten minutes after preparation or retrieval. Cloud jobs
  continue if the phone/page closes; use one device per booking.
- Booking data is sealed with AES-GCM before Durable Object storage and removed
  on cancellation, cleanup, or before a retrieval attempt. Browser Rendering is
  metered; check Cloudflare's limits and pricing before deployment.

### Self-hosted Docker alternative

The Python scheduler can instead run on any Linux host:

```sh
docker build -t auto-chope-seat .
docker run -d --restart unless-stopped -p 8000:8000 \
  -e ACS_TOKEN="$(openssl rand -hex 24)" auto-chope-seat
```

Open `http://<server>:8000`, enter the `ACS_TOKEN` value once, and optionally
use Share → "Add to Home Screen". `ACS_TOKEN` (12+ characters) is required and
must travel only over HTTPS or a VPN. Stopping the container cancels a pending
retrieval, and captured passenger pages remain inside the container.

## Installable Mac release

Tagged releases are built as signed and notarized disk images for both Apple
Silicon (`arm64`) and Intel (`x86_64`) Macs. Download the matching
`auto-chope-seat-v1.0.0-arm64.dmg` or
`auto-chope-seat-v1.0.0-x86_64.dmg` from the project's GitHub Releases page,
open the disk image, and drag the app into Applications. The packaged app
includes Chromium and does not require a separate Python installation.

Before publishing a tagged release, configure these repository Actions secrets
from an Apple Developer account:

- `APPLE_CERTIFICATE_P12_BASE64`: base64-encoded Developer ID Application
  certificate exported as a `.p12` file.
- `APPLE_CERTIFICATE_PASSWORD`: password for that certificate.
- `APPLE_NOTARY_KEY_ID`: App Store Connect API key ID.
- `APPLE_NOTARY_ISSUER_ID`: App Store Connect API issuer ID.
- `APPLE_NOTARY_PRIVATE_KEY`: contents of the corresponding `.p8` private key.

Never commit or share signing credentials. Pushing a `vMAJOR.MINOR.PATCH` tag
builds and notarizes both Mac versions, then publishes the GitHub release.
The Intel build runs on a GitHub-hosted Intel Mac runner; the Apple Silicon
build runs on an Apple Silicon runner.

## Use

1. Enter your six-character booking reference (PNR) and select or enter the
   exact departure station label used by Scoot. The PNR is normalized to
   uppercase. Suggestions contain 274 stations extracted from the
   [official Scoot homepage](https://www.flyscoot.com/en)'s From picker on
   2026-10-13, stored in `stations.json` with source metadata. Search by
   station name, country, or IATA code. Choosing a suggestion fills its
   original station name; suggestions can be filtered by station name, country, or
   IATA code. Manual station entry remains available.
2. Confirm the departure airport timezone. It is selected automatically for
   known stations, using the IATA airport dataset and explicit mappings for
   grouped or ambiguous entries. For an unrecognized or ambiguous station,
   enter its IANA timezone manually. The app rejects a mismatched timezone for
   known stations and handles daylight saving time for the scheduled date.
3. Use the **calendar icon beside the date field** (or enter `YYYY-MM-DD`) and set the
   **local API release time in HH:mm:ss.SSS**, for example `13:14:59.125`.
   The three digits after the decimal are milliseconds. These are interpreted
   in the departure airport's timezone, not the computer's timezone or flight
   departure time. Past calendar days are disabled in that timezone. Legacy
   second-only input remains accepted as `.000`. Choose a future time; past,
   elapsed, ambiguous or nonexistent airport-local times are rejected.
   The calendar opens beside the icon. Before selecting an airport, it uses a
   clearly labelled computer-date preview; scheduling still requires the
   departure timezone and validates the selected date in that timezone.
4. Select **Confirm schedule**. The browser opens twelve minutes early, waits
   for full page load, then fills the booking. Review the PNR and departure in the separate Scoot browser.
   Keep the app, browser, and computer awake. **Cancel schedule** prevents a
   click before submission begins.

The window follows the flow in three numbered sections: **1 Booking**,
**2 Schedule** and **3 Progress**. A fixed **Countdown** panel above them lists
the three timed stages (open browser and load, construct and hold POST,
release request) with millisecond times in the departure timezone, and keeps the
release offset and result after an attempt. **Progress** shows six steps,
with completed steps ticked and the current step highlighted: schedule confirmed,
open browser and fully load Scoot (T-12 min), fill the booking and keep the page
active, construct and hold the request (T-15 s), release at the scheduled time,
and response received. Below it, an **Activity log** records each stage change
and a **Release timing** table lists the scheduled release, construction click,
request hold, local release call and booking request/response.
The screen refreshes every 50 ms; its countdown is a visual aid, not the
release timer, and is not expected to display every millisecond.

Browser preparation waits for the full page `load` event before checking or
populating the booking controls. Full-load waiting is bounded to two minutes
per attempt and by the existing 60-second preparation cutoff. A stalled asset
causes an explicit preparation failure, not a partially loaded form submission.
The app then waits for uniquely identified visible booking controls,
an editable PNR and an enabled departure control before filling them, then
verifies the entries and the Retrieve Booking button. The `load` event does not
guarantee that all later application requests have finished. Scripts,
styles, and network requests are not blocked. Navigation errors, HTTP errors,
maintenance, and visible access-denied/verification pages are reported rather
than treated as successful preparation.
The browser still uses an isolated session rather than your everyday profile.
The app brings the Scoot tab to the front during preparation and once per
second while standing by and holding the request, without refreshing or
clicking it. Foreground commands stop during the final 250 ms before release
to avoid adding protocol work at the deadline. Background-throttling safeguards
and macOS keep-awake remain enabled. This can take focus from other tabs/apps;
it cannot prevent you or macOS from changing focus or closing the lid.

Fifteen seconds before the scheduled request-release time, the app verifies the
form and clicks **Retrieve Booking once**, holding the booking POST locally
until the target. It then reads visible result text briefly without clicking or
refreshing. A queue number or unsuccessful web check-in message stops the
automation; attend the airport staff-travel counter in person as instructed.
No automatic retries or further check-in actions are made, and no seat or
check-in success is inferred.

The user reported a mock-PNR attempt returning a booking/departure error.
Successful passenger-page continuation and queue results remain unverified.
The app stops if it cannot identify or verify the form. It
does not bypass maintenance, CAPTCHA, queues, or check-in opening times.

Scheduling uses airport-local time. Ambiguous or nonexistent daylight-saving
times and past times are rejected. There are no automatic submission retries.
A click already dispatched cannot be undone. Submission begins when early
request construction starts and cannot be cancelled through the UI.
The form, button, visibility and duplicate-click checks still apply.
Exact `.000` execution, request delivery, queue position, and seat availability
cannot be guaranteed on macOS, in Chromium, or over the network. The click-event
time is the local event guard's timestamp, not Scoot's receipt time.

### Standard request hold/release

Every schedule uses the original Scoot page, session, payload and response
handler. There is no mode checkbox or per-schedule experimental prompt, and
callers cannot select the legacy click-only mode. It does not independently replay the API.
Fifteen seconds before the target, it verifies the form and clicks once to
construct the request. An exact endpoint route holds the POST locally, then
calls Playwright's continuation only if the local wall-clock check is within
0-5 ms after the schedule. A missed window, cancellation, or duplicate POST is
aborted without a late fallback or resubmission. If no POST arrives by the
deadline, later matching POSTs remain blocked until the browser is closed.
Nonmatching traffic is not held. Service workers are disabled
so they cannot hide a request from the route. Routing also disables the
context's HTTP cache, which can slow preparation.

Submission becomes non-cancellable when early construction begins. The original
page receives the response and runs its normal continuation; no passenger
details are automatically submitted. Automated tests use local fixtures;
the user reported a mock-PNR error response from Scoot. Successful passenger-page
continuation, token validity across bookings, and queue results remain
**unverified**. Do not rely on a local release timestamp as proof of queue priority.

The recorded release-call time is **not** actual network transmission or
server receipt. Chromium's request-initiation event can occur *before* route
interception and must not be used as evidence of on-time transmission in this
mode. Neither Playwright nor macOS provides a guaranteed 0-5 ms release or
network window. There is no request body/header logging or credential export.
The local gate checks the wall clock on waits of at most 1 ms and refuses early
or more-than-5-ms-late releases. This is a fail-closed tolerance, **not an
exact-time guarantee**, including at our end: OS scheduling, Python and the
Playwright protocol can pause between the check and browser processing.
Only the measured local continuation-call timestamp is reported as local
release. Browser/network initiation and Scoot's receipt are separate events.
Keep the computer clock synchronized, the laptop awake and its lid open.

### Passive request timing diagnostics

After an attempt, open **Network details** for an in-memory snapshot of
Chromium's Fetch, XHR, and navigation request candidates, including popups.
The **Release timing** table shows event, time, schedule offset and meaning.
Request details are also tabular; use the horizontal scrollbar for all
columns and **Refresh** to update the snapshot. Each candidate shows its endpoint,
method, browser request-initiation time, offset from the schedule and click,
and response-header timing or failure when observed. These browser timestamps
remain useful when Python receives an event late. Cached/service-worker
responses are marked; request initiation does not prove a network transmission.
Up to 100 candidates are retained per attempt, starting when submission begins;
additional candidates are counted as omitted. Collection depends on browser
automation processing events and is not a continuous manual-session recorder.

The exact `POST https://apigw.singaporeair.com/chopemyseat/booking/retrieval`
endpoint, observed in a user-supplied trace, is labelled as the booking request.
Its schedule offset is evaluated against the 0-5 ms request-start target,
separately from click timing. This is diagnostic only: a late request is
reported, not prevented or recalled. Background requests remain included but
do not establish booking timing. A matching endpoint does not confirm server
receipt, response contents, queue position, or a seat.
Do not use the first fetch/XHR or a single measured delay to calculate an early
click: that could send the booking before its opening time. No early requests,
request interception, request resubmission, or latency compensation are added.

Diagnostics omit query strings, fragments, URL credentials and occurrences of
the entered PNR. They do not capture bodies or headers or write a trace to disk.
Endpoint paths can still contain other personal data; do not share them without
reviewing them. Closing the app discards the in-memory trace.

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
