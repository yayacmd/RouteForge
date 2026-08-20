/* RouteForge frontend. No build step: plain ES modules-free JS so a forker
   can edit this file and reload the page. */

(() => {
"use strict";

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
const state = {
  plan: { commodities: [], locations: [], vehicles: [], stops: [], depots: [], settings: {} },
  result: null,
  view: "commodities",
  map: null,
  layers: [],
  saveTimer: null,
};

const TRUCK_COLORS = ["#16202B", "#1E6FA8", "#8A4FBF", "#B85C00", "#1E8E5A",
                      "#A8324A", "#4A6B8A", "#7A6A1E"];

const uid = () => (crypto.randomUUID ? crypto.randomUUID()
                                     : "id-" + Math.random().toString(36).slice(2, 11));

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function toast(msg, isError = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.toggle("is-error", isError);
  el.classList.remove("hidden");
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.add("hidden"), 3200);
}

// minutes-from-midnight <-> "HH:MM"
const toClock = m => {
  if (m == null || isNaN(m)) return "";
  const v = ((Math.round(m) % 1440) + 1440) % 1440;
  return String(Math.floor(v / 60)).padStart(2, "0") + ":" + String(v % 60).padStart(2, "0");
};
const fromClock = s => {
  if (!s) return null;
  const [h, m] = s.split(":").map(Number);
  return h * 60 + m;
};

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------
async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (res.status === 401) { showLogin(); throw new Error("Signed out"); }
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { detail: text }; }
  if (!res.ok) {
    let msg = data?.detail || `Request failed (${res.status})`;
    if (Array.isArray(msg)) {
      msg = msg.map(e => e.msg || JSON.stringify(e)).join("; ");
    } else if (typeof msg === "object") {
      msg = JSON.stringify(msg);
    }
    throw new Error(msg);
  }
  return data;
}

function schedulePlanSave() {
  $("#save-state").textContent = "saving…";
  clearTimeout(state.saveTimer);
  state.saveTimer = setTimeout(async () => {
    try {
      await api("/api/plan", { method: "PUT", body: state.plan });
      $("#save-state").textContent = "saved";
      setTimeout(() => { $("#save-state").textContent = ""; }, 1500);
    } catch (e) {
      $("#save-state").textContent = "not saved";
      toast("Couldn't save: " + e.message, true);
    }
  }, 600);
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
async function boot() {
  wireGate();
  let status;
  try {
    status = await api("/api/status");
  } catch {
    $("#gate-loading").innerHTML =
      '<p class="form-error">Can\'t reach the server. Check that it\'s running, then reload.</p>';
    return;
  }
  if (status.organization_name) $("#brand-name").textContent = status.organization_name;

  if (!status.configured) { showPanel("gate-setup"); return; }

  // Configured: do we already have a session?
  try {
    state.plan = normalisePlan(await api("/api/plan"));
    startApp();
  } catch {
    showLogin(status.organization_name);
  }
}

function normalisePlan(p) {
  return {
    last_result: p.last_result || null,
    commodities: p.commodities || [],
    locations:   p.locations   || [],
    vehicles:    p.vehicles    || [],
    stops:       p.stops       || [],
    depots:      p.depots      || [],
    settings:    p.settings    || {},
  };
}

function showPanel(id) {
  $("#gate").classList.remove("hidden");
  $("#app").classList.add("hidden");
  ["gate-loading", "gate-setup", "gate-login"].forEach(p =>
    $("#" + p).classList.toggle("hidden", p !== id));
}

function showLogin(org) {
  if (org) $("#login-lead").textContent = `Sign in to plan routes for ${org}.`;
  showPanel("gate-login");
}

function wireGate() {
  const provider = $("#setup-provider");
  provider.addEventListener("change", () => {
    const osrm = provider.value === "osrm";
    $("#setup-osrm-fields").classList.toggle("hidden", !osrm);
    $("#setup-key-field").classList.toggle("hidden", osrm);
    const hints = {
      locationiq: 'Free key at <a href="https://locationiq.com/register" target="_blank" rel="noopener">locationiq.com</a>. Used to look up addresses and measure driving distances.',
      ors: 'Free key at <a href="https://openrouteservice.org/dev/#/signup" target="_blank" rel="noopener">openrouteservice.org</a>. A larger free allowance than LocationIQ.',
      osrm: "Point this at your own OSRM server. No key, no request limits — best if you plan routes every day.",
    };
    $("#provider-hint").innerHTML = hints[provider.value];
  });

  $("#gate-setup").addEventListener("submit", async ev => {
    ev.preventDefault();
    const f = new FormData(ev.target);
    const body = {
      password: f.get("password"),
      organization_name: f.get("organization_name"),
      routing_provider: f.get("routing_provider"),
      routing_api_key: f.get("routing_api_key") || "",
      routing_base_url: f.get("routing_base_url") || "",
      nominatim_url: f.get("nominatim_url") || "",
    };
    const err = $("#setup-error");
    err.classList.add("hidden");
    try {
      await api("/api/setup", { method: "POST", body });
      $("#brand-name").textContent = body.organization_name;
      state.plan = normalisePlan(await api("/api/plan"));
      startApp();
    } catch (e) {
      err.textContent = e.message;
      err.classList.remove("hidden");
    }
  });

  $("#gate-login").addEventListener("submit", async ev => {
    ev.preventDefault();
    const err = $("#login-error");
    err.classList.add("hidden");
    try {
      await api("/api/login", { method: "POST",
        body: { password: new FormData(ev.target).get("password") } });
      state.plan = normalisePlan(await api("/api/plan"));
      startApp();
    } catch (e) {
      err.textContent = e.message;
      err.classList.remove("hidden");
    }
  });
}

function startApp() {
  $("#gate").classList.add("hidden");
  $("#app").classList.remove("hidden");
  wireApp();
  loadSettingsIntoForm();
  restorePlanSettings();
  if (state.plan.last_result) {
    state.result = state.plan.last_result;
    try { renderResults(); } catch (e) { console.error(e); }
  }
  const firstEmpty = state.plan.commodities.length === 0;
  setView(firstEmpty ? "commodities" : "stops");
  renderAll();
}

// ---------------------------------------------------------------------------
// Navigation
// ---------------------------------------------------------------------------
function setView(name) {
  state.view = name;
  $$(".nav-item").forEach(b => b.classList.toggle("active", b.dataset.view === name));
  $$(".view").forEach(v => v.classList.toggle("active", v.dataset.view === name));
  $("#sidebar").classList.remove("open");
  if (name === "results" && state.map) setTimeout(() => state.map.invalidateSize(), 60);
  if (name === "plan") updatePlanSummary();
}

function wireApp() {
  $$(".nav-item").forEach(b => b.addEventListener("click", () => setView(b.dataset.view)));
  $("#nav-toggle").addEventListener("click", () => $("#sidebar").classList.toggle("open"));
  $("#btn-logout").addEventListener("click", async () => {
    await api("/api/logout", { method: "POST" }).catch(() => {});
    location.reload();
  });

  $("#add-commodity").addEventListener("click", addCommodity);
  $("#add-commodity-2").addEventListener("click", addCommodity);
  $("#load-demo").addEventListener("click", loadDemo);
  $("#add-vehicle").addEventListener("click", addVehicle);
  $("#add-depot").addEventListener("click", addDepot);
  $("#add-stop").addEventListener("click", addStop);
  $("#clear-stops").addEventListener("click", () => {
    if (!state.plan.stops.length) return;
    if (confirm("Remove every stop from today's board?")) {
      state.plan.stops = []; renderAll(); schedulePlanSave();
    }
  });

  $("#btn-solve").addEventListener("click", runSolve);
  $("#btn-resolve").addEventListener("click", () => { setView("plan"); runSolve(); });
  $("#btn-export").addEventListener("click", exportCsv);

  ["opt-day-start", "opt-max-shift", "opt-units", "opt-objective",
   "opt-effort", "opt-fss", "opt-lsm"].forEach(id =>
    $("#" + id).addEventListener("change", () => { capturePlanSettings(); schedulePlanSave(); }));

  wireLocationSearch();
  wireSettings();
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------
function renderAll() {
  renderCommodities();
  renderLocations();
  renderVehicles();
  renderDepots();
  renderStops();
  updateCounts();
  updatePlanSummary();
}

function updateCounts() {
  $("#count-commodities").textContent = state.plan.commodities.length;
  $("#count-locations").textContent = state.plan.locations.length;
  $("#count-vehicles").textContent = state.plan.vehicles.length;
  $("#count-depots").textContent = state.plan.depots.length;
  $("#count-stops").textContent = state.plan.stops.filter(s => !s.excluded).length;
}

function toggleEmpty(id, isEmpty) { $("#" + id).classList.toggle("hidden", !isEmpty); }

/* -- commodities -- */
function addCommodity() {
  state.plan.commodities.push({ id: uid(), name: "", unit: "units", minutes_per_unit: 0 });
  renderCommodities(); updateCounts(); schedulePlanSave();
}

function renderCommodities() {
  const list = $("#commodity-list");
  toggleEmpty("commodity-empty", state.plan.commodities.length === 0);
  list.innerHTML = state.plan.commodities.map((c, i) => `
    <div class="card" data-i="${i}">
      <div class="card-head">
        <span class="card-title">${esc(c.name) || "Untitled product"}</span>
        <div class="card-actions">
          <button class="btn btn-danger btn-sm" data-act="del-commodity" data-i="${i}">Remove</button>
        </div>
      </div>
      <div class="card-body grid-3">
        <label class="field"><span class="label">Name</span>
          <input type="text" data-f="name" data-i="${i}" value="${esc(c.name)}"
                 placeholder="e.g. Diesel"></label>
        <label class="field"><span class="label">Measured in</span>
          <input type="text" data-f="unit" data-i="${i}" value="${esc(c.unit)}"
                 placeholder="gallons"></label>
        <label class="field"><span class="label">Minutes per unit</span>
          <input type="number" data-f="minutes_per_unit" data-i="${i}" min="0" step="0.01"
                 value="${c.minutes_per_unit}">
          <span class="hint">Pumping/unloading rate</span></label>
      </div>
    </div>`).join("");

  list.oninput = ev => {
    const t = ev.target, f = t.dataset.f;
    if (!f) return;
    const c = state.plan.commodities[+t.dataset.i];
    c[f] = f === "minutes_per_unit" ? (parseFloat(t.value) || 0) : t.value;
    if (f === "name") {
      t.closest(".card").querySelector(".card-title").textContent = t.value || "Untitled product";
    }
    schedulePlanSave();
  };
  list.onclick = ev => {
    if (ev.target.dataset.act !== "del-commodity") return;
    state.plan.commodities.splice(+ev.target.dataset.i, 1);
    renderAll(); schedulePlanSave();
  };
}

/* -- locations -- */
function wireLocationSearch() {
  const input = $("#loc-search"), box = $("#loc-suggestions");
  let timer;
  input.addEventListener("input", () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < 3) { box.classList.add("hidden"); return; }
    timer = setTimeout(async () => {
      try {
        const data = await api(`/api/geocode?q=${encodeURIComponent(q)}`);
        if (!data.results.length) { box.classList.add("hidden"); return; }
        box.innerHTML = data.results.map((r, i) =>
          `<div class="suggestion" data-i="${i}">${esc(r.label)}</div>`).join("");
        box._results = data.results;
        box.classList.remove("hidden");
      } catch (e) { toast(e.message, true); }
    }, 350);
  });
  box.addEventListener("click", ev => {
    const el = ev.target.closest(".suggestion");
    if (!el) return;
    const r = box._results[+el.dataset.i];
    const shortName = r.label.split(",")[0];
    state.plan.locations.push({
      id: uid(), name: shortName, address: r.label,
      latitude: r.latitude, longitude: r.longitude,
    });
    input.value = ""; box.classList.add("hidden");
    renderAll(); schedulePlanSave();
    toast(`Added ${shortName}`);
  });
  document.addEventListener("click", ev => {
    if (!ev.target.closest(".search-wrap")) box.classList.add("hidden");
  });
}

function renderLocations() {
  const list = $("#location-list");
  toggleEmpty("location-empty", state.plan.locations.length === 0);
  list.innerHTML = state.plan.locations.map((l, i) => `
    <div class="card">
      <div class="card-head">
        <div>
          <div class="card-title">${esc(l.name)}</div>
          <div class="card-sub">${esc(l.address)}</div>
        </div>
        <div class="card-actions">
          <button class="btn btn-danger btn-sm" data-act="del-loc" data-i="${i}">Remove</button>
        </div>
      </div>
      <div class="card-body">
        <label class="field" style="max-width:340px"><span class="label">Short name</span>
          <input type="text" data-f="name" data-i="${i}" value="${esc(l.name)}"></label>
      </div>
    </div>`).join("");

  list.oninput = ev => {
    if (ev.target.dataset.f !== "name") return;
    state.plan.locations[+ev.target.dataset.i].name = ev.target.value;
    ev.target.closest(".card").querySelector(".card-title").textContent = ev.target.value;
    schedulePlanSave();
  };
  list.onclick = ev => {
    if (ev.target.dataset.act !== "del-loc") return;
    const loc = state.plan.locations[+ev.target.dataset.i];
    const used = state.plan.stops.some(s => s.location_id === loc.id)
      || state.plan.vehicles.some(v => v.start_location_id === loc.id)
      || state.plan.depots.some(d => d.location_id === loc.id);
    if (used && !confirm(`${loc.name} is used by a vehicle, stop, or depot. Remove it anyway?`)) return;
    state.plan.locations.splice(+ev.target.dataset.i, 1);
    renderAll(); schedulePlanSave();
  };
}

function locationOptions(selected) {
  return state.plan.locations.map(l =>
    `<option value="${l.id}" ${l.id === selected ? "selected" : ""}>${esc(l.name)}</option>`
  ).join("");
}

/* -- vehicles -- */
function addVehicle() {
  if (!state.plan.locations.length) {
    toast("Add a place first — a vehicle needs somewhere to start.", true);
    setView("locations"); return;
  }
  const caps = {}, load = {};
  state.plan.commodities.forEach(c => { caps[c.id] = 0; load[c.id] = 0; });
  state.plan.vehicles.push({
    id: uid(), name: `Truck ${state.plan.vehicles.length + 1}`,
    start_location_id: state.plan.locations[0].id,
    capacities: caps, starting_load: load,
    shift_start_minutes: null, max_shift_minutes: null,
  });
  renderVehicles(); updateCounts(); schedulePlanSave();
}

function renderVehicles() {
  const list = $("#vehicle-list");
  toggleEmpty("vehicle-empty", state.plan.vehicles.length === 0);
  list.innerHTML = state.plan.vehicles.map((v, i) => `
    <div class="card is-vehicle">
      <div class="card-head">
        <span class="card-title">${esc(v.name)}</span>
        <div class="card-actions">
          <button class="btn btn-danger btn-sm" data-act="del-veh" data-i="${i}">Remove</button>
        </div>
      </div>
      <div class="card-body">
        <div class="grid-2">
          <label class="field"><span class="label">Name</span>
            <input type="text" data-f="name" data-i="${i}" value="${esc(v.name)}"></label>
          <label class="field"><span class="label">Starts and ends at</span>
            <select data-f="start_location_id" data-i="${i}">${locationOptions(v.start_location_id)}</select></label>
        </div>
        <div class="grid-2">
          <label class="field"><span class="label">Shift starts (optional)</span>
            <input type="time" data-f="shift_start" data-i="${i}"
                   value="${toClock(v.shift_start_minutes)}">
            <span class="hint">Leave blank to use the day start</span></label>
          <label class="field"><span class="label">Max hours (optional)</span>
            <input type="number" data-f="max_shift" data-i="${i}" min="1" max="24" step="0.5"
                   value="${v.max_shift_minutes ? v.max_shift_minutes / 60 : ""}">
            <span class="hint">Leave blank to use the standard shift</span></label>
        </div>
        ${state.plan.commodities.length ? `
          <div class="grid-2">
            ${state.plan.commodities.map(c => `
              <label class="field"><span class="label">${esc(c.name || "Product")} capacity</span>
                <div class="input-suffix">
                  <input type="number" data-f="cap" data-c="${c.id}" data-i="${i}" min="0"
                         value="${v.capacities?.[c.id] ?? 0}">
                  <span>${esc(c.unit)}</span></div></label>
              <label class="field"><span class="label">Loaded at start</span>
                <div class="input-suffix">
                  <input type="number" data-f="load" data-c="${c.id}" data-i="${i}" min="0"
                         value="${v.starting_load?.[c.id] ?? 0}">
                  <span>${esc(c.unit)}</span></div></label>`).join("")}
          </div>` : `<p class="muted">Set up what you deliver to give this vehicle a capacity.</p>`}
      </div>
    </div>`).join("");

  list.oninput = ev => {
    const t = ev.target, f = t.dataset.f;
    if (!f) return;
    const v = state.plan.vehicles[+t.dataset.i];
    if (f === "cap")            { v.capacities[t.dataset.c] = parseFloat(t.value) || 0; }
    else if (f === "load")      { v.starting_load[t.dataset.c] = parseFloat(t.value) || 0; }
    else if (f === "shift_start") { v.shift_start_minutes = t.value ? fromClock(t.value) : null; }
    else if (f === "max_shift") { v.max_shift_minutes = t.value ? Math.round(parseFloat(t.value) * 60) : null; }
    else {
      v[f] = t.value;
      if (f === "name") t.closest(".card").querySelector(".card-title").textContent = t.value;
    }
    schedulePlanSave();
  };
  list.onchange = list.oninput;
  list.onclick = ev => {
    if (ev.target.dataset.act !== "del-veh") return;
    state.plan.vehicles.splice(+ev.target.dataset.i, 1);
    renderAll(); schedulePlanSave();
  };
}

/* -- depots -- */
function addDepot() {
  if (!state.plan.locations.length) {
    toast("Add a place first, then mark it as a reload depot.", true);
    setView("locations"); return;
  }
  state.plan.depots.push({ id: uid(), location_id: state.plan.locations[0].id, reload_minutes: 45 });
  renderDepots(); updateCounts(); schedulePlanSave();
}

function renderDepots() {
  const list = $("#depot-list");
  toggleEmpty("depot-empty", state.plan.depots.length === 0);
  list.innerHTML = state.plan.depots.map((d, i) => `
    <div class="card is-depot">
      <div class="card-head">
        <span class="card-title">Reload point</span>
        <div class="card-actions">
          <button class="btn btn-danger btn-sm" data-act="del-depot" data-i="${i}">Remove</button>
        </div>
      </div>
      <div class="card-body grid-2">
        <label class="field"><span class="label">Place</span>
          <select data-f="location_id" data-i="${i}">${locationOptions(d.location_id)}</select></label>
        <label class="field"><span class="label">Time to reload</span>
          <div class="input-suffix">
            <input type="number" data-f="reload_minutes" data-i="${i}" min="0" value="${d.reload_minutes}">
            <span>minutes</span></div></label>
      </div>
    </div>`).join("");

  list.onchange = list.oninput = ev => {
    const t = ev.target, f = t.dataset.f;
    if (!f) return;
    const d = state.plan.depots[+t.dataset.i];
    d[f] = f === "reload_minutes" ? (parseFloat(t.value) || 0) : t.value;
    schedulePlanSave();
  };
  list.onclick = ev => {
    if (ev.target.dataset.act !== "del-depot") return;
    state.plan.depots.splice(+ev.target.dataset.i, 1);
    renderAll(); schedulePlanSave();
  };
}

/* -- stops -- */
function addStop() {
  if (!state.plan.locations.length) {
    toast("Add a place first, then you can deliver to it.", true);
    setView("locations"); return;
  }
  const demands = {};
  state.plan.commodities.forEach(c => { demands[c.id] = 0; });
  state.plan.stops.push({
    id: uid(), location_id: state.plan.locations[0].id, demands,
    window_start_minutes: null, window_end_minutes: null,
    window_type: "required", fixed_service_minutes: 10,
    priority: "should", locked_vehicle_id: null, excluded: false,
  });
  renderStops(); updateCounts(); schedulePlanSave();
}

function renderStops() {
  const list = $("#stop-list");
  toggleEmpty("stop-empty", state.plan.stops.length === 0);
  list.innerHTML = state.plan.stops.map((s, i) => {
    const loc = state.plan.locations.find(l => l.id === s.location_id);
    return `
    <div class="card is-stop ${s.excluded ? "is-excluded" : ""}">
      <div class="card-head">
        <div>
          <span class="card-title">${esc(loc?.name || "Choose a place")}</span>
          ${s.excluded ? '<span class="tag">Held back</span>' : ""}
          ${s.window_type === "preferred" ? '<span class="tag amber">Preferred window</span>' : ""}
          ${s.locked_vehicle_id ? '<span class="tag go">Pinned</span>' : ""}
        </div>
        <div class="card-actions">
          <button class="btn btn-ghost btn-sm" data-act="toggle-stop" data-i="${i}">
            ${s.excluded ? "Put back" : "Hold back"}</button>
          <button class="btn btn-danger btn-sm" data-act="del-stop" data-i="${i}">Remove</button>
        </div>
      </div>
      <div class="card-body">
        <div class="grid-2">
          <label class="field"><span class="label">Deliver to</span>
            <select data-f="location_id" data-i="${i}">${locationOptions(s.location_id)}</select></label>
          <label class="field"><span class="label">Time on site</span>
            <div class="input-suffix">
              <input type="number" data-f="fixed_service_minutes" data-i="${i}" min="0"
                     value="${s.fixed_service_minutes}"><span>minutes</span></div>
            <span class="hint">Parking, paperwork — on top of unloading time</span></label>
        </div>
        ${state.plan.commodities.length ? `<div class="grid-2">
          ${state.plan.commodities.map(c => `
            <label class="field"><span class="label">${esc(c.name || "Product")}</span>
              <div class="input-suffix">
                <input type="number" data-f="demand" data-c="${c.id}" data-i="${i}" min="0"
                       value="${s.demands?.[c.id] ?? 0}"><span>${esc(c.unit)}</span></div></label>`
          ).join("")}</div>` : ""}
        <div class="grid-3">
          <label class="field"><span class="label">Earliest</span>
            <input type="time" data-f="win_start" data-i="${i}" value="${toClock(s.window_start_minutes)}"></label>
          <label class="field"><span class="label">Latest</span>
            <input type="time" data-f="win_end" data-i="${i}" value="${toClock(s.window_end_minutes)}"></label>
          <label class="field"><span class="label">This window is</span>
            <select data-f="window_type" data-i="${i}">
              <option value="required" ${s.window_type === "required" ? "selected" : ""}>Required — must hit it</option>
              <option value="preferred" ${s.window_type === "preferred" ? "selected" : ""}>Preferred — late is OK</option>
            </select></label>
        </div>
        <div class="grid-2">
          <label class="field"><span class="label">Importance</span>
            <select data-f="priority" data-i="${i}">
              <option value="must" ${s.priority === "must" ? "selected" : ""}>Must go today</option>
              <option value="should" ${s.priority === "should" ? "selected" : ""}>Deliver if possible</option>
              <option value="optional" ${s.priority === "optional" ? "selected" : ""}>Optional</option>
            </select></label>
          <label class="field"><span class="label">Assign to a specific vehicle</span>
            <select data-f="locked_vehicle_id" data-i="${i}">
              <option value="">Let the planner choose</option>
              ${state.plan.vehicles.map(v =>
                `<option value="${v.id}" ${v.id === s.locked_vehicle_id ? "selected" : ""}>${esc(v.name)}</option>`
              ).join("")}
            </select></label>
        </div>
      </div>
    </div>`;
  }).join("");

  list.onchange = list.oninput = ev => {
    const t = ev.target, f = t.dataset.f;
    if (!f) return;
    const s = state.plan.stops[+t.dataset.i];
    if (f === "demand")            s.demands[t.dataset.c] = parseFloat(t.value) || 0;
    else if (f === "win_start")    s.window_start_minutes = t.value ? fromClock(t.value) : null;
    else if (f === "win_end")      s.window_end_minutes = t.value ? fromClock(t.value) : null;
    else if (f === "fixed_service_minutes") s.fixed_service_minutes = parseFloat(t.value) || 0;
    else if (f === "locked_vehicle_id")     s.locked_vehicle_id = t.value || null;
    else                           s[f] = t.value;

    if (f === "location_id") {
      const loc = state.plan.locations.find(l => l.id === t.value);
      t.closest(".card").querySelector(".card-title").textContent = loc?.name || "";
    }
    updateCounts(); schedulePlanSave();
  };
  list.onclick = ev => {
    const act = ev.target.dataset.act;
    if (act === "del-stop") {
      state.plan.stops.splice(+ev.target.dataset.i, 1);
      renderStops(); updateCounts(); schedulePlanSave();
    } else if (act === "toggle-stop") {
      const s = state.plan.stops[+ev.target.dataset.i];
      s.excluded = !s.excluded;
      renderStops(); updateCounts(); schedulePlanSave();
    }
  };
}

// ---------------------------------------------------------------------------
// Plan settings
// ---------------------------------------------------------------------------
function capturePlanSettings() {
  state.plan.settings = {
    day_start_minutes: fromClock($("#opt-day-start").value) ?? 360,
    default_max_shift_minutes: Math.round((parseFloat($("#opt-max-shift").value) || 10) * 60),
    distance_unit: $("#opt-units").value,
    objective: $("#opt-objective").value,
    effort: $("#opt-effort").value,
    first_solution_strategy: $("#opt-fss").value || "AUTOMATIC",
    local_search_metaheuristic: $("#opt-lsm").value || "GUIDED_LOCAL_SEARCH",
  };
  updatePlanSummary();
}

function restorePlanSettings() {
  const s = state.plan.settings || {};
  if (s.day_start_minutes != null) $("#opt-day-start").value = toClock(s.day_start_minutes);
  if (s.default_max_shift_minutes) $("#opt-max-shift").value = s.default_max_shift_minutes / 60;
  if (s.distance_unit) $("#opt-units").value = s.distance_unit;
  if (s.objective) $("#opt-objective").value = s.objective;
  if (s.effort) $("#opt-effort").value = s.effort;
  if (s.first_solution_strategy) $("#opt-fss").value = s.first_solution_strategy;
  if (s.local_search_metaheuristic) $("#opt-lsm").value = s.local_search_metaheuristic;
}

function updatePlanSummary() {
  const stops = state.plan.stops.filter(s => !s.excluded).length;
  const veh = state.plan.vehicles.length;
  const el = $("#plan-summary");
  if (el) el.textContent = `${stops} stop${stops === 1 ? "" : "s"} across ${veh} vehicle${veh === 1 ? "" : "s"}.`;
}

// ---------------------------------------------------------------------------
// Solve
// ---------------------------------------------------------------------------
function buildSolvePayload() {
  capturePlanSettings();
  return {  // deliberately omits last_result — the solver takes inputs only
    commodities: state.plan.commodities,
    locations: state.plan.locations,
    vehicles: state.plan.vehicles,
    stops: state.plan.stops,
    depots: state.plan.depots,
    settings: state.plan.settings,
  };
}

async function runSolve() {
  const fb = $("#plan-feedback");
  const btn = $("#btn-solve");
  btn.disabled = true;
  fb.innerHTML = `<div class="panel"><div class="working">
      <div class="spinner"></div>
      <div><strong>Planning the day…</strong>
      <div class="muted">Measuring driving distances, then working out the best order.</div></div>
    </div></div>`;

  try {
    const result = await api("/api/solve", { method: "POST", body: buildSolvePayload() });
    state.result = result;
    // Keep the plan so a reload (or a driver opening the app later) still
    // shows today's routes instead of an empty screen.
    state.plan.last_result = result.status === "solved" ? result : null;
    schedulePlanSave();

    if (result.status !== "solved") {
      fb.innerHTML = feedbackBlock(
        "blocked",
        result.status === "error" ? "Couldn't plan the routes" : "This day won't fit",
        result.diagnostics.length ? result.diagnostics : ["No further detail available."]
      ) + (result.warnings.length
            ? feedbackBlock("caution", "Also worth knowing", result.warnings) : "");
      return;
    }

    fb.innerHTML = result.warnings.length
      ? feedbackBlock("caution", "Planned, with a few notes", result.warnings) : "";
    renderResults();
    setView("results");
    toast("Routes ready");
  } catch (e) {
    fb.innerHTML = feedbackBlock("blocked", "Something went wrong", [e.message]);
  } finally {
    btn.disabled = false;
  }
}

function feedbackBlock(kind, title, items) {
  return `<div class="feedback ${kind}"><h3>${esc(title)}</h3>
    <ul>${items.map(t => `<li>${esc(t)}</li>`).join("")}</ul></div>`;
}

// ---------------------------------------------------------------------------
// Results: totals, map, run sheets
// ---------------------------------------------------------------------------
function renderResults() {
  const r = state.result;
  if (!r || r.status !== "solved") return;

  $("#btn-export").classList.remove("hidden");
  $("#btn-resolve").classList.remove("hidden");
  $("#results-sub").textContent =
    `${r.routes.length} route${r.routes.length === 1 ? "" : "s"}, planned in ${r.solve_seconds}s.`;

  const totalStops = r.routes.reduce(
    (n, rt) => n + rt.stops.filter(s => s.kind === "delivery").length, 0);
  $("#results-totals").innerHTML = `
    <div class="totals">
      <div class="total-item"><div class="k">Total distance</div>
        <div class="v">${r.total_distance} <span style="font-size:.8rem">${esc(r.distance_unit)}</span></div></div>
      <div class="total-item"><div class="k">Deliveries</div><div class="v">${totalStops}</div></div>
      <div class="total-item"><div class="k">Vehicles used</div><div class="v">${r.routes.length}</div></div>
      <div class="total-item"><div class="k">Longest day</div>
        <div class="v">${fmtDur(Math.max(...r.routes.map(x => x.total_duration_minutes), 0))}</div></div>
    </div>`;

  // The map is the nice-to-have; the run sheets are the job. If the map
  // library is missing or a tile server is unreachable, the dispatcher still
  // gets their routes.
  try {
    renderMap(r);
  } catch (err) {
    console.error("Map failed to render", err);
    $("#map").classList.add("hidden");
  }
  renderRunSheets(r);
}

const fmtDur = m => `${Math.floor(m / 60)}h ${String(Math.round(m % 60)).padStart(2, "0")}m`;

function renderMap(r) {
  const el = $("#map");
  if (typeof L === "undefined") {
    el.classList.add("hidden");
    return;
  }
  el.classList.remove("hidden");
  if (!state.map) {
    state.map = L.map("map");
    L.tileLayer("https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png", {
      attribution: '&copy; OpenStreetMap, &copy; CARTO', maxZoom: 19,
    }).addTo(state.map);
  }
  state.layers.forEach(l => state.map.removeLayer(l));
  state.layers = [];

  const bounds = [];
  r.routes.forEach((route, ri) => {
    const color = TRUCK_COLORS[ri % TRUCK_COLORS.length];
    if (route.geometry?.length) {
      const line = L.polyline(route.geometry.map(([lon, lat]) => [lat, lon]),
        { color, weight: 4, opacity: .75 }).addTo(state.map);
      state.layers.push(line);
      bounds.push(...line.getLatLngs());
    }
    route.stops.forEach((s, si) => {
      const isTerminus = s.kind === "start" || s.kind === "end";
      const marker = L.circleMarker([s.latitude, s.longitude], {
        radius: isTerminus ? 7 : 6,
        color: s.kind === "depot" ? "#1E8E5A" : color,
        fillColor: isTerminus ? color : (s.kind === "depot" ? "#1E8E5A" : "#fff"),
        fillOpacity: 1, weight: 3,
      }).addTo(state.map);
      marker.bindPopup(
        `<strong>${esc(route.vehicle_name)}</strong><br>${esc(s.location_name)}<br>` +
        `${toClock(s.arrival_minutes)}${s.kind === "depot" ? " — reload" : ""}`);
      state.layers.push(marker);
      bounds.push([s.latitude, s.longitude]);
    });
  });
  if (bounds.length) state.map.fitBounds(L.latLngBounds(bounds).pad(0.15));
  setTimeout(() => state.map.invalidateSize(), 60);
}

function renderRunSheets(r) {
  const commodityName = id =>
    state.plan.commodities.find(c => c.id === id)?.name || "load";
  const commodityUnit = id =>
    state.plan.commodities.find(c => c.id === id)?.unit || "";

  const sheets = r.routes.map((route, ri) => {
    const color = TRUCK_COLORS[ri % TRUCK_COLORS.length];

    // Capacity per commodity, to scale the load bar.
    const veh = state.plan.vehicles.find(v => v.id === route.vehicle_id);
    const totalCap = state.plan.commodities
      .reduce((n, c) => n + (veh?.capacities?.[c.id] || 0), 0) || 1;

    const legs = route.stops.map((s, i) => {
      const isLate = s.late_by_minutes > 0;
      const classes = ["leg"];
      if (s.kind === "depot") classes.push("is-depot");
      if (s.kind === "start" || s.kind === "end") classes.push("is-terminus");
      if (isLate) classes.push("is-late");

      const delivered = Object.entries(s.delivered || {})
        .filter(([, v]) => v)
        .map(([cid, v]) => `${v.toLocaleString()} ${esc(commodityUnit(cid))} ${esc(commodityName(cid))}`)
        .join(" · ");

      const loadTotal = Object.values(s.load_after || {}).reduce((a, b) => a + b, 0);
      const pct = Math.max(0, Math.min(100, (loadTotal / totalCap) * 100));

      let label = esc(s.location_name);
      if (s.kind === "start") label += " — leave the yard";
      if (s.kind === "end")   label += " — back at base";
      if (s.kind === "depot") label += " — reload";

      return `<div class="${classes.join(" ")}">
        <div class="leg-time">${toClock(s.arrival_minutes)}</div>
        <div class="leg-track"><div class="leg-dot"></div></div>
        <div class="leg-body">
          <div class="leg-name">${label}</div>
          ${s.address ? `<div class="leg-meta">${esc(s.address)}</div>` : ""}
          ${delivered ? `<div class="leg-amounts">Drop ${delivered}</div>` : ""}
          ${isLate ? `<div class="leg-warn">${esc(s.window_warning || "Late")}</div>` : ""}
          ${s.kind !== "end"
            ? `<div class="load-bar" title="Aboard when leaving">
                 <div class="load-fill" style="width:${pct}%"></div></div>` : ""}
        </div>
      </div>`;
    }).join("");

    const dropped = Object.entries(route.delivered_totals || {})
      .filter(([, v]) => v)
      .map(([cid, v]) => `${v.toLocaleString()} ${esc(commodityUnit(cid))}`)
      .join(" · ") || "—";

    return `<article class="run-sheet" style="--truck:${color}">
      <header class="run-head">
        <span class="run-name">${esc(route.vehicle_name)}</span>
        <div class="run-stats">
          <div class="run-stat"><span class="k">Distance</span>
            <span class="v">${route.total_distance} ${esc(route.distance_unit)}</span></div>
          <div class="run-stat"><span class="k">On the road</span>
            <span class="v">${fmtDur(route.total_duration_minutes)}</span></div>
          <div class="run-stat"><span class="k">Delivered</span>
            <span class="v">${dropped}</span></div>
        </div>
      </header>
      <div class="rail">${legs}</div>
    </article>`;
  }).join("");

  const skipped = r.skipped.length ? `
    <div class="panel skipped-panel">
      <h3>Not scheduled</h3>
      ${r.skipped.map(s => `<div class="skipped-item">
        <strong>${esc(s.location_name)}</strong>
        <div class="muted">${esc(s.reason)}</div></div>`).join("")}
    </div>` : "";

  $("#run-sheets").innerHTML = sheets + skipped;
}

async function exportCsv() {
  try {
    const res = await fetch("/api/export/csv", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(state.result),
    });
    if (!res.ok) throw new Error("Export failed");
    const blob = await res.blob();
    downloadBlob(blob, "routes.csv");
  } catch (e) { toast(e.message, true); }
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click(); a.remove();
  URL.revokeObjectURL(url);
}

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------
async function loadSettingsIntoForm() {
  try {
    const s = await api("/api/settings");
    $("#set-org").value = s.organization_name || "";
    $("#set-provider").value = s.routing_provider || "";
    $("#api-token").textContent = s.api_token || "";
  } catch { /* not fatal */ }
}

function wireSettings() {
  $("#save-settings").addEventListener("click", async () => {
    const patch = { organization_name: $("#set-org").value };
    if ($("#set-key").value) patch.routing_api_key = $("#set-key").value;
    if ($("#set-password").value) patch.new_password = $("#set-password").value;
    try {
      await api("/api/settings", { method: "PATCH", body: patch });
      $("#set-key").value = ""; $("#set-password").value = "";
      $("#brand-name").textContent = patch.organization_name;
      toast("Settings saved");
    } catch (e) { toast(e.message, true); }
  });

  $("#copy-token").addEventListener("click", () => {
    navigator.clipboard.writeText($("#api-token").textContent)
      .then(() => toast("Token copied"))
      .catch(() => toast("Couldn't copy — select it manually", true));
  });

  $("#rotate-token").addEventListener("click", async () => {
    if (!confirm("Replace the API token? Any scripts using the old one will stop working.")) return;
    try {
      const s = await api("/api/settings", { method: "PATCH", body: { rotate_api_token: true } });
      $("#api-token").textContent = s.api_token;
      toast("New token issued");
    } catch (e) { toast(e.message, true); }
  });

  $("#export-plan").addEventListener("click", () => {
    downloadBlob(new Blob([JSON.stringify(state.plan, null, 2)],
      { type: "application/json" }), "routeforge-data.json");
  });

  $("#import-plan").addEventListener("click", () => $("#import-file").click());
  $("#import-file").addEventListener("change", async ev => {
    const file = ev.target.files[0];
    if (!file) return;
    try {
      const data = JSON.parse(await file.text());
      if (!confirm("Replace all current data with this file?")) return;
      state.plan = normalisePlan(data);
      await api("/api/plan", { method: "PUT", body: state.plan });
      restorePlanSettings(); renderAll();
      toast("Data restored");
    } catch (e) { toast("That file couldn't be read: " + e.message, true); }
    ev.target.value = "";
  });
}

// ---------------------------------------------------------------------------
// Demo data — a South Jersey fuel run
// ---------------------------------------------------------------------------
function loadDemo() {
  const c = { id: uid(), name: "Diesel", unit: "gallons", minutes_per_unit: 0.02 };
  const c2 = { id: uid(), name: "Gasoline", unit: "gallons", minutes_per_unit: 0.02 };
  const L = (name, lat, lon, address) => ({ id: uid(), name, latitude: lat, longitude: lon, address });

  const yard    = L("Main Yard", 39.4864, -75.0257, "Vineland, NJ");
  const farm    = L("Acme Farm", 39.5100, -75.0800, "Deerfield, NJ");
  const bridge  = L("Bridgeton Works", 39.4276, -75.2340, "Bridgeton, NJ");
  const mill    = L("Millville Shop", 39.4020, -75.0393, "Millville, NJ");
  const hammo   = L("Hammonton Co-op", 39.6362, -74.8024, "Hammonton, NJ");
  const glass   = L("Glassboro Depot", 39.7029, -75.1116, "Glassboro, NJ");

  const v1 = { id: uid(), name: "Truck 1", start_location_id: yard.id,
               capacities: { [c.id]: 3000, [c2.id]: 2000 },
               starting_load: { [c.id]: 3000, [c2.id]: 2000 },
               shift_start_minutes: null, max_shift_minutes: null };
  const v2 = { id: uid(), name: "Truck 2", start_location_id: yard.id,
               capacities: { [c.id]: 2500, [c2.id]: 1500 },
               starting_load: { [c.id]: 2500, [c2.id]: 1500 },
               shift_start_minutes: 8 * 60, max_shift_minutes: 8 * 60 };

  const mkStop = (loc, d1, d2, ws, we, type = "required") => ({
    id: uid(), location_id: loc.id,
    demands: { [c.id]: d1, [c2.id]: d2 },
    window_start_minutes: ws, window_end_minutes: we,
    window_type: type, fixed_service_minutes: 10,
    priority: "should", locked_vehicle_id: null, excluded: false,
  });

  state.plan = {
    commodities: [c, c2],
    locations: [yard, farm, bridge, mill, hammo, glass],
    vehicles: [v1, v2],
    depots: [{ id: uid(), location_id: glass.id, reload_minutes: 40 }],
    stops: [
      mkStop(farm,   900, 400, 7 * 60, 11 * 60),
      mkStop(bridge, 1200, 600, 8 * 60, 13 * 60),
      mkStop(mill,   700, 500, null, null),
      mkStop(hammo,  1100, 800, 9 * 60, 12 * 60, "preferred"),
    ],
    settings: {},
  };
  restorePlanSettings();
  renderAll();
  schedulePlanSave();
  setView("stops");
  toast("Demo day loaded — try Build routes");
}

// ---------------------------------------------------------------------------
if ("serviceWorker" in navigator && location.protocol === "https:") {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}

boot();
})();
