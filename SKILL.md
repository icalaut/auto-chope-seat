---
name: auto-chope-seat
description: Open and fill the Scoot staff web check-in form in the default browser, then stand by for timed booking retrieval.
---

# Auto Chope Seat

1. Ask one question per turn and wait for the answer before asking the next. Use these prompts in order: “What is your booking reference (PNR)?” Then “What is your departure city or airport?” Then “What is your scheduled check-in date and time in the departure airport’s local time? Use YYYY-MM-DD HH:MM:SS.sss.” Skip any prompt whose answer the user has already provided for this run. Accept a city or airport name without requiring an IATA code. Normalize the supplied PNR to uppercase before validation and form entry, preserving digits (for example, `ab12cd` becomes `AB12CD`). Accept lowercase input without asking the passenger to re-enter it. Keep the PNR out of logs and countdown displays.
2. Open https://chopemyseat.flyscoot.com/home in the user's default browser. Wait for the full page `load` event, then for the Departing control to be visible and enabled. Open the departure dropdown and wait for its station options to finish loading and the requested station to be uniquely available before filling either booking field. A visible input or DOMContentLoaded alone is insufficient. Do not use a fixed sleep as proof of readiness. Search its Departing dropdown using the supplied city or airport only after the list is ready. Use the station name and IATA code shown by Scoot. Select a unique match, ask the user to choose when multiple airports match, and ask for a corrected departure airport when none match. Resolve the selected airport's IANA time zone and confirm ambiguous local times or reject nonexistent local times. Verify the destination before entering the PNR. Populate Booking Reference (PNR), verify both fields, and leave the form ready for timed retrieval. Keep the intended check-in date and time in the countdown because the form has no fields for them. If browser control or form filling is unavailable, report which step needs manual completion.
3. Interpret the intended check-in date and time in the selected airport's IANA time zone. Convert it to an absolute instant and subtract exactly one second to get the Retrieve Booking dispatch target. Show both times in the airport's local time with UTC offset and milliseconds. Count down to the dispatch target and ask for a new scheduled time if that target has passed. The passenger is responsible for choosing a time within the airline's check-in window.
4. Once the PNR, departure port, and scheduled local check-in time are supplied, start one live job that stays on standby around the clock, including overnight, until its dispatch target. Standby depends on the Mac staying powered on, the runner remaining active, and the browser session remaining usable. Start a local background runner that can keep the prepared browser session available and trigger the click independently after the chat turn ends. Report the dispatch target, whether the runner is armed, and how to cancel it. Keep routine countdown updates quiet and report submission, failure, cancellation, or a missed deadline. Stop before submitting only when the user requests a dry run or explicitly asks to stop. Use a running local scheduler that calculates remaining time from the absolute target and uses monotonic timing for the final wait. Verify that it can trigger the browser action before reporting that the run is armed. If the scheduler is unavailable, the host sleeps through the target, or the deadline is missed, report the outcome and skip submission. End timed automation when Scoot returns the booking retrieval API response, while retaining the same browser window and session for the passenger. If the response times out, report it as unconfirmed and stop timed automation without retrying; retain the browser for review. Cancellation also ends the job. A chat wakeup cannot provide millisecond timing.
5. At scheduled check-in time minus exactly one second, click Retrieve Booking once in the prepared browser. This click triggers Scoot's request to https://apigw.singaporeair.com/chopemyseat/booking/retrieval with the booking reference and departure station. Use this single browser action for submission to avoid a duplicate API request. Record the actual local click time and, when observable, the API request start time and response status. Browser handling and network latency can shift API dispatch and server receipt relative to the click. Keep the PNR and credentials out of logs.
6. Report the booking retrieval outcome and leave the browser UI open on the response page. Keep the runner alive only to preserve the same browser session until the passenger closes its windows or explicitly asks to stop it. The passenger can review the response and manually continue with passenger details, baggage tagging, seat selection, and boarding pass issuance where Scoot offers those steps. Do not refresh, navigate away, close the browser, or automate these subsequent steps as part of timed retrieval. HTTP success alone does not establish completed check-in or boarding pass issuance.

## Desktop application implementation

The persistent desktop implementation is [app.py](app.py). Launch it with
`.venv/bin/python app.py` or open the packaged auto-chope-seat application.
The form replaces the conversational input prompts:

| Prompt input | Application field |
| --- | --- |
| Booking reference | Booking reference (PNR) |
| Departure city or airport | Departing airport |
| Resolved IANA timezone | Airport timezone |
| Scheduled local check-in date and time | Check-in date and Check-in time |

**Confirm schedule** starts the local worker immediately. The worker retains
its dedicated browser and implements steps 2–6 above independently of the chat.
The countdown shows check-in and dispatch times. **Armed** means the loaded form
has been verified. **Cancel schedule** cancels before dispatch. After retrieval,
leave the app running to preserve the browser for manual continuation.
Closing the app ends its worker and browser session.

The dispatcher checks a 0–50 ms local click window and aborts a missed target;
this is not a guarantee of network dispatch or Scoot receipt time. The
application does not interpret this Markdown at runtime: its code implements
this workflow, with regression tests in `tests/test_skill_workflow.py`.
