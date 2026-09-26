// The floor plan: one SVG in metres, drawn from a room and a live result.
//
// Three modes share the drawing code:
//   live   the map on a room page — zones light up, targets move
//   edit   the same map with handles: draw zones, move furniture, turn the
//          sensor; every change goes through commit() so it can be undone
//   thumb  the small map on an overview tile
//
// Live maps can also take a tap on the floor (options.onPick) and carry an
// extra layer of their owner's drawing (setExtra) — the calibration uses
// both to mark standpoints and to preview a proposed placement.
//
// Room coordinates: metres, origin top-left, x right, y down. A sensor at
// angle 0 looks down the plan; see app/geometry.py.

const Plan = (() => {
  const G = window.EcholotGeometry;
  const { escapeHtml } = Echolot;
  const PAD = 0.5;
  const SNAP = 0.05;
  const TRAIL = 14;
  const NS = "http://www.w3.org/2000/svg";

  const FURNITURE = {
    sofa: { label: "Sofa", w: 2.2, h: 0.9, icon: "living" },
    armchair: { label: "Sessel", w: 0.9, h: 0.9, icon: "armchair" },
    bed: { label: "Bett", w: 1.6, h: 2.0, icon: "bed" },
    table: { label: "Tisch", w: 1.6, h: 0.9, icon: "table" },
    desk: { label: "Schreibtisch", w: 1.4, h: 0.7, icon: "desk" },
    chair: { label: "Stuhl", w: 0.5, h: 0.5, icon: "chair" },
    tv: { label: "TV", w: 1.4, h: 0.35, icon: "tv" },
    wardrobe: { label: "Schrank", w: 1.8, h: 0.6, icon: "wardrobe" },
    plant: { label: "Pflanze", w: 0.5, h: 0.5, icon: "plant" },
    door: { label: "Tür", w: 0.9, h: 0.9, icon: "door" },
    window: { label: "Fenster", w: 1.2, h: 0.15, icon: "window" },
    kitchen: { label: "Küchenzeile", w: 2.4, h: 0.6, icon: "kitchen" },
    bath: { label: "Wanne", w: 1.7, h: 0.75, icon: "bath" },
    other: { label: "Objekt", w: 1.0, h: 1.0, icon: "generic" },
  };

  const round = (v, step = 0.01) => Math.round(v / step) * step;
  const r3 = (v) => Math.round(v * 1000) / 1000;
  const fmt = (v) => r3(v).toString();
  const zoneColor = (i) => `var(--z${(i || 0) % 8})`;
  const newId = (prefix) => prefix + Math.random().toString(16).slice(2, 10);

  function rotate(dx, dy, deg) {
    const a = (deg * Math.PI) / 180;
    return [dx * Math.cos(a) - dy * Math.sin(a), dx * Math.sin(a) + dy * Math.cos(a)];
  }

  function furnitureDetail(item) {
    const { w, h } = item;
    const m = Math.min(w, h);
    switch (item.kind) {
      case "sofa": {
        const b = Math.min(0.22, h * 0.28), a = Math.min(0.2, w * 0.12);
        return `<path class="detail" d="M${a} ${b} H${w - a} M${a} 0 V${h} M${w - a} 0 V${h}"/>`;
      }
      case "armchair": {
        const b = h * 0.28, a = w * 0.2;
        return `<path class="detail" d="M${a} ${b} H${w - a} M${a} 0 V${h} M${w - a} 0 V${h}"/>`;
      }
      case "bed": {
        const pw = w * 0.36, ph = Math.min(0.35, h * 0.16), gap = w * 0.08;
        return `<rect class="detail" x="${gap}" y="${gap}" width="${pw}" height="${ph}" rx="0.06"/>
                <rect class="detail" x="${w - gap - pw}" y="${gap}" width="${pw}" height="${ph}" rx="0.06"/>
                <path class="detail" d="M0 ${gap * 2 + ph} H${w}"/>`;
      }
      case "wardrobe": return `<path class="detail" d="M${w / 2} 0 V${h}"/>`;
      case "plant": return `<circle class="detail" cx="${w / 2}" cy="${h / 2}" r="${m * 0.3}"/>`;
      case "door": return `<path class="detail" d="M0 ${h} V0 A${w} ${h} 0 0 1 ${w} ${h}"/>`;
      case "window": return `<path class="detail" d="M0 ${h / 2} H${w}"/>`;
      case "tv": return "";
      case "kitchen": {
        let d = "";
        for (let x = 0.6; x < w; x += 0.6) d += `M${x} 0 V${h} `;
        return `<path class="detail" d="${d}"/>`;
      }
      case "bath": return `<rect class="detail" x="0.1" y="0.1" width="${w - 0.2}" height="${h - 0.2}" rx="${Math.min(0.3, h / 2 - 0.1)}"/>`;
      case "chair": return `<path class="detail" d="M0 ${h * 0.2} H${w}"/>`;
      default: return "";
    }
  }

  class PlanView {
    constructor(host, options = {}) {
      this.host = host;
      this.mode = options.mode || "live";
      this.onChange = options.onChange || (() => {});
      this.onSelect = options.onSelect || (() => {});
      this.onHint = options.onHint || (() => {});
      this.onPick = options.onPick || null;
      this.room = null;
      this.live = null;
      this.tool = "select";
      this.snap = true;
      this.selection = null; // {kind: "sensor"|"zone"|"furniture", id}
      this.draft = null;
      this.drag = null;
      this.history = [];
      this.historyIndex = -1;
      this.trails = [];
      this.lastFrames = null;
      this.targetEls = [];

      this.svg = document.createElementNS(NS, "svg");
      this.svg.setAttribute("role", "img");
      this.svg.setAttribute("aria-label", "Raumkarte");
      this.host.appendChild(this.svg);
      this.gStatic = this.layer("pl-static");
      this.gExtra = this.layer("pl-extra");
      this.gLive = this.layer("pl-live");
      this.gOverlay = this.layer("pl-overlay");

      if (this.mode === "live" && this.onPick) {
        this.svg.classList.add("picking");
        this.svg.addEventListener("click", (e) => {
          if (!this.room) return;
          const [x, y] = this.point(e);
          const r = this.room;
          if (x < -0.05 || y < -0.05 || x > r.width + 0.05 || y > r.height + 0.05) return;
          this.onPick([r3(G.clamp(x, 0, r.width)), r3(G.clamp(y, 0, r.height))]);
        });
      }

      if (this.mode === "edit") {
        this.svg.classList.add("editing");
        this.svg.addEventListener("pointerdown", (e) => this.pointerDown(e));
        this.svg.addEventListener("pointermove", (e) => this.pointerMove(e));
        this.svg.addEventListener("pointerup", (e) => this.pointerUp(e));
        this.svg.addEventListener("pointercancel", (e) => this.pointerUp(e));
        this.svg.addEventListener("dblclick", (e) => this.doubleClick(e));
        this.keyHandler = (e) => this.key(e);
        document.addEventListener("keydown", this.keyHandler);
      }
    }

    destroy() {
      if (this.keyHandler) document.removeEventListener("keydown", this.keyHandler);
      this.svg.remove();
    }

    layer(cls) {
      const g = document.createElementNS(NS, "g");
      g.setAttribute("class", cls);
      this.svg.appendChild(g);
      return g;
    }

    // ------------------------------------------------------------ state

    setRoom(room, { resetHistory = true } = {}) {
      this.room = JSON.parse(JSON.stringify(room));
      this.syncLinkedZones();
      if (resetHistory) {
        this.history = [JSON.stringify(this.snapshotable())];
        this.historyIndex = 0;
        this.savedJson = this.history[0];
      }
      this.render();
    }

    snapshotable() {
      const r = this.room;
      return { name: r.name, icon: r.icon, width: r.width, height: r.height, outline: r.outline || null, sensor: r.sensor,
        furniture: r.furniture, zones: r.zones, hold_s: r.hold_s, edge_margin_m: r.edge_margin_m,
        image: r.image ? { opacity: r.image.opacity } : null };
    }

    commit() {
      const json = JSON.stringify(this.snapshotable());
      if (json === this.history[this.historyIndex]) return;
      this.history = this.history.slice(0, this.historyIndex + 1);
      this.history.push(json);
      if (this.history.length > 80) this.history.shift();
      this.historyIndex = this.history.length - 1;
      this.onChange();
    }

    restore(index) {
      const data = JSON.parse(this.history[index]);
      const image = this.room.image;
      Object.assign(this.room, data);
      this.room.image = image ? { ...image, opacity: data.image ? data.image.opacity : image.opacity } : null;
      this.historyIndex = index;
      if (this.selection && !this.find(this.selection)) this.select(null);
      this.render();
      this.onChange();
    }

    canUndo() { return this.historyIndex > 0; }
    canRedo() { return this.historyIndex < this.history.length - 1; }
    undo() { if (this.canUndo()) this.restore(this.historyIndex - 1); }
    redo() { if (this.canRedo()) this.restore(this.historyIndex + 1); }
    isDirty() { return this.history[this.historyIndex] !== this.savedJson; }

    mutate(fn) {
      fn(this.room);
      this.render();
      this.commit();
    }

    find(sel) {
      if (!sel) return null;
      if (sel.kind === "sensor") return this.room.sensor;
      // The walls edit like a zone's corners; the wrapper shares the array.
      if (sel.kind === "outline") return this.room.outline ? { points: this.room.outline } : null;
      const list = sel.kind === "zone" ? this.room.zones : this.room.furniture;
      return list.find((x) => x.id === sel.id) || null;
    }

    select(sel) {
      // A zone that stands for a furniture item is edited through it.
      const zone = sel && sel.kind === "zone" ? this.room.zones.find((z) => z.id === sel.id) : null;
      if (zone && zone.furniture_id) sel = { kind: "furniture", id: zone.furniture_id };
      this.selection = sel;
      this.render();
      this.onSelect(sel);
    }

    setTool(tool, { target = "zone" } = {}) {
      this.tool = tool;
      this.polyTarget = tool === "poly" ? target : "zone";
      this.draft = null;
      this.render();
      const hints = {
        select: "Antippen wählt aus, Ziehen verschiebt. Eckpunkte ziehen, Mittelpunkte ziehen fügt einen Eckpunkt hinzu, Doppelklick auf einen Eckpunkt entfernt ihn.",
        rect: "Rechteck aufziehen, um eine Zone anzulegen.",
        poly: this.polyTarget === "outline"
          ? "Die Wände Ecke für Ecke nachzeichnen, rundherum. Auf die erste Ecke tippen oder Enter schließt, Esc bricht ab."
          : "Eckpunkte antippen. Auf den ersten Punkt tippen oder Enter schließt die Zone, Esc bricht ab.",
      };
      this.onHint(hints[tool] || "");
    }

    // The walls as drawn: the outline, or the plan's rectangle.
    wallPoints() {
      const r = this.room;
      return r.outline && r.outline.length >= 3 ? r.outline
        : [[0, 0], [r.width, 0], [r.width, r.height], [0, r.height]];
    }

    setLive(result) {
      this.live = result;
      // Zone fills and badges live in the static layer; redraw it when
      // what they show changed, and only then.
      const signature = result && result.available
        ? (result.zones || []).map((z) => `${z.id}:${z.occupied ? 1 : 0}:${z.count}`).join("|")
        : "off";
      if (signature !== this.zoneSignature) {
        this.zoneSignature = signature;
        if (!this.drag) { this.render(); return; }
      }
      this.renderLive();
    }

    // ------------------------------------------------------------ drawing

    render() {
      if (!this.room) return;
      this.syncLinkedZones();
      const r = this.room;
      const pad = this.mode === "thumb" ? 0.15 : PAD;
      this.svg.setAttribute("viewBox", `${-pad} ${-pad} ${r.width + 2 * pad} ${r.height + 2 * pad}`);
      this.svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
      const uid = this.uid || (this.uid = Math.random().toString(36).slice(2, 8));
      const shaped = !!(r.outline && r.outline.length >= 3);
      const walls = this.wallPoints();
      const wallPts = walls.map((p) => `${fmt(p[0])},${fmt(p[1])}`).join(" ");
      const crossed = shaped && G.selfIntersects(walls);
      let html = `<defs>
        <clipPath id="clip-${uid}"><polygon points="${wallPts}"/></clipPath>
        <pattern id="hatch-${uid}" width="0.18" height="0.18" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
          <path d="M0 0 V0.18" stroke="var(--danger)" stroke-opacity=".35" stroke-width=".03"/>
        </pattern>
        <pattern id="out-${uid}" width="0.14" height="0.14" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
          <path d="M0 0 V0.14" stroke="var(--faint)" stroke-opacity=".45" stroke-width=".02"/>
        </pattern></defs>`;
      // Outside the walls but on the plan: still clickable, drawn as
      // not-room.
      if (shaped) html += `<rect class="pl-canvas" x="0" y="0" width="${r.width}" height="${r.height}" data-kind="floor"/>`;
      html += `<polygon class="pl-floor" points="${wallPts}" data-kind="floor"/>`;
      if (r.image && r.image.url) {
        html += `<image href="${escapeHtml(r.image.url)}" x="0" y="0" width="${r.width}" height="${r.height}"
                 preserveAspectRatio="none" opacity="${r.image.opacity}" pointer-events="none"/>`;
      }
      if (shaped) {
        html += `<path class="pl-outside" fill-rule="evenodd" pointer-events="none"
                  d="M0 0H${r.width}V${r.height}H0Z M${walls.map((p) => `${fmt(p[0])} ${fmt(p[1])}`).join(" L")}Z"/>
                 <path fill="url(#out-${uid})" fill-rule="evenodd" pointer-events="none"
                  d="M0 0H${r.width}V${r.height}H0Z M${walls.map((p) => `${fmt(p[0])} ${fmt(p[1])}`).join(" L")}Z"/>`;
      }
      if (this.mode === "edit") html += `<polygon class="pl-wall-hit" points="${wallPts}" data-kind="outline"/>`;
      html += `<g clip-path="url(#clip-${uid})">`;
      if (this.mode !== "thumb") {
        let d = "";
        for (let x = 0.5; x < r.width; x += 0.5) d += `M${fmt(x)} 0V${r.height}`;
        for (let y = 0.5; y < r.height; y += 0.5) d += `M0 ${fmt(y)}H${r.width}`;
        html += `<path class="pl-grid-line" d="${d}" pointer-events="none"/>`;
      }
      html += this.fovPath();
      html += `</g>`;
      for (const item of r.furniture) html += this.furnitureSvg(item);
      r.zones.forEach((zone) => { html += this.zoneSvg(zone, uid); });
      if (this.mode !== "thumb") html += this.spotsSvg();
      const wallSel = this.mode === "edit" && this.selection && this.selection.kind === "outline";
      html += `<polygon class="pl-wall ${crossed ? "invalid" : ""} ${wallSel ? "selected" : ""}" points="${wallPts}" pointer-events="none"/>`;
      if (this.mode !== "thumb") {
        for (let x = 0; x <= r.width + 1e-6; x += 1) html += `<text class="pl-ruler" x="${x}" y="-0.18" text-anchor="middle">${x}</text>`;
        for (let y = 1; y <= r.height + 1e-6; y += 1) html += `<text class="pl-ruler" x="-0.14" y="${y}" text-anchor="end" dominant-baseline="middle">${y}</text>`;
        html += `<text class="pl-ruler" x="${r.width + 0.12}" y="-0.18">m</text>`;
      }
      html += this.sensorSvg();
      this.gStatic.innerHTML = html;
      this.renderOverlay();
      this.renderLive();
    }

    // Learned reflectors: stored in sensor coordinates, drawn where the
    // current placement puts them. Only the sensor they were learned
    // with has them.
    activeSpots() {
      const r = this.room;
      const cal = r.calibration;
      if (!cal || !r.sensor.device_id || cal.interference_device_id !== r.sensor.device_id) return [];
      return cal.interference || [];
    }

    spotsSvg(spots = this.activeSpots(), placement = this.room.sensor, cls = "") {
      return spots.map((spot) => {
        const c = G.toRoom(spot.x, spot.y, placement);
        return `<g class="pl-spot ${cls}" pointer-events="none">
            <circle cx="${fmt(c.x)}" cy="${fmt(c.y)}" r="${fmt(spot.r)}"/>
            <path d="M${fmt(c.x - 0.07)} ${fmt(c.y - 0.07)} L${fmt(c.x + 0.07)} ${fmt(c.y + 0.07)} M${fmt(c.x + 0.07)} ${fmt(c.y - 0.07)} L${fmt(c.x - 0.07)} ${fmt(c.y + 0.07)}"/>
          </g>`;
      }).join("");
    }

    // Anything the owner wants drawn in room metres, under the targets.
    setExtra(svg) {
      this.gExtra.innerHTML = svg || "";
    }

    sensorGhostSvg(placement) {
      const size = 0.17;
      return `<g class="pl-sensor ghost" pointer-events="none"
                transform="translate(${fmt(placement.x)} ${fmt(placement.y)}) rotate(${placement.angle || 0})">
          <circle class="pl-sensor-body" r="${size}"/>
          <path class="pl-sensor-nose" d="M${-size * 0.45} ${-size * 0.1} L${size * 0.45} ${-size * 0.1} L0 ${size * 0.62} Z"/>
        </g>`;
    }

    fovPath() {
      const s = this.room.sensor;
      const half = (s.fov_deg || 120) / 2;
      const range = s.range_m || 6;
      const steps = 24;
      let d = `M${fmt(s.x)} ${fmt(s.y)}`;
      for (let i = 0; i <= steps; i++) {
        const t = ((-half + (2 * half * i) / steps) * Math.PI) / 180;
        const p = G.toRoom(range * Math.sin(t), range * Math.cos(t), { ...s, mirror: false });
        d += ` L${fmt(p.x)} ${fmt(p.y)}`;
      }
      return `<path class="pl-fov" d="${d} Z" pointer-events="none"/>`;
    }

    furnitureSvg(item) {
      const sel = this.mode === "edit" && this.selection && this.selection.kind === "furniture" && this.selection.id === item.id;
      const label = item.name && item.w >= 0.7 && this.mode !== "thumb"
        ? `<text x="${item.w / 2}" y="${item.h / 2}" text-anchor="middle" dominant-baseline="middle">${escapeHtml(item.name)}</text>` : "";
      return `<g class="pl-furn" data-kind="furniture" data-id="${escapeHtml(item.id)}"
                transform="translate(${fmt(item.x)} ${fmt(item.y)}) rotate(${item.angle || 0} ${item.w / 2} ${item.h / 2})">
          <rect class="body ${sel ? "pl-selected" : ""}" width="${item.w}" height="${item.h}" rx="${Math.min(0.08, item.w / 4, item.h / 4)}"/>
          ${furnitureDetail(item)}${label}</g>`;
    }

    zoneState(zoneId) {
      if (!this.live || !this.live.available) return null;
      return (this.live.zones || []).find((z) => z.id === zoneId) || null;
    }

    zoneSvg(zone, uid) {
      const exclude = zone.kind === "exclude";
      const entry = zone.kind === "entry";
      const color = exclude ? "var(--danger)" : entry ? "var(--muted)" : zoneColor(zone.color);
      const state = zone.kind === "detect" ? this.zoneState(zone.id) : null;
      const occupied = state && state.occupied;
      const sel = this.mode === "edit" && this.selection && this.selection.kind === "zone" && this.selection.id === zone.id;
      const points = zone.points.map((p) => `${fmt(p[0])},${fmt(p[1])}`).join(" ");
      const fill = exclude ? `url(#hatch-${uid})` : color;
      const opacity = exclude ? 1 : entry ? 0.08 : occupied ? 0.34 : 0.13;
      // Name in the zone's top-left corner rather than its middle, where
      // the furniture it was drawn around already carries a label.
      const xs = zone.points.map((p) => p[0]), ys = zone.points.map((p) => p[1]);
      const lx = Math.min(...xs) + 0.1, ly = Math.min(...ys) + 0.2;
      let label = "";
      if (this.mode !== "thumb") {
        label = `<text class="pl-zone-label" x="${fmt(lx)}" y="${fmt(ly)}" style="fill:${color}">${escapeHtml(zone.name)}</text>`;
        if (state && state.count > 0) {
          const bx = Math.max(...xs) - 0.22, by = Math.min(...ys) + 0.2;
          label += `<g class="pl-zone-badge" transform="translate(${fmt(bx)} ${fmt(by)})">
              <rect x="-0.16" y="-0.12" width="0.32" height="0.24" rx="0.12"/>
              <text text-anchor="middle" dominant-baseline="central">${state.count}</text></g>`;
        }
      }
      return `<g data-kind="zone" data-id="${escapeHtml(zone.id)}">
          <polygon class="pl-zone ${exclude ? "exclude" : entry ? "entry" : ""} ${sel ? "pl-selected" : ""}" points="${points}"
                   style="fill:${fill};fill-opacity:${opacity};stroke:${color}"/>${label}</g>`;
    }

    sensorSvg() {
      const s = this.room.sensor;
      const missing = !s.device_id;
      const sel = this.mode === "edit" && this.selection && this.selection.kind === "sensor";
      const size = this.mode === "thumb" ? 0.22 : 0.17;
      return `<g class="pl-sensor ${missing ? "missing" : ""}" data-kind="sensor"
                transform="translate(${fmt(s.x)} ${fmt(s.y)}) rotate(${s.angle || 0})">
          <circle class="pl-sensor-body ${sel ? "pl-selected" : ""}" r="${size}"/>
          <path class="pl-sensor-nose" d="M${-size * 0.45} ${-size * 0.1} L${size * 0.45} ${-size * 0.1} L0 ${size * 0.62} Z"/>
        </g>`;
    }

    // -------------------------------------------------------------- live

    renderLive() {
      if (!this.room) return;
      const result = this.live;
      const targets = result && result.available ? result.targets : [];
      const frames = result && result.sensor ? result.sensor.frames_received : null;

      // A trail of where targets were, frame by frame — no identities
      // needed, the module does not have any.
      if (frames !== null && frames !== this.lastFrames) {
        this.lastFrames = frames;
        this.trails.push(targets.filter((t) => t.status === "counted").map((t) => [t.x, t.y]));
        if (this.trails.length > TRAIL) this.trails.shift();
      }
      if (!result || !result.available) this.trails = [];

      let ghosts = "";
      if (this.mode !== "thumb") {
        this.trails.slice(0, -1).forEach((frame, i) => {
          const opacity = ((i + 1) / this.trails.length) * 0.22;
          for (const [x, y] of frame) ghosts += `<circle class="pl-ghost" cx="${fmt(x)}" cy="${fmt(y)}" r="0.05" opacity="${opacity.toFixed(3)}"/>`;
        });
      }
      let ghostLayer = this.gLive.querySelector(".ghosts");
      if (!ghostLayer) {
        ghostLayer = document.createElementNS(NS, "g");
        ghostLayer.setAttribute("class", "ghosts");
        this.gLive.appendChild(ghostLayer);
      }
      ghostLayer.innerHTML = ghosts;

      // Keep each dot's element across frames — by the target's id where
      // the server follows targets, else by the nearest previous position
      // — so a dot glides instead of jumping between people when the
      // module reorders its list.
      const previous = this.targetEls.slice();
      const next = [];
      for (const t of targets) {
        let best = t.id !== undefined ? previous.findIndex((el) => el && el._id === t.id) : -1;
        let bestDist = 1.2;
        if (best < 0) previous.forEach((el, i) => {
          if (!el || (t.id !== undefined && el._id !== undefined)) return;
          const d = Math.hypot(el._x - t.x, el._y - t.y);
          if (d < bestDist) { bestDist = d; best = i; }
        });
        let el;
        if (best >= 0) { el = previous[best]; previous[best] = null; }
        else {
          el = document.createElementNS(NS, "g");
          const r = this.mode === "thumb" ? 0.16 : 0.11;
          el.innerHTML = `<circle class="halo" r="${r * 2}"/><circle class="pulse" r="${r * 1.6}"/><circle class="dot" r="${r}"/>`;
          el.style.transform = `translate(${t.x}px, ${t.y}px)`;
          this.gLive.appendChild(el);
        }
        el._x = t.x; el._y = t.y; el._id = t.id;
        el.setAttribute("class", `pl-target ${t.status}`);
        el.style.transform = `translate(${t.x}px, ${t.y}px)`;
        const title = {
          outside: "außerhalb des Raums — zählt nicht",
          excluded: "in einer Ausschlusszone — zählt nicht",
          pending: "noch nicht bestätigt — zählt, wenn es bleibt",
          interference: "an einer bekannten Störquelle — zählt nicht",
          held: "gerade nicht gemeldet — zählt noch, wo es zuletzt war",
        }[t.status] || "erkanntes Ziel";
        el.setAttribute("aria-label", title);
        next.push(el);
      }
      for (const el of previous) if (el) el.remove();
      this.targetEls = next;
      this.gStatic.classList.toggle("pl-stale", false);
    }

    // ----------------------------------------------------------- overlay

    renderOverlay() {
      if (this.mode !== "edit") { this.gOverlay.innerHTML = ""; return; }
      let html = "";
      const sel = this.selection;
      const item = this.find(sel);
      const hr = 0.09;
      if (sel && (sel.kind === "zone" || sel.kind === "outline") && item) {
        item.points.forEach((p, i) => {
          const q = item.points[(i + 1) % item.points.length];
          const mx = (p[0] + q[0]) / 2, my = (p[1] + q[1]) / 2;
          html += `<circle class="pl-handle mid" data-handle="mid" data-index="${i}" cx="${fmt(mx)}" cy="${fmt(my)}" r="${hr * 0.7}"/>`;
        });
        item.points.forEach((p, i) => {
          html += `<circle class="pl-handle" data-handle="vertex" data-index="${i}" cx="${fmt(p[0])}" cy="${fmt(p[1])}" r="${hr}"/>`;
        });
      }
      if (sel && sel.kind === "furniture" && item) {
        const cx = item.x + item.w / 2, cy = item.y + item.h / 2;
        const [rx, ry] = rotate(item.w / 2, item.h / 2, item.angle || 0);
        const [tx, ty] = rotate(0, -item.h / 2 - 0.35, item.angle || 0);
        const [ax, ay] = rotate(0, -item.h / 2, item.angle || 0);
        html += `<line x1="${fmt(cx + ax)}" y1="${fmt(cy + ay)}" x2="${fmt(cx + tx)}" y2="${fmt(cy + ty)}" stroke="var(--accent)" stroke-width=".02"/>`;
        html += `<circle class="pl-handle rotate" data-handle="rotate" cx="${fmt(cx + tx)}" cy="${fmt(cy + ty)}" r="${hr}"/>`;
        html += `<rect class="pl-handle" data-handle="resize" x="${fmt(cx + rx - hr)}" y="${fmt(cy + ry - hr)}" width="${hr * 2}" height="${hr * 2}" rx="0.02"/>`;
      }
      if (sel && sel.kind === "sensor") {
        const s = this.room.sensor;
        const a = ((s.angle || 0) * Math.PI) / 180;
        const hx = s.x - Math.sin(a) * 0.75, hy = s.y + Math.cos(a) * 0.75;
        html += `<line x1="${fmt(s.x)}" y1="${fmt(s.y)}" x2="${fmt(hx)}" y2="${fmt(hy)}" stroke="var(--accent)" stroke-width=".02"/>`;
        html += `<circle class="pl-handle rotate" data-handle="srotate" cx="${fmt(hx)}" cy="${fmt(hy)}" r="${hr}"/>`;
      }
      if (this.draft) {
        if (this.draft.type === "rect") {
          const { x0, y0, x1, y1 } = this.draft;
          html += `<rect class="pl-draft" x="${fmt(Math.min(x0, x1))}" y="${fmt(Math.min(y0, y1))}" width="${fmt(Math.abs(x1 - x0))}" height="${fmt(Math.abs(y1 - y0))}"/>`;
        } else if (this.draft.type === "poly") {
          const pts = [...this.draft.points, this.draft.cursor].filter(Boolean);
          html += `<polyline class="pl-draft" points="${pts.map((p) => `${fmt(p[0])},${fmt(p[1])}`).join(" ")}"/>`;
          this.draft.points.forEach((p, i) => {
            html += `<circle class="pl-draft-point" cx="${fmt(p[0])}" cy="${fmt(p[1])}" r="${i === 0 ? 0.08 : 0.05}"/>`;
          });
        }
      }
      this.gOverlay.innerHTML = html;
    }

    // --------------------------------------------------------- pointer

    point(e) {
      const p = this.svg.createSVGPoint();
      p.x = e.clientX; p.y = e.clientY;
      const m = this.svg.getScreenCTM();
      const q = p.matrixTransform(m.inverse());
      return [q.x, q.y];
    }

    snapped([x, y]) {
      const r = this.room;
      const s = this.snap ? SNAP : 0.01;
      return [G.clamp(round(x, s), 0, r.width), G.clamp(round(y, s), 0, r.height)];
    }

    pointerDown(e) {
      if (!this.room || e.button > 0) return;
      const raw = this.point(e);
      const p = this.snapped(raw);
      this.svg.setPointerCapture(e.pointerId);

      if (this.tool === "rect") {
        this.draft = { type: "rect", x0: p[0], y0: p[1], x1: p[0], y1: p[1] };
        this.drag = { type: "draft" };
        this.renderOverlay();
        return;
      }
      if (this.tool === "poly") {
        if (!this.draft) this.draft = { type: "poly", points: [], cursor: null };
        const first = this.draft.points[0];
        if (first && this.draft.points.length >= 3 && Math.hypot(first[0] - p[0], first[1] - p[1]) < 0.25) {
          this.finishPolygon();
          return;
        }
        this.draft.points.push(p);
        this.renderOverlay();
        return;
      }

      const handle = e.target.closest("[data-handle]");
      if (handle) {
        const item = this.find(this.selection);
        const kind = handle.dataset.handle;
        const index = Number(handle.dataset.index);
        if (kind === "mid") {
          item.points.splice(index + 1, 0, p);
          this.drag = { type: "vertex", item, index: index + 1 };
        } else if (kind === "vertex") {
          this.drag = { type: "vertex", item, index };
        } else {
          this.drag = { type: kind, item };
        }
        this.render();
        return;
      }

      let target = e.target.closest("[data-kind]");
      if (target && target.dataset.kind === "zone") {
        const zone = this.room.zones.find((z) => z.id === target.dataset.id);
        if (zone && zone.furniture_id) {
          target = this.gStatic.querySelector(`[data-kind="furniture"][data-id="${CSS.escape(zone.furniture_id)}"]`) || target;
        }
      }
      const kind = target ? target.dataset.kind : "floor";
      if (kind === "floor") {
        this.select(null);
        this.drag = null;
        return;
      }
      const sel = kind === "sensor" || kind === "outline" ? { kind } : { kind, id: target.dataset.id };
      const item = this.find(sel);
      if (!this.selection || this.selection.kind !== sel.kind || this.selection.id !== sel.id) this.select(sel);
      if (kind === "outline") {
        this.drag = null;
        return;
      }
      if (kind === "zone") {
        this.drag = { type: "move-zone", item, start: raw, origin: item.points.map((q) => q.slice()) };
      } else {
        this.drag = { type: "move", item, dx: raw[0] - item.x, dy: raw[1] - item.y };
      }
    }

    pointerMove(e) {
      if (!this.room) return;
      const raw = this.point(e);
      const p = this.snapped(raw);
      if (this.tool === "poly" && this.draft) {
        this.draft.cursor = p;
        this.renderOverlay();
        return;
      }
      const d = this.drag;
      if (!d) return;
      const r = this.room;
      if (d.type === "draft") {
        this.draft.x1 = p[0]; this.draft.y1 = p[1];
        this.renderOverlay();
        return;
      }
      if (d.type === "vertex") {
        d.item.points[d.index] = p;
      } else if (d.type === "move-zone") {
        let dx = raw[0] - d.start[0], dy = raw[1] - d.start[1];
        if (this.snap) { dx = round(dx, SNAP); dy = round(dy, SNAP); }
        const xs = d.origin.map((q) => q[0]), ys = d.origin.map((q) => q[1]);
        dx = G.clamp(dx, -Math.min(...xs), r.width - Math.max(...xs));
        dy = G.clamp(dy, -Math.min(...ys), r.height - Math.max(...ys));
        d.item.points = d.origin.map((q) => [r3(q[0] + dx), r3(q[1] + dy)]);
      } else if (d.type === "move") {
        const item = d.item;
        let x = raw[0] - d.dx, y = raw[1] - d.dy;
        if (this.snap) { x = round(x, SNAP); y = round(y, SNAP); }
        if (this.selection && this.selection.kind === "furniture") {
          x = G.clamp(x, -item.w / 2, r.width - item.w / 2);
          y = G.clamp(y, -item.h / 2, r.height - item.h / 2);
        } else {
          x = G.clamp(x, 0, r.width); y = G.clamp(y, 0, r.height);
        }
        item.x = r3(x); item.y = r3(y);
      } else if (d.type === "rotate") {
        const item = d.item;
        const cx = item.x + item.w / 2, cy = item.y + item.h / 2;
        let a = (Math.atan2(raw[1] - cy, raw[0] - cx) * 180) / Math.PI + 90;
        if (this.snap) a = Math.round(a / 15) * 15;
        item.angle = ((Math.round(a) + 540) % 360) - 180;
      } else if (d.type === "resize") {
        const item = d.item;
        const a = item.angle || 0;
        const cx = item.x + item.w / 2, cy = item.y + item.h / 2;
        const [tlx, tly] = rotate(-item.w / 2, -item.h / 2, a);
        const corner = [cx + tlx, cy + tly];
        const [lx, ly] = rotate(raw[0] - corner[0], raw[1] - corner[1], -a);
        let w = Math.max(0.1, lx), h = Math.max(0.1, ly);
        if (this.snap) { w = Math.max(0.1, round(w, SNAP)); h = Math.max(0.1, round(h, SNAP)); }
        const [hx, hy] = rotate(w / 2, h / 2, a);
        item.w = r3(w); item.h = r3(h);
        item.x = r3(corner[0] + hx - w / 2);
        item.y = r3(corner[1] + hy - h / 2);
      } else if (d.type === "srotate") {
        const s = this.room.sensor;
        let a = (Math.atan2(-(raw[0] - s.x), raw[1] - s.y) * 180) / Math.PI;
        if (this.snap) a = Math.round(a / 5) * 5;
        s.angle = Math.round(a);
      }
      this.render();
      this.onSelect(this.selection, { live: true });
    }

    pointerUp(e) {
      const d = this.drag;
      this.drag = null;
      try { this.svg.releasePointerCapture(e.pointerId); } catch { /* not captured */ }
      if (!d) return;
      if (d.type === "draft" && this.draft) {
        const { x0, y0, x1, y1 } = this.draft;
        this.draft = null;
        const w = Math.abs(x1 - x0), h = Math.abs(y1 - y0);
        if (w >= 0.2 && h >= 0.2) {
          const xa = Math.min(x0, x1), ya = Math.min(y0, y1);
          this.addZone([[xa, ya], [xa + w, ya], [xa + w, ya + h], [xa, ya + h]]);
        } else {
          this.renderOverlay();
          this.onHint("Zu klein — eine Zone braucht mindestens 20 × 20 cm.");
        }
        return;
      }
      this.commit();
      this.onSelect(this.selection);
    }

    doubleClick(e) {
      const handle = e.target.closest('[data-handle="vertex"]');
      if (handle) {
        const zone = this.find(this.selection);
        if (zone && zone.points.length > 3) {
          zone.points.splice(Number(handle.dataset.index), 1);
          this.render();
          this.commit();
        }
        return;
      }
      if (this.tool === "poly" && this.draft && this.draft.points.length >= 3) this.finishPolygon();
    }

    key(e) {
      if (e.target.closest && e.target.closest("input, textarea, select")) return;
      const mod = e.metaKey || e.ctrlKey;
      if (mod && e.key.toLowerCase() === "z") { e.preventDefault(); e.shiftKey ? this.redo() : this.undo(); return; }
      if (mod && e.key.toLowerCase() === "y") { e.preventDefault(); this.redo(); return; }
      if (e.key === "Escape") {
        if (this.draft) { this.draft = null; this.renderOverlay(); }
        else if (this.tool !== "select") this.setToolFromKey("select");
        else this.select(null);
      } else if (e.key === "Enter" && this.draft && this.draft.type === "poly" && this.draft.points.length >= 3) {
        this.finishPolygon();
      } else if ((e.key === "Delete" || e.key === "Backspace") && this.selection
                 && this.selection.kind !== "sensor" && this.selection.kind !== "outline") {
        e.preventDefault();
        this.removeSelected();
      } else if (!mod && e.key === "v") this.setToolFromKey("select");
      else if (!mod && e.key === "r") this.setToolFromKey("rect");
      else if (!mod && e.key === "p") this.setToolFromKey("poly");
    }

    setToolFromKey(tool) {
      this.setTool(tool);
      if (this.onToolChange) this.onToolChange(tool);
    }

    finishPolygon() {
      const pts = this.draft.points;
      this.draft = null;
      if (this.polyTarget === "outline") {
        if (G.polygonArea(pts) < 1) {
          this.renderOverlay();
          this.onHint("Zu klein — innerhalb der Wände braucht der Raum mindestens 1 m².");
          return;
        }
        if (G.selfIntersects(pts)) {
          this.renderOverlay();
          this.onHint("Die Wände kreuzen sich. Noch einmal, Ecke für Ecke rundherum.");
          return;
        }
        this.room.outline = pts.slice(0, 32).map((p) => [r3(p[0]), r3(p[1])]);
        this.setToolFromKey("select");
        this.select({ kind: "outline" });
        this.commit();
        return;
      }
      if (G.polygonArea(pts) < 0.04) {
        this.renderOverlay();
        this.onHint("Zu klein — eine Zone braucht mindestens 20 × 20 cm Fläche.");
        return;
      }
      this.addZone(pts.slice(0, 16));
    }

    // From the rectangle to walls that can be reshaped: its four corners.
    shapeWalls() {
      if (!this.room.outline) {
        this.room.outline = this.wallPoints().map((p) => p.slice());
        this.commit();
      }
      this.setToolFromKey("select");
      this.select({ kind: "outline" });
    }

    drawWalls() {
      this.select(null);
      this.setTool("poly", { target: "outline" });
      if (this.onToolChange) this.onToolChange("poly");
    }

    rectangleWalls() {
      if (!this.room.outline) return;
      this.room.outline = null;
      if (this.selection && this.selection.kind === "outline") this.selection = null;
      this.render();
      this.commit();
      this.onSelect(this.selection);
    }

    // Zones that stand for a furniture item follow it: corners from the
    // item, grown by the zone's margin and cut to the plan (the server
    // works them out the same way on save), name from the item's.
    syncLinkedZones() {
      const r = this.room;
      if (!r || !r.zones) return;
      const items = new Map(r.furniture.map((f) => [f.id, f]));
      r.zones = r.zones.filter((z) => !z.furniture_id || items.has(z.furniture_id));
      for (const zone of r.zones) {
        if (!zone.furniture_id) continue;
        const f = items.get(zone.furniture_id);
        zone.points = G.clipToPlan(G.furnitureOutline(f.x, f.y, f.w, f.h, f.angle || 0, zone.margin_m ?? 0.2), r.width, r.height);
        zone.name = this.uniqueZoneName(f.name || (FURNITURE[f.kind] || FURNITURE.other).label, zone.id);
      }
    }

    uniqueZoneName(base, ownId) {
      const taken = new Set(this.room.zones.filter((z) => z.id !== ownId).map((z) => z.name.trim().toLowerCase()));
      base = (base || "Zone").trim().slice(0, 36);
      let name = base, n = 2;
      while (taken.has(name.toLowerCase())) name = `${base} ${n++}`;
      return name;
    }

    furnitureZone(furnitureId) {
      return this.room.zones.find((z) => z.furniture_id === furnitureId) || null;
    }

    // kind: "detect", "exclude", "entry" or null to take the zone away.
    setFurnitureZone(furnitureId, kind) {
      const r = this.room;
      const existing = this.furnitureZone(furnitureId);
      if (!kind) {
        if (!existing) return;
        r.zones = r.zones.filter((z) => z !== existing);
      } else if (existing) {
        existing.kind = kind;
      } else {
        if (r.zones.length >= 16) { this.onHint("Höchstens 16 Zonen je Raum."); return; }
        const f = r.furniture.find((x) => x.id === furnitureId);
        if (!f) return;
        const used = new Set(r.zones.map((z) => z.color));
        let color = 0;
        while (used.has(color) && color < 7) color++;
        // Seats and beds hold longer: the radar loses people who sit still.
        const hold = ["sofa", "armchair", "bed", "chair"].includes(f.kind) ? 30 : 10;
        r.zones.push({ id: newId("z"), name: "", kind, points: [], hold_s: hold, color, furniture_id: f.id, margin_m: 0.2 });
      }
      this.render();
      this.commit();
    }

    addZone(points) {
      const used = new Set(this.room.zones.map((z) => z.color));
      let color = 0;
      while (used.has(color) && color < 7) color++;
      let n = this.room.zones.length + 1;
      const names = new Set(this.room.zones.map((z) => z.name.toLowerCase()));
      while (names.has(`zone ${n}`)) n++;
      const zone = { id: newId("z"), name: `Zone ${n}`, kind: "detect", points: points.map((p) => [r3(p[0]), r3(p[1])]),
        hold_s: 10, color };
      this.room.zones.push(zone);
      this.setToolFromKey("select");
      this.select({ kind: "zone", id: zone.id });
      this.commit();
    }

    addFurniture(kind) {
      const spec = FURNITURE[kind] || FURNITURE.other;
      const r = this.room;
      const w = Math.min(spec.w, r.width), h = Math.min(spec.h, r.height);
      const item = { id: newId("f"), kind, name: spec.label, x: r3((r.width - w) / 2), y: r3((r.height - h) / 2), w, h, angle: 0 };
      r.furniture.push(item);
      this.setToolFromKey("select");
      this.select({ kind: "furniture", id: item.id });
      this.commit();
    }

    removeSelected() {
      const sel = this.selection;
      if (!sel || sel.kind === "sensor" || sel.kind === "outline") return;
      if (sel.kind === "zone") this.room.zones = this.room.zones.filter((z) => z.id !== sel.id);
      else {
        this.room.furniture = this.room.furniture.filter((f) => f.id !== sel.id);
        this.room.zones = this.room.zones.filter((z) => z.furniture_id !== sel.id);
      }
      this.select(null);
      this.commit();
    }
  }

  return { PlanView, FURNITURE, zoneColor };
})();
