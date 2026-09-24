// The same arithmetic as app/geometry.py, for the map. Both are held to the
// same cases by tests/test_geometry.py, so what the map shows inside a zone
// is what Home Assistant counts inside it.

(function (root) {
  function toRoom(xM, yM, placement) {
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

  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

  const api = { toRoom, pointInPolygon, inRoom, distanceToPolygon, withinWalls, selfIntersects, polygonArea, centroid, clamp };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.EcholotGeometry = api;
})(typeof window !== "undefined" ? window : globalThis);
