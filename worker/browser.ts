import puppeteer, { type Browser, type Page } from "@cloudflare/puppeteer";
import { type Booking, assertSiteAvailable, resultStatus } from "./rules";

export const SITE = "https://chopemyseat.flyscoot.com/";

export async function openBrowser(binding: Fetcher, session?: string): Promise<Browser> {
  return session ? puppeteer.connect(binding, session) : puppeteer.launch(binding, { keep_alive: 600000 });
}

export async function browserPage(browser: Browser): Promise<Page> {
  const pages = await browser.pages();
  const page = pages.filter(p => !p.isClosed() && p.url() !== "about:blank").at(-1) ?? pages.at(-1) ?? await browser.newPage();
  await page.setViewport({ width: 1280, height: 900 });
  page.setDefaultTimeout(3000);
  return page;
}

export async function checkForm(page: Page, booking?: Booking) {
  assertSiteAvailable(await page.$eval("body", el => el.innerText));
  return page.evaluate((input) => {
    const visible = (el: Element) => el.getClientRects().length > 0;
    const label = (el: Element) => {
      const id = el.id;
      const associated = id ? [...document.querySelectorAll("label")].find(l => l.htmlFor === id)?.textContent : "";
      return el.getAttribute("aria-label") || associated || el.closest("label")?.textContent || "";
    };
    const texts = [...document.querySelectorAll<HTMLInputElement>('input:not([type]), input[type=text]')].filter(visible);
    const namedPnr = texts.filter(el => /Booking Reference|PNR/i.test(label(el)));
    const pnr = namedPnr.length === 1 ? namedPnr[0] : texts.length === 1 ? texts[0] : null;
    const selects = [...document.querySelectorAll<HTMLElement>('select, [role=combobox]')].filter(visible);
    const namedDeparture = selects.filter(el => /Departing/i.test(label(el)));
    const departure = namedDeparture.length === 1 ? namedDeparture[0] : selects.length === 1 ? selects[0] : null;
    const buttons = [...document.querySelectorAll<HTMLButtonElement>('button, [role=button], input[type=submit]')].filter(el => visible(el) &&
      /^Retrieve Booking\s*$/i.test(el.textContent?.trim() || el.value || el.getAttribute("aria-label") || ""));
    if (!pnr || !departure || buttons.length !== 1) throw new Error("Booking form cannot be uniquely identified; no retrieval.");
    if (input) {
      const selected = departure instanceof HTMLSelectElement ? departure.selectedOptions[0]?.textContent?.trim()
        : departure instanceof HTMLInputElement ? departure.value.trim() : departure.textContent?.trim();
      if (pnr.value.trim().toUpperCase() !== input.pnr || selected !== input.departure) throw new Error("Browser entries changed or station cannot be verified. Prepare again.");
      if (buttons[0].disabled || buttons[0].getAttribute("aria-disabled") === "true") throw new Error("Retrieve Booking is disabled.");
    }
    // Dedicated selector markers do not change site behavior.
    document.querySelectorAll("[data-chope-field]").forEach(el => el.removeAttribute("data-chope-field"));
    pnr.setAttribute("data-chope-field", "pnr");
    departure.setAttribute("data-chope-field", "departure");
    buttons[0].setAttribute("data-chope-field", "retrieve");
    return departure.tagName;
  }, booking);
}

export async function prepareForm(page: Page, booking: Booking) {
  const response = await page.goto(SITE, { waitUntil: "domcontentloaded", timeout: 20000 });
  assertSiteAvailable(await page.$eval("body", el => el.innerText), response?.status());
  await page.waitForFunction(() => /Retrieve Booking|temporarily unavailable|currently under maintenance|^\s*Access Denied\s*$|You don['\u2019]t have permission to access/im.test(document.body.innerText), { timeout: 10000 });
  const tag = await checkForm(page);
  await page.$eval('[data-chope-field=pnr]', (el, value) => {
    if (!(el instanceof HTMLInputElement)) throw new Error("PNR is not an input.");
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    if (!setter) throw new Error("PNR input setter unavailable.");
    setter.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }, booking.pnr);
  if (tag === "SELECT") {
    const value = await page.$eval('[data-chope-field=departure]', (el, name) => {
      if (!(el instanceof HTMLSelectElement)) throw new Error("Departure is not a select.");
      const matches = [...el.options].filter(option => option.textContent?.trim() === name);
      if (matches.length !== 1) throw new Error("Departure label not uniquely present in Scoot.");
      return matches[0].value;
    }, booking.departure);
    await page.select('[data-chope-field=departure]', value);
  } else {
    await page.click('[data-chope-field=departure]');
    await page.evaluate(name => {
      const matches = [...document.querySelectorAll<HTMLElement>('[role=option]')].filter(el => el.getClientRects().length && el.textContent?.trim() === name);
      if (matches.length !== 1) throw new Error("Departure option cannot be uniquely identified.");
      matches[0].click();
    }, booking.departure);
  }
  await checkForm(page, booking);
}

export async function observe(browser: Browser, original: Page) {
  let page = original;
  let result = resultStatus("");
  for (let i = 0; i < 20; i++) {
    const pages = await browser.pages();
    page = pages.filter(p => !p.isClosed()).at(-1) ?? original;
    result = resultStatus(await page.$eval("body", el => el.innerText));
    if (result.status !== "submitted" || result.message.startsWith("Scoot displays")) break;
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  return { ...result, page };
}
