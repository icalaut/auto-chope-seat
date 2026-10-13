import { Temporal } from "@js-temporal/polyfill";
import stations from "../stations.json";

export interface Booking {
  pnr: string;
  departure: string;
  timezone: string;
}

export function assertSiteAvailable(text: string, status?: number): void {
  if (status === 403 || /^\s*Access Denied\s*$/im.test(text) ||
      /You don['\u2019]t have permission to access/i.test(text)) {
    throw new Error("Scoot denied access to this browser. Cloud retrieval is unavailable while this restriction remains. Use the official site in your own browser or contact Scoot; the app will not bypass the restriction.");
  }
  if (/ChopeMySeat.{0,30}temporarily unavailable|currently under maintenance/i.test(text)) {
    throw new Error("Scoot is under maintenance. No retrieval.");
  }
}

export function validateBooking(data: Record<string, unknown>): Booking {
  const pnr = String(data.pnr ?? "").trim().toUpperCase();
  const departure = String(data.departure ?? "").trim();
  const timezone = String(data.timezone ?? "").trim();
  if (!/^[A-Z0-9]{6}$/.test(pnr)) throw new Error("PNR must contain six letters or numbers.");
  if (!departure || departure.length > 100) throw new Error("Enter the exact departure station.");
  const matches = stations.stations.filter(s => s.name.toLowerCase() === departure.toLowerCase());
  if (matches.length && !matches.some(station => station.timezone === timezone)) {
    throw new Error(`This station uses ${[...new Set(matches.map(station => station.timezone))].join(" or ")}.`);
  }
  try {
    new Intl.DateTimeFormat("en", { timeZone: timezone }).format();
  } catch {
    throw new Error("Select a valid airport timezone.");
  }
  return { pnr, departure, timezone };
}

export function scheduleTime(data: Record<string, unknown>, now = Date.now()): number {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(String(data.date)) ||
      !/^\d{2}:\d{2}:\d{2}$/.test(String(data.time))) {
    throw new Error("Supply a date and HH:mm:ss.");
  }
  let target: number;
  try {
    const local = Temporal.PlainDateTime.from(`${data.date}T${data.time}`, { overflow: "reject" });
    target = local.toZonedDateTime(String(data.timezone), { disambiguation: "reject" }).epochMilliseconds;
  } catch {
    throw new Error("Invalid or ambiguous airport-local date/time, including a daylight-saving clock change.");
  }
  if (target <= now + 5000) throw new Error("Schedule at least five seconds into the future.");
  if (target > now + 86400000) throw new Error("Schedule at most 24 hours ahead.");
  return target;
}

export function resultStatus(text: string): { status: string; message: string } {
  assertSiteAvailable(text);
  const queues = [...new Set([...text.matchAll(/\bQueue\s*:\s*([A-Z]+-\d+|\d+)\b/gi)].map(m => m[1]))];
  if (queues.length) return { status: "airport_queue", message: `Queue: ${queues.join(", ")}. Attend the airport staff-travel counter in person. Automation has stopped; no retries or confirmed seat.` };
  if (/^\s*(?:unsuccessful\s+(?:staff\s+)?web\s+check[- ]?in|(?:staff\s+)?web\s+check[- ]?in\s+(?:is\s+|was\s+)?unsuccessful)\b/im.test(text)) {
    return { status: "unsuccessful", message: "Unsuccessful web check-in. Automation has stopped. Follow Scoot's airport counter instructions." };
  }
  if (/\[Successful\]\s*Staff web check-in/i.test(text)) {
    return { status: "submitted", message: "Scoot displays successful staff web check-in. Follow its kiosk/service-desk instructions at least 65 minutes before departure; final acceptance remains subject to availability. Automation has stopped." };
  }
  return { status: "submitted", message: "Retrieve Booking was dispatched once. Review the remote browser and continue manually if required. No check-in or seat success is inferred; there will be no automatic retry." };
}

export function mayDispatch(status: string, target: number, now: number): boolean {
  return status === "scheduled" && now >= target && now - target <= 2000;
}

export interface Sealed {
  iv: number[];
  value: number[];
}

async function encryptionKey(secret: string): Promise<CryptoKey> {
  const bytes = Uint8Array.from(atob(secret), char => char.charCodeAt(0));
  if (bytes.length !== 32) throw new Error("BOOKING_KEY must be a base64-encoded 32-byte secret.");
  return crypto.subtle.importKey("raw", bytes, "AES-GCM", false, ["encrypt", "decrypt"]);
}

export async function seal(booking: Booking, secret: string): Promise<Sealed> {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const value = await crypto.subtle.encrypt({ name: "AES-GCM", iv }, await encryptionKey(secret), new TextEncoder().encode(JSON.stringify(booking)));
  return { iv: [...iv], value: [...new Uint8Array(value)] };
}

export async function unseal(data: Sealed, secret: string): Promise<Booking> {
  const decoded = await crypto.subtle.decrypt({ name: "AES-GCM", iv: new Uint8Array(data.iv) }, await encryptionKey(secret), new Uint8Array(data.value));
  return validateBooking(JSON.parse(new TextDecoder().decode(decoded)));
}
