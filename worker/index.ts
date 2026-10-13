import { DurableObject } from "cloudflare:workers";
import { createRemoteJWKSet, jwtVerify } from "jose";
import { type Browser, type Page } from "@cloudflare/puppeteer";
import { browserPage, checkForm, observe, openBrowser, prepareForm } from "./browser";
import { type Sealed, mayDispatch, scheduleTime, seal, unseal, validateBooking } from "./rules";

export interface Env {
  ASSETS: Fetcher;
  BROWSER: Fetcher;
  BOOKINGS: DurableObjectNamespace<BookingScheduler>;
  ACCESS_TEAM_DOMAIN: string;
  ACCESS_AUD: string;
  BOOKING_KEY: string;
}

interface Job {
  status: string;
  message: string;
  token: string;
  target?: number;
  expires: number;
  booking?: Sealed;
  session?: string;
  attempt?: string;
}

const json = (data: unknown, status = 200) => Response.json(data, { status, headers: { "Cache-Control": "no-store" } });
const keys = new Map<string, ReturnType<typeof createRemoteJWKSet>>();

export async function authenticatedUser(request: Request, env: Env): Promise<string> {
  const assertion = request.headers.get("Cf-Access-Jwt-Assertion");
  if (!assertion) throw new Error("Sign in through Cloudflare Access to use hosted automation.");
  const issuer = `https://${env.ACCESS_TEAM_DOMAIN}`;
  let jwks = keys.get(issuer);
  if (!jwks) {
    jwks = createRemoteJWKSet(new URL(`${issuer}/cdn-cgi/access/certs`));
    keys.set(issuer, jwks);
  }
  const { payload } = await jwtVerify(assertion, jwks, {
    issuer, audience: env.ACCESS_AUD, algorithms: ["RS256"], requiredClaims: ["exp", "iat", "sub", "email"]
  });
  if (!payload.sub || typeof payload.email !== "string" || !payload.email) throw new Error("An authenticated user identity is required.");
  return payload.sub;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (!url.pathname.startsWith("/api/")) return env.ASSETS.fetch(request);
    const setupErrors: string[] = [];
    if (!env.ACCESS_TEAM_DOMAIN) setupErrors.push("ACCESS_TEAM_DOMAIN is missing");
    else if (!/^[a-z0-9-]+\.cloudflareaccess\.com$/.test(env.ACCESS_TEAM_DOMAIN)) {
      setupErrors.push("ACCESS_TEAM_DOMAIN must be your team hostname without https:// or a trailing slash");
    }
    if (!env.ACCESS_AUD?.trim()) setupErrors.push("ACCESS_AUD is missing");
    if (!env.BOOKING_KEY) setupErrors.push("BOOKING_KEY secret is missing");
    else if (!/^[A-Za-z0-9+/]{43}=$/.test(env.BOOKING_KEY)) {
      setupErrors.push("BOOKING_KEY must be one base64-encoded 32-byte key (44 characters, no spaces or newlines)");
    }
    if (setupErrors.length) {
      return json({ mode: "cloud", error: `Hosted automation setup incomplete: ${setupErrors.join("; ")}. Set runtime Variables and Secrets, not build variables, then Deploy.` }, 503);
    }
    let user: string;
    try {
      user = await authenticatedUser(request, env);
    } catch {
      return json({ mode: "cloud", error: "Hosted automation requires configured Cloudflare Access authentication and the booking encryption secret. Sign in through the protected application." }, 401);
    }
    if (request.method === "POST" && request.headers.get("Origin") !== url.origin) {
      return json({ error: "Cross-origin action denied." }, 403);
    }
    const id = env.BOOKINGS.idFromName(user);
    const response = await env.BOOKINGS.get(id).fetch(request);
    const secured = new Response(response.body, response);
    secured.headers.set("Cache-Control", "no-store");
    secured.headers.set("X-Content-Type-Options", "nosniff");
    return secured;
  }
} satisfies ExportedHandler<Env>;

export class BookingScheduler extends DurableObject<Env> {
  private job!: Job;
  private readonly ready: Promise<void>;
  private serial = Promise.resolve();

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    this.ready = ctx.blockConcurrencyWhile(async () => {
      this.job = await ctx.storage.get<Job>("job") ?? {
        status: "idle", message: "Prepare the Scoot browser before scheduling.",
        token: crypto.randomUUID(), expires: Date.now() + 600000
      };
      if (this.job.status === "submitting" && this.job.attempt) {
        this.job.status = "uncertain";
        this.job.message = "Interrupted after recording the retrieval attempt. Inspect the remote browser manually; no automatic retry.";
      }
      await ctx.storage.put("job", this.job);
    });
  }

  private async save() { await this.ctx.storage.put("job", this.job); }
  private snapshot() {
    return {
      status: this.job.status, message: this.job.message,
      target: this.job.target ? this.job.target / 1000 : null,
      now: Date.now() / 1000, mode: "cloud", manual_available: Boolean(this.job.session &&
        Date.now() < this.job.expires && ["submitted", "airport_queue", "unsuccessful", "uncertain"].includes(this.job.status)),
      preview_available: Boolean(this.job.session && Date.now() < this.job.expires),
      session_expires: this.job.expires / 1000
    };
  }
  private async connectBrowser(): Promise<Browser> {
    if (!this.job.session) throw new Error("No live cloud browser. Prepare again, or wait for the scheduled warm-up.");
    return openBrowser(this.env.BROWSER, this.job.session);
  }
  private async closeSession() {
    if (!this.job.session) return;
    const session = this.job.session;
    this.job.session = undefined;
    await this.save();
    // Forget even a dead session so the next Prepare can recover. Report close failures explicitly.
    const browser = await openBrowser(this.env.BROWSER, session);
    try { await browser.close(); }
    finally { await browser.disconnect(); }
  }
  private async fail(message: string) {
    this.job.status = this.job.attempt ? "uncertain" : "error";
    this.job.message = message;
    this.job.booking = undefined;
    this.job.target = undefined;
    this.job.expires = Date.now() + 600000;
    await this.save();
    await this.ctx.storage.setAlarm(this.job.expires);
  }
  private async prepareBrowser() {
    if (!this.job.booking) throw new Error("Encrypted booking data is unavailable.");
    const booking = await unseal(this.job.booking, this.env.BOOKING_KEY);
    const browser = await openBrowser(this.env.BROWSER);
    this.job.session = browser.sessionId();
    await this.save();
    try {
      const page = await browserPage(browser);
      await prepareForm(page, booking);
    } finally { await browser.disconnect(); }
  }
  private async withPage<T>(callback: (page: Page, browser: Browser) => Promise<T>, timeoutMs?: number): Promise<T> {
    const browser = await this.connectBrowser();
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      const operation = browserPage(browser).then(page => callback(page, browser));
      if (!timeoutMs) return await operation;
      return await Promise.race([operation, new Promise<never>((_resolve, reject) => {
        timer = setTimeout(() => reject(new Error("Cloud preview timed out; the browser connection was released.")), timeoutMs);
      })]);
    } finally {
      clearTimeout(timer);
      // Disconnect also aborts outstanding protocol calls before releasing serialization.
      await browser.disconnect();
    }
  }

  async fetch(request: Request): Promise<Response> {
    await this.ready;
    const path = new URL(request.url).pathname;
    if (request.method === "GET" && path === "/api/config") {
      return json({ token: this.job.token, mode: "cloud" });
    }
    if (request.method === "GET" && path === "/api/status") return json(this.snapshot());
    if (request.headers.get("X-App-Token") !== this.job.token) return json({ error: "Invalid action token." }, 403);
    // Preview and browser operations are serialized so they cannot overlap the scheduled click.
    const previous = this.serial;
    let release!: () => void;
    this.serial = new Promise(resolve => { release = resolve; });
    await previous;
    try {
      if (request.method === "GET" && path === "/api/preview") {
        if (Date.now() >= this.job.expires) return json({ error: "Cloud browser session expired. Prepare again." }, 410);
        if (this.job.target && this.job.target - Date.now() < 15000) return json({ error: "Preview paused near the scheduled click." }, 409);
        const image = await this.withPage(page => page.screenshot({ type: "jpeg", quality: 65 }), 5000);
        return new Response(new Uint8Array(image), { headers: { "Content-Type": "image/jpeg", "Cache-Control": "no-store" } });
      }
      if (request.method !== "POST" || path !== "/api/action") return json({ error: "Not found." }, 404);
      const reader = request.body?.getReader();
      if (!reader) throw new Error("Missing action body.");
      const chunks: Uint8Array[] = [];
      let size = 0;
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        size += value.byteLength;
        if (size > 8192) {
          await reader.cancel();
          return json({ error: "Request too large." }, 413);
        }
        chunks.push(value);
      }
      const bytes = new Uint8Array(size);
      let offset = 0;
      for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
      const text = new TextDecoder().decode(bytes);
      const data: Record<string, unknown> = JSON.parse(text);
      if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error("Expected an action object.");
      if (data.action === "prepare") {
        if (["scheduled", "submitting"].includes(this.job.status)) throw new Error("Cancel the current schedule before preparing again.");
        const booking = validateBooking(data);
        await this.closeSession();
        this.job = { status: "working", message: "Opening and verifying Scoot in the cloud browser.",
          token: this.job.token, booking: await seal(booking, this.env.BOOKING_KEY), expires: Date.now() + 600000 };
        await this.save();
        await this.ctx.storage.setAlarm(this.job.expires);
        await this.prepareBrowser();
        this.job.status = "prepared";
        this.job.message = "Entries verified in the cloud browser. Review the preview, then schedule one retrieval.";
        await this.save();
      } else if (data.action === "schedule") {
        if (this.job.status !== "prepared" || !this.job.booking) throw new Error("Prepare and verify the browser first.");
        const booking = await unseal(this.job.booking, this.env.BOOKING_KEY);
        if (data.timezone !== booking.timezone || String(data.pnr).trim().toUpperCase() !== booking.pnr || data.departure !== booking.departure) {
          throw new Error("Booking inputs changed. Prepare again.");
        }
        const target = scheduleTime(data);
        await this.withPage(page => checkForm(page, booking));
        if (target - Date.now() <= 5000) throw new Error("Browser verification took too long. Choose a later time.");
        if (target - Date.now() > 300000) await this.closeSession();
        this.job.target = target;
        this.job.expires = target + 600000;
        this.job.status = "scheduled";
        this.job.message = this.job.session ? "Cloud retrieval scheduled. Preview pauses near submission."
          : "Cloud retrieval scheduled. The browser will reopen and verify entries 30 seconds before retrieval; no browser is held open while waiting.";
        await this.save();
        await this.ctx.storage.setAlarm(this.job.session ? target : Math.max(Date.now() + 1, target - 30000));
      } else if (data.action === "cancel") {
        if (this.job.attempt) throw new Error("Submission may have occurred. It cannot be undone or retried automatically.");
        this.job.booking = undefined;
        this.job.target = undefined;
        this.job.status = "idle";
        this.job.message = "Cancelled. No scheduled retrieval remains.";
        await this.ctx.storage.deleteAlarm();
        await this.save();
        await this.closeSession();
      } else if (data.action === "close") {
        if (["working", "scheduled", "submitting"].includes(this.job.status)) throw new Error("Cancel the schedule before closing.");
        this.job.booking = undefined;
        this.job.status = "expired";
        this.job.message = "Cloud session closed. Prepare again to start a new booking.";
        await this.save();
        await this.ctx.storage.deleteAlarm();
        await this.closeSession();
      } else if (data.action === "manual") {
        await this.manual(data);
      } else if (data.action === "show_browser") {
        return json({ error: "Use the remote-browser controls below the preview. A cloud session cannot open as a normal local browser tab." }, 400);
      } else throw new Error("Unknown action.");
      return json(this.snapshot());
    } catch (error) {
      const message = error instanceof Error ? error.message : "Cloud browser action failed.";
      if (this.job.status === "working") await this.fail(`Preparation failed: ${message}. No retrieval was attempted.`);
      return json({ error: message }, 400);
    } finally { release(); }
  }

  async alarm() {
    await this.ready;
    const previous = this.serial;
    let release!: () => void;
    this.serial = new Promise(resolve => { release = resolve; });
    await previous;
    try {
      if (this.job.attempt || this.job.status !== "scheduled") {
        if (Date.now() >= this.job.expires) {
          this.job.booking = undefined;
          this.job.target = undefined;
          this.job.status = "expired";
          this.job.message += " Cloud session expired and stored booking data was removed.";
          await this.save();
          await this.closeSession();
        } else await this.ctx.storage.setAlarm(this.job.expires);
        return;
      }
      const target = this.job.target!;
      if (Date.now() < target) {
        if (!this.job.session) await this.prepareBrowser();
        await this.ctx.storage.setAlarm(target);
        return;
      }
      if (!mayDispatch(this.job.status, target, Date.now())) {
        await this.fail("Scheduled time missed by more than two seconds. No late retrieval or automatic retry.");
        return;
      }
      const booking = await unseal(this.job.booking!, this.env.BOOKING_KEY);
      await this.withPage(async (page, browser) => {
        await checkForm(page, booking);
        if (!mayDispatch(this.job.status, target, Date.now())) throw new Error("Verification missed the scheduled time. No click.");
        // Persist the attempt BEFORE the external side effect: alarm replay must never click again.
        this.job.status = "submitting";
        this.job.attempt = crypto.randomUUID();
        this.job.booking = undefined;
        this.job.target = undefined;
        this.job.expires = Date.now() + 600000;
        this.job.message = "Retrieval attempt recorded. Dispatching once; an interruption will require manual review, never an automatic retry.";
        await this.save();
        await this.ctx.storage.setAlarm(this.job.expires);
        if (Date.now() - target > 2000) throw new Error("Time window elapsed while persisting the attempt. No click.");
        await page.click("[data-chope-field=retrieve]");
        const outcome = await observe(browser, page);
        this.job.status = outcome.status;
        this.job.message = outcome.message;
        await this.save();
      });
    } catch (error) {
      const reason = error instanceof Error ? error.message : "browser failure";
      if (this.job.status === "expired") {
        this.job.message += ` Browser close failed: ${reason}. The provider's idle expiry still applies.`;
        await this.save();
      } else {
        await this.fail(`Cloud retrieval stopped: ${reason}. ${this.job.attempt
          ? "Submission may be uncertain; inspect the result manually." : "No retrieval was attempted."} No automatic retry.`);
      }
    } finally { release(); }
  }

  private async manual(data: Record<string, unknown>) {
    if (!this.snapshot().manual_available || Date.now() >= this.job.expires) throw new Error("Manual controls are available only after retrieval, while the session is active.");
    const kind = data.kind;
    await this.withPage(async page => {
      if (kind === "click") {
        const x = Number(data.x), y = Number(data.y);
        if (!Number.isFinite(x) || !Number.isFinite(y) || x < 0 || y < 0 || x >= 1280 || y >= 900) throw new Error("Invalid preview click coordinates.");
        await page.mouse.click(x, y);
      } else if (kind === "type") {
        if (typeof data.text !== "string" || !data.text || data.text.length > 500) throw new Error("Enter at most 500 characters.");
        await page.keyboard.type(data.text);
      } else if (kind === "key") {
        const key = data.key;
        if (key !== "Tab" && key !== "Enter" && key !== "ArrowDown" && key !== "ArrowUp" && key !== "Escape" && key !== "Backspace") throw new Error("Unsupported key.");
        await page.keyboard.press(key);
      } else if (kind === "scroll") {
        if (![-600, 600].includes(Number(data.delta))) throw new Error("Unsupported scroll.");
        await page.mouse.wheel({ deltaY: Number(data.delta) });
      } else throw new Error("Unsupported manual action.");
    });
  }
}
