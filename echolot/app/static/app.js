// Echolot UI core: API access, routing, live polling, shared helpers.
//
// Every API path is relative (no leading slash) so it stays under Home
// Assistant's Ingress token prefix. Routes live in the hash for the same
// reason: a second real path would nest those relative URLs a level deep.

const Echolot = (() => {
  const LIVE_MS = 330;
  const DATA_MS = 5000;

  const state = {
    rooms: [],
    devices: [],
    legacy: [],
    live: {},
    info: null,
    loaded: false,
  };

  const ROOM_ICONS = {
    living: "Wohnzimmer", bedroom: "Schlafzimmer", kitchen: "Küche", dining: "Essbereich",
    office: "Arbeitszimmer", bath: "Bad", hall: "Flur", kids: "Kinderzimmer", generic: "Sonstiges",
  };

  function escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function icon(name, cls = "icon") {
    return `<svg class="${cls}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
  }

  function formatNumber(value, digits = 1) {
    return Number(value).toLocaleString("de-DE", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  }

  function formatSeconds(seconds) {
    if (seconds >= 60) {
      const m = Math.floor(seconds / 60);
      const rest = Math.round(seconds % 60);
      return rest ? `${m} min ${rest} s` : `${m} min`;
    }
    return `${Math.round(seconds)} s`;
  }

  function people(n) {
    return n === 1 ? "1 Person" : `${n} Personen`;
  }

  // FastAPI speaks three error shapes: a string, a validation list, and
  // the {message, …} object the room save uses for a conflict.
  function errorText(detail, status) {
    if (!detail) return `Fehler ${status}`;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail.map((e) => {
        const where = (e.loc || []).filter((p) => p !== "body").join(" › ");
        return where ? `${where}: ${e.msg}` : e.msg;
      }).join("\n");
    }
    if (detail.message) return detail.message;
    return JSON.stringify(detail);
  }

  class ApiError extends Error {
    constructor(status, detail) {
      super(errorText(detail, status));
      this.status = status;
      this.detail = detail;
    }
  }

  async function api(path, options = {}) {
    const init = { method: options.method || "GET", headers: {} };
    if (options.body instanceof Blob || options.body instanceof ArrayBuffer) {
      init.body = options.body;
      init.headers["Content-Type"] = options.contentType || options.body.type || "application/octet-stream";
    } else if (options.body !== undefined) {
      init.body = JSON.stringify(options.body);
      init.headers["Content-Type"] = "application/json";
    }
    const response = await fetch(path, init);
    if (response.status === 204) return null;
    let data = null;
    const text = await response.text();
    try { data = text ? JSON.parse(text) : null; } catch { data = text; }
    if (!response.ok) throw new ApiError(response.status, data && data.detail !== undefined ? data.detail : data);
    return data;
  }

  function toast(message, kind = "") {
    const host = document.getElementById("toasts");
    const el = document.createElement("div");
    el.className = `toast ${kind}`;
    el.textContent = message;
    host.appendChild(el);
    setTimeout(() => el.remove(), kind === "err" ? 7000 : 3200);
  }

  function confirmDialog({ title, text, confirm = "OK", cancel = "Abbrechen", danger = false }) {
    return new Promise((resolve) => {
      const wrap = document.createElement("div");
      wrap.className = "dialog-backdrop";
      wrap.innerHTML = `
        <div class="dialog" role="alertdialog" aria-modal="true">
          <h2>${escapeHtml(title)}</h2>
          <p>${escapeHtml(text)}</p>
          <div class="actions">
            <button class="btn" data-a="no">${escapeHtml(cancel)}</button>
            <button class="btn ${danger ? "danger" : "primary"}" data-a="yes">${escapeHtml(confirm)}</button>
          </div>
        </div>`;
      const done = (answer) => { wrap.remove(); document.removeEventListener("keydown", onKey); resolve(answer); };
      const onKey = (e) => { if (e.key === "Escape") done(false); };
      wrap.addEventListener("click", (e) => {
        if (e.target === wrap) done(false);
        const a = e.target.closest("[data-a]");
        if (a) done(a.dataset.a === "yes");
      });
      document.addEventListener("keydown", onKey);
      document.body.appendChild(wrap);
      wrap.querySelector('[data-a="yes"]').focus();
    });
  }

  // A side sheet (bottom sheet on phones). Returns {el, body, close}.
  function openSheet(title, { onClose } = {}) {
    const wrap = document.createElement("div");
    wrap.className = "sheet-backdrop";
    wrap.innerHTML = `
      <section class="sheet" role="dialog" aria-modal="true" aria-label="${escapeHtml(title)}">
        <div class="sheet-head">
          <h2>${escapeHtml(title)}</h2>
          <button class="btn ghost icon-only" data-close aria-label="Schließen">${icon("close")}</button>
        </div>
        <div class="sheet-body"></div>
      </section>`;
    const close = () => {
      wrap.remove();
      document.removeEventListener("keydown", onKey);
      if (onClose) onClose();
    };
    const onKey = (e) => { if (e.key === "Escape" && !document.querySelector(".dialog-backdrop")) close(); };
    wrap.addEventListener("click", (e) => {
      if (e.target === wrap || e.target.closest("[data-close]")) close();
    });
    document.addEventListener("keydown", onKey);
    document.body.appendChild(wrap);
    return { el: wrap, body: wrap.querySelector(".sheet-body"), head: wrap.querySelector(".sheet-head h2"), close };
  }

  // ---------------------------------------------------------------- data

  async function loadData() {
    const [rooms, devices, legacy] = await Promise.all([
      api("api/rooms"), api("api/devices"), api("api/legacy-devices"),
    ]);
    state.rooms = rooms;
    state.devices = devices;
    state.legacy = legacy;
    state.loaded = true;
    renderNav();
    if (current && current.onData) current.onData();
  }

  async function loadInfo() {
    try {
      state.info = await api("api/info");
      const foot = document.getElementById("sidebar-foot");
      foot.textContent = `Version ${state.info.version}`;
    } catch { /* the footer can wait */ }
  }

  let livePending = false;
  async function pollLive() {
    if (document.hidden || livePending) return;
    livePending = true;
    try {
      const data = await api("api/live");
      const next = {};
      for (const r of data.rooms) next[r.room_id] = r;
      state.live = next;
      renderNavLive();
      if (current && current.onLive) current.onLive();
    } catch { /* the next tick tries again */ }
    finally { livePending = false; }
  }

  function roomStatus(roomId) {
    const r = state.live[roomId];
    if (!r) return { kind: "pending", text: "wird ausgewertet…", count: null };
    if (!r.available) return { kind: "off", text: "nicht verfügbar", count: null };
    if (r.count > 0) return { kind: "present", text: people(r.count), count: r.count };
    if (r.occupied) return { kind: "present", text: `hält noch ${formatSeconds(r.hold_remaining)}`, count: 0 };
    return { kind: "empty", text: "niemand da", count: 0 };
  }

  // ---------------------------------------------------------------- nav

  function renderNav() {
    const host = document.getElementById("nav-rooms");
    host.innerHTML = state.rooms.map((room) => `
      <a class="nav-item" href="#/room/${encodeURIComponent(room.id)}" data-nav="room:${escapeHtml(room.id)}">
        ${icon(room.icon)}
        <span class="grow">${escapeHtml(room.name)}</span>
        <span class="nav-count" data-room-count="${escapeHtml(room.id)}"></span>
        <span class="nav-dot" data-room-dot="${escapeHtml(room.id)}"></span>
      </a>`).join("");
    document.getElementById("nav-device-count").textContent = state.devices.length || "";
    renderNavLive();
    markNav();
  }

  function renderNavLive() {
    for (const room of state.rooms) {
      const status = roomStatus(room.id);
      const dot = document.querySelector(`[data-room-dot="${CSS.escape(room.id)}"]`);
      const count = document.querySelector(`[data-room-count="${CSS.escape(room.id)}"]`);
      if (dot) dot.className = `nav-dot ${status.kind === "present" ? "present" : status.kind === "empty" ? "empty" : ""}`;
      if (count) count.textContent = status.count ? String(status.count) : "";
    }
  }

  function markNav() {
    const key = navKey();
    document.querySelectorAll("[data-nav]").forEach((el) => {
      el.classList.toggle("active", el.dataset.nav === key);
    });
  }

  // -------------------------------------------------------------- router

  const views = {};
  let current = null;

  function register(name, view) { views[name] = view; }

  function parseRoute() {
    const hash = location.hash.replace(/^#\/?/, "");
    const parts = hash.split("/").filter(Boolean).map(decodeURIComponent);
    if (!parts.length) return { view: "home", params: {} };
    if (parts[0] === "room" && parts[1]) {
      const sub = { edit: "editor", calibrate: "calibrate" }[parts[2]] || "room";
      return { view: sub, params: { id: parts[1] } };
    }
    if (parts[0] === "new-room") return { view: "newRoom", params: {} };
    if (parts[0] === "devices") return { view: "devices", params: { action: parts[1] } };
    if (parts[0] === "device" && parts[1]) return { view: "devices", params: { open: parts[1] } };
    if (parts[0] === "system") return { view: "system", params: {} };
    return { view: "home", params: {} };
  }

  function navKey() {
    const route = parseRoute();
    if (route.view === "room" || route.view === "editor" || route.view === "calibrate") return `room:${route.params.id}`;
    if (route.view === "newRoom") return "new-room";
    return route.view;
  }

  let lastHash = location.hash;
  async function route() {
    const next = parseRoute();
    if (current && current.canLeave && !(await current.canLeave())) {
      history.replaceState(null, "", lastHash || "#/");
      return;
    }
    if (current && current.unmount) current.unmount();
    lastHash = location.hash;
    const view = views[next.view] || views.home;
    const el = document.getElementById("view");
    el.innerHTML = "";
    current = view;
    markNav();
    view.mount(el, next.params);
    window.scrollTo(0, 0);
  }

  function go(hash) {
    if (location.hash === hash) route();
    else location.hash = hash;
  }

  async function start() {
    window.addEventListener("hashchange", route);
    window.addEventListener("beforeunload", (e) => {
      if (current && current.dirty && current.dirty()) { e.preventDefault(); e.returnValue = ""; }
    });
    try {
      await loadData();
    } catch (err) {
      document.getElementById("view").innerHTML =
        `<div class="notice err"><div class="grow"><strong>Echolot antwortet nicht</strong>${escapeHtml(err.message)}</div></div>`;
      return;
    }
    loadInfo();
    await pollLive();
    route();
    setInterval(pollLive, LIVE_MS);
    setInterval(() => { if (!document.hidden) loadData().catch(() => {}); }, DATA_MS);
  }

  return {
    state, api, ApiError, escapeHtml, icon, toast, confirmDialog, openSheet, formatNumber, formatSeconds,
    people, roomStatus, register, go, start, loadData, loadInfo, ROOM_ICONS,
    refresh: loadData,
  };
})();
