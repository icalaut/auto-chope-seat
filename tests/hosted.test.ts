import assert from "node:assert/strict";
import { test } from "node:test";
import stations from "../stations.json";
import { assertSiteAvailable, mayDispatch, resultStatus, scheduleTime, seal, unseal, validateBooking } from "../worker/rules";

const booking = { pnr: "ABC123", departure: "Singapore", timezone: "Asia/Singapore" };
const secret = btoa("k".repeat(32));

test("airline access denial and maintenance stop explicitly, never treated as a booking result", () => {
  const denial = 'Access Denied\nYou don\'t have permission to access "http://chopemyseat.flyscoot.com/" on this server.';
  for (const text of [denial, "Access Denied", "You don\u2019t have permission to access this site"]) {
    assert.throws(() => assertSiteAvailable(text), /Scoot denied access/);
    assert.throws(() => resultStatus(text), /Scoot denied access/);
  }
  assert.throws(() => assertSiteAvailable("", 403), /Scoot denied access/);
  assert.throws(() => assertSiteAvailable("ChopeMySeat is temporarily unavailable"), /maintenance/);
  assert.throws(() => assertSiteAvailable("Currently under maintenance"), /maintenance/);
  assert.doesNotThrow(() => assertSiteAvailable("Booking Reference\nDeparting\nRetrieve Booking", 200));
  assert.doesNotThrow(() => assertSiteAvailable("If you see Access Denied, contact support."));
});
test("booking normalization and all bundled airport timezone mappings", () => {
  assert.deepEqual(validateBooking({ ...booking, pnr: " abc123 " }), booking);
  for (const station of stations.stations) {
    assert.equal(validateBooking({ ...booking, departure: station.name, timezone: station.timezone }).timezone, station.timezone);
  }
  for (const fields of [{ pnr: "bad" }, { departure: "" }, { timezone: "Invalid/Zone" }, { timezone: "Asia/Taipei" }]) {
    assert.throws(() => validateBooking({ ...booking, ...fields }));
  }
  assert.equal(validateBooking({ ...booking, departure: "Custom station", timezone: "UTC" }).timezone, "UTC");
});

test("airport-local schedule with strict format, time window and DST rejection", () => {
  const target = Date.parse("2027-01-02T14:03:04Z");
  const data = { date: "2027-01-02", time: "22:03:04", timezone: booking.timezone };
  assert.equal(scheduleTime(data, target - 6000), target);
  assert.throws(() => scheduleTime(data, target - 5000), /five seconds/);
  assert.throws(() => scheduleTime(data, target), /five seconds/);
  assert.equal(scheduleTime(data, target - 86400000), target);
  assert.throws(() => scheduleTime(data, target - 86400001), /24 hours/);
  for (const fields of [{ date: "2027-02-30" }, { time: "24:00:00" }, { time: "2:00:00" }]) {
    assert.throws(() => scheduleTime({ ...data, ...fields }, target - 6000));
  }
  for (const [date, time] of [["2027-03-14", "02:30:00"], ["2027-11-07", "01:30:00"]]) {
    assert.throws(() => scheduleTime({ date, time, timezone: "America/New_York" }, 0), /ambiguous/);
  }
});

test("encrypted booking roundtrip, random nonce and tamper/wrong-secret rejection", async () => {
  const ciphertext = await seal(booking, secret);
  assert.deepEqual(await unseal(ciphertext, secret), booking);
  assert.notDeepEqual((await seal(booking, secret)).iv, ciphertext.iv);
  const tampered = structuredClone(ciphertext);
  tampered.value[0] ^= 1;
  await assert.rejects(unseal(tampered, secret));
  await assert.rejects(unseal(ciphertext, btoa("x".repeat(32))));
  await assert.rejects(seal(booking, btoa("short")));
  assert.ok(!JSON.stringify(ciphertext).includes(booking.pnr));
});

test("only scheduled on-time jobs may dispatch, never replay a recorded attempt", () => {
  assert.equal(mayDispatch("scheduled", 10000, 9999), false);
  assert.equal(mayDispatch("scheduled", 10000, 10000), true);
  assert.equal(mayDispatch("scheduled", 10000, 12000), true);
  assert.equal(mayDispatch("scheduled", 10000, 12001), false);
  for (const status of ["idle", "prepared", "submitting", "submitted", "airport_queue", "uncertain", "error"]) {
    assert.equal(mayDispatch(status, 10000, 10000), false);
  }
});

test("queues and unsuccessful results stop; help text and unknown pages never imply success", () => {
  assert.equal(resultStatus("Unsuccessful staff web check-in\nQueue: TRY-1").status, "airport_queue");
  assert.equal(resultStatus("Web check-in is unsuccessful").status, "unsuccessful");
  assert.match(resultStatus("If unsuccessful staff web check-in, attend the airport.").message, /No check-in or seat success is inferred/);
  assert.match(resultStatus("Unknown result").message, /No check-in or seat success is inferred/);
  assert.match(resultStatus("[Successful] Staff web check-in").message, /65 minutes/);
});
