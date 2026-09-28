// The same arithmetic as app/geometry.py, for the map. Both are held to the
// same cases by tests/test_geometry.py, so what the map shows inside a zone
// is what Home Assistant counts inside it.

(function (root) {
  const MODEL_DEFAULTS = { range_scale: 1, range_offset_m: 0, azimuth_scale: 1, slant: false };

  function isNeutral(placement) {
    return Object.entries(MODEL_DEFAULTS).every(([k, v]) => (placement[k] ?? v) === v);
  }

  // See correct() in app/geometry.py.
  function correct(xM, yM, placement) {
    if (isNeutral(placement)) return [xM, yM];
    const r = Math.hypot(xM, yM);
    let phi = Math.atan2(xM, yM);
    const k = placement.range_scale ?? 1, b = placement.range_offset_m ?? 0;
    let d = Math.max(0, (r - b) / k);
    if (placement.slant && placement.mount_height_m !== null && placement.mount_height_m !== undefined) {
      const dh = placement.mount_height_m - (placement.target_height_m ?? 1);
      d = Math.sqrt(Math.max(d * d - dh * dh, 0));
    }
    phi /= placement.azimuth_scale ?? 1;
    return [d * Math.sin(phi), d * Math.cos(phi)];
  }

  // See model_shortfall() and MODEL_TOLERANCE_M in app/geometry.py.
  const MODEL_TOLERANCE_M = 0.2;

  function modelShortfall(xM, yM, placement) {
    if (isNeutral(placement)) return 0;
    const r = Math.hypot(xM, yM);
    const d = (r - (placement.range_offset_m ?? 0)) / (placement.range_scale ?? 1);
    if (placement.slant && placement.mount_height_m !== null && placement.mount_height_m !== undefined) {
      const dh = Math.abs(placement.mount_height_m - (placement.target_height_m ?? 1));
      return Math.max(0, dh - d);
    }
    return Math.max(0, -d);
  }

  function toRoom(xM, yM, placement) {
    [xM, yM] = correct(xM, yM, placement);
    if (placement.mirror) xM = -xM;
    const a = ((placement.angle || 0) * Math.PI) / 180;
    const c = Math.cos(a), s = Math.sin(a);
    return { x: placement.x + xM * c - yM * s, y: placement.y + xM * s + yM * c };
  }

  function pointInPolygon(x, y, points) {
    let inside = false;
    const n = points.length;
    if (n < 3) return false;
    for (let i = 0, j = n - 1; i < n; j = i++) {
      const [xi, yi] = points[i];
      const [xj, yj] = points[j];
      if ((yi > y) !== (yj > y)) {
        const crossing = ((xj - xi) * (y - yi)) / (yj - yi) + xi;
        if (x < crossing) inside = !inside;
      }
    }
    return inside;
  }

  function inRoom(x, y, width, height, margin) {
    margin = margin || 0;
    return x >= -margin && x <= width + margin && y >= -margin && y <= height + margin;
  }

  function segmentDistance(x, y, ax, ay, bx, by) {
    const dx = bx - ax, dy = by - ay;
    const length = dx * dx + dy * dy;
    const t = length === 0 ? 0 : Math.max(0, Math.min(1, ((x - ax) * dx + (y - ay) * dy) / length));
    return Math.hypot(x - (ax + t * dx), y - (ay + t * dy));
  }

  function distanceToPolygon(x, y, points) {
    const n = points.length;
    if (n < 2) return Infinity;
    let best = Infinity;
    for (let i = 0; i < n; i++) {
      const [ax, ay] = points[i];
      const [bx, by] = points[(i + 1) % n];
      best = Math.min(best, segmentDistance(x, y, ax, ay, bx, by));
    }
    return best;
  }

  function withinWalls(x, y, width, height, outline, margin) {
    margin = margin || 0;
    if (!outline || !outline.length) return inRoom(x, y, width, height, margin);
    return pointInPolygon(x, y, outline) || distanceToPolygon(x, y, outline) <= margin;
  }

  function cross(ax, ay, bx, by, cx, cy) {
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax);
  }

  function segmentsCross(p1, p2, p3, p4) {
    const d1 = cross(...p3, ...p4, ...p1), d2 = cross(...p3, ...p4, ...p2);
    const d3 = cross(...p1, ...p2, ...p3), d4 = cross(...p1, ...p2, ...p4);
    if (((d1 > 0) !== (d2 > 0)) && ((d3 > 0) !== (d4 > 0)) && d1 && d2 && d3 && d4) return true;
    const on = (a, b, c, d) => d === 0 && Math.min(a[0], b[0]) <= c[0] && c[0] <= Math.max(a[0], b[0])
      && Math.min(a[1], b[1]) <= c[1] && c[1] <= Math.max(a[1], b[1]);
    return on(p3, p4, p1, d1) || on(p3, p4, p2, d2) || on(p1, p2, p3, d3) || on(p1, p2, p4, d4);
  }

  function selfIntersects(points) {
    const n = points.length;
    for (let i = 0; i < n; i++) {
      const a = points[i], b = points[(i + 1) % n];
      for (let j = i + 1; j < n; j++) {
        if ((j + 1) % n === i || j === (i + 1) % n) continue;
        if (segmentsCross(a, b, points[j], points[(j + 1) % n])) return true;
      }
    }
    return false;
  }

  function polygonArea(points) {
    let total = 0;
    for (let i = 0; i < points.length; i++) {
      const [x1, y1] = points[i];
      const [x2, y2] = points[(i + 1) % points.length];
      total += x1 * y2 - x2 * y1;
    }
    return Math.abs(total) / 2;
  }

  function centroid(points) {
    let x = 0, y = 0;
    for (const p of points) { x += p[0]; y += p[1]; }
    return [x / points.length, y / points.length];
  }

  function furnitureOutline(x, y, w, h, angle, margin) {
    margin = margin || 0;
    const cx = x + w / 2, cy = y + h / 2;
    const hw = w / 2 + margin, hh = h / 2 + margin;
    const a = ((angle || 0) * Math.PI) / 180;
    const c = Math.cos(a), s = Math.sin(a);
    return [[-hw, -hh], [hw, -hh], [hw, hh], [-hw, hh]].map(([dx, dy]) => [cx + dx * c - dy * s, cy + dx * s + dy * c]);
  }

  // The box a turned item fills on the plan: [left, top, right, bottom].
  // Its top-left corner is what the editor calls an item's X and Y — the
  // stored x, y is the corner before turning, and a long item turned by
  // 90° about its centre shows up (w - h) / 2 away from it.
  function furnitureBox(x, y, w, h, angle) {
    const pts = furnitureOutline(x, y, w, h, angle, 0);
    const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
    return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
  }

  // The stored x, y that puts an item's box with its top-left at left, top.
  function furnitureAt(left, top, w, h, angle) {
    const [bx, by] = furnitureBox(0, 0, w, h, angle);
    return [left - bx, top - by];
  }

  function clipToPlan(points, width, height) {
    const clip = (pts, inside, cut) => {
      const out = [];
      pts.forEach((cur, i) => {
        const prev = pts[(i - 1 + pts.length) % pts.length];
        if (inside(cur)) {
          if (!inside(prev)) out.push(cut(prev, cur));
          out.push(cur);
        } else if (inside(prev)) out.push(cut(prev, cur));
      });
      return out;
    };
    const atX = (xv) => (p, q) => [xv, p[1] + ((q[1] - p[1]) * (xv - p[0])) / (q[0] - p[0])];
    const atY = (yv) => (p, q) => [p[0] + ((q[0] - p[0]) * (yv - p[1])) / (q[1] - p[1]), yv];
    let pts = points.map((p) => [p[0], p[1]]);
    for (const [inside, cut] of [
      [(p) => p[0] >= 0, atX(0)], [(p) => p[0] <= width, atX(width)],
      [(p) => p[1] >= 0, atY(0)], [(p) => p[1] <= height, atY(height)],
    ]) {
      if (!pts.length) break;
      pts = clip(pts, inside, cut);
    }
    // Millimetres, like the server. It recomputes on save anyway; this is
    // only what the editor draws in between.
    const r3 = (v) => Math.round(v * 1000) / 1000 + 0;
    const out = [];
    for (const [px, py] of pts) {
      const q = [r3(px), r3(py)];
      const last = out[out.length - 1];
      if (!last || last[0] !== q[0] || last[1] !== q[1]) out.push(q);
    }
    if (out.length > 1 && out[0][0] === out[out.length - 1][0] && out[0][1] === out[out.length - 1][1]) out.pop();
    return out;
  }

  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

  // ------------------------------------------------ where the sensor looks
  //
  // The plan's field of view: what the sensor entry says it covers
  // (range_m, fov_deg), not what the module measures — that shows in its
  // reports. It exists to catch a sensor drawn looking the wrong way: a
  // plan that sends the view out of the room puts every report somewhere
  // it is not.

  // With this little or less of its view going into the room (viewFit),
  // the sensor as drawn looks out of it — what is left grazes its own
  // wall. Below PARTLY_OUT_FIT, a good part of the view lies beyond the
  // walls. Chosen, not measured.
  const LOOKS_OUT_FIT = 0.1;
  const PARTLY_OUT_FIT = 0.5;
  // Closer than this to a wall, the sensor hangs on it (angleIntoRoom).
  const ON_WALL_M = 0.35;

  // Straight ahead of a sensor turned by `angle`: 0° looks down the plan,
  // positive angles turn it clockwise (app/geometry.py, to_room).
  function ahead(angle) {
    const a = ((angle || 0) * Math.PI) / 180;
    return [-Math.sin(a), Math.cos(a)];
  }

  // Whether (x, y) is in view: no farther than range_m, and no more than
  // half of fov_deg off straight ahead.
  function sees(x, y, placement) {
    const dx = x - placement.x, dy = y - placement.y;
    const d = Math.hypot(dx, dy);
    if (d > (placement.range_m || 6)) return false;
    if (d === 0) return true;
    const [fx, fy] = ahead(placement.angle);
    return (dx * fx + dy * fy) / d >= Math.cos((((placement.fov_deg || 120) / 2) * Math.PI) / 180);
  }

  // How much floor there is and how much of it is in view, in m²: counted
  // on a grid of ten-centimetre cells — coarser in a large room, so never
  // much more than 10 000 of them.
  function viewCover(placement, width, height, outline) {
    const step = Math.max(0.1, Math.sqrt((width * height) / 10000));
    const nx = Math.max(1, Math.ceil(width / step - 1e-9));
    const ny = Math.max(1, Math.ceil(height / step - 1e-9));
    const walls = outline && outline.length ? outline : null;
    let floor = 0, seen = 0;
    for (let i = 0; i < nx; i++) {
      const x = ((i + 0.5) * width) / nx;
      for (let j = 0; j < ny; j++) {
        const y = ((j + 0.5) * height) / ny;
        if (walls && !pointInPolygon(x, y, walls)) continue;
        floor++;
        if (sees(x, y, placement)) seen++;
      }
    }
    const cell = (width / nx) * (height / ny);
    return { floor: floor * cell, seen: seen * cell };
  }

  // How much of the sensor's view goes into the room, 0 to 1: of 25 rays
  // spread across its field of view, the share still inside the walls
  // half a metre out — the smallest room is a metre across. Not the share of the room it sees — in a hall far beyond
  // its reach that is small for a sensor looking straight in, and in the
  // middle of a room it is small whichever way the sensor looks. What
  // this catches is a view that leaves the room: 1 looking straight in
  // from a wall, about ¾ diagonally out of a corner, ½ along a wall, 0
  // looking out.
  const VIEW_RAYS = 25;
  function viewFit(placement, width, height, outline) {
    const walls = outline && outline.length ? outline : null;
    const half = (placement.fov_deg || 120) / 2;
    const reach = Math.min(0.5, placement.range_m || 6);
    const at = { x: placement.x, y: placement.y, angle: placement.angle || 0 };
    let inside = 0;
    for (let i = 0; i < VIEW_RAYS; i++) {
      const t = ((-half + (2 * half * i) / (VIEW_RAYS - 1)) * Math.PI) / 180;
      const p = toRoom(reach * Math.sin(t), reach * Math.cos(t), at);
      if (withinWalls(p.x, p.y, width, height, walls, 0)) inside++;
    }
    return inside / VIEW_RAYS;
  }

  // The middle of the floor: its centre of area, not of its corners.
  function floorCentre(points) {
    let a = 0, cx = 0, cy = 0;
    for (let i = 0; i < points.length; i++) {
      const [x1, y1] = points[i];
      const [x2, y2] = points[(i + 1) % points.length];
      const k = x1 * y2 - x2 * y1;
      a += k; cx += (x1 + x2) * k; cy += (y1 + y2) * k;
    }
    return a ? [cx / (3 * a), cy / (3 * a)] : centroid(points);
  }

  // Which way a sensor at the placement's x, y looks into the room, in the
  // plan's degrees: straight away from the wall it hangs on, diagonally
  // out of a corner, towards the middle of the floor when it hangs on no
  // wall. A proposal to start from — a sensor mounted askew is turned on
  // from there.
  function angleIntoRoom(placement, width, height, outline) {
    const walls = outline && outline.length ? outline : [[0, 0], [width, 0], [width, height], [0, height]];
    const sx = placement.x, sy = placement.y;
    let turn = 0;
    for (let i = 0; i < walls.length; i++) {
      const [x1, y1] = walls[i];
      const [x2, y2] = walls[(i + 1) % walls.length];
      turn += x1 * y2 - x2 * y1;
    }
    const near = [];
    walls.forEach(([ax, ay], i) => {
      const [bx, by] = walls[(i + 1) % walls.length];
      const length = Math.hypot(bx - ax, by - ay);
      const d = segmentDistance(sx, sy, ax, ay, bx, by);
      if (!length || d > ON_WALL_M) return;
      // Walked in the outline's order, the inside lies to the right of a
      // wall when the outline runs clockwise on screen, to its left when
      // it runs the other way.
      const s = turn > 0 ? 1 : -1;
      near.push([d, (-(by - ay) * s) / length, ((bx - ax) * s) / length]);
    });
    near.sort((a, b) => a[0] - b[0]);
    let dx = 0, dy = 0;
    for (const [, nx, ny] of near.slice(0, 2)) { dx += nx; dy += ny; }
    if (Math.hypot(dx, dy) < 1e-6) {
      const [cx, cy] = floorCentre(walls);
      dx = cx - sx; dy = cy - sy;
      if (Math.hypot(dx, dy) < 1e-6) return normalizeAngle(placement.angle || 0);
    }
    return normalizeAngle(Math.round((Math.atan2(-dx, dy) * 180) / Math.PI));
  }

  // -180 < angle <= 180.
  function normalizeAngle(angle) {
    const a = ((angle % 360) + 360) % 360;
    return a > 180 ? a - 360 : a;
  }

  // "nach rechts", "nach links oben": which way an angle looks on the plan
  // as it shows on screen, in eight steps.
  const FACING = ["nach unten", "nach links unten", "nach links", "nach links oben",
    "nach oben", "nach rechts oben", "nach rechts", "nach rechts unten"];
  function facingWords(angle) {
    const a = ((normalizeAngle(angle || 0) % 360) + 360) % 360;
    return FACING[Math.round(a / 45) % 8];
  }

  const api = { toRoom, correct, modelShortfall, MODEL_TOLERANCE_M, pointInPolygon, inRoom, distanceToPolygon, withinWalls, selfIntersects, polygonArea, centroid, clamp, furnitureOutline, furnitureBox, furnitureAt, clipToPlan,
    LOOKS_OUT_FIT, PARTLY_OUT_FIT, ON_WALL_M, sees, viewCover, viewFit, floorCentre, angleIntoRoom, normalizeAngle, facingWords };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.EcholotGeometry = api;
})(typeof window !== "undefined" ? window : globalThis);
