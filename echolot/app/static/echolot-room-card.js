// Echolot room card for Home Assistant dashboards.
//
//   type: custom:echolot-room-card
//   addon: local_echolot        # the Echolot add-on's slug
//   room: wohnzimmer-1a2b       # the room's id in Echolot
//
// Home Assistant lets the browser reach an add-on only through Ingress,
// and Ingress only with a session an administrator asks the Supervisor
// for. This card does what Home Assistant's own add-on panel does: it
// asks for a session, keeps it alive every minute, and shows the add-on's
// page `embed?room=<id>` in a frame. Everything inside is drawn by the
// add-on, so updating the add-on updates the card.
//
// Cards on one dashboard share one session: the session travels in a
// cookie, and a cookie per card would overwrite the others'.

const ECHOLOT_CARD_VERSION = 1;
const KEEP_ALIVE_MS = 60000;
const INGRESS_PREFIX = "/api/hassio_ingress/";

// ------------------------------------------------------------ session

const echolotIngress = (window.__echolotIngress ||= { session: null, pending: null, users: new Set(), timer: null, hass: null });

function echolotSetCookie(session) {
  document.cookie = `ingress_session=${session};path=${INGRESS_PREFIX};SameSite=Strict${location.protocol === "https:" ? ";Secure" : ""}`;
}

async function echolotNewSession(hass) {
  const response = await hass.callWS({ type: "supervisor/api", endpoint: "/ingress/session", method: "post" });
  echolotIngress.session = response.session;
  echolotSetCookie(response.session);
  return response.session;
}

// A session for `card`, shared. `refused` is one Ingress turned down: it
// is replaced — unless another card has replaced it already.
async function echolotSession(card, hass, refused = null) {
  echolotIngress.users.add(card);
  echolotIngress.hass = hass;
  if (refused && echolotIngress.session === refused) echolotIngress.session = null;
  if (!echolotIngress.session) {
    echolotIngress.pending ||= echolotNewSession(hass).finally(() => { echolotIngress.pending = null; });
    await echolotIngress.pending;
  }
  if (!echolotIngress.timer) {
    echolotIngress.timer = setInterval(async () => {
      const { hass: current, session } = echolotIngress;
      try {
        await current.callWS({ type: "supervisor/api", endpoint: "/ingress/validate_session", method: "post", data: { session } });
        // Home Assistant's add-on panel sets the same cookie for its own
        // session, which ends when the panel closes: set ours again.
        echolotSetCookie(session);
      } catch {
        try { await echolotNewSession(current); } catch { /* the next minute tries again */ }
      }
    }, KEEP_ALIVE_MS);
  }
  return echolotIngress.session;
}

function echolotRelease(card) {
  echolotIngress.users.delete(card);
  if (!echolotIngress.users.size && echolotIngress.timer) {
    clearInterval(echolotIngress.timer);
    echolotIngress.timer = null;
  }
}

// --------------------------------------------------------------- card

class EcholotRoomCard extends HTMLElement {
  static getStubConfig() {
    return { addon: "local_echolot", room: "" };
  }

  setConfig(config) {
    if (!config || !config.addon) throw new Error("„addon“ fehlt: das Kürzel des Echolot-Add-ons, z. B. local_echolot");
    if (!config.room) throw new Error("„room“ fehlt: die Raum-ID aus Echolot (Raumseite → Im Dashboard)");
    const changed = !this._config || this._config.addon !== config.addon || this._config.room !== config.room;
    this._config = config;
    if (changed) this._src = null;
    this._render();
    if (changed && this._hass && this.isConnected) this._open();
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    const theme = this._theme();
    if (theme !== this._lastTheme) {
      this._lastTheme = theme;
      this._post({ type: "theme", theme });
    }
    if (first && this._config && this.isConnected) this._open();
  }

  connectedCallback() {
    this._onMessage ||= (event) => this._message(event);
    window.addEventListener("message", this._onMessage);
    if (this._hass && this._config) this._open();
  }

  disconnectedCallback() {
    window.removeEventListener("message", this._onMessage);
    echolotRelease(this);
  }

  getCardSize() {
    return Math.max(3, Math.ceil((this._height || 320) / 50));
  }

  getGridOptions() {
    return { columns: 12, min_columns: 6 };
  }

  _theme() {
    const dark = this._hass && this._hass.themes && this._hass.themes.darkMode;
    return dark === undefined ? "" : dark ? "dark" : "light";
  }

  _render() {
    if (!this.shadowRoot) {
      this.attachShadow({ mode: "open" }).innerHTML = `
        <style>
          ha-card { overflow: hidden; }
          iframe { display: block; width: 100%; height: 320px; border: 0; background: transparent; }
          .note { padding: 16px; color: var(--secondary-text-color); line-height: 1.45; }
          .note strong { display: block; color: var(--primary-text-color); margin-bottom: 4px; }
          [hidden] { display: none !important; }
        </style>
        <ha-card>
          <iframe title="Echolot" hidden></iframe>
          <div class="note" hidden></div>
        </ha-card>`;
      this._frame = this.shadowRoot.querySelector("iframe");
      this._note = this.shadowRoot.querySelector(".note");
    }
    const title = this._config && this._config.title;
    this.shadowRoot.querySelector("ha-card").setAttribute("header", title || "");
  }

  _say(title, text) {
    this._frame.hidden = true;
    this._note.hidden = false;
    this._note.innerHTML = "";
    const strong = document.createElement("strong");
    strong.textContent = title;
    this._note.append(strong, document.createTextNode(text));
  }

  async _open(fresh = false) {
    if (this._opening) return;
    this._opening = true;
    try {
      const hass = this._hass;
      if (!hass.user || !hass.user.is_admin) {
        this._say("Nur für Administratoren", "Home Assistant öffnet Add-ons nur für Administratoren. Die Anwesenheit dieses Raums zeigen die Echolot-Entitäten auch hier, etwa in einer Kachel.");
        return;
      }
      let info;
      try {
        info = await hass.callWS({ type: "supervisor/api", endpoint: `/addons/${encodeURIComponent(this._config.addon)}/info`, method: "get" });
      } catch {
        this._say("Add-on nicht gefunden", `Kein Add-on mit dem Kürzel „${this._config.addon}“. Das richtige steht in Echolot auf der Raumseite unter „Im Dashboard“.`);
        return;
      }
      if (!info.ingress_url || !info.ingress_url.startsWith(INGRESS_PREFIX)) {
        this._say("Kein Zugang", "Dieses Add-on hat keinen Ingress-Zugang. Ist es wirklich Echolot?");
        return;
      }
      if (info.state !== "started") {
        this._say("Echolot läuft nicht", "Das Add-on ist gestoppt. Unter Einstellungen → Add-ons starten, dann erscheint der Raum hier.");
        return;
      }
      try {
        this._session = await echolotSession(this, hass, fresh ? this._session : null);
      } catch {
        this._say("Keine Sitzung", "Home Assistant hat keine Ingress-Sitzung für das Add-on geöffnet. Die Karte versucht es beim nächsten Laden wieder.");
        return;
      }
      const theme = this._theme();
      const src = `${info.ingress_url}embed?room=${encodeURIComponent(this._config.room)}${theme ? `&theme=${theme}` : ""}&card=${ECHOLOT_CARD_VERSION}`;
      this._note.hidden = true;
      this._frame.hidden = false;
      if (fresh || this._src !== src) {
        this._src = src;
        this._frame.src = src;
      }
    } finally {
      this._opening = false;
    }
  }

  _post(message) {
    if (this._frame && this._frame.contentWindow && !this._frame.hidden) {
      this._frame.contentWindow.postMessage({ source: "echolot-card", ...message }, location.origin);
    }
  }

  _message(event) {
    if (!this._frame || event.source !== this._frame.contentWindow || event.origin !== location.origin) return;
    const data = event.data || {};
    if (data.source !== "echolot-embed") return;
    if (data.type === "size" && Number.isFinite(data.height) && data.height > 0) {
      this._height = Math.min(4000, Math.round(data.height));
      this._frame.style.height = `${this._height}px`;
    } else if (data.type === "auth") {
      // Ingress turned the page away: a new session, at most every 10 s.
      const now = Date.now();
      if (now - (this._lastRenew || 0) < 10000) return;
      this._lastRenew = now;
      this._open(true);
    }
  }
}

if (!customElements.get("echolot-room-card")) {
  customElements.define("echolot-room-card", EcholotRoomCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "echolot-room-card",
    name: "Echolot-Raum",
    description: "Ein Echolot-Raum live: Plan, Personen, Zonen.",
    preview: false,
  });
}
