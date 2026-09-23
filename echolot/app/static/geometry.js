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

  const api = { toRoom, pointInPolygon, inRoom, polygonArea, centroid, clamp };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.EcholotGeometry = api;
})(typeof window !== "undefined" ? window : globalThis);
