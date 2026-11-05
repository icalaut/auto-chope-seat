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

Tagged releases build disk images for both Apple Silicon (`arm64`) and Intel
(`x86_64`) Macs. When Apple signing secrets are configured, builds are signed
and notarized. Otherwise assets include `-unsigned` in their names and are
not notarized; macOS may block them from opening. Download the matching
`auto-chope-seat-v1.1.0-arm64-unsigned.dmg` or
`auto-chope-seat-v1.1.0-x86_64-unsigned.dmg` from the project's GitHub Releases page,
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
builds both Mac versions, notarizes them when all signing secrets are available,
then publishes the GitHub release.
The Intel build runs on a GitHub-hosted Intel Mac runner; the Apple Silicon
build runs on an Apple Silicon runner.

## Use

The Mac application follows the local [SKILL.md](SKILL.md) workflow. Fill these fields:

| Application field | Skill input |
| --- | --- |
| Booking reference (PNR) | Booking reference; normalized to uppercase |
| Departing airport | Departure city or airport; choose a unique station |
| Airport timezone | IANA timezone resolved from the departure station |
| Check-in date | Date portion of the scheduled airport-local check-in time |
| Check-in time | Time portion, including three millisecond digits |

Select **Confirm schedule** to start preparation immediately. The app waits for
full page load, selects the requested departure station before entering the PNR,
and verifies the form before reporting **Armed**. It retains this browser session
while waiting, including overnight. The Mac must remain powered on and awake,
with the lid open and the application running.

The dispatch target is **exactly one second before the entered check-in time**.
Both times appear with milliseconds and the departure timezone's UTC offset.
For example, check-in `08:00:00.125` targets a single Retrieve Booking click at
`07:59:59.125`. It no longer constructs and holds a POST for timed API release.
Browser and network delays can shift the actual request time. A missed local
click window stops the attempt without a late fallback or automatic retry.

Use **Cancel schedule** before dispatch to cancel a pending job. A dispatched
click cannot be recalled. After the exact booking API response, or an unconfirmed
response timeout, timed automation ends and the same browser remains open.
Review Scoot's result and continue passenger details, baggage tagging, seats and
boarding passes manually. The timed workflow does not capture passenger forms,
refresh, navigate away or automate subsequent check-in steps. HTTP success does
not establish completed check-in or a seat.

The Python self-hosted server shares this scheduler. The separate Cloudflare
Worker scheduler retains its own behavior described above.

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
