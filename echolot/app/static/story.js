// A room's state in words, for the overview and the room page, and what
// of a room makes a page redraw it. Plain
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
    // The server says (room_engine.MOVING_SPEED_M_S); worked out here only
    // for a result from before it did.
    const v = target.velocity || [0, 0];
    const moving = target.moving !== undefined ? !!target.moving
      : !target.resting && target.status === "counted" && Math.hypot(v[0], v[1]) >= WALK_SPEED;
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

  // What a room looks like on a page — everything but `learning`, whose
  // counters (observed_s) move on every load. Pages redraw a room only
  // when this changes: redrawing on every load (five seconds) made the
  // room page jump and the overview rebuild its maps.
  function roomShape(room) {
    if (!room) return "";
    const { learning, ...rest } = room;
    return JSON.stringify(rest);
  }

  const api = { describe, roomShape, WALK_SPEED };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.EcholotStory = api;
})(typeof window !== "undefined" ? window : globalThis);
