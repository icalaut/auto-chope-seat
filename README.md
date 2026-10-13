# auto-chope-seat

Local or authenticated Cloudflare Scoot staff-travel booking retrieval scheduler.
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

## Hosted Cloudflare application

The hosted backend uses a Worker, Browser Run (Browser Rendering), and one SQLite
Durable Object per authenticated user. Static assets alone cannot provide the
preview or scheduled clicks. The frontend probes `/api/config`; missing APIs,
missing setup, and failed authentication explicitly disable automation without
affecting the airport/timezone lists.

### Deploy to the existing Worker

Node.js 22.15+ is required for the hosted test harness; use a current Node 22 LTS.

1. Run `npm ci`, `npm run typecheck`, and `npm test`.
2. Sign in with `npx wrangler login` in your own browser. Do not put Cloudflare
   API tokens, passwords, booking references, or passport data in Git.
3. Enable Cloudflare Access for
   `auto-chope-seat.wenyap68.workers.dev` in the Worker's domain settings, or
   protect a custom hostname with an Access self-hosted application.
   Create an **Allow** policy limited to your email/approved users, not a public
   Bypass policy. The Worker independently verifies signed Access JWTs.
4. Under Worker **Settings > Variables and Secrets**, add a **Text** runtime
   variable `ACCESS_TEAM_DOMAIN` containing your complete team hostname
   (e.g. `your-team.cloudflareaccess.com`, without `https://`) and `ACCESS_AUD`
   to that Access application's Audience (AUD) tag. These are identifiers,
   not authentication secrets. Add `ACCESS_AUD` as a **Text** variable too,
   then select **Deploy**. They must match the application protecting this
   hostname. Do not put them only in build variables. Unset or mismatched
   configuration fails closed. `keep_vars: true` preserves these dashboard-managed
   runtime variables on subsequent Wrangler deployments; no empty `vars`
   placeholders are shipped to overwrite them.
5. Create and upload an encryption secret without printing it or saving it:

   ```sh
   openssl rand -base64 32 | npx wrangler secret put BOOKING_KEY
   ```

   If Wrangler asks to create the Worker first, run `npm run deploy` with Access
   configured, then repeat the secret command. APIs remain disabled without
   the secret. Do not rotate this key while a booking is pending.
6. Confirm your Cloudflare account supports Browser Run and SQLite Durable
   Objects and review its quotas/billing. `wrangler.jsonc` supplies `BROWSER`,
   `BOOKINGS`, the `v1` SQLite migration, and the `ASSETS` binding.
7. Run `npm run deploy` or `npx wrangler deploy`. Wrangler's configured build
   hook runs `npm run build` automatically to create `dist` before validating
   and uploading the five public assets and actual Worker entrypoint.
   For Cloudflare Git builds, leave the separate build command empty and use
   `npx wrangler deploy` as deploy command; the hook handles the asset build.
   Deploying only `dist` is insufficient.
8. Open your Access-protected URL and sign in. Prepare a booking only when ready
   to contact Scoot. Verify the actual browser preview before scheduling.

No cloud deployment or live airline submission is part of the test suite.
The initial configuration intentionally omits Access identifiers;
you must supply your own runtime variables. Browser sign-in is required only for deployment;
visitors authenticate separately through Access.

### Hosted behavior and limits

- The same uppercase PNR, exact station, and timezone verification applies.
  Some public airport names are ambiguous (Kochi is both India and Japan);
  choose the full country/code suggestion. A bare ambiguous name does not
  select a timezone. A duplicate live Scoot option is rejected rather than
  guessed.
- Scheduling requires a time more than five seconds and at most 24 hours ahead.
  Airport-local DST gaps and repeated times are rejected. For schedules over
  five minutes away, the prepared browser is closed and reopened 30 seconds
  before retrieval. This saves idle browser charges but loses the original
  session; entries are reverified, and maintenance/form failures stop the job.
- Durable Object alarms are at-least-once, not exact-second guarantees.
  A persisted attempt marker precedes the external click. Replayed alarms
  never click again. An interruption can leave **uncertain** status and possibly
  no click; review manually instead of assuming success or automatically retrying.
  Verification or platform delays over two seconds stop retrieval.
- The queue/unsuccessful rules are unchanged: the automated workflow stops after
  one retrieval. Nationality, passport and remaining check-in are never
  autopopulated. Follow Scoot's displayed airport instructions; retrieval does
  not guarantee a seat or permission to board.
- Cloud preview updates every five seconds while visible and pauses 15 seconds
  before retrieval. No live browser means no screenshot; scheduled warm-up
  restores the preview. Request latency may delay an alarm, in which case the
  lateness guard prevents a late click.
- After retrieval, explicit manual controls act on the **same remote session**:
  enable preview clicks, focus a field, type text, use keys, and scroll.
  The app uses the newest browser tab if Scoot opens a popup. A cloud session
  cannot be opened as a normal local tab; copying its URL would lose that session.
- Sessions expire ten minutes after preparation or retrieval as applicable;
  a scheduled job sets its cleanup deadline relative to retrieval. Manual
  activity does not extend this deadline. Use **Close cloud session** to end it
  sooner, or **Cancel** before retrieval. Provider failures may end it earlier.
  Cleanup alarms can be delayed by the platform.
- Cloud jobs continue if the frontend closes. Closing a local terminal has no
  effect on cloud jobs. Use one device/tab per booking to avoid conflicting
  manual controls.

Browser Run is metered; even a prepared preview consumes browser session time.
Check [current limits](https://developers.cloudflare.com/browser-run/limits/)
and pricing before deployment. The free quota may be insufficient for preparing,
waiting, and continuing one booking. Platform updates may close browser sessions.

### Hosted privacy

Booking data is encrypted with AES-GCM and a unique nonce before Durable Object
storage. Ciphertext is removed on cancellation, cleanup, or before a retrieval
attempt; the encryption key is a Wrangler secret. Browser session identifiers,
status, schedule and attempt metadata are stored, but result URLs, screenshots
and manual text are not. Manual text is sent only when explicitly requested and
cleared from its input immediately. Airline/provider browser storage is outside
the app's control.

Access JWT signature, issuer, audience, expiry and identity are validated.
Users have separate Durable Objects. Actions require a per-user token and
same-origin POST; previews require authentication and that token. API responses
are uncached, and Worker observability is disabled. Preview images can contain
personal information; keep your screen private. Never make this API public by
removing authentication to work around setup errors.

`npm audit` currently reports high-severity transitive issues in Puppeteer's
browser-download helpers (`extract-zip` and `basic-ftp`). This app connects to
Cloudflare-managed browsers and does not download/extract browser archives
through those helpers. No compatible upstream fix was offered for the whole
chain during validation; do not force an obsolete Puppeteer downgrade or use
those helpers for untrusted archives. Recheck upstream advisories before
deployment.

## Current live integration limitation

### Static Cloudflare hosting

Airport suggestions and timezone options load independently from `stations.json`
and `timezones.json`, with airport timezone mappings bundled in the station data.
Include both JSON files alongside `index.html`, `app.js`, and `style.css` in the
published static directory. No Python API is needed for these dropdowns.

A static Cloudflare deployment does **not** run the Python/Playwright scheduler.
The hosted page explicitly displays "Hosted automation unavailable" and disables
automation controls when the Worker API is absent. Deploy the backend described
above for cloud automation, or use the local Python app.

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

## Local app privacy

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

For hosted rules, encryption, authenticated identity isolation, request guards,
alarm replay/interruption, late clicks and session cleanup:

```sh
npm run typecheck
npm test
npm run build
npx wrangler deploy --dry-run
```

Scheduler tests mock the external cloud-browser boundary; the Python suite also
tests the hosted frontend with synthetic APIs and manual controls. These tests
do not certify the live Scoot DOM or real Cloudflare scheduling latency.
