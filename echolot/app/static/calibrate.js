// Live calibration of a room: learn reflections in the empty room, align
// the sensor from standpoints, and set how strictly targets are filtered.
//
// The recordings run on the server (app/calibration.py) — the browser
// sees the room three times a second, the module reports more often. This
// page starts them, polls them, draws what they found onto the live map,
// and keeps a result only when asked to.

(() => {
  const E = Echolot;
  const G = window.EcholotGeometry;
  const { escapeHtml, icon, api, toast, state } = E;
  const POLL_MS = 400;
  const f2 = (v) => E.formatNumber(v, 2);
  const cm = (m) => `${Math.round(m * 100)} cm`;

  // Standpoints survive leaving the page, not a reload: they are only
  // worth something together with the placement they were measured at.
  const standpoints = {};

  function spotsCount(n) {
    return n === 1 ? "1 Störquelle" : `${n} Störquellen`;
  }

  function when(ts) {
    if (!ts) return "";
    return new Date(ts * 1000).toLocaleString("de-DE", { dateStyle: "medium", timeStyle: "short" });
  }

  const view = {
    mount(el, params) {
      this.el = el;
      this.id = params.id;
      this.tab = this.tab && this.tabRoom === params.id ? this.tab : "spots";
      this.tabRoom = params.id;
      this.capture = null;
      this.proposal = null;
      this.pending = null; // a picked, unmeasured standpoint [x, y]
      this.render();
      this.resume();
    },
    unmount() {
      clearTimeout(this.timer);
      this.timer = null;
      if (this.plan) this.plan.destroy();
      this.plan = null;
    },
    room() { return state.rooms.find((r) => r.id === this.id) || null; },
    points() { return standpoints[this.id] || (standpoints[this.id] = []); },
    onData() {
      const room = this.room();
      if (!room) { this.render(); return; }
      const json = JSON.stringify(room);
      if (this.plan && json !== this.roomJson) {
        this.roomJson = json;
        this.plan.setRoom(room);
        this.drawExtra();
        if (!this.busy()) this.renderSide();
      }
    },
    onLive() {
      if (!this.plan) return;
      this.plan.setLive(state.live[this.id] || null);
      this.updateTop();
    },
    busy() {
      return this.capture && (this.capture.phase === "waiting" || this.capture.phase === "recording");
    },

    render() {
      this.unmount();
      const room = this.room();
      if (!room) {
        this.el.innerHTML = `<div class="empty-state card pad">Diesen Raum gibt es nicht mehr. <a href="#/">Zur Übersicht</a></div>`;
        return;
      }
      this.roomJson = JSON.stringify(room);
      this.el.innerHTML = `
        <div class="page-head">
          <div><div class="eyebrow">Kalibrieren</div><h1>${escapeHtml(room.name)}</h1></div>
          <div class="head-actions">
            <a class="btn" href="#/room/${encodeURIComponent(room.id)}/edit">${icon("edit")}Raum einrichten</a>
            <a class="btn primary" href="#/room/${encodeURIComponent(room.id)}">${icon("check")}Fertig</a>
          </div>
        </div>
        <div class="room-layout">
          <section class="card plan-card">
            <div class="plan-top"><span id="cal-chip" class="chip">…</span><span class="dims" id="cal-top"></span></div>
            <div class="editor-hint" id="cal-hint"></div>
            <div class="plan-stage" id="cal-stage"></div>
            <div class="plan-legend">
              <span><i class="lg-dot"></i>zählt</span>
              <span><i class="lg-pending"></i>noch nicht bestätigt</span>
              <span><i class="lg-ring"></i>zählt nicht</span>
              <span><i class="lg-spot"></i>Störquelle</span>
            </div>
          </section>
          <aside class="side">
            <div class="segmented cal-tabs" role="tablist">
              <button type="button" data-tab="spots">Störquellen</button>
              <button type="button" data-tab="align">Ausrichten</button>
              <button type="button" data-tab="filter">Filter</button>
            </div>
            <div id="cal-side"></div>
          </aside>
        </div>`;
      this.plan = new Plan.PlanView(this.el.querySelector("#cal-stage"), {
        mode: "live",
        onPick: (p) => this.pick(p),
      });
      this.plan.setRoom(room);
      this.plan.setLive(state.live[room.id] || null);
      this.el.querySelectorAll("[data-tab]").forEach((b) => b.addEventListener("click", () => {
        this.tab = b.dataset.tab;
        this.renderSide();
        this.drawExtra();
      }));
      this.renderSide();
      this.drawExtra();
      this.updateTop();
    },

    updateTop() {
      const live = state.live[this.id] || null;
      const chip = this.el.querySelector("#cal-chip");
      if (!chip) return;
      const status = E.roomStatus(this.id);
      chip.className = `chip ${status.kind === "present" ? "present" : status.kind === "empty" ? "ok" : "warn"}`;
      chip.textContent = status.kind === "present" ? (status.count ? E.people(status.count) : "Anwesend") : status.kind === "empty" ? "Leer" : "Nicht verfügbar";
      const pending = live && live.available ? live.targets.filter((t) => t.status === "pending").length : 0;
      const top = this.el.querySelector("#cal-top");
      top.textContent = pending ? `${pending} noch nicht bestätigt` : "";
    },

    hint(text) {
      const el = this.el.querySelector("#cal-hint");
      if (el) el.textContent = text || "";
    },

    // ----------------------------------------------------------- side

    renderSide() {
      const host = this.el.querySelector("#cal-side");
      if (!host) return;
      this.el.querySelectorAll("[data-tab]").forEach((b) => b.classList.toggle("active", b.dataset.tab === this.tab));
      const room = this.room();
      if (!room) return;
      if (!room.sensor.device_id) {
        host.innerHTML = `<div class="card"><h2>Kein Sensor</h2><p class="hint">Diesem Raum ist noch kein Sensor zugeordnet. Kalibrieren geht erst mit einem.</p>
          <div class="actions" style="margin-top:12px"><a class="btn primary" href="#/room/${encodeURIComponent(room.id)}/edit">Sensor zuordnen</a></div></div>`;
        this.hint("");
        return;
      }
      if (this.tab === "spots") host.innerHTML = this.spotsCard(room);
      else if (this.tab === "align") host.innerHTML = this.alignCard(room);
      else host.innerHTML = this.filterCard(room);
      this.bindSide(host);
    },

    progressHtml(cap) {
      if (cap.phase === "waiting") {
        return `<div class="cal-count"><div class="big-number">${Math.ceil(cap.starts_in_s)}</div>
          <div>${cap.kind === "empty" ? "Sekunden, um den Raum zu verlassen" : "Sekunden bis zur Messung — hinstellen"}</div></div>`;
      }
      const share = Math.min(100, (cap.elapsed_s / cap.duration_s) * 100);
      return `<div class="cal-progress"><div class="bar"><i style="width:${share.toFixed(1)}%"></i></div>
        <div class="meta"><span>${cap.kind === "empty" ? "Hört zu" : "Misst"} · ${Math.round(cap.elapsed_s)} / ${Math.round(cap.duration_s)} s</span>
        <span>${cap.reports} Meldungen</span></div>
        ${cap.sensor_fresh ? "" : `<p class="hint warn-text">Vom Sensor kommt gerade nichts an.</p>`}</div>`;
    },

    spotsCard(room) {
      const cal = room.calibration || {};
      const cap = this.capture && this.capture.kind === "empty" ? this.capture : null;
      const own = cal.interference_device_id === room.sensor.device_id;
      const learned = own ? (cal.interference || []) : [];
      let body = "";
      if (cap && (cap.phase === "waiting" || cap.phase === "recording")) {
        this.hint(cap.phase === "waiting" ? "Bitte den Raum verlassen und die Tür schließen." : "Echolot hört zu. Alles, was jetzt auftaucht, ist eine Reflexion.");
        body = `${this.progressHtml(cap)}<div class="actions"><button class="btn" data-cancel>Abbrechen</button></div>`;
      } else if (cap && cap.phase === "done" && cap.result) {
        const r = cap.result;
        this.hint(r.ok ? "Gefundene Störquellen sind orange umrandet. Übernehmen ersetzt die bisherigen." : "");
        if (!r.ok) {
          body = `<div class="notice err">${icon("alert")}<div class="grow">${escapeHtml(r.error)}</div></div>
            <div class="actions"><button class="btn primary" data-start>Noch einmal</button><button class="btn" data-dismiss>Schließen</button></div>`;
        } else {
          const facts = [];
          if (r.receiving === 0 && r.quiet > 0) {
            facts.push(`Das Modul hat im leeren Raum <b>geschwiegen</b> (${r.quiet} Stille-Meldungen, keine Berichte). Damit der Raum dann „leer“ statt „nicht verfügbar“ ist, beim Sensor „Stille = leerer Raum“ einschalten.`);
          } else if (r.empty_reports > 0) {
            facts.push(`Das Modul schickt im leeren Raum leere Berichte (${r.empty_reports} von ${r.reports}). „Stille = leerer Raum“ ist nicht nötig.`);
          }
          body = `<div class="cal-result"><div class="big-number">${r.spots.length}</div><div>${r.spots.length === 1 ? "Störquelle gefunden" : "Störquellen gefunden"}<br><span class="hint">${r.reports} Meldungen ausgewertet</span></div></div>
            ${r.spots.length ? `<div class="zone-list">${r.spots.map((s, i) => `<div class="zone-row"><span class="swatch spot"></span>
              <div class="grow"><div class="name">Stelle ${i + 1}</div><div class="meta">in ${Math.round(s.share * 100)} % der Meldungen · Radius ${cm(s.r)}</div></div></div>`).join("")}</div>` : `<p class="hint">Im leeren Raum hat das Radar nichts Festes gemeldet.</p>`}
            ${facts.map((f) => `<p class="hint">${f}</p>`).join("")}
            ${(r.warnings || []).map((w) => `<div class="notice warn">${icon("alert")}<div class="grow">${escapeHtml(w)}</div></div>`).join("")}
            <div class="actions"><button class="btn primary" data-keep>${icon("check")}${r.spots.length ? "Übernehmen" : learned.length ? "Übernehmen (alte entfernen)" : "Übernehmen"}</button>
              <button class="btn" data-dismiss>Verwerfen</button></div>`;
        }
      } else {
        this.hint("");
        body = `<p class="hint">Echolot hört im <b>leeren</b> Raum zu und merkt sich, wo das Radar trotzdem etwas meldet — Heizkörper, Spiegel, Metall, ein Ventilator. Ziele, die dort <i>entstehen</i>, zählen danach nicht. Wer in so eine Stelle hineingeht, zählt weiter.</p>
          ${learned.length ? `<div class="notice ok">${icon("check")}<div class="grow"><strong>${spotsCount(learned.length)} gelernt</strong>${escapeHtml(when(cal.interference_learned_at))}</div>
            <button class="btn small danger" data-forget aria-label="Alle Störquellen entfernen">${icon("trash")}</button></div>
            <div class="zone-list">${learned.map((s, i) => `<div class="zone-row"><span class="swatch spot"></span>
              <div class="grow"><div class="name">Stelle ${i + 1}</div><div class="meta">in ${Math.round(s.share * 100)} % der Meldungen · Radius ${cm(s.r)}</div></div>
              <button class="btn small ghost icon-only" data-drop-spot="${i}" aria-label="Stelle ${i + 1} entfernen">${icon("close")}</button></div>`).join("")}</div>
            <p class="hint">Deckt eine Stelle einen Platz ab, an dem jemand sitzt — etwa ein Sofa mit Metallgestell —, nimm sie heraus: Wer länger als 20 s auf einer Störquelle bleibt, zählt dort nicht mehr.</p>` : ""}
          ${!own && (cal.interference || []).length ? `<div class="notice warn">${icon("alert")}<div class="grow">Die gespeicherten Störquellen stammen von einem anderen Sensor und werden nicht verwendet.</div></div>` : ""}
          <div class="row2">
            <label class="field"><span>Zeit zum Verlassen</span><select id="cal-delay">${[10, 20, 30, 60].map((s) => `<option value="${s}" ${s === 20 ? "selected" : ""}>${s} s</option>`).join("")}</select></label>
            <label class="field"><span>Zuhören</span><select id="cal-duration">${[30, 45, 60, 120].map((s) => `<option value="${s}" ${s === 45 ? "selected" : ""}>${s} s</option>`).join("")}</select></label>
          </div>
          <div class="actions"><button class="btn primary" data-start>${icon("radar")}Aufnahme starten</button></div>`;
      }
      return `<div class="card"><h2>Störquellen lernen</h2>${body}</div>`;
    },

    // Heights as the page has them: typed in, or as saved.
    mounting() {
      const room = this.room();
      const saved = room ? room.sensor : {};
      return {
        mount_height_m: this.mount !== undefined ? this.mount : saved.mount_height_m ?? null,
        target_height_m: this.target !== undefined ? this.target : saved.target_height_m ?? 1,
      };
    },

    // The next suggested spot nobody has stood on yet.
    nextSuggestion() {
      const done = this.points().map((p) => p.ref.join(","));
      return (this.suggested || []).find((q) => !done.includes(q.join(","))) || null;
    },

    alignCard(room) {
      const points = this.points();
      const cap = this.capture && this.capture.kind === "point" ? this.capture : null;
      const measuring = cap && (cap.phase === "waiting" || cap.phase === "recording");
      const pr = this.proposal;
      const m = this.mounting();
      const cal = room.calibration || {};
      const s = room.sensor;

      const mount = `<div class="cal-block"><h3>Montage</h3>
        <div>
          <label class="field"><span>Höhe des Sensors über dem Boden · m</span>
            <input type="number" id="cal-mount" min="0.2" max="4" step="0.05" inputmode="decimal" placeholder="z. B. 2,0" value="${m.mount_height_m ?? ""}"></label>
          <label class="field"><span>Gemessen wird</span>
            <select id="cal-target">${[[1.1, "Oberkörper im Stehen · 1,1 m"], [1.0, "Oberkörper · 1,0 m"], [0.8, "Oberkörper im Sitzen · 0,8 m"]].map(([v, l]) =>
              `<option value="${v}" ${Math.abs(m.target_height_m - v) < 0.01 ? "selected" : ""}>${l}</option>`).join("")}</select></label>
        </div>
        <p class="hint">Hängt das Radar höher als der Oberkörper, misst es womöglich die schräge Linie zu dir, nicht den Abstand am Boden. Mit der Höhe prüft die Kalibrierung beides und nimmt, was die Messung zeigt.</p></div>`;

      const rows = points.map((p, i) => {
        const after = pr && pr.errors_after_m ? pr.errors_after_m[i] : null;
        const before = pr && pr.errors_before_m ? pr.errors_before_m[i] : null;
        const meta = after !== null && after !== undefined
          ? `daneben: ${cm(before)} → ${cm(after)}` : "gemessen";
        return `<div class="zone-row cal-point"><span class="spot-num">${i + 1}</span>
          <div class="grow"><div class="name">${f2(p.ref[0])} · ${f2(p.ref[1])} m</div><div class="meta">${meta}</div></div>
          <button class="btn small ghost icon-only" data-remeasure="${i}" title="Neu messen" aria-label="Standpunkt ${i + 1} neu messen" ${measuring ? "disabled" : ""}>${icon("rotate")}</button>
          <button class="btn small ghost icon-only" data-drop="${i}" title="Entfernen" aria-label="Standpunkt ${i + 1} entfernen" ${measuring ? "disabled" : ""}>${icon("close")}</button></div>`;
      }).join("");

      const total = Math.max((this.suggested || []).length, points.length);
      let action = "";
      if (measuring) {
        this.hint(cap.phase === "waiting" ? "Auf den markierten Punkt stellen." : "Stehen bleiben, gern leicht hin und her wiegen.");
        action = `${this.progressHtml(cap)}<div class="actions"><button class="btn" data-cancel>Abbrechen</button></div>`;
      } else if (this.pending) {
        const label = this.remeasure !== null && this.remeasure !== undefined ? `Standpunkt ${this.remeasure + 1} neu` : `Standpunkt ${points.length + 1}${total ? ` von ${Math.max(total, points.length + 1)}` : ""}`;
        this.hint("Stell dich auf den markierten Punkt — genau über die Stelle, die Füße mittig — und tippe „Messen“. Ein Tipp auf den Plan wählt einen anderen Punkt.");
        action = `<div class="notice">${icon("target")}<div class="grow"><strong>${label}</strong>${f2(this.pending[0])} m · ${f2(this.pending[1])} m</div></div>
          <div class="actions"><button class="btn primary" data-measure>${icon("radar")}Messen (5 s)</button><button class="btn" data-unpick>Abbrechen</button></div>`;
      } else if (!points.length) {
        this.hint("Tippe auf dem Plan auf die Stelle, an der du gleich stehen wirst — oder lass dir Punkte vorschlagen.");
        action = `<div class="actions"><button class="btn primary" data-suggest>${icon("target")}Standpunkte vorschlagen</button></div>`;
      } else {
        this.hint(points.length < 5 ? "Weitere Standpunkte machen das Ergebnis belastbarer — fünf, nah und fern, links und rechts." : "Tippe auf den Plan für einen weiteren Standpunkt, oder übernimm das Ergebnis.");
      }

      let report = "";
      if (pr && points.length && !measuring) {
        const changes = [];
        if (pr.shift_m >= 0.01) changes.push(`${cm(pr.shift_m)} verschieben`);
        if (Math.abs(pr.turn_deg) >= 0.1) changes.push(`${E.formatNumber(pr.turn_deg, 1)}° drehen`);
        if (pr.mirror_changed) changes.push("links und rechts tauschen");
        const model = [];
        if (pr.range_scale !== 1) model.push(`Entfernungen um ${E.formatNumber(Math.abs(pr.range_scale - 1) * 100, 1)} % ${pr.range_scale > 1 ? "zu lang" : "zu kurz"}`);
        if (pr.range_offset_m) model.push(`${cm(Math.abs(pr.range_offset_m))} ${pr.range_offset_m > 0 ? "zu weit" : "zu nah"}`);
        if (pr.azimuth_scale !== 1) model.push(`Winkel ${pr.azimuth_scale < 1 ? "gestaucht" : "gedehnt"} (× ${E.formatNumber(pr.azimuth_scale, 2)})`);
        if (pr.slant) model.push(`misst die Schräge (Höhe ${E.formatNumber(m.mount_height_m, 2)} m)`);
        const grade = { good: ["ok", "gut"], fair: ["warn", "brauchbar"], poor: ["err", "ungenau"] }[pr.quality];
        const accuracy = pr.check_m !== null && pr.check_m !== undefined
          ? `±${cm(pr.check_m).replace(" cm", "")} cm <small>(Kreuzprobe)</small>` : `${cm(pr.rms_m)} <small>(Restabweichung)</small>`;
        report = `<div class="cal-proposal">
            <div class="cal-grade"><span class="chip ${grade[0]}">${grade[1]}</span><span>Genauigkeit ${accuracy}</span></div>
            <p>${changes.length ? escapeHtml(changes.join(", ")) + "." : "Position und Richtung stimmen."}${pr.mode === "direction" ? " Nur die Richtung — für die Position braucht es einen zweiten Standpunkt." : ""}</p>
            ${pr.model ? `<p class="hint">Modell: ${escapeHtml(pr.model_label)}${model.length ? ` — ${escapeHtml(model.join(", "))}` : ""}.</p>` : ""}
            <div class="stat-lines">
              <div class="stat-line"><span>Abweichung jetzt</span><span>${cm(pr.rms_before_m)}</span></div>
              <div class="stat-line"><span>danach</span><span><b>${cm(pr.rms_m)}</b></span></div>
            </div>
            ${(pr.warnings || []).map((w) => `<div class="notice warn">${icon("alert")}<div class="grow">${escapeHtml(w)}</div></div>`).join("")}
            <div class="actions"><button class="btn primary" data-apply ${changes.length || pr.model_changed ? "" : "disabled"}>${icon("check")}Übernehmen</button>
              <button class="btn" data-clear>Punkte verwerfen</button></div></div>`;
      }

      const active = [];
      if (s.range_scale !== 1) active.push(`Entfernung × ${E.formatNumber(s.range_scale, 3)}`);
      if (s.range_offset_m) active.push(`Versatz ${cm(s.range_offset_m)}`);
      if (s.azimuth_scale !== 1) active.push(`Winkel × ${E.formatNumber(s.azimuth_scale, 2)}`);
      if (s.slant) active.push("Schrägkorrektur");
      const state = cal.aligned_at ? `<div class="notice ok">${icon("check")}<div class="grow"><strong>Ausgerichtet ${escapeHtml(when(cal.aligned_at))}</strong>
          ${cal.alignment_points} Standpunkte · ${cal.alignment_check_m !== null && cal.alignment_check_m !== undefined ? `Genauigkeit ±${Math.round(cal.alignment_check_m * 100)} cm` : `Restabweichung ${cm(cal.alignment_rms_m || 0)}`}${active.length ? `<br>${escapeHtml(active.join(" · "))}` : ""}</div>
          ${active.length ? `<button class="btn small" data-reset-model title="Korrekturen des Sensormodells zurücksetzen">Zurücksetzen</button>` : ""}</div>` : "";

      return `<div class="card"><h2>Sensor ausrichten</h2>
        <p class="hint">Stell dich an Stellen, die du auf dem Plan genau wiederfindest. Echolot vergleicht, wo das Radar dich sieht, mit dem markierten Punkt, und rechnet daraus Position, Richtung und Links/Rechts des Sensors — und ab drei Punkten, wie das Modul Entfernungen und Winkel verzerrt. Nur eine Person im Raum.</p>
        ${state}${mount}
        <div class="cal-block"><h3>Standpunkte</h3>
        ${rows ? `<div class="zone-list">${rows}</div>` : ""}
        ${action}${report}</div></div>`;
    },

    filterCard(room) {
      const cal = room.calibration || { confirm_s: 1, smoothing: "normal" };
      const live = state.live[this.id];
      const version = live && live.filter ? live.filter.definition_version : null;
      return `<div class="card"><h2>Filter</h2>
        <label class="field"><span>Bestätigungszeit · <b id="cal-confirm-out">${E.formatNumber(cal.confirm_s, 1)} s</b></span>
          <input type="range" id="cal-confirm" min="0" max="5" step="0.5" value="${cal.confirm_s}">
          <p class="hint">Ein neues Ziel zählt erst, wenn das Radar es so lange meldet. Reflexionen, die kurz aufblitzen, erreichen das nie. Auf der Karte sind sie bis dahin hohl gezeichnet. 0 s zählt jede Meldung sofort, wie Echolot 1.0.</p></label>
        <div class="field"><span>Glättung</span><div class="segmented" id="cal-smooth">
          ${[["off", "Aus"], ["normal", "Normal"], ["strong", "Stark"]].map(([k, v]) => `<button type="button" data-smooth="${k}" class="${cal.smoothing === k ? "active" : ""}">${v}</button>`).join("")}</div>
          <p class="hint">Mittelt die Position über die letzten Meldungen. Ruhigere Punkte an Zonengrenzen, dafür folgt der Punkt einer gehenden Person etwas später.</p></div>
        ${version ? `<p class="hint">Messdefinition ${version}. Home Assistant bekommt Version und Filter als Attribute jeder Entität.</p>` : ""}
      </div>`;
    },

    bindSide(host) {
      const on = (sel, fn) => host.querySelectorAll(sel).forEach((b) => b.addEventListener("click", fn));
      on("[data-start]", () => this.startEmpty());
      on("[data-cancel]", () => this.cancel());
      on("[data-dismiss]", () => this.dismiss());
      on("[data-keep]", () => this.keepSpots());
      on("[data-forget]", () => this.forgetSpots());
      host.querySelectorAll("[data-drop-spot]").forEach((b) => b.addEventListener("click", () => this.dropSpot(Number(b.dataset.dropSpot))));
      on("[data-measure]", () => this.measure());
      on("[data-unpick]", () => { this.pending = null; this.renderSide(); this.drawExtra(); });
      on("[data-apply]", () => this.applyAlignment());
      on("[data-suggest]", () => this.suggest());
      on("[data-reset-model]", () => this.resetModel());
      host.querySelectorAll("[data-remeasure]").forEach((b) => b.addEventListener("click", () => {
        const i = Number(b.dataset.remeasure);
        this.remeasure = i;
        this.pending = this.points()[i].ref.slice();
        this.renderSide();
        this.drawExtra();
      }));
      const mount = host.querySelector("#cal-mount");
      if (mount) mount.addEventListener("change", () => {
        const v = mount.value.trim() === "" ? null : Number(mount.value.replace(",", "."));
        if (v !== null && !(v >= 0.2 && v <= 4)) { toast("Höhe zwischen 0,2 und 4 m", "err"); return; }
        this.mount = v;
        this.saveMounting();
      });
      const target = host.querySelector("#cal-target");
      if (target) target.addEventListener("change", () => { this.target = Number(target.value); this.saveMounting(); });
      on("[data-clear]", () => { standpoints[this.id] = []; this.proposal = null; this.suggested = null; this.pending = null; this.renderSide(); this.drawExtra(); });
      host.querySelectorAll("[data-drop]").forEach((b) => b.addEventListener("click", () => {
        this.points().splice(Number(b.dataset.drop), 1);
        this.solve();
      }));
      const confirm = host.querySelector("#cal-confirm");
      if (confirm) {
        confirm.addEventListener("input", () => { host.querySelector("#cal-confirm-out").textContent = `${E.formatNumber(Number(confirm.value), 1)} s`; });
        confirm.addEventListener("change", () => this.saveFilter({ confirm_s: Number(confirm.value) }));
      }
      host.querySelectorAll("[data-smooth]").forEach((b) => b.addEventListener("click", () => this.saveFilter({ smoothing: b.dataset.smooth })));
    },

    // --------------------------------------------------------- drawing

    drawExtra() {
      if (!this.plan) return;
      const room = this.room();
      if (!room) return;
      let svg = "";
      const cap = this.capture;
      if (this.tab === "spots" && cap && cap.kind === "empty") {
        if (cap.phase === "recording" || cap.phase === "done") {
          for (const [x, y] of cap.points) {
            const p = G.toRoom(x, y, room.sensor);
            svg += `<circle class="cal-sample" cx="${p.x.toFixed(3)}" cy="${p.y.toFixed(3)}" r="0.04"/>`;
          }
        }
        if (cap.phase === "done" && cap.result && cap.result.ok) svg += this.plan.spotsSvg(cap.result.spots, room.sensor, "proposal");
      }
      if (this.tab === "align") {
        const pr = this.proposal ? { ...room.sensor, ...this.mounting(), ...this.proposal } : null;
        const done = this.points().map((p) => p.ref.join(","));
        const pendingKey = this.pending ? this.pending.join(",") : null;
        (this.suggested || []).forEach((q, i) => {
          if (done.includes(q.join(",")) || q.join(",") === pendingKey) return;
          svg += `<g class="cal-mark suggested"><circle cx="${q[0]}" cy="${q[1]}" r="0.14"/>
            <text x="${q[0]}" y="${q[1]}" text-anchor="middle" dominant-baseline="central">${String.fromCharCode(65 + i)}</text></g>`;
        });
        this.points().forEach((p, i) => {
          const now = G.toRoom(p.raw[0], p.raw[1], room.sensor);
          svg += `<line class="cal-error" x1="${p.ref[0]}" y1="${p.ref[1]}" x2="${now.x.toFixed(3)}" y2="${now.y.toFixed(3)}"/>`;
          svg += `<circle class="cal-seen" cx="${now.x.toFixed(3)}" cy="${now.y.toFixed(3)}" r="0.06"/>`;
          if (pr) {
            const then = G.toRoom(p.raw[0], p.raw[1], pr);
            svg += `<circle class="cal-seen after" cx="${then.x.toFixed(3)}" cy="${then.y.toFixed(3)}" r="0.06"/>`;
          }
          svg += `<g class="cal-mark"><circle cx="${p.ref[0]}" cy="${p.ref[1]}" r="0.14"/>
            <text x="${p.ref[0]}" y="${p.ref[1]}" text-anchor="middle" dominant-baseline="central">${i + 1}</text></g>`;
        });
        if (this.pending) {
          svg += `<g class="cal-mark pending"><circle cx="${this.pending[0]}" cy="${this.pending[1]}" r="0.14"/>
            <text x="${this.pending[0]}" y="${this.pending[1]}" text-anchor="middle" dominant-baseline="central">${this.points().length + 1}</text></g>`;
        }
        if (pr && this.points().length) svg += this.plan.sensorGhostSvg(pr);
      }
      this.plan.setExtra(svg);
    },

    // ------------------------------------------------------- recordings

    async resume() {
      try {
        this.capture = await api(`api/rooms/${encodeURIComponent(this.id)}/capture`);
      } catch { this.capture = null; }
      // A finished standpoint has lost its marked spot with the page.
      if (this.capture && this.capture.kind === "point" && this.capture.phase === "done") this.capture = null;
      else if (this.capture && this.capture.kind === "empty") this.tab = "spots";
      this.renderSide();
      this.drawExtra();
      if (this.busy()) this.poll();
    },

    async startEmpty() {
      const delay = Number((this.el.querySelector("#cal-delay") || {}).value || 20);
      const duration = Number((this.el.querySelector("#cal-duration") || {}).value || 45);
      await this.start({ kind: "empty", delay_s: delay, duration_s: duration });
    },

    async start(body) {
      try {
        this.capture = await api(`api/rooms/${encodeURIComponent(this.id)}/capture`, { method: "POST", body });
      } catch (err) { toast(err.message, "err"); return; }
      this.renderSide();
      this.drawExtra();
      this.poll();
    },

    poll() {
      clearTimeout(this.timer);
      this.timer = setTimeout(async () => {
        if (!this.plan) return;
        try {
          this.capture = await api(`api/rooms/${encodeURIComponent(this.id)}/capture`);
        } catch { this.capture = null; }
        if (!this.capture) {
          // Ended elsewhere — another tab, or a restart of the add-on.
          this.measuring = null;
          this.renderSide();
          this.drawExtra();
          return;
        }
        if (this.capture.phase === "done") this.finished();
        else { this.renderSide(); this.drawExtra(); this.poll(); }
      }, POLL_MS);
    },

    finished() {
      const cap = this.capture;
      if (cap.kind === "point") {
        const r = cap.result;
        const ref = this.measuring;
        this.capture = null;
        this.measuring = null;
        if (r && r.ok && ref) {
          const entry = { ref, raw: r.raw, warnings: r.warnings };
          if (this.remeasure !== null && this.remeasure !== undefined) this.points()[this.remeasure] = entry;
          else this.points().push(entry);
          this.remeasure = null;
          this.pending = this.nextSuggestion();
          this.solve();
          if (r.warnings.length) toast(r.warnings[0]);
          return;
        }
        toast(r ? r.error : "Messung fehlgeschlagen", "err");
      }
      this.renderSide();
      this.drawExtra();
    },

    async cancel() {
      clearTimeout(this.timer);
      try { await api(`api/rooms/${encodeURIComponent(this.id)}/capture`, { method: "DELETE" }); } catch { /* already gone */ }
      this.capture = null;
      this.measuring = null;
      this.renderSide();
      this.drawExtra();
    },

    async dismiss() {
      await this.cancel();
    },

    async keepSpots() {
      const r = this.capture && this.capture.result;
      if (!r || !r.ok) return;
      try {
        await api(`api/rooms/${encodeURIComponent(this.id)}/calibration`, { method: "PUT",
          body: { interference: { spots: r.spots, device_id: r.device_id } } });
        await api(`api/rooms/${encodeURIComponent(this.id)}/capture`, { method: "DELETE" }).catch(() => {});
        this.capture = null;
        await E.refresh();
        toast(r.spots.length ? `${spotsCount(r.spots.length)} übernommen.` : "Keine Störquellen — die alten sind entfernt.");
      } catch (err) { toast(err.message, "err"); }
      this.onData();
      this.renderSide();
      this.drawExtra();
    },

    async forgetSpots() {
      const ok = await E.confirmDialog({ title: "Störquellen entfernen?", text: "Ziele an diesen Stellen zählen danach wieder wie überall.",
        confirm: "Entfernen", danger: true });
      if (!ok) return;
      try {
        await api(`api/rooms/${encodeURIComponent(this.id)}/calibration`, { method: "PUT", body: { interference: null } });
        await E.refresh();
      } catch (err) { toast(err.message, "err"); }
      this.onData();
      this.renderSide();
    },

    async dropSpot(index) {
      const room = this.room();
      const spots = (room.calibration.interference || []).filter((_, i) => i !== index);
      try {
        await api(`api/rooms/${encodeURIComponent(this.id)}/calibration`, { method: "PUT",
          body: { interference: { spots, device_id: room.sensor.device_id } } });
        await E.refresh();
      } catch (err) { toast(err.message, "err"); }
      this.onData();
      this.renderSide();
    },

    // -------------------------------------------------------- alignment

    pick(p) {
      if (this.tab !== "align" || this.busy()) return;
      if (this.points().length >= 12) { toast("Mehr als zwölf Standpunkte bringen nichts mehr."); return; }
      this.remeasure = null;
      this.pending = p;
      this.renderSide();
      this.drawExtra();
    },

    async measure() {
      if (!this.pending) return;
      this.measuring = this.pending;
      await this.start({ kind: "point", delay_s: 3, duration_s: 5 });
    },

    async solve() {
      const points = this.points();
      if (!points.length) { this.proposal = null; this.renderSide(); this.drawExtra(); return; }
      try {
        const m = this.mounting();
        this.proposal = await api(`api/rooms/${encodeURIComponent(this.id)}/alignment`, { method: "POST",
          body: { points: points.map((p) => ({ raw: p.raw, ref: p.ref })), mount_height_m: m.mount_height_m, target_height_m: m.target_height_m } });
      } catch (err) { this.proposal = null; toast(err.message, "err"); }
      this.renderSide();
      this.drawExtra();
    },

    async applyAlignment() {
      const pr = this.proposal;
      if (!pr) return;
      try {
        const m = this.mounting();
        await api(`api/rooms/${encodeURIComponent(this.id)}/calibration`, { method: "PUT", body: { alignment: {
          x: pr.x, y: pr.y, angle: pr.angle, mirror: pr.mirror, slant: pr.slant,
          range_scale: pr.range_scale, range_offset_m: pr.range_offset_m, azimuth_scale: pr.azimuth_scale,
          mount_height_m: m.mount_height_m, target_height_m: m.target_height_m,
          rms_m: pr.rms_m, check_m: pr.check_m, model: pr.model, points: pr.points } } });
        await E.refresh();
        // The standpoints stay: measured raw, they now show how well the
        // new placement fits them.
        await this.solve();
        toast(`Übernommen — ${pr.check_m !== null && pr.check_m !== undefined ? `Genauigkeit ±${Math.round(pr.check_m * 100)} cm` : `Restabweichung ${cm(pr.rms_m)}`}.`);
      } catch (err) { toast(err.message, "err"); }
      this.onData();
      this.renderSide();
      this.drawExtra();
    },

    async suggest() {
      try {
        const r = await api(`api/rooms/${encodeURIComponent(this.id)}/alignment/suggest?count=5`);
        this.suggested = r.points;
      } catch (err) { toast(err.message, "err"); return; }
      if (!this.suggested.length) toast("Im geplanten Sichtbereich ist kein Platz für Standpunkte — stimmen Sensor und Wände?", "err");
      this.remeasure = null;
      this.pending = this.nextSuggestion();
      this.renderSide();
      this.drawExtra();
    },

    async saveMounting() {
      const m = this.mounting();
      try {
        await api(`api/rooms/${encodeURIComponent(this.id)}/calibration`, { method: "PUT", body: { mounting: m } });
        await E.refresh();
        this.mount = undefined;
        this.target = undefined;
      } catch (err) { toast(err.message, "err"); }
      if (this.points().length) await this.solve();
      else { this.renderSide(); this.drawExtra(); }
    },

    async resetModel() {
      const ok = await E.confirmDialog({ title: "Korrekturen zurücksetzen?",
        text: "Entfernungs-, Winkel- und Schrägkorrektur fallen weg; Position und Richtung bleiben.", confirm: "Zurücksetzen", danger: true });
      if (!ok) return;
      try {
        await api(`api/rooms/${encodeURIComponent(this.id)}/calibration`, { method: "PUT", body: { reset_model: true } });
        await E.refresh();
      } catch (err) { toast(err.message, "err"); }
      if (this.points().length) await this.solve();
      else { this.onData(); this.renderSide(); }
    },

    // ----------------------------------------------------------- filter

    async saveFilter(change) {
      try {
        await api(`api/rooms/${encodeURIComponent(this.id)}/calibration`, { method: "PUT", body: { filter: change } });
        await E.refresh();
      } catch (err) { toast(err.message, "err"); }
      this.onData();
      this.renderSide();
    },
  };

  E.register("calibrate", view);
})();
