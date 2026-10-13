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
function selectDepartureTimezone() {
  const station = stationNames.get(departureInput.value.trim().toLowerCase());
  timezoneInput.value = station?.timezone || "";
  prefillToday(station?.timezone || undefined);
  document.querySelector("#timezone-note").textContent = station?.timezone
    ? `Automatically selected ${station.timezone} for ${station.name} (${station.code}). Daylight saving is applied for the scheduled date.`
    : "Station not recognized. Select its airport timezone manually before preparing; no timezone is assumed.";
}
departureInput.addEventListener("input", () => {
  const station = stationLabels.get(departureInput.value);
  if (station) departureInput.value = station.name;
  selectDepartureTimezone();
});

function updateButtons() {
  const status = state?.status;
  buttons.prepare.disabled = !token || busy || ["working", "scheduled", "submitting", "observing"].includes(status);
  buttons.schedule.disabled = !token || busy || status !== "prepared";
  buttons.cancel.disabled = !token || busy || !["prepared", "scheduled"].includes(status);
  document.querySelector("#show-browser").disabled = !token || busy || ["working", "submitting", "observing"].includes(status);
  for (const input of form.querySelectorAll("input")) {
    input.disabled = busy || ["working", "scheduled", "submitting", "observing"].includes(status);
  }
}

async function request(url, options) {
  const response = await fetch(url, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `Request failed (${response.status}).`);
  return result;
}

async function send(action) {
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
      body: JSON.stringify({ action, ...fields })
    });
    state = await request("/api/status");
  } catch (problem) {
    error.textContent = problem.message;
  } finally {
    busy = false;
    updateButtons();
  }
}

buttons.prepare.addEventListener("click", () => send("prepare"));
buttons.cancel.addEventListener("click", () => send("cancel"));
document.querySelector("#show-browser").addEventListener("click", () => send("show_browser"));
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
        stationNames.set(station.name.toLowerCase(), station);
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
  if (!["127.0.0.1", "localhost"].includes(location.hostname)) {
    document.querySelector("#status").textContent = "Hosted form only";
    document.querySelector("#message").textContent = "Airport and timezone selection works here. Scheduled retrieval and the browser preview require the local Python app at http://127.0.0.1:8766; this static deployment does not run browser automation.";
    document.querySelector("#preview-message").textContent = "Controlled-browser preview is available in the local app only.";
    updateButtons();
    return;
  }
  try {
    const config = await request("/api/config");
    token = config.token;
    refreshPreview();
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
        updateButtons();
      } catch (problem) {
        token = null;
        error.textContent = `Connection lost: ${problem.message}. Pending work may still be running in the terminal.`;
        updateButtons();
        return;
      }
      setTimeout(poll, 250);
    };
    await poll();
  } catch (problem) {
    document.querySelector("#status").textContent = "Automation unavailable";
    document.querySelector("#message").textContent = `Local automation service unavailable: ${problem.message}. Airport and timezone lists are independent; start the Python app to schedule retrieval.`;
    updateButtons();
  }
}

let previewUrl;
async function refreshPreview() {
  if (!token) return;
  const message = document.querySelector("#preview-message");
  try {
    if (["working", "submitting", "observing"].includes(state?.status) ||
        (state?.status === "scheduled" && state.target - state.now <= 3)) {
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
      message.textContent = state?.preview_error || (state?.preview_time
        ? `Preview captured ${new Date(state.preview_time * 1000).toLocaleTimeString()}.`
        : "Waiting for the first browser capture.");
    }
  } catch (problem) {
    message.textContent = problem.message;
  }
  setTimeout(refreshPreview, 1000);
}
start();
