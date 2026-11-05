# auto-chope-seat v1.1.0

The desktop and Python self-hosted application now follow the checked-in SKILL.md workflow by default.

- Retrieve Booking is clicked once at the entered airport-local check-in time minus exactly one second.
- The departure station is selected before the PNR is entered, after full page load and station readiness.
- The browser prepares immediately and can remain on standby overnight while the app and Mac stay awake.
- The countdown shows check-in and dispatch times with milliseconds and UTC offsets.
- Missed dispatches stop without a late click or automatic retry.
- After the booking API response or an unconfirmed timeout, the same browser remains available for manual continuation.
- The timed workflow does not hold the booking POST or capture passenger forms.

The separate Cloudflare Worker scheduler is unchanged. No live airline booking was used for validation.

Mac disk images containing `-unsigned` in the filename are unsigned and not notarized because Apple signing credentials are not configured. macOS may block these builds. Signed, notarized builds are produced automatically when all repository signing secrets are configured.
