// Echolot, learning by itself: a room's "Echolot lernt" card, the
// activity map on its plan, and the settings on the system page.
// Everything comes from api/rooms/<id>/learning (app/learner.py) and
// changes nothing but through its routes.

const EcholotLearn = (() => {
  const E = Echolot;
  const { escapeHtml, icon, api, toast } = E;
  const RELOAD_MS = 60000;

  function span(seconds) {
    const s = Math.max(0, seconds || 0);
    if (s >= 2 * 86400) return `${Math.floor(s / 86400)} Tage`;
    if (s >= 86400) return `1 Tag ${Math.floor((s - 86400) / 3600)} h`;
    if (s >= 3600) return `${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min`;
    return `${Math.round(s / 60)} min`;
  }

  function ago(t) {
    const s = Date.now() / 1000 - t;
    if (s < 90) return "gerade eben";
    if (s < 3600) return `vor ${Math.round(s / 60)} min`;
    if (s < 86400) return `vor ${Math.round(s / 3600)} h`;
    return new Date(t * 1000).toLocaleString("de-DE", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  }

  const pct = (v) => `${Math.round(v * 100)} %`;

  // What each verdict of the plan check looks like (learning.placement_verdict).
  const VERDICTS = {
    fits: ["ok", "passt"],
    turn: ["warn", "Blickrichtung?"],
    mirror: ["warn", "Seiten vertauscht?"],
    ambiguous: ["warn", "Gang-Test nötig"],
    elsewhere: ["err", "Position prüfen"],
    unclear: ["warn", "Wege außerhalb"],
  };

  const STATES = {
    applied: ["ok", "übernommen"], undone: ["plain", "zurückgenommen"], declined: ["plain", "abgelehnt"],
    done: ["plain", "erledigt"],
  };

  // ------------------------------------------------------------ the map

  // Where people were, as soft blobs under the targets; the most-used
  // place at full strength. Cells come in the sensor's grid, turned
  // into the room: circles, not squares, so the turn does not show.
  function heatSvg(activity) {
    if (!activity || !activity.cells.length) return "";
    const r = activity.cell * 0.6;
    // Blurred by about a cell: one soft field rather than a cell each.
    return `<defs><filter id="pl-heat-blur" x="-20%" y="-20%" width="140%" height="140%">
        <feGaussianBlur stdDeviation="${activity.cell * 0.55}"/></filter></defs>
      <g class="pl-heat" pointer-events="none" filter="url(#pl-heat-blur)">${activity.cells.map(([x, y, w]) =>
      `<circle cx="${x}" cy="${y}" r="${r}" fill-opacity="${(0.16 + 0.84 * w).toFixed(3)}"/>`).join("")}</g>`;
  }

  // A proposed zone, drawn where it would go.
  function zoneSvg(points) {
    if (!points || points.length < 3) return "";
    return `<polygon class="pl-proposal-zone" pointer-events="none" points="${points.map((p) => `${p[0]},${p[1]}`).join(" ")}"/>`;
  }

  // A proposed placement of the sensor, drawn over the plan.
  function placementSvg(plan, placement) {
    if (!placement || !plan) return "";
    const p = { ...plan.room.sensor, ...placement };
    const half = (p.fov_deg || 120) / 2;
    const range = Math.min(p.range_m || 6, 8);
    let d = `M${p.x} ${p.y}`;
    for (let i = 0; i <= 24; i++) {
      const t = ((-half + (2 * half * i) / 24) * Math.PI) / 180;
      const q = window.EcholotGeometry.toRoom(range * Math.sin(t), range * Math.cos(t), { ...p, mirror: false });
      d += ` L${q.x} ${q.y}`;
    }
    return `<g class="pl-proposal" pointer-events="none"><path class="pl-proposal-fov" d="${d} Z"/>${plan.sensorGhostSvg(p)}</g>`;
  }

  // ------------------------------------------------------------ a room

  class RoomCard {
    constructor(host, roomId, { plan } = {}) {
      this.host = host;
      this.roomId = roomId;
      this.plan = plan || null;
      this.view = null;
      this.showMap = false;
      this.preview = null;
      this.busy = false;
      this.journalOpen = false;
      try { this.showMap = localStorage.getItem("echolot-heat") === "1"; } catch { /* per-browser nicety */ }
      this.host.innerHTML = `<div class="card learn-card"><h2>Echolot lernt</h2><p class="hint">Lädt …</p></div>`;
      this.load();
      this.timer = setInterval(() => { if (!document.hidden) this.load(); }, RELOAD_MS);
    }

    destroy() {
      clearInterval(this.timer);
      if (this.plan) this.plan.setExtra("");
    }

    async load() {
      try {
        this.view = await api(`api/rooms/${encodeURIComponent(this.roomId)}/learning`);
      } catch (err) {
        this.host.innerHTML = `<div class="card learn-card"><h2>Echolot lernt</h2><div class="notice err">${escapeHtml(err.message)}</div></div>`;
        return;
      }
      this.render();
    }

    drawPlan() {
      if (!this.plan) return;
      const v = this.view;
      let svg = this.showMap && v && v.activity ? heatSvg(v.activity) : "";
      if (this.preview && this.preview.points) svg += zoneSvg(this.preview.points);
      else if (this.preview) svg += placementSvg(this.plan, this.preview);
      this.plan.setExtra(svg);
    }

    progress(v) {
      const rows = [];
      // Reflectors.
      const spots = v.learned.spots;
      let reflectors;
      if (spots.length) {
        reflectors = `<span class="chip ok">${spots.length} gelernt</span>`;
      } else if (v.presence.home === null || v.presence.home === undefined) {
        reflectors = `<span class="chip warn">braucht Home Assistant</span>`;
      } else if (v.away.chunks < v.needs.chunks) {
        reflectors = `${v.away.chunks} von ${v.needs.chunks} Abwesenheiten${v.away.collecting ? " · sammelt gerade" : ""}`;
      } else {
        reflectors = "keine gefunden";
      }
      rows.push(["Störquellen", reflectors]);
      // Hold times.
      const holds = [];
      if (v.learned.hold_s) holds.push(`Raum ${E.formatSeconds(v.learned.hold_s)}`);
      const zones = (E.state.rooms.find((r) => r.id === this.roomId) || { zones: [] }).zones;
      for (const [id, s] of Object.entries(v.learned.zone_hold_s || {})) {
        const z = zones.find((x) => x.id === id);
        if (z) holds.push(`${escapeHtml(z.name)} ${E.formatSeconds(s)}`);
      }
      rows.push(["Haltezeiten", holds.length ? holds.join(" · ")
        : v.holds && v.holds.room ? "Einstellung reicht" : `sammelt Aussetzer · ${v.stays} Aufenthalte`]);
      // The plan.
      const verdict = v.verdict && v.verdict.verdict;
      let planText;
      if (verdict && VERDICTS[verdict]) {
        const [kind, text] = VERDICTS[verdict];
        planText = `<span class="chip ${kind}">${text}</span>${v.verdict.inside !== null && v.verdict.inside !== undefined ? ` ${pct(v.verdict.inside)} der Wege im Raum` : ""}`;
      } else {
        planText = `sammelt Wege · ${Math.min(v.walks, v.needs.walks)}/${v.needs.walks}, ${Math.min(v.needs.points_now, v.needs.points)}/${v.needs.points} Punkte`;
      }
      rows.push(["Plan", planText]);
      return `<div class="stat-lines learn-lines">${rows.map(([a, b]) =>
        `<div class="stat-line"><span>${a}</span><span>${b}</span></div>`).join("")}</div>`;
    }

    proposalHtml(e) {
      const room = encodeURIComponent(this.roomId);
      const verdict = e.data && e.data.verdict;
      const buttons = [];
      if (e.kind === "placement" && verdict === "elsewhere") {
        buttons.push(`<a class="btn small" href="#/room/${room}/edit/sensor">Im Editor prüfen</a>`);
        buttons.push(`<button class="btn small" data-act="accept" data-id="${e.id}">Erledigt</button>`);
      } else if (e.kind === "placement" && verdict === "unclear") {
        buttons.push(`<a class="btn small" href="#/room/${room}/calibrate">Reichweite begrenzen</a>`);
        buttons.push(`<button class="btn small" data-act="accept" data-id="${e.id}">Erledigt</button>`);
      } else {
        if (e.kind === "placement" && (verdict === "mirror" || verdict === "ambiguous")) {
          buttons.push(`<a class="btn small" href="#/room/${room}/calibrate">Gang-Test</a>`);
        }
        buttons.push(`<button class="btn small primary" data-act="accept" data-id="${e.id}">${icon("check")}Übernehmen</button>`);
      }
      if ((e.kind === "placement" && e.data && e.data.placement) || (e.kind === "zone" && e.data && e.data.zone)) {
        const on = this.preview && this.previewId === e.id;
        buttons.push(`<button class="btn small" data-preview="${e.id}" aria-pressed="${on}">${on ? "Vorschau aus" : "Vorschau"}</button>`);
      }
      buttons.push(`<button class="btn small" data-act="decline" data-id="${e.id}">Ablehnen</button>`);
      return `<div class="learn-proposal">
          <div class="learn-proposal-title">${icon("spark")}<strong>${escapeHtml(e.title)}</strong></div>
          <p class="hint">${escapeHtml(e.detail)}</p>
          <div class="learn-actions">${buttons.join("")}</div></div>`;
    }

    entryHtml(e) {
      const [kind, label] = STATES[e.state] || ["", ""];
      const undo = e.state === "applied" && ["spots", "hold", "placement"].includes(e.kind)
        ? `<button class="btn small" data-act="undo" data-id="${e.id}">${icon("undo")}Rückgängig</button>` : "";
      // The title says what happened; why, and taking it back, one tap away.
      return `<li class="learn-entry"><details>
          <summary><div class="learn-entry-head"><span class="learn-when">${escapeHtml(ago(e.at))}</span>
            ${label ? `<span class="chip ${kind}">${label}${e.auto ? " · selbst" : ""}</span>` : ""}</div>
          <div class="learn-entry-title">${escapeHtml(e.title)}</div></summary>
          ${e.detail ? `<p class="hint">${escapeHtml(e.detail)}</p>` : ""}${undo ? `<div class="learn-actions">${undo}</div>` : ""}
        </details></li>`;
    }

    render() {
      const v = this.view;
      if (!v) return;
      if (!v.active) {
        const why = v.reason === "no_sensor" ? "Sobald dem Raum ein Sensor zugeordnet ist, lernt Echolot ihn kennen."
          : "Lernen ist unter „System“ ausgeschaltet.";
        this.host.innerHTML = `<div class="card learn-card"><div class="learn-head"><h2>Echolot lernt</h2>
          <span class="chip plain">aus</span></div><p class="hint">${why}</p></div>`;
        this.drawPlan();
        return;
      }
      const placesCount = (v.places || []).length;
      const inside = v.verdict && v.verdict.inside;
      const shown = this.journalOpen ? v.journal : v.journal.slice(0, 4);
      this.host.innerHTML = `
        <div class="card learn-card">
          <div class="learn-head"><h2>Echolot lernt</h2>
            <span class="chip ${v.mode === "auto" ? "ok" : "warn"}">${v.mode === "auto" ? "selbstständig" : "nur Vorschläge"}</span></div>
          <p class="hint">Seit ${escapeHtml(ago(v.since))} · ${span(v.observed_s)} beobachtet${v.analysed_at ? ` · ausgewertet ${escapeHtml(ago(v.analysed_at))}` : ""}</p>
          <div class="learn-stats">
            <div><b>${v.walks}</b><span>Wege</span></div>
            <div><b>${placesCount}</b><span>${placesCount === 1 ? "Lieblingsplatz" : "Lieblingsplätze"}</span></div>
            <div><b>${v.learned.spots.length}</b><span>Störquellen</span></div>
            <div><b>${inside !== null && inside !== undefined ? pct(inside) : "–"}</b><span>Plan passt</span></div>
          </div>
          ${this.progress(v)}
          ${v.proposals.length ? `<div class="learn-proposals">${v.proposals.map((e) => this.proposalHtml(e)).join("")}</div>` : ""}
          ${v.away.tainted ? `<p class="hint learn-note">${icon("alert")}${v.away.tainted} Abwesenheit${v.away.tainted === 1 ? "" : "en"} ohne Wert: Während niemand zu Hause war, hat sich etwas bewegt.</p>` : ""}
          <div class="learn-tools">
            <button class="btn small" id="learn-map" aria-pressed="${this.showMap}">${this.showMap ? "Aktivität ausblenden" : "Aktivität zeigen"}</button>
            <button class="btn small" id="learn-now" ${this.busy ? "disabled" : ""}>Jetzt auswerten</button>
          </div>
          ${v.journal.length ? `<h3 class="learn-sub">Tagebuch</h3><ul class="learn-journal">${shown.map((e) => this.entryHtml(e)).join("")}</ul>
            ${v.journal.length > 4 ? `<button class="btn small link" id="learn-more">${this.journalOpen ? "Weniger" : `Alle ${v.journal.length} Einträge`}</button>` : ""}` : ""}
          <button class="btn small link danger-text" id="learn-reset">Neu beginnen</button>
        </div>`;
      this.bind();
      this.drawPlan();
    }

    bind() {
      const el = this.host;
      el.querySelector("#learn-map").addEventListener("click", () => {
        this.showMap = !this.showMap;
        try { localStorage.setItem("echolot-heat", this.showMap ? "1" : "0"); } catch { /* ignore */ }
        this.render();
      });
      el.querySelector("#learn-now").addEventListener("click", () => this.analyse());
      const more = el.querySelector("#learn-more");
      if (more) more.addEventListener("click", () => { this.journalOpen = !this.journalOpen; this.render(); });
      el.querySelector("#learn-reset").addEventListener("click", () => this.reset());
      el.querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", () => this.act(b.dataset.id, b.dataset.act, b)));
      el.querySelectorAll("[data-preview]").forEach((b) => b.addEventListener("click", () => {
        const entry = this.view.proposals.find((e) => e.id === b.dataset.preview);
        const on = this.previewId === entry.id && this.preview;
        this.preview = on ? null : entry.kind === "zone" ? { points: entry.data.zone.points } : entry.data.placement;
        this.previewId = on ? null : entry.id;
        this.render();
      }));
    }

    async act(id, action, button) {
      if (action === "undo" || action === "decline") {
        const entry = [...this.view.proposals, ...this.view.journal].find((e) => e.id === id);
        const ok = await E.confirmDialog({
          title: action === "undo" ? "Zurücknehmen?" : "Ablehnen?",
          text: action === "undo"
            ? `„${entry.title}“ wird zurückgenommen. Echolot schlägt es nicht noch einmal vor.`
            : `„${entry.title}“ wird nicht übernommen, und Echolot schlägt es nicht noch einmal vor.`,
          confirm: action === "undo" ? "Zurücknehmen" : "Ablehnen",
        });
        if (!ok) return;
      }
      if (button) button.disabled = true;
      try {
        const res = await api(`api/rooms/${encodeURIComponent(this.roomId)}/learning/${encodeURIComponent(id)}/${action}`,
          { method: "POST" });
        this.view = res.learning;
        if (this.previewId === id) { this.preview = null; this.previewId = null; }
        toast({ accept: "Übernommen.", decline: "Abgelehnt.", undo: "Zurückgenommen." }[action]);
        this.render();
        await E.refresh();
      } catch (err) {
        if (button) button.disabled = false;
        toast(err.message, "err");
      }
    }

    async analyse() {
      this.busy = true;
      this.render();
      try {
        this.view = await api(`api/rooms/${encodeURIComponent(this.roomId)}/learning/analyse`, { method: "POST" });
        toast("Ausgewertet.");
        await E.refresh();
      } catch (err) {
        toast(err.message, "err");
      }
      this.busy = false;
      this.render();
    }

    async reset() {
      const ok = await E.confirmDialog({
        title: "Neu beginnen?",
        text: "Alles, was Echolot über diesen Raum gelernt hat, wird vergessen — auch gelernte Störquellen und Haltezeiten. Was du selbst eingestellt hast, bleibt.",
        confirm: "Vergessen", danger: true,
      });
      if (!ok) return;
      try {
        await api(`api/rooms/${encodeURIComponent(this.roomId)}/learning`, { method: "DELETE" });
        await E.refresh();
        await this.load();
        toast("Echolot lernt den Raum neu kennen.");
      } catch (err) {
        toast(err.message, "err");
      }
    }
  }

  // ------------------------------------------------------------ settings

  async function systemCard(host) {
    let data;
    try {
      data = await api("api/learning");
    } catch (err) {
      host.innerHTML = `<div class="notice err">${escapeHtml(err.message)}</div>`;
      return;
    }
    const s = data.settings;
    const p = data.presence || {};
    const home = p.home === true ? '<span class="chip present">jemand zu Hause</span>'
      : p.home === false ? `<span class="chip ok">niemand zu Hause${p.away_s ? ` · ${span(p.away_s)}` : ""}</span>`
        : '<span class="chip warn">unbekannt</span>';
    const source = p.source === "person" ? `${p.persons ?? 0} Personen, ${p.persons_home ?? 0} zu Hause`
      : p.source ? escapeHtml(p.source) : "—";
    const proposals = Object.values(data.rooms || {}).reduce((n, r) => n + (r.proposals || 0), 0);
    host.innerHTML = `
      <h2>Selbstlernen</h2>
      <p class="hint">Echolot beobachtet jeden Raum und lernt daraus: Störquellen, wie lange Sitzende aus dem Blick geraten, ob der Plan zu den Wegen passt, wo Zonen fehlen.${proposals ? ` <b>${proposals} Vorschlag${proposals === 1 ? "" : "e"} offen.</b>` : ""}</p>
      <div class="segmented fill" id="learn-mode" style="margin-top:12px">
        <button data-mode="auto" class="${s.mode === "auto" ? "active" : ""}">Selbstständig</button>
        <button data-mode="suggest" class="${s.mode === "suggest" ? "active" : ""}">Nur vorschlagen</button>
        <button data-mode="off" class="${s.mode === "off" ? "active" : ""}">Aus</button></div>
      <p class="hint" style="margin-top:8px">${{
        auto: "Übernimmt, was eindeutig ist, und schlägt den Rest vor. Alles steht im Tagebuch des Raums und lässt sich zurücknehmen.",
        suggest: "Ändert nichts selbst — alles wartet auf dein „Übernehmen“.",
        off: "Beobachtet nichts; Gelerntes gilt nicht.",
      }[s.mode]}</p>
      <div class="stat-lines">
        <div class="stat-line"><span>Anwesenheit laut Home Assistant</span><span>${home}</span></div>
        <div class="stat-line"><span>Quelle</span><span>${source}</span></div>
      </div>
      ${p.error ? `<p class="hint warn-text" style="margin-top:8px">${escapeHtml(p.error)}</p>` : ""}
      <label class="field" style="margin-top:12px"><span>Statt aller Personen: eine Entität</span>
        <input id="learn-entity" type="text" inputmode="text" autocomplete="off" spellcheck="false"
          placeholder="z. B. zone.home oder binary_sensor.jemand_da" value="${escapeHtml(s.presence_entity || "")}"></label>
      <p class="hint">Störquellen lernt Echolot nur, solange niemand zu Hause ist — und nur, wenn sich dabei nirgends etwas bewegt.</p>`;
    host.querySelectorAll("[data-mode]").forEach((b) => b.addEventListener("click", async () => {
      if (b.dataset.mode === s.mode) return;
      if (b.dataset.mode === "off") {
        const ok = await E.confirmDialog({
          title: "Selbstlernen ausschalten?",
          text: "Gelernte Störquellen und Haltezeiten gelten dann nicht mehr, und Echolot beobachtet nichts. Was es schon weiß, bleibt gespeichert.",
          confirm: "Ausschalten",
        });
        if (!ok) return;
      }
      await save({ mode: b.dataset.mode });
    }));
    const entity = host.querySelector("#learn-entity");
    entity.addEventListener("change", () => save({ presence_entity: entity.value.trim() || null }));

    async function save(change) {
      try {
        await api("api/learning/settings", { method: "PUT", body: change });
        toast("Gespeichert.");
        await systemCard(host);
      } catch (err) {
        toast(err.message, "err");
      }
    }
  }

  return { RoomCard, systemCard, heatSvg };
})();
