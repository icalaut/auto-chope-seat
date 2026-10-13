const form = document.querySelector("#booking");
const buttons = Object.fromEntries(["prepare", "schedule", "cancel"].map(id => [id, document.getElementById(id)]));
const error = document.querySelector("#error");
const pnrInput = form.elements.namedItem("pnr");
pnrInput.addEventListener("input", () => {
  const start = pnrInput.selectionStart;
  const end = pnrInput.selectionEnd;
  pnrInput.value = pnrInput.value.toUpperCase();
  pnrInput.setSelectionRange(start, end);
});
let token;
let state;
let busy = false;
let mode = "local";
let previewBusy = false;
let previewRefresh;
const stationLabels = new Map();
const stationNames = new Map();
const departureInput = form.elements.namedItem("departure");
const timezoneInput = form.elements.namedItem("timezone");
const dateInput = form.elements.namedItem("date");
const timeInput = form.elements.namedItem("time");
let dateWasEdited = false;
let timeWasEdited = false;
function prefillToday(timezone = undefined) {
  if (dateWasEdited && timeWasEdited) return;
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: timezone,
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23"
  }).formatToParts(new Date());
  const values = Object.fromEntries(parts.map(part => [part.type, part.value]));
  if (!dateWasEdited) dateInput.value = `${values.year}-${values.month}-${values.day}`;
  if (!timeWasEdited) timeInput.value = `${values.hour}:${values.minute}:${values.second}`;
}
prefillToday();
dateInput.addEventListener("input", () => { dateWasEdited = true; });
timeInput.addEventListener("input", () => { timeWasEdited = true; });
timezoneInput.addEventListener("input", () => {
  try {
    prefillToday(timezoneInput.value);
  } catch (problem) {
    if (!(problem instanceof RangeError)) throw problem;
    document.querySelector("#timezone-note").textContent = "Select a valid airport timezone to determine today's local date.";
  }
});
function selectDepartureTimezone(selected) {
  const station = selected || stationNames.get(departureInput.value.trim().toLowerCase());
  timezoneInput.value = station?.timezone || "";
  prefillToday(station?.timezone || undefined);
  document.querySelector("#timezone-note").textContent = station?.timezone
    ? `Automatically selected ${station.timezone} for ${station.name} (${station.code}). Daylight saving is applied for the scheduled date.`
    : "Station not recognized or its name is ambiguous. Select a full station suggestion or its airport timezone manually; no timezone is assumed.";
}
departureInput.addEventListener("input", () => {
  const station = stationLabels.get(departureInput.value);
  if (station) departureInput.value = station.name;
  selectDepartureTimezone(station);
});

function updateButtons() {
  const status = state?.status;
  buttons.prepare.disabled = !token || busy || ["working", "scheduled", "submitting", "observing"].includes(status);
  buttons.schedule.disabled = !token || busy || status !== "prepared";
  buttons.cancel.disabled = !token || busy || !["prepared", "scheduled"].includes(status);
  document.querySelector("#show-browser").disabled = !token || busy || ["working", "submitting", "observing"].includes(status);
  document.querySelector("#remote-controls").disabled = !token || busy || previewBusy || !state?.manual_available;
  document.querySelector("#close-cloud").disabled = !token || busy || !state?.preview_available ||
    ["working", "scheduled", "submitting"].includes(status);
  document.querySelector("#browser-preview").classList.toggle("interactive",
    mode === "cloud" && state?.manual_available && !busy && !previewBusy && document.querySelector("#enable-clicks").checked);
  for (const input of form.querySelectorAll("input")) {
    input.disabled = busy || ["working", "scheduled", "submitting", "observing"].includes(status);
  }
}

async function request(url, options) {
  const response = await fetch(url, options);
  const contentType = response.headers.get("Content-Type") || "";
  if (!contentType.includes("application/json")) {
    throw new Error(response.status === 404 || response.ok
      ? "Automation API not found. Deploy the Worker backend, not just its static assets."
      : `Authentication or API unavailable (${response.status}). Open the Access-protected application and sign in.`);
  }
  const result = await response.json();
  if (url === "/api/config" && result.mode === "cloud") mode = "cloud";
  if (!response.ok) throw new Error(result.error || `Request failed (${response.status}).`);
  return result;
}

async function send(action, extra = {}) {
  if (busy) return;
  const fields = Object.fromEntries(new FormData(form));
  if (action === "prepare") {
    for (const name of ["pnr", "departure", "timezone"]) {
      if (!form.elements[name].reportValidity()) return;
    }
  } else if (action === "schedule" && !form.reportValidity()) return;
  busy = true;
  error.textContent = "";
  updateButtons();
  try {
    await request("/api/action", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-App-Token": token },
      body: JSON.stringify({ action, ...(action === "manual" || action === "close" ? {} : fields), ...extra })
    });
    state = await request("/api/status");
  } catch (problem) {
    error.textContent = problem.message;
  } finally {
    busy = false;
    updateButtons();
    if (mode === "cloud") {
      clearTimeout(previewRefresh);
      refreshPreview();
    }
  }
}

buttons.prepare.addEventListener("click", () => send("prepare"));
buttons.cancel.addEventListener("click", () => send("cancel"));
document.querySelector("#show-browser").addEventListener("click", () => send("show_browser"));
document.querySelector("#close-cloud").addEventListener("click", () => send("close"));
document.querySelector("#enable-clicks").addEventListener("change", updateButtons);
document.querySelector("#browser-preview").addEventListener("click", event => {
  if (mode !== "cloud" || !state?.manual_available || busy || previewBusy || !document.querySelector("#enable-clicks").checked) return;
  const image = event.currentTarget;
  const rect = image.getBoundingClientRect();
  const x = (event.clientX - rect.left - image.clientLeft) * 1280 / image.clientWidth;
  const y = (event.clientY - rect.top - image.clientTop) * 900 / image.clientHeight;
  if (x >= 0 && x < 1280 && y >= 0 && y < 900) send("manual", { kind: "click", x, y });
});
document.querySelector("#send-text").addEventListener("click", () => {
  const input = document.querySelector("#remote-text");
  const text = input.value;
  input.value = "";
  if (text) send("manual", { kind: "type", text });
});
document.querySelector("#remote-keys").addEventListener("click", event => {
  if (event.target.dataset.key) send("manual", { kind: "key", key: event.target.dataset.key });
  else if (event.target.dataset.scroll) send("manual", { kind: "scroll", delta: Number(event.target.dataset.scroll) });
});
form.addEventListener("submit", event => { event.preventDefault(); send("schedule"); });
form.addEventListener("input", () => {
  if (state?.status === "prepared") {
    buttons.schedule.disabled = true;
    document.querySelector("#message").textContent = "Inputs changed. Prepare the browser again before scheduling.";
    state = { ...state, status: "edited" };
  }
});

async function start() {
  const lists = await Promise.allSettled([
    request("./timezones.json").then(zones => {
      if (!Array.isArray(zones) || !zones.length) throw new Error("Invalid timezone dataset.");
      document.querySelector("#timezones").replaceChildren(...zones.map(zone => {
        const option = document.createElement("option");
        option.value = zone;
        return option;
      }));
    }),
    request("./stations.json").then(data => {
      if (!Array.isArray(data.stations) || !data.stations.length ||
          data.stations.some(station => !station.name || !station.code || !station.timezone)) {
        throw new Error("Invalid station dataset or missing airport timezones.");
      }
      document.querySelector("#stations").replaceChildren(...data.stations.map(station => {
        const option = document.createElement("option");
        option.value = `${station.name} (${station.code}) — ${station.country}`;
        stationLabels.set(option.value, station);
        const name = station.name.toLowerCase();
        stationNames.set(name, stationNames.has(name) ? null : station);
        stationNames.set(station.code.toLowerCase(), station);
        return option;
      }));
      if (departureInput.value.trim()) selectDepartureTimezone();
    })
  ]);
  const failures = lists.filter(result => result.status === "rejected");
  if (failures.length) {
    error.textContent = `Dropdown data unavailable: ${failures.map(result => result.reason.message).join(" ")} Enter the exact station and timezone manually.`;
  }
  const local = ["127.0.0.1", "localhost"].includes(location.hostname);
  try {
    const config = await request("/api/config");
    if (!config.token) throw new Error("Automation service returned no action token.");
    mode = config.mode === "cloud" ? "cloud" : "local";
    token = config.token;
    if (mode === "cloud") {
      document.querySelector("#show-browser").hidden = true;
      document.querySelector("#remote-controls").hidden = false;
      document.querySelector("#close-cloud").hidden = false;
      document.querySelector("#preview-help").textContent = "Cloud browser preview updates every five seconds while visible. It pauses 15 seconds before retrieval. Use the controls below to continue manually after retrieval; the session cannot be opened as a normal local tab.";
      document.querySelector("#runtime-note").textContent = "Use only one device per booking. Cloud scheduling can continue while this page is closed. Schedule 5 seconds to 24 hours ahead. A distant schedule reopens the browser 30 seconds before retrieval. Exact timing is not guaranteed; clicks more than two seconds late are stopped.";
      document.querySelector("#privacy-note").textContent = "PNR is encrypted in Cloudflare Durable Object storage until cancellation, expiry, or the recorded retrieval attempt. Browser sessions expire after ten minutes. Preview and manual text may contain personal data; they are not saved by this app. Cloud browser usage is metered. Close the cloud session when finished.";
    }
    const poll = async () => {
      try {
        const remote = await request("/api/status");
        if (state?.status === "edited" && remote.status === "prepared") remote.status = "edited";
        state = remote;
        document.querySelector("#status").textContent = remote.status;
        document.querySelector("#message").textContent = remote.status === "edited"
          ? "Inputs changed. Prepare the browser again before scheduling." : remote.message;
        document.querySelector("#countdown").textContent = remote.target && remote.status === "scheduled"
          ? `${Math.max(0, remote.target - remote.now).toFixed(1)} seconds remaining` : "";
        document.querySelector("#session-expiry").textContent = mode === "cloud" && remote.preview_available
          ? `Cloud session closes at ${new Date(remote.session_expires * 1000).toLocaleTimeString()}.` : "";
        updateButtons();
      } catch (problem) {
        token = null;
        error.textContent = `Connection lost: ${problem.message}. Pending work may still be running ${mode === "cloud" ? "in Cloudflare" : "in the terminal"}. Reopen this page to reconnect.`;
        updateButtons();
        return;
      }
      setTimeout(poll, mode === "cloud" ? 1000 : 250);
    };
    await poll();
    refreshPreview();
  } catch (problem) {
    const hosted = !local || mode === "cloud";
    document.querySelector("#status").textContent = hosted ? "Hosted automation unavailable" : "Automation unavailable";
    document.querySelector("#message").textContent = !hosted
      ? `Local automation service unavailable: ${problem.message}. Airport and timezone lists are independent; start the Python app to schedule retrieval.`
      : `Airport and timezone lists work independently. ${problem.message}`;
    document.querySelector("#preview-message").textContent = !hosted
      ? "Start the local Python app for browser preview."
      : "Cloud preview requires the deployed Worker backend, Browser Rendering and Cloudflare Access setup.";
    updateButtons();
  }
}

let previewUrl;
async function refreshPreview() {
  if (!token || previewBusy) return;
  previewBusy = true;
  updateButtons();
  const message = document.querySelector("#preview-message");
  try {
    if (mode === "cloud" && !state?.preview_available) {
      document.querySelector("#browser-preview").hidden = true;
      message.textContent = state?.status === "scheduled" ? "Browser preview resumes during scheduled warm-up." : "Prepare the cloud browser to see its preview.";
    } else if (mode === "cloud" && document.hidden) {
      message.textContent = "Preview paused while this page is hidden.";
    } else if (busy || ["working", "submitting", "observing"].includes(state?.status) ||
        (state?.status === "scheduled" && state.target - state.now <= (mode === "cloud" ? 15 : 3))) {
      message.textContent = "Preview paused to prioritize the booking action.";
    } else {
      const response = await fetch("/api/preview", { headers: { "X-App-Token": token } });
      if (!response.ok) {
        const problem = await response.json();
        throw new Error(problem.error);
      }
      const image = document.querySelector("#browser-preview");
      const previous = previewUrl;
      previewUrl = URL.createObjectURL(await response.blob());
      image.src = previewUrl;
      image.hidden = false;
      if (previous) URL.revokeObjectURL(previous);
      message.textContent = mode === "cloud" ? `Cloud preview captured ${new Date().toLocaleTimeString()}.` : state?.preview_error || (state?.preview_time
        ? `Preview captured ${new Date(state.preview_time * 1000).toLocaleTimeString()}.`
        : "Waiting for the first browser capture.");
    }
  } catch (problem) {
    message.textContent = problem.message;
    if (mode === "cloud") document.querySelector("#browser-preview").hidden = true;
  } finally {
    previewBusy = false;
    updateButtons();
  }
  clearTimeout(previewRefresh);
  previewRefresh = setTimeout(refreshPreview, mode === "cloud" ? 5000 : 1000);
}
start();
