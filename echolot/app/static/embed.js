// One room, live, for the dashboard card (static/echolot-room-card.js,
// app/dashboard.py). The card shows this page in a frame; it tells the
// page light or dark, and the page tells the card how tall it is and when
// Ingress stopped letting it through.
//
// Loaded before plan.js: the plan takes escapeHtml from `Echolot`, which
// on the add-on's own pages is app.js.

window.Echolot = {
  escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  },
};

window.EcholotEmbed = (() => {
  const LIVE_MS = 330;
  const ROOM_MS = 10000;
  const params = new URLSearchParams(location.search);
  const roomId = params.get("room") || "";
  let plan = null;
  let roomJson = "";
  let livePending = false;
  let failures = 0;
  let lastHeight = 0;

  const $ = (id) => document.getElementById(id);
  const number = (v) => Number(v).toLocaleString("de-DE", { maximumFractionDigits: 1 });
  const people = (n) => (n === 1 ? "1 Person" : `${n} Personen`);

  // Only the frame's own parent: the card, on Home Assistant's origin.
  function tellCard(message) {
    if (window.parent !== window) window.parent.postMessage({ source: "echolot-embed", ...message }, location.origin);
  }

  function setTheme(theme) {
    if (theme === "dark" || theme === "light") document.documentElement.dataset.theme = theme;
    else delete document.documentElement.dataset.theme;
  }

  function note(text) {
    const el = $("embed-note");
    el.hidden = !text;
    el.textContent = text || "";
  }

  // As the room page words it (app.js roomStatus).
  function status(live) {
    if (!live) return ["", "…"];
    if (!live.available) return ["warn", "Nicht verfügbar"];
    if (live.count > 0) return ["present", people(live.count)];
    if (live.assumed_present) return ["present", "Vermutlich belegt"];
    if (live.occupied) return ["present", "Belegt"];
    return ["ok", "Leer"];
  }

  async function get(path) {
    const response = await fetch(path, { credentials: "same-origin", cache: "no-store" });
    if (response.status === 401 || response.status === 403) {
      // The Ingress session has run out: the card makes a new one.
      tellCard({ type: "auth" });
      throw new Error("auth");
    }
    if (!response.ok) {
      const error = new Error(`HTTP ${response.status}`);
      error.status = response.status;
      throw error;
    }
    return response.json();
  }

  async function loadRoom() {
    let room;
    try {
      room = await get(`api/rooms/${encodeURIComponent(roomId)}`);
    } catch (err) {
      if (err.status === 404) {
        note("Diesen Raum gibt es im Echolot-Add-on nicht mehr. In der Karte eine andere Raum-ID eintragen.");
        $("embed-chip").className = "chip warn";
        $("embed-chip").textContent = "Unbekannter Raum";
      }
      return;
    }
    const json = JSON.stringify(room);
    if (json === roomJson) return;
    roomJson = json;
    $("embed-name").textContent = room.name;
    document.title = `Echolot · ${room.name}`;
    $("embed-dims").textContent = `${number(room.width)} × ${number(room.height)} m`;
    if (!plan) plan = new Plan.PlanView($("embed-stage"), { mode: "live" });
    plan.setRoom(room);
  }

  async function pollLive() {
    if (document.hidden || livePending || !plan) return;
    livePending = true;
    try {
      const data = await get("api/live");
      const live = data.rooms.find((r) => r.room_id === roomId) || null;
      plan.setLive(live);
      const [kind, text] = status(live);
      $("embed-chip").className = `chip ${kind}`;
      $("embed-chip").textContent = text;
      note(live && !live.available ? live.reason_text : "");
      failures = 0;
    } catch {
      failures += 1;
      if (failures === 10) note("Keine Verbindung zum Echolot-Add-on. Es wird weiter versucht.");
    } finally {
      livePending = false;
    }
  }

  function reportSize() {
    const height = Math.ceil(document.documentElement.getBoundingClientRect().height);
    if (Math.abs(height - lastHeight) < 2) return;
    lastHeight = height;
    tellCard({ type: "size", height });
  }

  function start() {
    setTheme(params.get("theme"));
    window.addEventListener("message", (event) => {
      if (event.source !== window.parent || event.origin !== location.origin) return;
      const data = event.data || {};
      if (data.source === "echolot-card" && data.type === "theme") setTheme(data.theme);
    });
    if (!roomId) {
      note("In der Karte fehlt die Raum-ID (room).");
      return;
    }
    new ResizeObserver(reportSize).observe(document.documentElement);
    loadRoom().then(pollLive);
    setInterval(pollLive, LIVE_MS);
    setInterval(() => { if (!document.hidden) loadRoom(); }, ROOM_MS);
  }

  return { start, status };
})();
