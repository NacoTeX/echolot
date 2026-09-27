// Recordings of what a room's sensor says (app/recording.py): start one,
// say while it runs what is really going on, and play it back later
// through the same engine (app/replay.py) — to see what the engine did,
// and, where the marks say what was true, how far off it was.

(() => {
  const E = Echolot;
  const { escapeHtml, icon, api, toast, state } = E;
  const POLL_MS = 1000;
  const DURATIONS = [60, 300, 600, 1800, 3600];
  const UNCERTAINTIES = [[0.1, "±10 cm"], [0.25, "±25 cm"], [0.5, "±50 cm"]];
  const SPEEDS = [1, 4, 16];

  const cm = (m) => `${Math.round(m * 100)} cm`;
  const pct = (v) => `${Math.round(v * 100)} %`;
  const seconds = (s) => `${E.formatNumber(s, 1)} s`;

  function clock(s) {
    const total = Math.max(0, Math.round(s));
    return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
  }

  function size(bytes) {
    return bytes >= 1048576 ? `${E.formatNumber(bytes / 1048576, 1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;
  }

  function when(ts) {
    return new Date(ts * 1000).toLocaleString("de-DE", { dateStyle: "medium", timeStyle: "short" });
  }

  const MARK_TEXT = {
    standpoint: (m) => `Steht bei ${E.formatNumber(m.x, 2)} / ${E.formatNumber(m.y, 2)} m (±${cm(m.uncertainty_m)})`,
    standpoint_end: () => "Standpunkt verlassen",
    people: (m) => `${m.count === 1 ? "1 Person" : `${m.count} Personen`} im Raum`,
    zone: (m, zones) => `${escapeHtml((zones[m.zone_id] || {}).name || m.zone_id)} ${m.inside ? "betreten" : "verlassen"}`,
    note: (m) => `Notiz: ${escapeHtml(m.text)}`,
  };

  const view = {
    mount(el, params) {
      this.el = el;
      this.id = params.id;
      this.status = null;
      this.list = [];
      this.usage = null;
      this.picked = null; // [x, y] tapped on the plan, for a standpoint
      this.inside = {}; // zone id -> true/false as marked in this recording
      this.player = null;
      this.render();
      this.refresh();
    },
    unmount() {
      clearTimeout(this.timer);
      this.timer = null;
      this.stopPlayer();
      if (this.plan) this.plan.destroy();
      this.plan = null;
    },
    room() { return state.rooms.find((r) => r.id === this.id) || null; },
    onLive() {
      if (this.plan && !this.player) this.plan.setLive(state.live[this.id] || null);
    },
    onData() {
      const room = this.room();
      if (this.plan && room && !this.player) this.plan.setRoom(room);
    },

    async refresh() {
      clearTimeout(this.timer);
      try {
        const [listing, status] = await Promise.all([
          api("api/recordings"),
          api(`api/rooms/${encodeURIComponent(this.id)}/recording`),
        ]);
        this.list = listing.recordings;
        this.usage = listing.usage;
        const was = this.status;
        this.status = status || null;
        if (!this.status) { this.inside = {}; this.picked = null; }
        if (!was || !this.status || was.id !== this.status.id) this.renderSide();
        else this.updateRunning();
      } catch (err) {
        toast(err.message, "err");
      }
      if (this.status) this.timer = setTimeout(() => this.refresh(), POLL_MS);
      this.drawExtra();
    },

    render() {
      this.unmount();
      const room = this.room();
      if (!room) {
        this.el.innerHTML = `<div class="empty-state card pad">Diesen Raum gibt es nicht mehr. <a href="#/">Zur Übersicht</a></div>`;
        return;
      }
      this.el.innerHTML = `
        <div class="page-head">
          <div><div class="eyebrow">Aufzeichnen</div><h1>${escapeHtml(room.name)}</h1></div>
          <div class="head-actions">
            <a class="btn primary" href="#/room/${encodeURIComponent(room.id)}">${icon("check")}Fertig</a>
          </div>
        </div>
        <div class="room-layout">
          <section class="card plan-card">
            <div class="plan-top"><span id="rec-chip" class="chip">Live</span><span class="dims" id="rec-top"></span></div>
            <div class="editor-hint" id="rec-hint"></div>
            <div class="plan-stage" id="rec-stage"></div>
            <div id="rec-player"></div>
          </section>
          <aside class="side" id="rec-side"></aside>
        </div>`;
      this.plan = new Plan.PlanView(this.el.querySelector("#rec-stage"), {
        mode: "live",
        onPick: (p) => {
          if (!this.status || this.player) return;
          this.picked = p;
          this.drawExtra();
          this.updateRunning();
        },
      });
      this.plan.setRoom(room);
      this.plan.setLive(state.live[room.id] || null);
      this.renderSide();
    },

    hint(text) {
      const el = this.el.querySelector("#rec-hint");
      if (el) el.textContent = text || "";
    },

    // Where somebody said they stand, and the spot just tapped.
    drawExtra() {
      if (!this.plan) return;
      let svg = "";
      const open = this.status && this.status.standpoint;
      if (open && !this.player) {
        svg += `<g class="cal-mark check"><circle cx="${open.x}" cy="${open.y}" r="0.16"/></g>`;
      }
      if (this.picked && !this.player) {
        svg += `<g class="cal-mark pending"><circle cx="${this.picked[0]}" cy="${this.picked[1]}" r="0.14"/></g>`;
      }
      if (this.player) {
        const t = this.player.t;
        const stand = [...this.player.marks].reverse().find((m) => m.t <= t && (m.kind === "standpoint" || m.kind === "standpoint_end"));
        if (stand && stand.kind === "standpoint") {
          svg += `<g class="cal-mark check"><circle cx="${stand.x}" cy="${stand.y}" r="${Math.max(0.1, stand.uncertainty_m)}"/></g>`;
        }
      }
      this.plan.setExtra(svg);
    },

    // ----------------------------------------------------------- side

    renderSide() {
      const host = this.el.querySelector("#rec-side");
      if (!host) return;
      const room = this.room();
      if (!room) return;
      host.innerHTML = (this.status ? this.runningCard(room) : this.startCard(room)) + this.listCard();
      this.bindSide(host);
      this.hint(this.status ? "Tippe auf den Plan, wo du stehst — dann „Ich stehe hier“." : "");
    },

    startCard(room) {
      const noSensor = !room.sensor.device_id;
      const use = this.usage;
      return `<div class="card"><h2>Neue Aufzeichnung</h2>
        <p class="hint" style="margin-bottom:12px">Hält fest, was der Sensor meldet — jede Zeile, wie sie ankommt —, dazu die Einstellungen des Raums. Keine Schlüssel, kein WLAN. Später lässt sie sich durch dieselbe Auswertung abspielen, auch mit anderen Einstellungen.</p>
        ${noSensor ? `<div class="notice warn">${icon("alert")}<div class="grow">Diesem Raum ist kein Sensor zugeordnet.</div></div>` : `
        <div class="field"><span>Dauer in Minuten, dann endet sie von selbst</span><div class="segmented fill">
          ${DURATIONS.map((s) => `<button type="button" data-limit="${s}" class="${s === (this.limit || 600) ? "active" : ""}">${s / 60}</button>`).join("")}</div></div>
        <label class="field"><span>Notiz</span><input id="rec-note" maxlength="200" placeholder="z. B. Sofa, abends, Licht aus"></label>
        <div class="actions"><button class="btn primary" id="rec-start">${icon("record")}Aufzeichnung starten</button></div>`}
        ${use ? `<p class="hint" style="margin-top:10px">${size(use.bytes_used)} von ${size(use.bytes_max)} belegt · ${use.count} von ${use.count_max} Aufzeichnungen</p>` : ""}
      </div>`;
    },

    runningCard(room) {
      const zones = room.zones.filter((z) => z.kind === "detect");
      return `<div class="card rec-running"><h2>${icon("record")} Zeichnet auf</h2>
        <div class="cal-progress"><div class="bar"><i id="rec-bar"></i></div>
          <div class="meta"><span id="rec-time"></span><span id="rec-counts"></span></div></div>
        <p class="hint" id="rec-fresh"></p>
        <h3 style="margin-top:14px">Standpunkt</h3>
        <p class="hint" id="rec-stand"></p>
        <div class="row2">
          <label class="field"><span>Genauigkeit der Markierung</span><select id="rec-unc">
            ${UNCERTAINTIES.map(([v, label]) => `<option value="${v}" ${v === 0.25 ? "selected" : ""}>${label}</option>`).join("")}</select></label>
        </div>
        <div class="actions">
          <button class="btn" id="rec-stand-on">${icon("target")}Ich stehe hier</button>
          <button class="btn" id="rec-stand-off">Standpunkt verlassen</button>
        </div>
        <h3 style="margin-top:14px">Personen im Raum</h3>
        <div class="segmented fill" id="rec-people">
          ${[0, 1, 2, 3, 4].map((n) => `<button type="button" data-people="${n}">${n}</button>`).join("")}</div>
        ${zones.length ? `<h3 style="margin-top:14px">Zonen</h3>
          <div class="rec-zones">${zones.map((z) => `
            <div class="rec-zone"><span class="grow">${escapeHtml(z.name)}</span>
              <div class="segmented"><button type="button" data-zone="${escapeHtml(z.id)}" data-inside="1">betreten</button>
              <button type="button" data-zone="${escapeHtml(z.id)}" data-inside="0">verlassen</button></div></div>`).join("")}</div>` : ""}
        <h3 style="margin-top:14px">Notiz</h3>
        <div class="actions" style="flex-wrap:nowrap"><input class="grow" id="rec-text" maxlength="200" placeholder="Was gerade passiert" style="flex:1;min-width:0">
          <button class="btn" id="rec-note-add">Merken</button></div>
        <div class="actions" style="margin-top:16px"><button class="btn danger" id="rec-stop">Aufzeichnung beenden</button></div>
      </div>`;
    },

    updateRunning() {
      const s = this.status;
      if (!s) return;
      const bar = this.el.querySelector("#rec-bar");
      if (!bar) return;
      bar.style.width = `${Math.min(100, (s.elapsed_s / s.limit_s) * 100).toFixed(1)}%`;
      this.el.querySelector("#rec-time").textContent = `${clock(s.elapsed_s)} / ${clock(s.limit_s)}`;
      this.el.querySelector("#rec-counts").textContent = `${s.lines} Zeilen · ${s.marks} Markierungen`;
      const live = state.live[this.id];
      this.el.querySelector("#rec-fresh").textContent = live && !live.available ? `Gerade nichts vom Sensor: ${live.reason_text}` : "";
      const stand = this.el.querySelector("#rec-stand");
      stand.textContent = s.standpoint
        ? `Steht bei ${E.formatNumber(s.standpoint.x, 2)} / ${E.formatNumber(s.standpoint.y, 2)} m seit ${clock(s.elapsed_s - s.standpoint.since)}.`
        : this.picked ? `Gewählt: ${E.formatNumber(this.picked[0], 2)} / ${E.formatNumber(this.picked[1], 2)} m.` : "Auf den Plan tippen, wo du stehst.";
      this.el.querySelector("#rec-stand-on").disabled = !this.picked;
      this.el.querySelector("#rec-stand-off").disabled = !s.standpoint;
      this.el.querySelectorAll("[data-people]").forEach((b) => b.classList.toggle("active", Number(b.dataset.people) === s.people));
      this.el.querySelectorAll("[data-zone]").forEach((b) => {
        const inside = this.inside[b.dataset.zone];
        b.classList.toggle("active", inside !== undefined && inside === (b.dataset.inside === "1"));
      });
    },

    listCard() {
      const rows = this.list.map((r) => {
        const chips = [
          r.state === "recording" ? '<span class="chip present">läuft</span>' : "",
          r.state === "interrupted" ? `<span class="chip warn">unterbrochen</span>` : "",
          r.synthetic ? '<span class="chip warn">synthetisch</span>' : "",
          r.imported ? '<span class="chip plain">importiert</span>' : "",
          r.room_id !== this.id ? `<span class="chip plain">${escapeHtml(r.room_name || "anderer Raum")}</span>` : "",
        ].join("");
        const facts = [clock(r.duration_s), `${r.lines} Zeilen`, r.marks ? `${r.marks} Markierungen` : "ohne Markierungen", size(r.bytes)];
        return `<div class="rec-item" data-id="${escapeHtml(r.id)}">
          <div class="grow"><div class="name">${escapeHtml(when(r.started_at))}${r.note ? ` · ${escapeHtml(r.note)}` : ""}</div>
            <div class="meta">${facts.join(" · ")}${r.stop_reason && r.state !== "recording" ? ` · ${escapeHtml(r.stop_reason)}` : ""}</div>
            <div class="chips" style="margin-top:4px">${chips}</div></div>
          ${r.state === "recording" ? "" : `<div class="actions rec-actions">
            <button class="btn small" data-report="${escapeHtml(r.id)}">Auswerten</button>
            <button class="btn small" data-play="${escapeHtml(r.id)}">${icon("play")}Abspielen</button>
            <a class="btn small" href="api/recordings/${encodeURIComponent(r.id)}/export" download>${icon("download")}</a>
            <button class="btn small danger" data-delete="${escapeHtml(r.id)}" aria-label="Löschen">${icon("trash")}</button></div>`}
        </div>`;
      }).join("");
      return `<div class="card"><h2>Aufzeichnungen</h2>
        ${rows || '<p class="hint">Noch keine. Eine Aufzeichnung macht Einstellungen vergleichbar: dieselben Meldungen, andere Regeln.</p>'}
        <div class="actions" style="margin-top:12px"><label class="btn">${icon("upload")}Importieren<input type="file" id="rec-import" accept=".jsonl,.ndjson,application/x-ndjson,text/plain" hidden></label></div>
      </div>`;
    },

    bindSide(host) {
      const on = (sel, ev, fn) => host.querySelectorAll(sel).forEach((b) => b.addEventListener(ev, fn));
      on("[data-limit]", "click", (e) => {
        this.limit = Number(e.currentTarget.dataset.limit);
        host.querySelectorAll("[data-limit]").forEach((b) => b.classList.toggle("active", b === e.currentTarget));
      });
      on("#rec-start", "click", () => this.start());
      on("#rec-stop", "click", () => this.stop());
      on("#rec-stand-on", "click", () => this.mark({
        kind: "standpoint", x: this.picked[0], y: this.picked[1],
        uncertainty_m: Number(host.querySelector("#rec-unc").value),
      }, () => { this.picked = null; }));
      on("#rec-stand-off", "click", () => this.mark({ kind: "standpoint_end" }));
      on("[data-people]", "click", (e) => this.mark({ kind: "people", count: Number(e.currentTarget.dataset.people) }));
      on("[data-zone]", "click", (e) => {
        const zone = e.currentTarget.dataset.zone, inside = e.currentTarget.dataset.inside === "1";
        this.mark({ kind: "zone", zone_id: zone, inside }, () => { this.inside[zone] = inside; });
      });
      on("#rec-note-add", "click", () => {
        const input = host.querySelector("#rec-text");
        if (!input.value.trim()) return;
        this.mark({ kind: "note", text: input.value.trim() }, () => { input.value = ""; });
      });
      on("[data-report]", "click", (e) => this.openReport(e.currentTarget.dataset.report));
      on("[data-play]", "click", (e) => this.play(e.currentTarget.dataset.play));
      on("[data-delete]", "click", (e) => this.remove(e.currentTarget.dataset.delete));
      on("#rec-import", "change", (e) => this.importFile(e.currentTarget.files[0]));
      this.updateRunning();
    },

    async start() {
      const note = (this.el.querySelector("#rec-note") || {}).value || "";
      try {
        this.status = await api(`api/rooms/${encodeURIComponent(this.id)}/recording`, {
          method: "POST", body: { limit_s: this.limit || 600, note },
        });
        this.inside = {};
        toast("Aufzeichnung läuft.");
      } catch (err) { toast(err.message, "err"); }
      await this.refresh();
      this.renderSide();
    },

    async stop() {
      try {
        await api(`api/rooms/${encodeURIComponent(this.id)}/recording/stop`, { method: "POST" });
        toast("Aufzeichnung beendet.");
      } catch (err) { toast(err.message, "err"); }
      this.status = null;
      await this.refresh();
      this.renderSide();
    },

    async mark(body, done) {
      try {
        await api(`api/rooms/${encodeURIComponent(this.id)}/recording/marks`, { method: "POST", body });
        if (done) done();
      } catch (err) { toast(err.message, "err"); }
      await this.refresh();
    },

    async remove(id) {
      const ok = await E.confirmDialog({ title: "Aufzeichnung löschen?", text: "Sie lässt sich nicht wiederherstellen.", confirm: "Löschen", danger: true });
      if (!ok) return;
      try { await api(`api/recordings/${encodeURIComponent(id)}`, { method: "DELETE" }); } catch (err) { toast(err.message, "err"); }
      if (this.player && this.player.id === id) this.stopPlayer();
      await this.refresh();
      this.renderSide();
    },

    async importFile(file) {
      if (!file) return;
      try {
        await api("api/recordings/import", { method: "POST", body: file, contentType: "application/x-ndjson" });
        toast("Aufzeichnung importiert.");
      } catch (err) { toast(err.message, "err"); }
      await this.refresh();
      this.renderSide();
    },

    // ----------------------------------------------------------- report

    variantsFor(entry, choice) {
      const recorded = { label: "Wie aufgezeichnet", base: "recorded" };
      if (choice === "current") return [recorded, { label: "Heutige Einstellungen", base: "current" }];
      if (choice === "custom") return [recorded, { label: "Geänderte Filter", base: "recorded", settings: this.custom }];
      return [recorded];
    },

    async openReport(id) {
      const entry = this.list.find((r) => r.id === id);
      if (!entry) return;
      const sheet = E.openSheet("Auswertung");
      this.custom = this.custom || { confirm_s: 1, smoothing: "normal" };
      const roomExists = state.rooms.some((r) => r.id === entry.room_id);
      const draw = async (choice) => {
        sheet.body.innerHTML = `<p class="hint">Wird abgespielt…</p>`;
        let result;
        try {
          result = await api(`api/recordings/${encodeURIComponent(id)}/replay`, { method: "POST", body: { variants: this.variantsFor(entry, choice) } });
        } catch (err) {
          sheet.body.innerHTML = `<div class="notice err"><div class="grow">${escapeHtml(err.message)}</div></div>`;
          return;
        }
        sheet.body.innerHTML = this.reportHtml(entry, result, choice, roomExists);
        sheet.body.querySelectorAll("[data-compare]").forEach((b) => b.addEventListener("click", () => draw(b.dataset.compare)));
        const confirm = sheet.body.querySelector("#rep-confirm"), smooth = sheet.body.querySelector("#rep-smooth");
        if (confirm) confirm.addEventListener("change", () => { this.custom.confirm_s = Number(confirm.value); });
        if (smooth) smooth.addEventListener("change", () => { this.custom.smoothing = smooth.value; });
      };
      draw("none");
    },

    reportHtml(entry, result, choice, roomExists) {
      const variants = result.variants;
      const zones = Object.fromEntries((result.room.zones || []).map((z) => [z.id, z]));
      const head = variants.map((v) => `<th>${escapeHtml(v.label)}</th>`).join("");
      const row = (label, get, fmt) => `<tr><td>${label}</td>${variants.map((v) => {
        const value = get(v.report);
        return `<td>${value === null || value === undefined ? "—" : fmt(value)}</td>`;
      }).join("")}</tr>`;
      const section = (title) => `<tr class="rep-section"><td colspan="${variants.length + 1}">${title}</td></tr>`;
      const r0 = variants[0].report;
      let rows = section("Was die Auswertung tat")
        + row("Dauer", (r) => r.summary.duration_s, clock)
        + row("Neue Meldungen je Sekunde", (r) => r.summary.reports_per_s, (v) => E.formatNumber(v, 1))
        + row("Verfügbar", (r) => r.summary.available_share, pct)
        + row("Wechsel der Personenzahl", (r) => r.summary.count_changes, String)
        + row("Wechsel belegt/frei", (r) => r.summary.occupied_changes, String);
      if (r0.standpoints) {
        rows += section(`Standpunkte · Referenz ±${cm(r0.standpoints.reference_uncertainty_m)}`)
          + row("Abstand, Median", (r) => r.standpoints && r.standpoints.median_error_m, cm)
          + row("Abstand, 95 %", (r) => r.standpoints && r.standpoints.p95_error_m, cm)
          + row("Streuung im Stillstand", (r) => r.standpoints && r.standpoints.jitter_m, cm)
          + row("Erkannt", (r) => r.standpoints && r.standpoints.detected_share, pct)
          + row("Ziel-ID gewechselt", (r) => r.standpoints && r.standpoints.id_changes, String);
      }
      if (r0.people) {
        rows += section("Personen im Raum")
          + row("Anzahl richtig", (r) => r.people && r.people.count_right_share, pct)
          + row("Belegt, obwohl leer", (r) => r.people && r.people.occupied_while_empty_s, seconds)
          + row("Leer, obwohl belegt", (r) => r.people && r.people.empty_while_occupied_s, seconds);
      }
      for (const zoneId of Object.keys(r0.zones || {})) {
        const name = escapeHtml((zones[zoneId] || {}).name || zoneId);
        const z = (r) => r.zones && r.zones[zoneId];
        rows += section(`Zone ${name}`)
          + row("Wechsel: gemessen / wirklich", (r) => z(r) && `${z(r).measured_changes} / ${z(r).reference_changes}`, String)
          + row("Zusätzliche Wechsel", (r) => z(r) && z(r).extra_changes, String)
          + row("Verzögerung hinein", (r) => z(r) && z(r).enter_latency_s.length ? z(r).enter_latency_s : null, (v) => v.map((x) => (x === null ? "nie" : seconds(x))).join(", "))
          + row("Verzögerung hinaus", (r) => z(r) && z(r).leave_latency_s.length ? z(r).leave_latency_s : null, (v) => v.map((x) => (x === null ? "nie" : seconds(x))).join(", "));
      }
      return `
        ${entry.synthetic ? '<div class="notice warn">' + icon("alert") + '<div class="grow"><strong>Synthetische Aufzeichnung.</strong> Von einem Programm erzeugt, nicht von einem Sensor gemessen.</div></div>' : ""}
        <p class="hint">${escapeHtml(when(entry.started_at))} · ${escapeHtml(entry.room_name || "")} · aufgezeichnet mit Messdefinition ${escapeHtml(String(result.header.definition ?? "?"))}</p>
        ${r0.reference ? "" : '<p class="hint" style="margin-top:8px">Ohne Markierungen gibt es keine Fehlerwerte — nur, was die Auswertung getan hat. Wer beim Aufzeichnen Standpunkte, Personenzahl oder Zonen markiert, bekommt hier Abstände, Verzögerungen und Fehlbelegungen.</p>'}
        <div class="rep-wrap"><table class="rep-table"><thead><tr><th></th>${head}</tr></thead><tbody>${rows}</tbody></table></div>
        <h3 style="margin-top:16px">Vergleichen mit</h3>
        <div class="actions">
          ${roomExists ? `<button class="btn ${choice === "current" ? "primary" : ""}" data-compare="current">Heutigen Einstellungen</button>` : ""}
          <button class="btn ${choice === "custom" ? "primary" : ""}" data-compare="custom">Geänderten Filtern</button>
          ${choice !== "none" ? '<button class="btn" data-compare="none">Nur wie aufgezeichnet</button>' : ""}
        </div>
        <div class="row2" style="margin-top:10px">
          <label class="field"><span>Bestätigungszeit</span><select id="rep-confirm">${[0, 0.5, 1, 2, 3, 5].map((v) => `<option value="${v}" ${v === this.custom.confirm_s ? "selected" : ""}>${E.formatNumber(v, 1)} s</option>`).join("")}</select></label>
          <label class="field"><span>Glättung</span><select id="rep-smooth">${[["off", "Aus"], ["normal", "Normal"], ["strong", "Stark"]].map(([k, l]) => `<option value="${k}" ${k === this.custom.smoothing ? "selected" : ""}>${l}</option>`).join("")}</select></label>
        </div>
        <p class="hint">Dieselben Meldungen, durch dieselbe Auswertung — nur die Regeln unterscheiden sich. Ein Vergleich gilt für diese Aufzeichnung, nicht für jeden Raum und jede Tageszeit.</p>`;
    },

    // ----------------------------------------------------------- playback

    async play(id) {
      const entry = this.list.find((r) => r.id === id);
      if (!entry) return;
      this.stopPlayer();
      let result;
      try {
        result = await api(`api/recordings/${encodeURIComponent(id)}/replay`, { method: "POST", body: { timeline: true } });
      } catch (err) { toast(err.message, "err"); return; }
      const timeline = result.variants[0].timeline || [];
      if (!timeline.length) { toast("Die Aufzeichnung ist leer.", "err"); return; }
      this.player = {
        id, entry, timeline, marks: result.marks, zones: Object.fromEntries((result.room.zones || []).map((z) => [z.id, z])),
        t: 0, speed: 1, playing: false, index: -1, end: timeline[timeline.length - 1].t, last: null,
      };
      this.plan.setRoom(result.room);
      this.renderPlayer();
      this.seek(0);
      this.drawExtra();
      this.hint("");
    },

    stopPlayer() {
      if (!this.player) return;
      cancelAnimationFrame(this.player.frame);
      this.player = null;
      const host = this.el && this.el.querySelector("#rec-player");
      if (host) host.innerHTML = "";
      const room = this.room();
      if (this.plan && room) {
        this.plan.setRoom(room);
        this.plan.setLive(state.live[this.id] || null);
      }
      const chip = this.el && this.el.querySelector("#rec-chip");
      if (chip) { chip.className = "chip"; chip.textContent = "Live"; }
      this.drawExtra();
      this.hint(this.status ? "Tippe auf den Plan, wo du stehst — dann „Ich stehe hier“." : "");
    },

    renderPlayer() {
      const p = this.player;
      const host = this.el.querySelector("#rec-player");
      host.innerHTML = `<div class="rec-player">
        <div class="actions" style="flex-wrap:nowrap">
          <button class="btn" id="pl-play">${icon("play")}</button>
          <input type="range" id="pl-seek" min="0" max="${p.end}" step="0.1" value="0" class="grow" style="flex:1;min-width:0">
          <span class="rec-clock" id="pl-clock"></span>
        </div>
        <div class="actions" style="justify-content:space-between">
          <div class="segmented">${SPEEDS.map((s) => `<button type="button" data-speed="${s}" class="${s === p.speed ? "active" : ""}">${s}×</button>`).join("")}</div>
          <button class="btn" id="pl-close">Zurück zu live</button>
        </div>
        <div class="rec-marks">${p.marks.map((m) => `<button type="button" class="rec-mark" data-seek="${m.t}">
          <span class="rec-clock">${clock(m.t)}</span> ${MARK_TEXT[m.kind](m, p.zones)}</button>`).join("") || '<p class="hint">Ohne Markierungen aufgezeichnet.</p>'}</div>
      </div>`;
      host.querySelector("#pl-play").addEventListener("click", () => this.toggle());
      host.querySelector("#pl-close").addEventListener("click", () => this.stopPlayer());
      const seek = host.querySelector("#pl-seek");
      seek.addEventListener("input", () => this.seek(Number(seek.value)));
      host.querySelectorAll("[data-speed]").forEach((b) => b.addEventListener("click", () => {
        p.speed = Number(b.dataset.speed);
        host.querySelectorAll("[data-speed]").forEach((x) => x.classList.toggle("active", x === b));
      }));
      host.querySelectorAll("[data-seek]").forEach((b) => b.addEventListener("click", () => this.seek(Number(b.dataset.seek))));
      const chip = this.el.querySelector("#rec-chip");
      chip.className = "chip warn";
      chip.textContent = p.entry.synthetic ? "Wiedergabe · synthetisch" : "Wiedergabe";
    },

    toggle() {
      const p = this.player;
      if (!p) return;
      p.playing = !p.playing;
      if (p.playing && p.t >= p.end) this.seek(0);
      this.el.querySelector("#pl-play").innerHTML = icon(p.playing ? "pause" : "play");
      if (p.playing) {
        p.last = null;
        const step = (now) => {
          if (!this.player || !p.playing) return;
          if (p.last !== null) this.seek(Math.min(p.end, p.t + ((now - p.last) / 1000) * p.speed));
          p.last = now;
          if (p.t >= p.end) { p.playing = false; this.el.querySelector("#pl-play").innerHTML = icon("play"); return; }
          p.frame = requestAnimationFrame(step);
        };
        p.frame = requestAnimationFrame(step);
      } else {
        cancelAnimationFrame(p.frame);
      }
    },

    // The evaluation shown at time t: the last one at or before it.
    seek(t) {
      const p = this.player;
      if (!p) return;
      p.t = Math.max(0, Math.min(p.end, t));
      let lo = 0, hi = p.timeline.length - 1;
      while (lo < hi) {
        const mid = (lo + hi + 1) >> 1;
        if (p.timeline[mid].t <= p.t) lo = mid; else hi = mid - 1;
      }
      if (lo !== p.index) {
        p.index = lo;
        this.plan.setLive(p.timeline[lo]);
        this.drawExtra();
      }
      const item = p.timeline[p.index];
      const seek = this.el.querySelector("#pl-seek");
      // Not while somebody drags it.
      if (seek && document.activeElement !== seek) seek.value = String(p.t);
      this.el.querySelector("#pl-clock").textContent = `${clock(p.t)} / ${clock(p.end)}`;
      const top = this.el.querySelector("#rec-top");
      top.textContent = !item.available ? `nicht verfügbar${item.reason ? `: ${item.reason}` : ""}`
        : `${E.people(item.count)}${item.occupied ? " · belegt" : " · frei"}${item.assumed_present ? " · vermutlich noch da" : ""}`;
    },
  };

  E.register("record", view);
})();
