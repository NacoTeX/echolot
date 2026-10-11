// A room's state in words, for the overview and the room page. Plain
// script for the browser (window.EcholotStory) and a module for node, so
// tests/test_frontend.py holds it to its cases.

(function (root) {
  // What is happening in a room, as a sentence — only what the result
  // says: how many, whether each is still or on the move (the tracker's
  // velocity and rest, definition 9) and in which zone. Not whether
  // somebody sits or stands, which the radar cannot tell.
  const NUMBERS = ["Niemand", "Eine Person", "Zwei Personen", "Drei Personen", "Vier Personen", "Fünf Personen"];
  // m/s from which a target counts as moving (plan.js draws a heading).
  const WALK_SPEED = 0.3;

  function activity(target, zoneNames) {
    const v = target.velocity || [0, 0];
    const moving = !target.resting && target.status === "counted" && Math.hypot(v[0], v[1]) >= WALK_SPEED;
    const zone = (target.zones || []).map((id) => zoneNames[id]).find(Boolean);
    return { moving, zone };
  }

  function describe(room, live, formatSeconds) {
    if (!live || !live.available) return null;
    const counted = (live.targets || []).filter((t) => t.status === "counted" || t.status === "held");
    if (!counted.length) {
      if (live.assumed_present) return "Gerade nicht zu sehen — vermutlich sitzt noch jemand still im Raum.";
      if (live.occupied) return `Eben war noch jemand da — der Raum gilt noch ${formatSeconds(live.hold_remaining)} als belegt.`;
      return "Niemand im Raum.";
    }
    const zoneNames = Object.fromEntries(((room && room.zones) || []).map((z) => [z.id, z.name]));
    const parts = counted.map((t) => activity(t, zoneNames));
    const phrase = ({ moving, zone }) => zone
      ? (moving ? `in Bewegung im Bereich „${zone}“` : `ruhig im Bereich „${zone}“`)
      : (moving ? "in Bewegung" : "ruhig im Raum");
    if (parts.length === 1) {
      const [{ moving, zone }] = parts;
      if (zone) return moving ? `Eine Person bewegt sich im Bereich „${zone}“.` : `Eine Person verweilt im Bereich „${zone}“.`;
      return moving ? "Eine Person bewegt sich durch den Raum." : "Eine Person verweilt im Raum.";
    }
    const items = parts.map((p) => `eine ${phrase(p)}`);
    const list = items.length > 1 ? `${items.slice(0, -1).join(", ")} und ${items[items.length - 1]}` : items[0];
    return `${NUMBERS[parts.length] || `${parts.length} Personen`}: ${list}.`;
  }

  const api = { describe, WALK_SPEED };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.EcholotStory = api;
})(typeof window !== "undefined" ? window : globalThis);
