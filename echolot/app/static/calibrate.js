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

  function spotsCount(n) {
    return n === 1 ? "1 Störquelle" : `${n} Störquellen`;
  }

  // What a standpoint's recording said about itself.
  function measuredMeta(p) {
    const facts = [p.source === "capture" ? "gemessen" : "übergeben"];
    if (p.spread_m !== null && p.spread_m !== undefined) facts.push(`Streuung ${cm(p.spread_m)}`);
    if (p.share !== null && p.share !== undefined) facts.push(`in ${Math.round(p.share * 100)} % der Meldungen`);
    return facts.join(" · ");
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
      this.pendingRole = "fit";
      this.moduleForm = null; // mounting typed in, not yet written to the module
      this.remeasure = null; // id of the standpoint being measured again
      this.preview = null; // id of the history record drawn on the plan
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
    // The standpoints are kept on the server with the room: a reload, or
    // the iPad after the laptop, finds them there.
    points() {
      const room = this.room();
      const draft = room && room.calibration && room.calibration.alignment_draft;
      return draft && draft.device_id === room.sensor.device_id ? draft.points : [];
    },
    // A route answered with the room as it is now.
    takeRoom(room) {
      const i = state.rooms.findIndex((r) => r.id === room.id);
      if (i >= 0) state.rooms[i] = room;
      this.roomJson = JSON.stringify(room);
      if (this.plan) this.plan.setRoom(room);
    },
    async reloadRoom() {
      try { this.takeRoom(await api(`api/rooms/${encodeURIComponent(this.id)}`)); } catch { await E.refresh(); }
    },
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

    // Heights as the page has them: from the module when it says how it
    // hangs on a wall, else typed in, or as saved.
    mounting() {
      const room = this.room();
      const saved = room ? room.sensor : {};
      const mm = room && room.module && room.module.entities ? room.module.mounting : null;
      return {
        mount_height_m: mm && mm.mode === "side" ? mm.height_m
          : this.typedMount !== undefined ? this.typedMount : saved.mount_height_m ?? null,
        target_height_m: this.typedTarget !== undefined ? this.typedTarget : saved.target_height_m ?? 1,
      };
    },

    // The next suggested spot nobody has stood on yet: the spots to fit
    // first, then the control spots.
    nextSuggestion() {
      const done = this.points().map((p) => p.ref.join(","));
      const fit = (this.suggested || []).find((q) => !done.includes(q.join(",")));
      if (fit) return { point: fit, role: "fit" };
      const check = (this.suggestedChecks || []).find((q) => !done.includes(q.join(",")));
      return check ? { point: check, role: "check" } : null;
    },

    queueNext() {
      const next = this.nextSuggestion();
      this.pending = next ? next.point : null;
      this.pendingRole = next ? next.role : "fit";
    },

    alignCard(room) {
      const points = this.points();
      const cap = this.capture && this.capture.kind === "point" ? this.capture : null;
      const measuring = cap && (cap.phase === "waiting" || cap.phase === "recording");
      const pr = this.proposal;
      const m = this.mounting();
      const cal = room.calibration || {};
      const s = room.sensor;

      const targetField = `<label class="field"><span>Gemessen wird</span>
            <select id="cal-target">${[[1.1, "Oberkörper im Stehen · 1,1 m"], [1.0, "Oberkörper · 1,0 m"], [0.8, "Oberkörper im Sitzen · 0,8 m"]].map(([v, l]) =>
              `<option value="${v}" ${Math.abs(m.target_height_m - v) < 0.01 ? "selected" : ""}>${l}</option>`).join("")}</select></label>`;
      const remount = `<button class="btn small" data-remount ${measuring ? "disabled" : ""}>${icon("rotate")}Sensor neu montiert …</button>`;
      const mod = room.module || {};
      let mount;
      if (mod.entities) {
        // The module keeps its own mounting and computes its positions
        // with it: shown as it reads it back, changed by writing to it.
        const mm = mod.mounting;
        const f = this.moduleForm || (mm ? { ...mm } : { mode: "side", height_m: m.mount_height_m ?? 2.6, angle_deg: 30 });
        const same = mm && mm.mode === f.mode && Math.abs(mm.height_m - f.height_m) < 0.005 && Math.abs(mm.angle_deg - f.angle_deg) < 0.005;
        const said = mm
          ? `<div class="notice ok">${icon("check")}<div class="grow"><strong>Das Modul meldet</strong>${mm.mode === "side" ? "Wand" : "Decke"} · ${f2(mm.height_m)} m · ${E.formatNumber(mm.angle_deg, 1)}° Neigung</div></div>`
          : `<div class="notice warn">${icon("alert")}<div class="grow">${mod.connected ? "Das Modul hat seine Montage noch nicht gemeldet." : "Der Sensor ist nicht verbunden — was das Modul eingestellt hat, ist gerade nicht lesbar."}</div></div>`;
        mount = `<div class="cal-block"><h3>Montage</h3>${said}
          <div class="field"><span>Montageart</span><div class="segmented" id="cal-mode">
            <button type="button" data-mode="side" class="${f.mode === "side" ? "active" : ""}">Wand</button>
            <button type="button" data-mode="top" class="${f.mode === "top" ? "active" : ""}">Decke</button></div></div>
          <div class="row2">
            <label class="field"><span>Höhe über dem Boden · m</span>
              <input type="number" id="cal-mheight" min="0.5" max="5" step="0.01" inputmode="decimal" value="${f.height_m}"></label>
            <label class="field"><span>Neigung nach unten · °</span>
              <input type="number" id="cal-mangle" min="0" max="90" step="0.5" inputmode="decimal" value="${f.angle_deg}"></label>
          </div>
          <p class="hint">Das Modul rechnet Montageart, Höhe und Neigung selbst in die Positionen ein, die es meldet. Hi-Link empfiehlt an der Wand 2,2–2,7 m Höhe und 25–40° Neigung. Wer sie ändert, ändert, was das Modul meldet: Ausrichtung und Störquellen werden dann verworfen.</p>
          <div class="actions"><button class="btn primary" data-write-mount ${same || !mod.connected || measuring ? "disabled" : ""}>${icon("check")}Ins Modul schreiben</button></div>
          ${targetField}
          <div class="actions">${remount}</div></div>`;
      } else {
        mount = `<div class="cal-block"><h3>Montage</h3>
          ${mod.connected ? `<div class="notice warn">${icon("alert")}<div class="grow">Die Firmware auf dem Sensor kennt die Montage des Moduls noch nicht. Neu bauen und flashen — dann lassen sich Montageart, Höhe und Neigung hier ins Modul schreiben.</div></div>` : ""}
          <div>
            <label class="field"><span>Höhe des Sensors über dem Boden · m</span>
              <input type="number" id="cal-mount" min="0.2" max="4" step="0.05" inputmode="decimal" placeholder="z. B. 2,0" value="${m.mount_height_m ?? ""}"></label>
            ${targetField}
          </div>
          <p class="hint">Hängt das Radar höher als der Oberkörper, misst es womöglich die schräge Linie zu dir, nicht den Abstand am Boden. Mit der Höhe prüft die Kalibrierung beides und nimmt, was die Messung zeigt.</p>
          <div class="actions">${remount}</div></div>`;
      }

      // Spots to fit and control spots, each numbered in its own series;
      // the proposal's errors come in the same two orders.
      const fitIdx = [], checkIdx = [];
      points.forEach((p, i) => ((p.role === "check" ? checkIdx : fitIdx).push(i)));
      const row = (p, i, label, meta) => `<div class="zone-row cal-point ${p.role === "check" ? "check" : ""}"><span class="spot-num">${label}</span>
          <div class="grow"><div class="name">${f2(p.ref[0])} · ${f2(p.ref[1])} m</div><div class="meta">${meta}</div></div>
          <button class="btn small ghost icon-only" data-remeasure="${escapeHtml(p.id)}" title="Neu messen" aria-label="Punkt ${label} neu messen" ${measuring ? "disabled" : ""}>${icon("rotate")}</button>
          <button class="btn small ghost icon-only" data-drop="${escapeHtml(p.id)}" title="Entfernen" aria-label="Punkt ${label} entfernen" ${measuring ? "disabled" : ""}>${icon("close")}</button></div>`;
      const fitRows = fitIdx.map((i, k) => {
        const after = pr && pr.errors_after_m ? pr.errors_after_m[k] : undefined;
        const before = pr && pr.errors_before_m ? pr.errors_before_m[k] : undefined;
        const held = pr && pr.cv_errors_m ? pr.cv_errors_m[k] : undefined;
        const meta = after !== undefined
          ? `daneben: ${cm(before)} → ${cm(after)}${held !== undefined ? ` · ohne ihn: ${cm(held)}` : ""}` : measuredMeta(points[i]);
        return row(points[i], i, String(k + 1), meta);
      }).join("");
      const checkRows = checkIdx.map((i, k) => {
        const e = pr && pr.validation_errors_m ? pr.validation_errors_m[k] : undefined;
        return row(points[i], i, `K${k + 1}`, e !== undefined ? `Kontrolle: ${cm(e)} daneben` : measuredMeta(points[i]));
      }).join("");

      const total = Math.max((this.suggested || []).length, fitIdx.length);
      let action = "";
      if (measuring) {
        this.hint(cap.phase === "waiting" ? "Auf den markierten Punkt stellen." : "Stehen bleiben, gern leicht hin und her wiegen.");
        action = `${this.progressHtml(cap)}<div class="actions"><button class="btn" data-cancel>Abbrechen</button></div>`;
      } else if (this.pending) {
        const isCheck = this.pendingRole === "check";
        const label = this.remeasure
          ? "Punkt neu messen"
          : isCheck ? `Kontrollpunkt K${checkIdx.length + 1}`
            : `Standpunkt ${fitIdx.length + 1}${total ? ` von ${Math.max(total, fitIdx.length + 1)}` : ""}`;
        this.hint(isCheck
          ? "Ein Kontrollpunkt fließt nicht in die Rechnung ein — er prüft sie. Stell dich genau auf den Punkt und tippe „Messen“."
          : "Stell dich auf den markierten Punkt — genau über die Stelle, die Füße mittig — und tippe „Messen“. Ein Tipp auf den Plan wählt einen anderen Punkt.");
        action = `<div class="notice">${icon("target")}<div class="grow"><strong>${label}</strong>${f2(this.pending[0])} m · ${f2(this.pending[1])} m</div></div>
          <div class="actions"><button class="btn primary" data-measure>${icon("radar")}Messen (5 s)</button><button class="btn" data-unpick>Abbrechen</button></div>`;
      } else if (!points.length) {
        this.hint("Tippe auf dem Plan auf die Stelle, an der du gleich stehen wirst — oder lass dir Punkte vorschlagen.");
        action = `<div class="actions"><button class="btn primary" data-suggest>${icon("target")}Standpunkte vorschlagen</button></div>`;
      } else {
        this.hint(this.pendingRole === "check"
          ? "Tippe auf dem Plan auf die Stelle für den Kontrollpunkt — abseits der Standpunkte."
          : fitIdx.length < 5 ? "Weitere Standpunkte machen das Ergebnis belastbarer — fünf, nah und fern, links und rechts." : "Tippe auf den Plan für einen weiteren Standpunkt, oder prüfe das Ergebnis mit Kontrollpunkten.");
        if (fitIdx.length >= 2) {
          action = `<div class="actions"><button class="btn" data-add-check>${icon("target")}Kontrollpunkt messen</button></div>`;
        }
      }

      let report = "";
      if (pr && fitIdx.length && !measuring) {
        const changes = [];
        if (pr.shift_m >= 0.01) changes.push(`${cm(pr.shift_m)} verschieben`);
        if (Math.abs(pr.turn_deg) >= 0.1) changes.push(`${E.formatNumber(pr.turn_deg, 1)}° drehen`);
        if (pr.mirror_changed) changes.push("links und rechts tauschen");
        const model = [];
        if (pr.range_scale !== 1) model.push(`Entfernungen um ${E.formatNumber(Math.abs(pr.range_scale - 1) * 100, 1)} % ${pr.range_scale > 1 ? "zu lang" : "zu kurz"}`);
        if (pr.range_offset_m) model.push(`${cm(Math.abs(pr.range_offset_m))} ${pr.range_offset_m > 0 ? "zu weit" : "zu nah"}`);
        if (pr.azimuth_scale !== 1) model.push(`Winkel ${pr.azimuth_scale < 1 ? "gestaucht" : "gedehnt"} (× ${E.formatNumber(pr.azimuth_scale, 2)})`);
        if (pr.slant) model.push(`misst die Schräge (Höhe ${E.formatNumber(m.mount_height_m, 2)} m)`);
        const grade = pr.validation_status === "validated"
          ? { good: ["ok", "geprüft: gut"], fair: ["warn", "geprüft: brauchbar"], poor: ["err", "geprüft: ungenau"] }[pr.quality]
          : ["warn", "nicht unabhängig geprüft"];
        const metric = (label, value, note) => `<div class="stat-line"><span>${label}${note ? `<small>${note}</small>` : ""}</span><span>${value}</span></div>`;
        report = `<div class="cal-proposal">
            <div class="cal-grade"><span class="chip ${grade[0]}">${grade[1]}</span></div>
            <p>${changes.length ? escapeHtml(changes.join(", ")) + "." : "Position und Richtung stimmen."}${pr.mode === "direction" ? " Nur die Richtung — für die Position braucht es einen zweiten Standpunkt." : ""}</p>
            ${pr.model ? `<p class="hint">Modell: ${escapeHtml(pr.model_label)}${model.length ? ` — ${escapeHtml(model.join(", "))}` : ""}.</p>` : ""}
            <div class="stat-lines cal-metrics">
              ${metric("Abweichung vorher", cm(pr.rms_before_m), "an den Standpunkten, jetzige Einstellung")}
              ${metric("Anpassung", `<b>${cm(pr.fit_rms_m)}</b>`, "RMS an den Standpunkten, aus denen gerechnet wurde")}
              ${metric("Kreuzprüfungs-RMS", pr.cv_rms_m !== null ? cm(pr.cv_rms_m) : "—", pr.cv_rms_m !== null ? `jeder Standpunkt weggelassen, Modell je Durchgang neu gewählt (${pr.points} Punkte)` : "ab 3 Standpunkten")}
              ${metric("Kontrollpunkte-RMS", pr.validation_points ? cm(pr.validation_rms_m) : "—", pr.validation_points ? `${pr.validation_points} Punkt${pr.validation_points === 1 ? "" : "e"}, nicht in der Rechnung${pr.validation_points < 2 ? " — für eine Bewertung braucht es zwei" : ""}` : "keine gemessen")}
            </div>
            ${pr.validation_status !== "validated" ? `<p class="hint">Anpassung und Kreuzprüfung beruhen auf denselben Standpunkten. Wie genau es im Raum ist, zeigen erst Kontrollpunkte, die nicht in die Rechnung eingehen — zwei reichen für eine Bewertung.</p>` : ""}
            ${(pr.warnings || []).map((w) => `<div class="notice warn">${icon("alert")}<div class="grow">${escapeHtml(w)}</div></div>`).join("")}
            <div class="actions"><button class="btn primary" data-apply ${changes.length || pr.model_changed ? "" : "disabled"}>${icon("check")}Übernehmen</button>
              <button class="btn" data-clear>Punkte verwerfen</button></div></div>`;
      }

      const active = [];
      if (s.range_scale !== 1) active.push(`Entfernung × ${E.formatNumber(s.range_scale, 3)}`);
      if (s.range_offset_m) active.push(`Versatz ${cm(s.range_offset_m)}`);
      if (s.azimuth_scale !== 1) active.push(`Winkel × ${E.formatNumber(s.azimuth_scale, 2)}`);
      if (s.slant) active.push("Schrägkorrektur");
      const rep = cal.alignment_report || {};
      const st = room.alignment_state || { state: "none", changed: [] };
      let summary = "";
      if (st.state === "unknown") summary = "Mit Echolot 1.3 ausgerichtet — welche Prüfung hinter der damaligen Zahl stand, ist nicht belegt.";
      else if (rep.validation_status === "validated") summary = `Kontrollpunkte-RMS ${cm(rep.validation_rms_m)} (${rep.validation_points} Punkte)`;
      else if (rep.fit_rms_m !== undefined && rep.fit_rms_m !== null) summary = `Nicht unabhängig geprüft · Anpassung ${cm(rep.fit_rms_m)}${rep.cv_rms_m !== null && rep.cv_rms_m !== undefined ? ` · Kreuzprüfung ${cm(rep.cv_rms_m)}` : ""}`;
      const stale = st.state === "stale";
      const dropped = !cal.aligned_at && cal.invalidated_at
        ? `<div class="notice warn">${icon("alert")}<div class="grow"><strong>Kalibrierung verworfen · ${escapeHtml(when(cal.invalidated_at))}</strong>${escapeHtml(cal.invalidated_reason || "")}: Ausrichtung, Sensormodell und Störquellen gehörten zur alten Montage. Neu ausrichten und Störquellen neu lernen.</div></div>` : "";
      const state = cal.aligned_at ? `<div class="notice ${stale || st.state === "unknown" ? "warn" : "ok"}">${icon(stale ? "alert" : "check")}<div class="grow"><strong>${stale ? "Ausrichtung veraltet" : "Ausgerichtet"} · ${escapeHtml(when(cal.aligned_at))}</strong>
          ${stale ? `Seitdem: ${escapeHtml(st.changed.join(", "))}. Die Zahlen beschreiben den Raum nicht mehr — neu messen.<br>` : ""}${escapeHtml(summary)}${active.length ? `<br>${escapeHtml(active.join(" · "))}
          <div class="actions"><button class="btn small" data-reset-model title="Korrekturen des Sensormodells zurücksetzen">Korrekturen zurücksetzen</button></div>` : ""}</div></div>` : "";

      return `<div class="card"><h2>Sensor ausrichten</h2>
        <p class="hint">Stell dich an Stellen, die du auf dem Plan genau wiederfindest. Echolot vergleicht, wo das Radar dich sieht, mit dem markierten Punkt, und rechnet daraus Position, Richtung und Links/Rechts des Sensors — und ab drei Punkten, wie das Modul Entfernungen und Winkel verzerrt. Nur eine Person im Raum.</p>
        ${state}${dropped}${mount}
        <div class="cal-block"><h3>Standpunkte</h3>
        ${fitRows ? `<div class="zone-list">${fitRows}</div>` : ""}
        ${checkRows ? `<h3 class="cal-sub">Kontrollpunkte</h3><div class="zone-list">${checkRows}</div>` : ""}
        ${action}${report}</div>${this.historyBlock(room, measuring)}</div>`;
    },

    // Every placement the sensor had from an alignment, and the drawing
    // before the first: look at one on the plan, go back to it.
    historyBlock(room, measuring) {
      const cal = room.calibration || {};
      const records = cal.alignment_history || [];
      if (!records.length) return "";
      const rows = records.map((r) => {
        const current = r.id === cal.alignment_id;
        const foreign = r.device_id !== room.sensor.device_id || r.epoch !== cal.mounting_epoch;
        const rep = r.report || {};
        const facts = [];
        if (r.origin === "before") facts.push("so stand der Sensor davor");
        else {
          facts.push(`${rep.points || 0} Standpunkte`);
          if (rep.validation_status === "validated") facts.push(`Kontrollpunkte-RMS ${cm(rep.validation_rms_m)}`);
          else if (rep.fit_rms_m !== null && rep.fit_rms_m !== undefined) facts.push(`nicht unabhängig geprüft · Anpassung ${cm(rep.fit_rms_m)}`);
        }
        if (r.restored_at) facts.push(`wiederhergestellt ${when(r.restored_at)}`);
        if (foreign) facts.push("frühere Montage oder anderer Sensor");
        const previewing = this.preview === r.id;
        return `<div class="zone-row cal-history ${current ? "current" : ""}">
          <div class="grow"><div class="name">${r.origin === "before" ? "Vorher" : "Ausrichtung"}${current ? `<span class="chip ok">aktuell</span>` : ""}</div>
            <div class="meta">${escapeHtml([when(r.created_at), ...facts].join(" · "))}</div>
            <div class="actions"><button class="btn small ghost ${previewing ? "active" : ""}" data-preview="${escapeHtml(r.id)}" aria-pressed="${previewing}">Vorschau</button>
              ${current || foreign ? "" : `<button class="btn small" data-restore="${escapeHtml(r.id)}" ${measuring ? "disabled" : ""}>Zurück</button>`}</div></div></div>`;
      }).join("");
      return `<div class="cal-block"><h3>Verlauf</h3><div class="zone-list">${rows}</div>
        <p class="hint">Die Vorschau zeichnet den Sensor, wie er damals stand, gestrichelt auf den Plan. „Zurück“ stellt ihn so wieder her; der jetzige Stand bleibt im Verlauf.</p></div>`;
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
      on("[data-unpick]", () => { this.pending = null; this.pendingRole = "fit"; this.remeasure = null; this.renderSide(); this.drawExtra(); });
      on("[data-apply]", () => this.applyAlignment());
      on("[data-suggest]", () => this.suggest());
      on("[data-add-check]", () => {
        this.pendingRole = "check";
        this.remeasure = null;
        const next = (this.suggestedChecks || []).find((q) => !this.points().some((p) => p.ref.join(",") === q.join(",")));
        this.pending = next || null;
        this.renderSide();
        this.drawExtra();
      });
      on("[data-reset-model]", () => this.resetModel());
      host.querySelectorAll("[data-remeasure]").forEach((b) => b.addEventListener("click", () => {
        const point = this.points().find((p) => p.id === b.dataset.remeasure);
        if (!point) return;
        this.remeasure = point.id;
        this.pending = point.ref.slice();
        this.renderSide();
        this.drawExtra();
      }));
      on("[data-remount]", () => this.remount());
      const form = () => {
        const room = this.room();
        const mm = room.module && room.module.mounting;
        return this.moduleForm || (mm ? { ...mm } : { mode: "side", height_m: this.mounting().mount_height_m ?? 2.6, angle_deg: 30 });
      };
      host.querySelectorAll("[data-mode]").forEach((b) => b.addEventListener("click", () => {
        this.moduleForm = { ...form(), mode: b.dataset.mode };
        this.renderSide();
      }));
      // Typing changes only the form and whether there is something to
      // write. No re-render: it would pull the field out from under a
      // blur, and with it the tap on the button that caused the blur.
      for (const [id, key] of [["#cal-mheight", "height_m"], ["#cal-mangle", "angle_deg"]]) {
        const input = host.querySelector(id);
        if (input) input.addEventListener("input", () => {
          const v = Number(input.value.replace(",", "."));
          if (!Number.isFinite(v)) return;
          this.moduleForm = { ...form(), [key]: v };
          const room = this.room();
          const mm = room.module && room.module.mounting;
          const f = this.moduleForm;
          const same = mm && mm.mode === f.mode && Math.abs(mm.height_m - f.height_m) < 0.005 && Math.abs(mm.angle_deg - f.angle_deg) < 0.005;
          const button = host.querySelector("[data-write-mount]");
          if (button) button.disabled = !!same || !(room.module && room.module.connected) || !!this.busy();
        });
      }
      on("[data-write-mount]", () => this.writeModuleMounting(form()));
      host.querySelectorAll("[data-preview]").forEach((b) => b.addEventListener("click", () => {
        this.preview = this.preview === b.dataset.preview ? null : b.dataset.preview;
        this.renderSide();
        this.drawExtra();
      }));
      host.querySelectorAll("[data-restore]").forEach((b) => b.addEventListener("click", () => this.restore(b.dataset.restore)));
      const mount = host.querySelector("#cal-mount");
      if (mount) mount.addEventListener("change", () => {
        const v = mount.value.trim() === "" ? null : Number(mount.value.replace(",", "."));
        if (v !== null && !(v >= 0.2 && v <= 4)) { toast("Höhe zwischen 0,2 und 4 m", "err"); return; }
        this.typedMount = v;
        this.saveMounting();
      });
      const target = host.querySelector("#cal-target");
      if (target) target.addEventListener("change", () => { this.typedTarget = Number(target.value); this.saveMounting(); });
      on("[data-clear]", () => this.clearPoints());
      host.querySelectorAll("[data-drop]").forEach((b) => b.addEventListener("click", () => this.dropPoint(b.dataset.drop)));
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
          const isCheck = p.role === "check";
          const label = isCheck ? `K${this.points().slice(0, i + 1).filter((q) => q.role === "check").length}`
            : String(this.points().slice(0, i + 1).filter((q) => q.role !== "check").length);
          svg += isCheck
            ? `<g class="cal-mark check"><rect x="${p.ref[0] - 0.14}" y="${p.ref[1] - 0.14}" width="0.28" height="0.28" rx="0.04"/>
                <text x="${p.ref[0]}" y="${p.ref[1]}" text-anchor="middle" dominant-baseline="central">${label}</text></g>`
            : `<g class="cal-mark"><circle cx="${p.ref[0]}" cy="${p.ref[1]}" r="0.14"/>
                <text x="${p.ref[0]}" y="${p.ref[1]}" text-anchor="middle" dominant-baseline="central">${label}</text></g>`;
        });
        if (this.pending) {
          const check = this.pendingRole === "check" && !this.remeasure;
          const n = this.points().filter((q) => (q.role === "check") === check).length + 1;
          svg += `<g class="cal-mark pending"><circle cx="${this.pending[0]}" cy="${this.pending[1]}" r="0.14"/>
            <text x="${this.pending[0]}" y="${this.pending[1]}" text-anchor="middle" dominant-baseline="central">${this.remeasure ? "↻" : check ? `K${n}` : n}</text></g>`;
        }
        if (pr && this.points().length) svg += this.plan.sensorGhostSvg(pr);
        const shown = this.preview && (room.calibration.alignment_history || []).find((r) => r.id === this.preview);
        if (shown) {
          const then = { ...room.sensor, ...shown.sensor };
          svg += `<g class="cal-preview">${this.plan.sensorGhostSvg(then)}`;
          for (const p of shown.standpoints || []) {
            const seen = G.toRoom(p.raw[0], p.raw[1], then);
            svg += `<line class="cal-error" x1="${p.ref[0]}" y1="${p.ref[1]}" x2="${seen.x.toFixed(3)}" y2="${seen.y.toFixed(3)}"/>
              <circle class="cal-seen" cx="${seen.x.toFixed(3)}" cy="${seen.y.toFixed(3)}" r="0.05"/>
              <circle class="cal-ref" cx="${p.ref[0]}" cy="${p.ref[1]}" r="0.09"/>`;
          }
          svg += "</g>";
        }
      }
      this.plan.setExtra(svg);
    },

    // ------------------------------------------------------- recordings

    async resume() {
      try {
        this.capture = await api(`api/rooms/${encodeURIComponent(this.id)}/capture`);
      } catch { this.capture = null; }
      const cap = this.capture;
      if (cap && cap.kind === "point") {
        // Asking for it has kept a finished one with the standpoints; a
        // running one knows where the person stands.
        if (cap.phase === "done" || !cap.standpoint) this.capture = null;
        else { this.measuring = cap.standpoint.ref; this.tab = "align"; }
        if (cap.kept) await this.reloadRoom();
      } else if (cap && cap.kind === "empty") this.tab = "spots";
      this.renderSide();
      this.drawExtra();
      if (this.busy()) this.poll();
      else if (this.points().length) this.solve();
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

    async finished() {
      const cap = this.capture;
      if (cap.kind === "point") {
        const r = cap.result;
        this.capture = null;
        this.measuring = null;
        if (cap.kept) {
          // The server has kept it with the room's standpoints.
          await this.reloadRoom();
          this.remeasure = null;
          this.queueNext();
          await this.solve();
          if (r.warnings.length) toast(r.warnings[0]);
          return;
        }
        toast(cap.keep_error || (r ? r.error : "Messung fehlgeschlagen"), "err");
        if (cap.keep_error) await this.reloadRoom();
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
          body: { interference: { spots: r.spots, device_id: r.device_id, epoch: this.capture.epoch } } });
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
      const role = this.pendingRole === "check" ? "check" : "fit";
      const same = this.points().filter((q) => q.role === role).length;
      if (same >= (role === "check" ? 6 : 12)) { toast(role === "check" ? "Sechs Kontrollpunkte reichen." : "Mehr als zwölf Standpunkte bringen nichts mehr."); return; }
      this.remeasure = null;
      this.pending = p;
      this.pendingRole = this.pendingRole === "check" ? "check" : "fit";
      this.renderSide();
      this.drawExtra();
    },

    async measure() {
      if (!this.pending) return;
      const again = this.remeasure ? this.points().find((p) => p.id === this.remeasure) : null;
      this.measuring = this.pending;
      await this.start({ kind: "point", delay_s: 3, duration_s: 5, standpoint: {
        ref: this.pending, role: again ? again.role : this.pendingRole || "fit", replace_id: again ? again.id : null } });
      if (!this.capture) this.measuring = null;
    },

    async dropPoint(id) {
      try { this.takeRoom(await api(`api/rooms/${encodeURIComponent(this.id)}/alignment/points/${encodeURIComponent(id)}`, { method: "DELETE" })); }
      catch (err) { toast(err.message, "err"); await this.reloadRoom(); }
      await this.solve();
    },

    async clearPoints() {
      const ok = await E.confirmDialog({ title: "Standpunkte verwerfen?", text: "Alle gemessenen Standpunkte dieses Raums werden entfernt. Die übernommene Ausrichtung bleibt.",
        confirm: "Verwerfen", danger: true });
      if (!ok) return;
      try { this.takeRoom(await api(`api/rooms/${encodeURIComponent(this.id)}/alignment/points`, { method: "DELETE" })); }
      catch (err) { toast(err.message, "err"); }
      this.proposal = null; this.suggested = null; this.suggestedChecks = null; this.pending = null; this.remeasure = null;
      this.renderSide();
      this.drawExtra();
    },

    async solve() {
      const points = this.points();
      if (!points.length) { this.proposal = null; this.renderSide(); this.drawExtra(); return; }
      try {
        const m = this.mounting();
        // From the standpoints kept on the server; the answer names what it
        // was computed for, and applying checks that.
        this.proposal = await api(`api/rooms/${encodeURIComponent(this.id)}/alignment`, { method: "POST",
          body: { mount_height_m: m.mount_height_m, target_height_m: m.target_height_m } });
      } catch (err) { this.proposal = null; toast(err.message, "err"); }
      this.renderSide();
      this.drawExtra();
    },

    async applyAlignment() {
      const pr = this.proposal;
      if (!pr) return;
      try {
        // The server computes it once more from the kept standpoints and
        // saves it only if the room is still what `basis` says.
        this.takeRoom(await api(`api/rooms/${encodeURIComponent(this.id)}/alignment/apply`, { method: "POST", body: { basis: pr.basis } }));
        this.typedMount = undefined;
        this.typedTarget = undefined;
        // The standpoints stay: measured raw, they now show how well the
        // new placement fits them.
        await this.solve();
        toast(pr.validation_status === "validated"
          ? `Übernommen — Kontrollpunkte-RMS ${cm(pr.validation_rms_m)}.`
          : "Übernommen — noch nicht unabhängig geprüft. Kontrollpunkte zeigen, wie genau es im Raum ist.");
      } catch (err) {
        toast(err.message, "err");
        if (err.status === 409) {
          // Computed for a room that has changed since: compute it again
          // and let the user look before applying.
          if (err.detail && err.detail.room) this.takeRoom(err.detail.room);
          else await E.refresh();
          await this.solve();
        }
      }
      this.onData();
      this.renderSide();
      this.drawExtra();
    },

    async suggest() {
      try {
        const r = await api(`api/rooms/${encodeURIComponent(this.id)}/alignment/suggest?count=7`);
        // Five to fit, the rest kept back as control spots.
        this.suggested = r.points.slice(0, 5);
        this.suggestedChecks = r.points.slice(5);
      } catch (err) { toast(err.message, "err"); return; }
      if (!this.suggested.length) toast("Im geplanten Sichtbereich ist kein Platz für Standpunkte — stimmen Sensor und Wände?", "err");
      this.remeasure = null;
      this.queueNext();
      this.renderSide();
      this.drawExtra();
    },

    async saveMounting() {
      const m = this.mounting();
      try {
        await api(`api/rooms/${encodeURIComponent(this.id)}/calibration`, { method: "PUT", body: { mounting: m } });
        await E.refresh();
        this.typedMount = undefined;
        this.typedTarget = undefined;
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

    async remount() {
      const ok = await E.confirmDialog({ title: "Sensor neu montiert?",
        text: "Abgenommen und wieder aufgehängt, anders gedreht, oder ein anderes Modul am selben Platz: Ausrichtung, Sensormodell, gelernte Störquellen und die Standpunkte gehören dann zur alten Montage und werden verworfen. Die Zeichnung auf dem Plan und die Höhe bleiben. Nur die Zeichnung korrigieren geht im Raumeditor, ohne das hier.",
        confirm: "Verwerfen und neu beginnen", danger: true });
      if (!ok) return;
      const room = this.room();
      try {
        this.takeRoom(await api(`api/rooms/${encodeURIComponent(this.id)}/remount`, { method: "POST", body: { revision: room.revision } }));
        toast("Verworfen. Jetzt Störquellen lernen und neu ausrichten.");
      } catch (err) {
        toast(err.message, "err");
        await E.refresh();
      }
      this.capture = null; this.measuring = null;
      this.proposal = null; this.suggested = null; this.suggestedChecks = null; this.pending = null; this.remeasure = null; this.preview = null;
      this.renderSide();
      this.drawExtra();
    },

    async writeModuleMounting(f) {
      const room = this.room();
      const cal = room.calibration || {};
      const measured = cal.aligned_at || (cal.interference || []).length || this.points().length;
      if (!(f.height_m >= 0.5 && f.height_m <= 5 && f.angle_deg >= 0 && f.angle_deg <= 90)) {
        toast("Höhe 0,5–5 m und Neigung 0–90°", "err");
        return;
      }
      if (measured) {
        const ok = await E.confirmDialog({ title: "Montage im Modul ändern?",
          text: "Das Modul meldet danach andere Positionen. Ausrichtung, Sensormodell, Störquellen und Standpunkte passen dann nicht mehr und werden verworfen — danach neu lernen und ausrichten.",
          confirm: "Schreiben und verwerfen", danger: true });
        if (!ok) return;
      }
      const btn = this.el.querySelector("[data-write-mount]");
      if (btn) btn.disabled = true;
      try {
        const r = await api(`api/devices/${encodeURIComponent(room.sensor.device_id)}/mounting`, { method: "PUT",
          body: { mode: f.mode, height_m: f.height_m, angle_deg: f.angle_deg } });
        if (r.confirmed) toast("Das Modul hat die Montage übernommen.");
        else toast(r.mounting
          ? `Das Modul hat die Änderung nicht bestätigt — es meldet weiter ${r.mounting.mode === "side" ? "Wand" : "Decke"}, ${f2(r.mounting.height_m)} m, ${E.formatNumber(r.mounting.angle_deg, 1)}°.`
          : "Das Modul hat die Änderung nicht bestätigt.", "err");
        this.moduleForm = null;
      } catch (err) { toast(err.message, "err"); }
      await E.refresh();
      this.proposal = null; this.suggested = null; this.suggestedChecks = null; this.pending = null; this.remeasure = null;
      if (this.points().length) await this.solve();
      else { this.renderSide(); this.drawExtra(); }
    },

    async restore(id) {
      const room = this.room();
      const record = (room.calibration.alignment_history || []).find((r) => r.id === id);
      if (!record) return;
      const ok = await E.confirmDialog({ title: "Diesen Stand wiederherstellen?",
        text: `Der Sensor steht dann wieder wie am ${when(record.created_at)}${record.origin === "before" ? ", vor der Ausrichtung" : ""}. Der jetzige Stand bleibt im Verlauf.`,
        confirm: "Wiederherstellen" });
      if (!ok) return;
      try {
        this.takeRoom(await api(`api/rooms/${encodeURIComponent(this.id)}/alignment/restore`, { method: "POST", body: { id, revision: room.revision } }));
        this.preview = null;
        toast("Wiederhergestellt.");
      } catch (err) {
        toast(err.message, "err");
        if (err.detail && err.detail.room) this.takeRoom(err.detail.room);
        else await E.refresh();
      }
      if (this.points().length) await this.solve();
      else { this.renderSide(); this.drawExtra(); }
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
