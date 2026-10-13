import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";
import { generateKeyPair, exportJWK, SignJWT } from "jose";

// Replace only Cloudflare's runtime base class and external browser boundary.
registerHooks({
  resolve(specifier, context, next) {
    let source;
    if (specifier === "cloudflare:workers") {
      source = "export class DurableObject { constructor(ctx, env) { this.ctx = ctx; this.env = env; } }";
    } else if (specifier === "./browser" && context.parentURL?.endsWith("/worker/index.ts")) {
      source = ["openBrowser", "browserPage", "checkForm", "prepareForm", "observe"]
        .map(name => `export const ${name} = (...args) => globalThis.cloudMocks.${name}(...args);`).join("\n");
    }
    return source ? { url: `data:text/javascript,${encodeURIComponent(source)}`, shortCircuit: true } : next(specifier, context);
  }
});
const { BookingScheduler, default: worker } = await import("../worker/index.ts");
const { seal } = await import("../worker/rules.ts");
const secret = btoa("k".repeat(32));
const booking = { pnr: "ABC123", departure: "Singapore", timezone: "Asia/Singapore" };

function fixture(job) {
  let saved = structuredClone(job);
  let clicks = 0;
  let closed = 0;
  let disconnected = 0;
  let alarm;
  const storage = {
    get: async () => structuredClone(saved),
    put: async (_key, value) => { saved = structuredClone(value); },
    setAlarm: async value => { alarm = value; },
    deleteAlarm: async () => { alarm = undefined; }
  };
  const page = {
    click: async () => {
      assert.equal(saved.status, "submitting");
      assert.ok(saved.attempt);
      assert.equal(saved.booking, undefined);
      clicks++;
    },
    url: () => "https://chopemyseat.flyscoot.com/result",
    screenshot: async () => Buffer.from([0xff, 0xd8]),
    mouse: { click: async () => { clicks++; } },
    keyboard: { type: async () => {}, press: async () => {} }
  };
  const browser = { close: async () => { closed++; }, disconnect: async () => { disconnected++; }, sessionId: () => "browser-session" };
  globalThis.cloudMocks = {
    openBrowser: async () => browser,
    browserPage: async () => page,
    checkForm: async () => {},
    prepareForm: async () => {},
    observe: async () => ({ status: "airport_queue", message: "Queue: TRY-1", page })
  };
  const ctx = { storage, blockConcurrencyWhile: callback => callback() };
  const env = { BOOKING_KEY: secret, BROWSER: {} };
  return { scheduler: new BookingScheduler(ctx, env), ctx, env, saved: () => saved,
    clicks: () => clicks, closed: () => closed, disconnected: () => disconnected, alarm: () => alarm };
}
const initial = () => ({ status: "scheduled", message: "", token: "action-token", target: Date.now(), expires: Date.now() + 600000, session: "browser-session" });
const action = data => new Request("https://app.example/api/action", {
  method: "POST", headers: { "X-App-Token": "action-token" }, body: JSON.stringify(data)
});

test("persist attempt before click; duplicate alarms and object restarts never resubmit", async () => {
  const f = fixture({ ...initial(), booking: await seal(booking, secret) });
  await f.scheduler.alarm();
  assert.equal(f.clicks(), 1);
  assert.equal(f.saved().status, "airport_queue");
  await f.scheduler.alarm();
  await new BookingScheduler(f.ctx, f.env).alarm();
  assert.equal(f.clicks(), 1);
});

test("interruption after attempt persistence exposes manual review, not retry", async () => {
  const f = fixture({ ...initial(), status: "submitting", attempt: "persisted-attempt", booking: undefined });
  await f.scheduler.alarm();
  assert.equal(f.clicks(), 0);
  assert.equal(f.saved().status, "uncertain");
  const response = await f.scheduler.fetch(new Request("https://app.example/api/status"));
  assert.equal((await response.json()).manual_available, true);
  assert.equal((await f.scheduler.fetch(action({ action: "cancel" }))).status, 400);
});

test("late alarm refuses retrieval; early alarm only prepares and rearms", async () => {
  const late = fixture({ ...initial(), target: Date.now() - 3000, booking: await seal(booking, secret) });
  await late.scheduler.alarm();
  assert.equal(late.clicks(), 0);
  assert.equal(late.saved().status, "error");
  const early = fixture({ ...initial(), session: undefined, target: Date.now() + 20000, booking: await seal(booking, secret) });
  await early.scheduler.alarm();
  assert.equal(early.clicks(), 0);
  assert.equal(early.saved().status, "scheduled");
  assert.equal(early.alarm(), early.saved().target);
});

test("cancel removes schedule even when a dead cloud session cannot be closed", async () => {
  const f = fixture({ ...initial(), booking: await seal(booking, secret) });
  globalThis.cloudMocks.openBrowser = async () => { throw new Error("Browser session unavailable."); };
  const response = await f.scheduler.fetch(action({ action: "cancel" }));
  assert.equal(response.status, 400);
  assert.equal(f.saved().status, "idle");
  assert.equal(f.saved().target, undefined);
  assert.equal(f.saved().session, undefined);
  assert.equal(f.saved().booking, undefined);
  assert.equal(f.alarm(), undefined);
});

test("expiry removes secrets and browser; manual and oversize actions are rejected", async () => {
  const f = fixture({ ...initial(), status: "prepared", expires: Date.now() - 1, booking: await seal(booking, secret) });
  await f.scheduler.alarm();
  assert.equal(f.saved().booking, undefined);
  assert.equal(f.saved().session, undefined);
  assert.equal(f.closed(), 1);
  assert.equal((await f.scheduler.fetch(action({ action: "manual", kind: "click", x: 10, y: 10 }))).status, 400);
  assert.equal((await f.scheduler.fetch(action({ action: "prepare", pnr: "x".repeat(9000) }))).status, 413);
  const noToken = new Request("https://app.example/api/action", { method: "POST", body: "{}" });
  assert.equal((await f.scheduler.fetch(noToken)).status, 403);
});

test("preview pauses before retrieval and bounds connected screenshot work to five seconds", async () => {
  const near = fixture({ ...initial(), target: Date.now() + 10000 });
  const preview = new Request("https://app.example/api/preview", { headers: { "X-App-Token": "action-token" } });
  assert.equal((await near.scheduler.fetch(preview)).status, 409);
  assert.equal(near.disconnected(), 0);
  const slow = fixture({ ...initial(), status: "prepared", target: undefined });
  globalThis.cloudMocks.browserPage = async () => ({ screenshot: () => new Promise(() => {}) });
  const start = Date.now();
  const response = await slow.scheduler.fetch(preview);
  assert.equal(response.status, 400);
  assert.match((await response.json()).error, /timed out/);
  assert.ok(Date.now() - start >= 4900 && Date.now() - start < 6500);
  assert.equal(slow.disconnected(), 1);
  assert.equal(slow.clicks(), 0);
});

test("Worker fails closed without setup or without a verified Access JWT", async () => {
  const env = { ACCESS_TEAM_DOMAIN: "", ACCESS_AUD: "", BOOKING_KEY: secret };
  const request = new Request("https://app.example/api/config");
  assert.equal((await worker.fetch(request, env)).status, 503);
  env.ACCESS_TEAM_DOMAIN = "example.cloudflareaccess.com";
  env.ACCESS_AUD = "audience";
  assert.equal((await worker.fetch(request, env)).status, 401);
  const fake = new Request(request, { headers: { "Cf-Access-Jwt-Assertion": "forged.jwt.token" } });
  assert.equal((await worker.fetch(fake, env)).status, 401);
});

test("verified Access identity isolation, origin guard and wrong/expired audience rejection", async () => {
  const { publicKey, privateKey } = await generateKeyPair("RS256");
  const key = { ...await exportJWK(publicKey), kid: "fixture-key", use: "sig", alg: "RS256" };
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async url => {
    assert.equal(String(url), "https://verified-fixture.cloudflareaccess.com/cdn-cgi/access/certs");
    return Response.json({ keys: [key] });
  };
  const names = [];
  const env = {
    ACCESS_TEAM_DOMAIN: "verified-fixture.cloudflareaccess.com", ACCESS_AUD: "fixture-audience", BOOKING_KEY: secret,
    BOOKINGS: {
      idFromName: subject => { names.push(subject); return subject; },
      get: subject => ({ fetch: async () => Response.json({ subject }) })
    }
  };
  const token = async (subject, audience = env.ACCESS_AUD, expiry = "1h") => new SignJWT({ email: "synthetic@example.invalid" })
    .setProtectedHeader({ alg: "RS256", kid: key.kid }).setIssuer(`https://${env.ACCESS_TEAM_DOMAIN}`)
    .setSubject(subject).setAudience(audience).setIssuedAt().setExpirationTime(expiry).sign(privateKey);
  try {
    for (const user of ["user-a", "user-b"]) {
      const response = await worker.fetch(new Request("https://app.example/api/status", {
        headers: { "Cf-Access-Jwt-Assertion": await token(user) }
      }), env);
      assert.deepEqual(await response.json(), { subject: user });
      assert.equal(response.headers.get("Cache-Control"), "no-store");
    }
    const crossOrigin = await worker.fetch(new Request("https://app.example/api/action", {
      method: "POST", headers: { Origin: "https://attacker.example", "Cf-Access-Jwt-Assertion": await token("user-a") }, body: "{}"
    }), env);
    assert.equal(crossOrigin.status, 403);
    assert.deepEqual(names, ["user-a", "user-b"]);
    for (const assertion of [await token("user-a", "wrong-audience"), await token("user-a", env.ACCESS_AUD, Math.floor(Date.now() / 1000) - 10)]) {
      assert.equal((await worker.fetch(new Request("https://app.example/api/status", {
        headers: { "Cf-Access-Jwt-Assertion": assertion }
      }), env)).status, 401);
    }
  } finally { globalThis.fetch = originalFetch; }
});
