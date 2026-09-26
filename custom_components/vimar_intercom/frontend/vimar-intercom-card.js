/**
 * Vimar Intercom dashboard card.
 *
 * Ships inside the vimar_intercom integration, which serves this file and
 * loads it on every dashboard (see dashboard_card.py): there is no
 * resource to add by hand, and the card always matches the entities of the
 * release it came with.
 *
 * Plain HTMLElement and shadow DOM, deliberately. Home Assistant does not
 * export Lit: borrowing it through an internal element's prototype works
 * until the frontend's bundling changes, and then every dashboard that has
 * this card shows an error instead of the doorbell. Nothing here needs a
 * template engine. Each section is rendered to a string and its DOM
 * replaced only when that string changes, so the one element that must
 * never be re-created while it plays - the live stream - is created once
 * per viewing and only has its `hass` updated.
 *
 * Entities come from what the frontend already holds: the registry's
 * display entries (`hass.entities`: platform, device, translation key) and
 * the states. Every entity of the integration carries an `intercom_role`
 * attribute, and the per-panel buttons a `panel`, so nothing is guessed
 * from names, which are the installer's and the user's to change.
 *
 * Two behaviours differ from what a phone app would do, because of what
 * the integration can and cannot do:
 *
 * - A ring does not start the video. The panel sends no video until the
 *   call is answered, and opening the stream while a panel rings answers
 *   it (hub.py, `_do_auto_call`). A wall tablet showing this card would
 *   otherwise pick up every visitor, and the indoor unit would stop
 *   ringing for everybody else. Answer starts the video.
 * - The red button while ringing is Dismiss, not Decline. The integration
 *   has no decline action, and Hang up does not refuse a ringing call; the
 *   other devices of the house should keep ringing anyway. Dismiss only
 *   hides the banner here.
 */

const DOMAIN = "vimar_intercom";
const CARD_TYPE = "vimar-intercom-card";
const EDITOR_TYPE = "vimar-intercom-card-editor";

// How long a ring counts as ringing. The frontend is told when a panel
// rings, not when it gives up or another device answers, so a ring nobody
// takes here expires on its own.
const RING_WINDOW_MS = 30000;
// A door opens on a second tap within this window, or on a hold.
const CONFIRM_WINDOW_MS = 3000;
const HOLD_MS = 600;
// How long "Opened" / "Done" stays on a button.
const FEEDBACK_MS = 3000;
// The integration waits up to 45 s for a panel to answer a call; a user
// looking at a spinner does not. After this the card gives up and says so.
const CALL_SETUP_MS = 20000;
const HANGUP_WAIT_MS = 8000;
// After this, a stream that has not shown the call as up drops its
// "Connecting" overlay and lets the player show whatever it has.
const CONNECTING_MAX_MS = 25000;
// A call this card placed or answered is hung up when the card has been
// gone this long - navigated away, tab closed - as the integration does
// for the calls its camera places. Short absences (an edit-mode re-render,
// a view switch and back) keep it.
const LEAVE_GRACE_MS = 15000;

const STRINGS = {
  en: {
    card_name: "Vimar Intercom",
    card_description:
      "Live video, doorbell, doors and lights of a Vimar intercom, in one card.",
    watch: "Watch {panel}",
    watch_call: "Watch the call",
    watch_default: "Watch",
    video_on_answer: "The video starts when you answer",
    calling: "Calling {panel}…",
    connecting: "Connecting…",
    live: "Live",
    stop: "Stop video",
    offline_video: "The intercom is offline",
    unavailable: "Unavailable",
    panels: "Entrance panels",
    show_panel: "Watch {panel}",
    ringing: "{panel} is ringing",
    ringing_generic: "Someone is at the door",
    ringing_hint: "Answer to see and hear who is there",
    rang_during_call: "{panel} rang during this call",
    answer: "Answer",
    dismiss: "Dismiss",
    hang_up: "Hang up",
    in_call: "In call",
    in_call_with: "In call with {panel}",
    open_door: "Open door",
    open_named: "Open {name}",
    confirm_hint: "Tap twice or hold",
    tap_again: "Tap again to open",
    tap_again_for: "Tap again to open {name}",
    opening: "Opening…",
    opened: "Opened",
    opened_named: "{name} opened",
    done: "Done",
    doors: "Doors",
    controls: "Controls",
    connected: "Connected",
    disconnected: "Disconnected",
    status_unknown: "Status unknown",
    reconnect: "Reconnect",
    reconnecting: "Reconnecting…",
    call_failed: "{panel} did not answer",
    call_failed_generic: "The call did not connect",
    stream_unavailable:
      "This Home Assistant version cannot show the camera stream here",
    no_device:
      "No Vimar Intercom device found. Add the Vimar Intercom integration first.",
    device_missing: "The intercom device selected for this card no longer exists.",
    editor_device_id: "Intercom",
    editor_title: "Title (optional)",
    editor_show_actuators: "Show lights and other controls",
    editor_hidden_entities: "Hide these items",
    editor_hidden_help:
      "A hidden door, control or panel button is left out of the card.",
    editor_unavailable:
      "The visual editor is not available. Use the code editor.",
  },
  it: {
    card_name: "Citofono Vimar",
    card_description:
      "Video in diretta, campanello, porte e luci di un citofono Vimar, in una sola scheda.",
    watch: "Guarda {panel}",
    watch_call: "Guarda la chiamata",
    watch_default: "Guarda",
    video_on_answer: "Il video parte quando rispondi",
    calling: "Chiamata a {panel}…",
    connecting: "Connessione…",
    live: "Live",
    stop: "Ferma il video",
    offline_video: "Il citofono non è connesso",
    unavailable: "Non disponibile",
    panels: "Pulsantiere",
    show_panel: "Guarda {panel}",
    ringing: "Suonano a {panel}",
    ringing_generic: "Qualcuno è alla porta",
    ringing_hint: "Rispondi per vedere e sentire chi c'è",
    rang_during_call: "Hanno suonato a {panel} durante la chiamata",
    answer: "Rispondi",
    dismiss: "Ignora",
    hang_up: "Riaggancia",
    in_call: "In chiamata",
    in_call_with: "In chiamata con {panel}",
    open_door: "Apri la porta",
    open_named: "Apri {name}",
    confirm_hint: "Tocca due volte o tieni premuto",
    tap_again: "Tocca di nuovo per aprire",
    tap_again_for: "Tocca di nuovo per aprire {name}",
    opening: "Apertura…",
    opened: "Aperto",
    opened_named: "{name} aperto",
    done: "Fatto",
    doors: "Porte",
    controls: "Comandi",
    connected: "Connesso",
    disconnected: "Disconnesso",
    status_unknown: "Stato sconosciuto",
    reconnect: "Riconnetti",
    reconnecting: "Riconnessione…",
    call_failed: "{panel} non ha risposto",
    call_failed_generic: "La chiamata non si è collegata",
    stream_unavailable:
      "Questa versione di Home Assistant non può mostrare qui il video",
    no_device:
      "Nessun dispositivo Citofono Vimar trovato. Aggiungi prima l'integrazione Vimar Intercom.",
    device_missing: "Il citofono scelto per questa scheda non esiste più.",
    editor_device_id: "Citofono",
    editor_title: "Titolo (facoltativo)",
    editor_show_actuators: "Mostra luci e altri comandi",
    editor_hidden_entities: "Nascondi questi elementi",
    editor_hidden_help:
      "Una porta, un comando o un pulsante di pulsantiera nascosto non compare nella scheda.",
    editor_unavailable:
      "L'editor visuale non è disponibile. Usa l'editor di codice.",
  },
};

// Roles by translation key, for an entity whose state has not arrived yet
// or lost its attributes (unavailable). The attribute is the contract; this
// is the fallback, and it cannot place a panel button on a panel.
const ROLE_BY_TRANSLATION_KEY = {
  "camera.intercom": "camera",
  "event.doorbell": "doorbell",
  "binary_sensor.sip_registration": "registration",
  "binary_sensor.in_call": "in_call",
  "button.answer": "answer",
  "button.hang_up": "hangup",
  "button.reconnect": "reconnect",
  "lock.door": "door",
};
const ROLE_BY_DOMAIN = { camera: "camera", event: "doorbell", lock: "door" };
const OPEN_LOCK_STATES = new Set(["unlocked", "unlocking", "open", "opening"]);

// ─── helpers ─────────────────────────────────────────────────────────

function language(hass) {
  const lang = (hass && ((hass.locale && hass.locale.language) || hass.language)) || "en";
  return String(lang).split("-")[0].toLowerCase();
}

function t(hass, key, vars) {
  const table = STRINGS[language(hass)] || STRINGS.en;
  const text = table[key] !== undefined ? table[key] : STRINGS.en[key] !== undefined ? STRINGS.en[key] : key;
  return text.replace(/\{(\w+)\}/g, (_, name) => (vars && vars[name] != null ? vars[name] : ""));
}

const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
function esc(value) {
  return String(value == null ? "" : value).replace(/[&<>"']/g, (c) => ESCAPES[c]);
}

function fire(node, type, detail) {
  node.dispatchEvent(new CustomEvent(type, { detail, bubbles: true, composed: true }));
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function icon(name) {
  return `<ha-icon icon="${esc(name)}" aria-hidden="true"></ha-icon>`;
}

function errorText(err) {
  if (!err) return "";
  if (typeof err === "string") return err;
  return err.message || err.error || err.code || String(err);
}

function formatDuration(ms) {
  const total = Math.max(0, Math.floor(ms / 1000));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

/** The devices that have entities of this integration, in registry order. */
function intercomDevices(hass) {
  const found = [];
  const entities = (hass && hass.entities) || {};
  for (const entry of Object.values(entities)) {
    if (entry.platform === DOMAIN && entry.device_id && !found.includes(entry.device_id)) {
      found.push(entry.device_id);
    }
  }
  return found;
}

function deviceName(hass, deviceId) {
  const device = hass.devices && hass.devices[deviceId];
  return (device && (device.name_by_user || device.name)) || "";
}

/**
 * The name the user sees, without the device name Home Assistant puts in
 * front of every entity of a device.
 */
function entityName(hass, entityId, devName) {
  const entry = hass.entities[entityId];
  if (entry && entry.name) return entry.name;
  const state = hass.states[entityId];
  const friendly = (state && state.attributes.friendly_name) || entityId;
  if (devName && friendly.startsWith(`${devName} `)) return friendly.slice(devName.length + 1);
  return friendly;
}

function roleOf(entityId, entry, state) {
  const attr = state && state.attributes && state.attributes.intercom_role;
  if (attr) return attr;
  const domain = entityId.split(".")[0];
  return ROLE_BY_TRANSLATION_KEY[`${domain}.${entry.translation_key}`] || ROLE_BY_DOMAIN[domain] || null;
}

/**
 * Everything the card shows, from the registry and the states.
 * Returns { error } when there is nothing to show.
 */
function resolveModel(hass, config) {
  if (!hass.entities) return { error: "no_device" };
  const devices = intercomDevices(hass);
  const deviceId = config.device_id || devices[0];
  if (!deviceId) return { error: "no_device" };
  if (!devices.includes(deviceId)) return { error: "device_missing" };

  const hidden = new Set(config.hidden_entities || []);
  const devName = deviceName(hass, deviceId);
  const model = {
    deviceId,
    camera: null,
    doorbell: null,
    registration: null,
    inCall: null,
    answer: null,
    hangup: null,
    reconnect: null,
    defaultPanel: null,
    panels: [],
    doors: [],
    actuators: [],
    // Every entity this card reads, for the "did anything change" check.
    watched: [],
  };
  const panels = new Map();
  const panelOf = (ext) => {
    if (!panels.has(ext)) panels.set(ext, { ext, name: ext, call: null, open: null });
    return panels.get(ext);
  };

  for (const [entityId, entry] of Object.entries(hass.entities)) {
    if (entry.platform !== DOMAIN || entry.device_id !== deviceId) continue;
    const state = hass.states[entityId];
    // A disabled entity has a registry entry and no state.
    if (!state) continue;
    model.watched.push(entityId);
    const role = roleOf(entityId, entry, state);
    const attrs = state.attributes || {};
    switch (role) {
      case "camera":
        model.camera = entityId;
        model.defaultPanel = attrs.default_panel != null ? String(attrs.default_panel) : null;
        if (model.defaultPanel && attrs.default_panel_name) {
          panelOf(model.defaultPanel).name = attrs.default_panel_name;
        }
        break;
      case "doorbell":
        model.doorbell = entityId;
        break;
      case "registration":
        model.registration = entityId;
        break;
      case "in_call":
        model.inCall = entityId;
        break;
      case "answer":
        model.answer = entityId;
        break;
      case "hangup":
        model.hangup = entityId;
        break;
      case "reconnect":
        model.reconnect = entityId;
        break;
      case "call":
      case "open": {
        if (attrs.panel == null || hidden.has(entityId)) break;
        const panel = panelOf(String(attrs.panel));
        if (attrs.panel_name) panel.name = attrs.panel_name;
        panel[role] = entityId;
        break;
      }
      case "door":
        if (!hidden.has(entityId)) {
          model.doors.push({ id: entityId, name: entityName(hass, entityId, devName), icon: entry.icon || null });
        }
        break;
      case "actuator":
        if (!hidden.has(entityId)) {
          model.actuators.push({
            id: entityId,
            name: entityName(hass, entityId, devName),
            icon: entry.icon || attrs.icon || "mdi:gesture-tap-button",
          });
        }
        break;
      default:
        break;
    }
  }

  // A panel is a chip only if it can be watched: it has a call button, or
  // it is the one the camera calls on its own.
  model.panels = [...panels.values()].filter((p) => p.call || p.ext === model.defaultPanel);
  model.panels.sort((a, b) => (a.ext === model.defaultPanel ? -1 : b.ext === model.defaultPanel ? 1 : 0));
  model.panelByExt = panels;
  return model;
}

/** Everything the visual editor may offer to hide. */
function hideableEntities(hass, config) {
  const model = resolveModel(hass, { ...config, hidden_entities: [] });
  if (model.error) return [];
  const ids = [...model.doors.map((d) => d.id), ...model.actuators.map((a) => a.id)];
  for (const panel of model.panelByExt.values()) {
    if (panel.call) ids.push(panel.call);
    if (panel.open) ids.push(panel.open);
  }
  return ids;
}

/**
 * `ha-camera-stream` is part of the frontend but loaded on demand. Creating
 * (not attaching) a picture-entity card makes the frontend load it.
 */
async function ensureCameraStream(cameraId) {
  if (customElements.get("ha-camera-stream")) return true;
  try {
    const helpers = window.loadCardHelpers && (await window.loadCardHelpers());
    if (helpers) {
      await helpers.createCardElement({ type: "picture-entity", entity: cameraId, camera_view: "live" });
    }
  } catch (_err) {
    // Fall through to the wait: the element may still be on its way.
  }
  await Promise.race([customElements.whenDefined("ha-camera-stream"), sleep(5000)]);
  return Boolean(customElements.get("ha-camera-stream"));
}

/** `ha-form` is loaded with the card editors; make sure it is. */
async function ensureHaForm() {
  if (customElements.get("ha-form")) return true;
  try {
    const helpers = window.loadCardHelpers && (await window.loadCardHelpers());
    if (helpers) {
      const card = await helpers.createCardElement({ type: "entities", entities: [] });
      if (card && card.constructor.getConfigElement) await card.constructor.getConfigElement();
    }
  } catch (_err) {
    // Handled below.
  }
  await Promise.race([customElements.whenDefined("ha-form"), sleep(3000)]);
  return Boolean(customElements.get("ha-form"));
}

// ─── styles ──────────────────────────────────────────────────────────

const STYLES = `
  :host {
    display: block;
    --vic-success: var(--success-color, #43a047);
    --vic-danger: var(--error-color, #db4437);
    --vic-radius: var(--ha-card-border-radius, 12px);
    --vic-tile: var(--secondary-background-color, rgba(127, 127, 127, 0.12));
    --vic-gap: 12px;
  }
  ha-card { overflow: hidden; display: flex; flex-direction: column; }
  [hidden] { display: none !important; }
  button {
    font: inherit;
    color: inherit;
    border: none;
    background: none;
    cursor: pointer;
    -webkit-tap-highlight-color: transparent;
    touch-action: manipulation;
  }
  button:disabled { cursor: default; opacity: 0.55; }
  button:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 2px;
  }
  ha-icon { --mdc-icon-size: 24px; display: inline-flex; flex: none; }
  .sr {
    position: absolute; width: 1px; height: 1px; margin: -1px; padding: 0;
    overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0;
  }
  .header {
    padding: 16px 16px 12px;
    font-size: var(--ha-card-header-font-size, 1.25rem);
    font-weight: 500;
    line-height: 1.3;
    color: var(--ha-card-header-color, var(--primary-text-color));
  }
  .message {
    padding: 16px;
    display: flex;
    gap: 12px;
    align-items: flex-start;
    color: var(--primary-text-color);
  }
  .message ha-icon { color: var(--warning-color, #ffa600); }

  /* Video */
  .video {
    position: relative;
    aspect-ratio: 16 / 9;
    background: radial-gradient(ellipse at center, #26292e 0%, #0d0e10 75%);
    color: #fff;
    overflow: hidden;
  }
  .poster, .stream {
    position: absolute; inset: 0; width: 100%; height: 100%;
  }
  .poster { object-fit: cover; opacity: 0.6; }
  .stream { display: flex; align-items: center; justify-content: center; }
  .stream ha-camera-stream {
    width: 100%;
    height: 100%;
    --video-max-height: 100%;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .overlay { position: absolute; inset: 0; pointer-events: none; }
  .overlay > * { pointer-events: auto; }
  .center {
    position: absolute; inset: 0;
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    gap: 12px; padding: 16px; text-align: center;
    pointer-events: none;
  }
  .center > * { pointer-events: auto; }
  .center > ha-icon { --mdc-icon-size: 40px; opacity: 0.85; }
  .play {
    display: flex; flex-direction: column; align-items: center; gap: 10px;
    padding: 8px; border-radius: 16px;
    color: #fff;
    text-shadow: 0 1px 3px rgba(0, 0, 0, 0.6);
    font-weight: 500;
  }
  .play .disc {
    width: 72px; height: 72px; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    background: rgba(255, 255, 255, 0.16);
    border: 1px solid rgba(255, 255, 255, 0.35);
    -webkit-backdrop-filter: blur(8px);
    backdrop-filter: blur(8px);
    transition: transform 0.15s ease, background 0.15s ease;
  }
  .play .disc ha-icon { --mdc-icon-size: 40px; }
  .play:hover .disc { background: rgba(255, 255, 255, 0.26); }
  .play:active .disc { transform: scale(0.94); }
  .play:disabled .disc { opacity: 0.5; }
  .note {
    font-size: 0.9rem;
    color: rgba(255, 255, 255, 0.85);
    text-shadow: 0 1px 3px rgba(0, 0, 0, 0.6);
  }
  .spinner {
    width: 40px; height: 40px; border-radius: 50%;
    border: 3px solid rgba(255, 255, 255, 0.25);
    border-top-color: #fff;
    animation: vic-spin 0.9s linear infinite;
  }
  .badge {
    position: absolute; top: 12px; left: 12px;
    display: inline-flex; align-items: center; gap: 6px;
    padding: 4px 10px; border-radius: 999px;
    background: rgba(0, 0, 0, 0.55);
    font-size: 0.8rem; font-weight: 600; letter-spacing: 0.02em;
    max-width: calc(100% - 88px);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .badge .dot { background: #ff3b30; }
  .corner {
    position: absolute; top: 8px; right: 8px;
    width: 44px; height: 44px; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    background: rgba(0, 0, 0, 0.55);
    color: #fff;
  }
  .corner:hover { background: rgba(0, 0, 0, 0.75); }

  /* Panel chips */
  .chips {
    display: flex; gap: 8px;
    padding: 12px 16px 0;
    overflow-x: auto;
    scrollbar-width: none;
  }
  .chips::-webkit-scrollbar { display: none; }
  .chip {
    flex: none;
    display: inline-flex; align-items: center; gap: 6px;
    min-height: 44px; padding: 0 16px;
    border-radius: 22px;
    border: 1px solid var(--divider-color, rgba(127, 127, 127, 0.3));
    color: var(--primary-text-color);
    font-size: 0.95rem;
  }
  .chip ha-icon { --mdc-icon-size: 20px; color: var(--secondary-text-color); }
  .chip[aria-pressed="true"] {
    background: var(--primary-color);
    border-color: var(--primary-color);
    color: var(--text-primary-color, #fff);
  }
  .chip[aria-pressed="true"] ha-icon { color: inherit; }

  /* Ringing and in-call */
  .ring {
    margin: 12px 16px 0;
    padding: 16px;
    border-radius: var(--vic-radius);
    background: color-mix(in srgb, var(--primary-color) 14%, transparent);
    border: 1px solid color-mix(in srgb, var(--primary-color) 35%, transparent);
  }
  .ring-head { display: flex; align-items: center; gap: 12px; }
  .bell {
    width: 44px; height: 44px; border-radius: 50%; flex: none;
    display: flex; align-items: center; justify-content: center;
    background: var(--primary-color); color: var(--text-primary-color, #fff);
    animation: vic-ring 1.6s ease-in-out infinite;
  }
  .ring-title { font-size: 1.15rem; font-weight: 600; color: var(--primary-text-color); line-height: 1.3; }
  .ring-sub { font-size: 0.9rem; color: var(--secondary-text-color); margin-top: 2px; }
  .actions {
    display: flex; justify-content: space-around; align-items: flex-start;
    gap: 8px; margin-top: 16px;
  }
  .action {
    display: flex; flex-direction: column; align-items: center; gap: 6px;
    min-width: 72px; padding: 4px;
    font-size: 0.85rem; color: var(--primary-text-color);
    border-radius: 12px;
  }
  .action .disc {
    width: 64px; height: 64px; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    color: #fff;
    transition: transform 0.15s ease, filter 0.15s ease;
  }
  .action .disc ha-icon { --mdc-icon-size: 30px; }
  .action:active .disc { transform: scale(0.94); }
  .action:hover .disc { filter: brightness(1.08); }
  .disc.success { background: var(--vic-success); }
  .disc.danger { background: var(--vic-danger); }
  .disc.neutral {
    background: var(--vic-tile);
    color: var(--primary-text-color);
  }
  .action.armed .disc.neutral { background: var(--primary-color); color: var(--text-primary-color, #fff); }
  .action.opened .disc.neutral { background: var(--vic-success); color: #fff; }
  .incall {
    margin: 12px 16px 0;
    padding: 12px 12px 12px 16px;
    border-radius: var(--vic-radius);
    background: var(--vic-tile);
    display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  }
  .incall-text { flex: 1 1 140px; min-width: 0; }
  .incall-title {
    display: flex; align-items: center; gap: 8px;
    font-weight: 600; color: var(--primary-text-color);
  }
  .incall-title span:last-child { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .timer { font-variant-numeric: tabular-nums; color: var(--secondary-text-color); font-size: 0.9rem; }
  .incall-note { font-size: 0.85rem; color: var(--secondary-text-color); margin-top: 2px; }
  .incall-actions { display: flex; gap: 8px; flex-wrap: wrap; }
  .pill {
    position: relative; overflow: hidden;
    display: inline-flex; align-items: center; justify-content: center; gap: 8px;
    min-height: 44px; padding: 0 16px;
    border-radius: 22px;
    font-weight: 500;
  }
  .pill.danger { background: var(--vic-danger); color: #fff; }
  .pill.neutral {
    background: var(--card-background-color, var(--ha-card-background, #fff));
    color: var(--primary-text-color);
    border: 1px solid var(--divider-color, rgba(127, 127, 127, 0.3));
  }
  .pill.neutral.armed { background: var(--primary-color); border-color: var(--primary-color); color: var(--text-primary-color, #fff); }
  .pill.neutral.opened { background: var(--vic-success); border-color: var(--vic-success); color: #fff; }

  /* Doors and controls */
  .section { padding: 16px 16px 0; }
  .section-title {
    font-size: 0.8rem; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase;
    color: var(--secondary-text-color);
    margin: 0 0 8px;
  }
  .grid {
    display: grid; gap: 8px;
    /* One full-width button per door on a phone, more side by side on
       a wide card. */
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  }
  .door {
    position: relative; overflow: hidden;
    display: flex; align-items: center; gap: 12px;
    min-height: 64px; padding: 10px 14px;
    border-radius: var(--vic-radius);
    background: var(--vic-tile);
    color: var(--primary-text-color);
    text-align: left;
    user-select: none; -webkit-user-select: none; -webkit-touch-callout: none;
    transition: background 0.2s ease, color 0.2s ease;
  }
  .door .glyph {
    width: 40px; height: 40px; border-radius: 50%; flex: none;
    display: flex; align-items: center; justify-content: center;
    background: color-mix(in srgb, var(--primary-color) 16%, transparent);
    color: var(--primary-color);
  }
  .door .text { display: flex; flex-direction: column; min-width: 0; position: relative; }
  .door .label { font-weight: 600; line-height: 1.25; overflow-wrap: anywhere; }
  .door .hint { font-size: 0.8rem; color: var(--secondary-text-color); margin-top: 2px; }
  .door.armed { background: var(--primary-color); color: var(--text-primary-color, #fff); }
  .door.armed .glyph { background: rgba(255, 255, 255, 0.2); color: inherit; }
  .door.armed .hint { color: inherit; opacity: 0.85; }
  .door.opened { background: var(--vic-success); color: #fff; }
  .door.opened .glyph { background: rgba(255, 255, 255, 0.2); color: inherit; }
  .door.opened .hint { color: inherit; opacity: 0.85; }
  /* Hold-to-open fill, and the second-tap countdown. */
  .confirm::before {
    content: ""; position: absolute; inset: 0;
    background: color-mix(in srgb, var(--primary-color) 30%, transparent);
    transform: scaleX(0); transform-origin: left;
    pointer-events: none;
  }
  .confirm > * { position: relative; }
  .confirm.holding::before { transform: scaleX(1); transition: transform ${HOLD_MS}ms linear; }
  .confirm.armed::after {
    content: ""; position: absolute; left: 0; right: 0; bottom: 0; height: 3px;
    background: currentColor; opacity: 0.6;
    transform-origin: left;
    animation: vic-countdown ${CONFIRM_WINDOW_MS}ms linear forwards;
    animation-delay: var(--vic-armed-elapsed, 0ms);
  }
  .action.confirm::before, .action.confirm::after { border-radius: 12px; }
  .acts { display: flex; flex-wrap: wrap; gap: 8px; }
  .act {
    display: inline-flex; align-items: center; gap: 8px;
    min-height: 44px; padding: 0 14px 0 12px;
    border-radius: 22px;
    background: var(--vic-tile);
    color: var(--primary-text-color);
    transition: background 0.2s ease;
  }
  .act ha-icon { --mdc-icon-size: 20px; color: var(--state-icon-color, var(--secondary-text-color)); }
  .act.done { background: var(--vic-success); color: #fff; }
  .act.done ha-icon { color: inherit; }

  /* Status */
  .footer {
    display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
    min-height: 44px;
    padding: 12px 16px 12px;
    margin-top: 12px;
    border-top: 1px solid var(--divider-color, rgba(127, 127, 127, 0.2));
    font-size: 0.9rem;
    color: var(--secondary-text-color);
  }
  .dot { width: 8px; height: 8px; border-radius: 50%; flex: none; background: var(--disabled-text-color, #9e9e9e); }
  .dot.ok { background: var(--vic-success); }
  .dot.bad { background: var(--vic-danger); }
  .dot.live { background: #ff3b30; animation: vic-pulse 1.4s ease-in-out infinite; }
  .footer .grow { flex: 1; }
  .link {
    min-height: 44px; padding: 0 12px; border-radius: 22px;
    color: var(--primary-color); font-weight: 500;
    display: inline-flex; align-items: center; gap: 6px;
  }
  .link:hover { background: color-mix(in srgb, var(--primary-color) 10%, transparent); }

  @keyframes vic-spin { to { transform: rotate(360deg); } }
  @keyframes vic-countdown { from { transform: scaleX(1); } to { transform: scaleX(0); } }
  @keyframes vic-pulse { 50% { opacity: 0.35; } }
  @keyframes vic-ring {
    0%, 60%, 100% { transform: rotate(0); }
    10%, 30%, 50% { transform: rotate(-12deg); }
    20%, 40% { transform: rotate(12deg); }
  }
  @media (prefers-reduced-motion: reduce) {
    .bell, .dot.live { animation: none; }
    .spinner { animation-duration: 2.5s; }
    .confirm.holding::before { transition: none; }
  }
`;

// ─── talk-back ───────────────────────────────────────────────────────
//
// The Talk button: the browser's microphone, played out of the entrance
// panel. One self-contained block - its strings, styles, audio capture
// and button - that the card touches in four places (`_build`,
// `_render`, `disconnectedCallback` and the style tag), so the rest of
// the card can change around it without conflicts.
//
// The audio goes over the websocket the frontend already holds, not a
// new endpoint: `vimar_intercom/talk` (talk_api.py) answers with a
// binary handler ID, and every 20 ms frame is sent as one binary message
// with that ID as its first byte, which is how Assist streams microphone
// audio. The integration refuses when there is no call; closing the
// subscription or the websocket ends the stream.
//
// Frames are 16-bit little-endian mono PCM at 8 kHz, 160 samples: the
// panel's own rate, so the server only has to encode µ-law. Resampling
// happens here, in an AudioWorklet (a ScriptProcessor where there is
// none): Firefox refuses to connect a microphone to an AudioContext
// running at another rate than the device, so asking for an 8 kHz
// context is not an option.
//
// While Talk is on, the stream's own audio is muted (ducked), and
// restored after. The panel's microphone hears its own speaker, and the
// visitor's audio reaches the browser seconds late through the stream,
// so without ducking you hear your own voice come back after a delay -
// an echo the browser's echo canceller cannot remove, since it never
// played that audio through the call. Intercoms work this way too:
// while you talk you do not hear the door.

// Strings are merged into the card's dictionaries rather than written
// into them, so this block stays in one place.
const TALK_STRINGS = {
  en: {
    talk: "Hold to talk",
    talk_starting: "Starting the microphone…",
    talk_active: "Talking - tap to stop",
    talk_active_hold: "Talking - release to stop",
    talk_note: "Hold while you speak, or tap to keep talking",
    talk_started: "Talking to the door",
    talk_stopped: "Stopped talking",
    talk_needs_https:
      "Talking to the door needs Home Assistant over HTTPS, so the browser can use the microphone",
    talk_denied: "Microphone access was denied",
    talk_no_mic: "No microphone was found",
    talk_failed: "Talk-back could not start: {error}",
    talk_replaced: "Someone else is talking to the door now",
    talk_no_call: "There is no call to talk into",
    talk_no_audio: "This call carries no audio from Home Assistant",
  },
  it: {
    talk: "Tieni premuto per parlare",
    talk_starting: "Avvio del microfono…",
    talk_active: "Stai parlando - tocca per smettere",
    talk_active_hold: "Stai parlando - rilascia per smettere",
    talk_note: "Tieni premuto mentre parli, o tocca per continuare a parlare",
    talk_started: "Stai parlando alla porta",
    talk_stopped: "Hai smesso di parlare",
    talk_needs_https:
      "Per parlare alla porta Home Assistant deve essere in HTTPS, così il browser può usare il microfono",
    talk_denied: "L'accesso al microfono è stato negato",
    talk_no_mic: "Nessun microfono trovato",
    talk_failed: "Impossibile parlare alla porta: {error}",
    talk_replaced: "Ora sta parlando alla porta qualcun altro",
    talk_no_call: "Non c'è nessuna chiamata in cui parlare",
    talk_no_audio: "Questa chiamata non porta audio da Home Assistant",
  },
};
for (const lang of Object.keys(TALK_STRINGS)) Object.assign(STRINGS[lang], TALK_STRINGS[lang]);

const TALK_COMMAND = `${DOMAIN}/talk`;
// A press shorter than this is a tap, which keeps Talk on until the next
// tap; a longer one is push-to-talk, and letting go stops.
const TALK_HOLD_MS = 400;
// Frames are dropped, not queued, when the socket is this far behind: a
// late voice is worse than a gap. 16 KB is about half a second of audio.
const TALK_MAX_BUFFERED = 16384;
const TALK_ERRORS = { no_call: "talk_no_call", no_audio: "talk_no_audio" };

const TALK_STYLES = `
  .talk { margin: 12px 16px 0; display: flex; flex-direction: column; gap: 6px; }
  .talk-btn {
    display: flex; align-items: center; justify-content: center; gap: 10px;
    width: 100%; min-height: 52px; padding: 0 20px;
    border-radius: 26px;
    background: var(--vic-tile);
    color: var(--primary-text-color);
    font-weight: 600;
    user-select: none; -webkit-user-select: none; -webkit-touch-callout: none;
    /* A hold must not turn into a scroll. */
    touch-action: none;
    transition: background 0.15s ease, color 0.15s ease;
  }
  .talk-btn.starting { opacity: 0.75; }
  .talk-btn.active {
    background: var(--primary-color, #03a9f4);
    color: var(--text-primary-color, #fff);
  }
  .talk-btn.active ha-icon { animation: vic-pulse 1.2s ease-in-out infinite; }
  .talk-note {
    font-size: 0.85rem; color: var(--secondary-text-color);
    text-align: center;
  }
  .talk-hint {
    display: flex; gap: 8px; align-items: flex-start;
    font-size: 0.85rem; color: var(--secondary-text-color);
  }
  .talk-hint ha-icon { --mdc-icon-size: 20px; }
  @media (prefers-reduced-motion: reduce) {
    .talk-btn.active ha-icon { animation: none; }
  }
`;

function talkSupported() {
  return Boolean(
    window.isSecureContext &&
      navigator.mediaDevices &&
      typeof navigator.mediaDevices.getUserMedia === "function" &&
      (window.AudioContext || window.webkitAudioContext),
  );
}

/** Every <video>/<audio> at or under `node`, through open shadow roots. */
function talkMediaElements(node, found = [], depth = 0) {
  if (!node || depth > 32) return found;
  if (node.tagName === "VIDEO" || node.tagName === "AUDIO") found.push(node);
  if (node.shadowRoot) {
    for (const child of node.shadowRoot.children) talkMediaElements(child, found, depth + 1);
  }
  for (const child of node.children || []) talkMediaElements(child, found, depth + 1);
  return found;
}

/**
 * Microphone samples at the device rate in, 20 ms PCM frames at 8 kHz
 * out. Self-contained - no outer names - because its source text is
 * also what the AudioWorklet runs (see `_captureNode`).
 *
 * A 6th-order Butterworth low-pass at 3.2 kHz (three biquads) first,
 * so that what cannot be carried at 8 kHz is removed instead of folding
 * back into the speech band - 6 kHz would come back as 2 kHz - then
 * linear interpolation at the output instants.
 */
class TalkResampler {
  constructor(inRate, onFrame) {
    this.step = inRate / 8000;
    this.t = 1;
    this.prev = 0;
    this.onFrame = onFrame;
    this.buf = new ArrayBuffer(320);
    this.view = new DataView(this.buf);
    this.n = 0;
    this.stages = [];
    if (inRate > 7000) {
      const w0 = (2 * Math.PI * 3200) / inRate;
      for (const q of [0.5176, 0.7071, 1.9319]) {
        const alpha = Math.sin(w0) / (2 * q);
        const cos = Math.cos(w0);
        const a0 = 1 + alpha;
        this.stages.push({
          b0: (1 - cos) / 2 / a0, b1: (1 - cos) / a0, b2: (1 - cos) / 2 / a0,
          a1: (-2 * cos) / a0, a2: (1 - alpha) / a0,
          x1: 0, x2: 0, y1: 0, y2: 0,
        });
      }
    }
  }

  process(input) {
    for (let i = 0; i < input.length; i++) {
      let x = input[i];
      for (const s of this.stages) {
        const y = s.b0 * x + s.b1 * s.x1 + s.b2 * s.x2 - s.a1 * s.y1 - s.a2 * s.y2;
        s.x2 = s.x1; s.x1 = x; s.y2 = s.y1; s.y1 = y;
        x = y;
      }
      while (this.t <= 1) {
        this.emit(this.prev + (x - this.prev) * this.t);
        this.t += this.step;
      }
      this.t -= 1;
      this.prev = x;
    }
  }

  emit(value) {
    const v = Math.max(-1, Math.min(1, value));
    this.view.setInt16(this.n * 2, v < 0 ? v * 0x8000 : v * 0x7fff, true);
    this.n += 1;
    if (this.n === 160) {
      this.onFrame(this.buf);
      this.buf = new ArrayBuffer(320);
      this.view = new DataView(this.buf);
      this.n = 0;
    }
  }
}

const TALK_WORKLET = `${TalkResampler.toString()}
class VimarTalkCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.resampler = new TalkResampler(sampleRate, (buf) => this.port.postMessage(buf, [buf]));
  }
  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (channel) this.resampler.process(channel);
    return true;
  }
}
registerProcessor("vimar-talk-capture", VimarTalkCapture);
`;

class VimarTalkBack {
  /**
   * `el` is the card's talk section, which this owns. `host` gives the
   * card's toast, live-region announcement and current stream element.
   */
  constructor(el, host) {
    this._el = el;
    this._host = host;
    this._hass = null;
    // idle | starting | talking
    this._state = "idle";
    this._shown = "";
    this._lang = null;
    this._gen = 0;
    this._press = null;
    this._handlerId = null;
    this._socket = null;
    this._unsub = null;
    this._audio = null;
    this._ducked = [];
    el.addEventListener("pointerdown", (ev) => this._onPointerDown(ev));
    el.addEventListener("pointerup", (ev) => this._onPointerUp(ev));
    el.addEventListener("pointercancel", (ev) => this._onPointerUp(ev));
    el.addEventListener("click", (ev) => this._onClick(ev));
    el.addEventListener("contextmenu", (ev) => {
      if (ev.target.closest && ev.target.closest(".talk-btn")) ev.preventDefault();
    });
  }

  /** Called on every card render: show, hide or stop as the call goes. */
  update(hass, watchingCall) {
    this._hass = hass;
    const supported = talkSupported();
    const shown = !watchingCall ? "" : supported ? "button" : window.isSecureContext ? "" : "hint";
    if (!watchingCall && this._state !== "idle") this.stop();
    const lang = language(hass);
    if (shown !== this._shown || lang !== this._lang) {
      this._shown = shown;
      this._lang = lang;
      this._build();
    }
    this._paint();
  }

  _build() {
    const hass = this._hass;
    if (this._shown === "button") {
      this._el.innerHTML = `
        <button class="talk-btn" data-key="talk" aria-pressed="false">
          ${icon("mdi:microphone")}<span class="talk-label"></span>
        </button>
        <div class="talk-note">${esc(t(hass, "talk_note"))}</div>`;
    } else if (this._shown === "hint") {
      this._el.innerHTML = `<div class="talk-hint">${icon("mdi:microphone-off")}<span>${esc(t(hass, "talk_needs_https"))}</span></div>`;
    } else {
      this._el.innerHTML = "";
    }
    this._el.hidden = !this._shown;
  }

  /** State changes touch attributes only: a held button is never replaced. */
  _paint() {
    const btn = this._el.querySelector(".talk-btn");
    if (!btn) return;
    const hass = this._hass;
    const talking = this._state === "talking";
    const key = this._state === "starting"
      ? "talk_starting"
      : talking
        ? this._press && this._press.started ? "talk_active_hold" : "talk_active"
        : "talk";
    btn.classList.toggle("starting", this._state === "starting");
    btn.classList.toggle("active", talking);
    btn.setAttribute("aria-pressed", String(this._state !== "idle"));
    const label = btn.querySelector(".talk-label");
    const text = t(hass, key);
    if (label.textContent !== text) label.textContent = text;
    const note = this._el.querySelector(".talk-note");
    if (note) note.hidden = this._state !== "idle";
  }

  // ── input ──

  _onPointerDown(ev) {
    const btn = ev.target.closest && ev.target.closest(".talk-btn");
    if (!btn || (ev.button !== undefined && ev.button !== 0)) return;
    ev.preventDefault();
    try {
      btn.setPointerCapture(ev.pointerId);
    } catch (_err) {
      // Capture is a nicety: pointerup still arrives on the button.
    }
    const started = this._state === "idle";
    this._press = { at: Date.now(), started };
    if (started) this._start();
    this._paint();
  }

  _onPointerUp(_ev) {
    const press = this._press;
    this._press = null;
    if (!press) return;
    if (!press.started) {
      // A tap while talking ends it.
      this.stop();
      return;
    }
    // Held, and live by now: push-to-talk, so letting go stops. A tap -
    // or a press released while the microphone was still starting, as
    // when the permission prompt took the pointer - keeps talking until
    // the next tap.
    if (Date.now() - press.at >= TALK_HOLD_MS && this._state === "talking") this.stop();
    else this._paint();
  }

  _onClick(ev) {
    const btn = ev.target.closest && ev.target.closest(".talk-btn");
    // Pointer clicks were handled by the pointer events; `detail` 0 is
    // Enter or Space, which toggles.
    if (!btn || ev.detail !== 0) return;
    if (this._state === "idle") this._start();
    else this.stop();
  }

  // ── the stream ──

  async _start() {
    if (this._state !== "idle" || !this._hass) return;
    const gen = ++this._gen;
    const hass = this._hass;
    this._state = "starting";
    this._paint();

    // Created inside the gesture, or browsers start it suspended.
    let ctx;
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      ctx = new Ctx();
    } catch (err) {
      this.stop();
      this._host.toast(t(hass, "talk_failed", { error: errorText(err) }));
      return;
    }
    this._audio = { ctx, mic: null, source: null, node: null };
    if (ctx.state === "suspended") ctx.resume().catch(() => {});

    const [mic, sub] = await Promise.allSettled([
      navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
          channelCount: 1,
        },
        video: false,
      }),
      hass.connection.subscribeMessage(
        (event) => this._onEvent(gen, event),
        { type: TALK_COMMAND },
        { resubscribe: false },
      ),
    ]);
    if (mic.status === "fulfilled" && this._audio && gen === this._gen) this._audio.mic = mic.value;
    if (sub.status === "fulfilled") {
      if (gen === this._gen) this._unsub = sub.value;
      else Promise.resolve().then(sub.value).catch(() => {});
    }
    if (gen !== this._gen) {
      // Stopped while starting: release what arrived late.
      if (mic.status === "fulfilled") mic.value.getTracks().forEach((track) => track.stop());
      return;
    }
    if (mic.status === "rejected" || sub.status === "rejected") {
      this.stop();
      this._host.toast(this._errorText(mic.status === "rejected" ? mic.reason : sub.reason));
      return;
    }
    this._socket = hass.connection.socket;

    try {
      const source = ctx.createMediaStreamSource(mic.value);
      const node = await this._captureNode(ctx, (buf) => this._send(buf));
      if (gen !== this._gen) {
        node.disconnect();
        return;
      }
      source.connect(node);
      // Silent (nothing is written to the output), but connected, or the
      // node is never pulled and never runs.
      node.connect(ctx.destination);
      Object.assign(this._audio, { source, node });
    } catch (err) {
      if (gen !== this._gen) return;
      this.stop();
      this._host.toast(t(this._hass, "talk_failed", { error: errorText(err) }));
      return;
    }
    this._state = "talking";
    this._duck();
    this._paint();
    this._host.announce(t(this._hass, "talk_started"));
  }

  async _captureNode(ctx, onFrame) {
    if (ctx.audioWorklet && window.AudioWorkletNode) {
      const url = URL.createObjectURL(new Blob([TALK_WORKLET], { type: "text/javascript" }));
      try {
        await ctx.audioWorklet.addModule(url);
        const node = new AudioWorkletNode(ctx, "vimar-talk-capture", {
          numberOfInputs: 1,
          numberOfOutputs: 1,
          channelCount: 1,
          channelCountMode: "explicit",
        });
        node.port.onmessage = (ev) => onFrame(ev.data);
        return node;
      } catch (_err) {
        // A blocked blob: URL, or an old engine: fall through.
      } finally {
        URL.revokeObjectURL(url);
      }
    }
    // Deprecated but everywhere; runs on the main thread, which is fine
    // for 8 kHz speech.
    const node = ctx.createScriptProcessor(2048, 1, 1);
    const resampler = new TalkResampler(ctx.sampleRate, onFrame);
    node.onaudioprocess = (ev) => {
      resampler.process(ev.inputBuffer.getChannelData(0));
      ev.outputBuffer.getChannelData(0).fill(0);
    };
    return node;
  }

  _onEvent(gen, event) {
    if (gen !== this._gen || !event) return;
    if (event.type === "start") {
      this._handlerId = event.handler_id;
    } else if (event.type === "end") {
      this.stop();
      if (event.reason === "replaced") this._host.toast(t(this._hass, "talk_replaced"));
    }
  }

  _send(buf) {
    if (this._state !== "talking" || !this._handlerId) return;
    const socket = this._hass && this._hass.connection && this._hass.connection.socket;
    if (!socket || socket !== this._socket || socket.readyState !== 1) {
      // The websocket reconnected: the subscription died with the old one.
      this.stop();
      return;
    }
    if (socket.bufferedAmount > TALK_MAX_BUFFERED) return;
    const message = new Uint8Array(1 + buf.byteLength);
    message[0] = this._handlerId;
    message.set(new Uint8Array(buf), 1);
    socket.send(message);
  }

  /** End talking, whatever state it is in. Safe to call any time. */
  stop() {
    this._gen += 1;
    const was = this._state;
    this._state = "idle";
    this._handlerId = null;
    this._socket = null;
    if (this._unsub) {
      const unsub = this._unsub;
      this._unsub = null;
      // Rejects when the socket is already gone, which ends it anyway.
      Promise.resolve().then(unsub).catch(() => {});
    }
    const audio = this._audio;
    this._audio = null;
    if (audio) {
      try {
        if (audio.node) {
          audio.node.disconnect();
          if (audio.node.port) audio.node.port.onmessage = null;
          audio.node.onaudioprocess = null;
        }
        if (audio.source) audio.source.disconnect();
      } catch (_err) {
        // Already disconnected.
      }
      if (audio.mic) audio.mic.getTracks().forEach((track) => track.stop());
      audio.ctx.close().catch(() => {});
    }
    this._unduck();
    this._paint();
    if (was === "talking" && this._hass) this._host.announce(t(this._hass, "talk_stopped"));
  }

  _errorText(err) {
    const hass = this._hass;
    const name = err && err.name;
    if (name === "NotAllowedError" || name === "SecurityError") return t(hass, "talk_denied");
    if (name === "NotFoundError" || name === "OverconstrainedError") return t(hass, "talk_no_mic");
    if (err && TALK_ERRORS[err.code]) return t(hass, TALK_ERRORS[err.code]);
    return t(hass, "talk_failed", { error: errorText(err) });
  }

  // ── ducking ──

  _duck() {
    this._unduck();
    const stream = this._host.streamEl();
    for (const media of talkMediaElements(stream)) {
      if (!media.muted) {
        media.muted = true;
        this._ducked.push(media);
      }
    }
  }

  _unduck() {
    for (const media of this._ducked) media.muted = false;
    this._ducked = [];
  }
}

// ─── the card ────────────────────────────────────────────────────────

class VimarIntercomCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = null;
    this._hass = null;
    this._model = null;
    this._watchKey = null;
    this._last = {};
    // Video: idle | connecting | live
    this._view = "idle";
    // Who started what is on screen: watch (the camera's own call),
    // call (a call button), answer, join (a call already up).
    this._origin = null;
    this._activePanel = null;
    this._selected = null;
    this._sawCall = false;
    this._streamEl = null;
    this._gen = 0;
    this._prevInCall = undefined;
    this._ringId = undefined;
    this._ring = null;
    this._answering = false;
    this._armed = new Map();
    this._feedback = new Map();
    this._pending = new Set();
    this._confirmables = new Map();
    this._hold = null;
    this._holdFired = null;
    this._waiters = new Set();
    this._timers = {};
    this._posterSrc = null;
    this._posterOk = false;
    this._build();
  }

  // ── Lovelace API ──

  static getConfigElement() {
    return document.createElement(EDITOR_TYPE);
  }

  static getStubConfig(hass) {
    const [deviceId] = intercomDevices(hass);
    return deviceId ? { device_id: deviceId } : {};
  }

  setConfig(config) {
    if (!config || typeof config !== "object") throw new Error("Invalid configuration");
    if (config.hidden_entities != null && !Array.isArray(config.hidden_entities)) {
      throw new Error("hidden_entities must be a list of entity IDs");
    }
    const changedDevice = this._config && this._config.device_id !== config.device_id;
    this._config = { show_actuators: true, ...config };
    if (changedDevice) this._reset();
    this._watchKey = null;
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._config) return;
    // Every state change anywhere in Home Assistant lands here. Rendering
    // only when one of this card's entities changed keeps a busy
    // installation's dashboard cheap.
    const key = this._computeWatchKey(hass);
    if (key !== null && key === this._watchKey) {
      if (this._streamEl) this._streamEl.hass = hass;
      return;
    }
    this._watchKey = key;
    this._render();
    this._checkWaiters();
  }

  get hass() {
    return this._hass;
  }

  getCardSize() {
    return 8;
  }

  getGridOptions() {
    return { columns: 12, min_columns: 6 };
  }

  connectedCallback() {
    clearTimeout(this._timers.leave);
    this._timers.leave = null;
    this._render();
  }

  disconnectedCallback() {
    this._stopTicker();
    // The microphone never stays open behind a card nobody can see.
    this._talk.stop();
    if (this._view !== "idle" && (this._origin === "call" || this._origin === "answer")) {
      clearTimeout(this._timers.leave);
      this._timers.leave = setTimeout(() => {
        if (!this.isConnected) this._hangUp();
      }, LEAVE_GRACE_MS);
    }
  }

  // ── structure ──

  _build() {
    const root = this.shadowRoot;
    root.innerHTML = `
      <style>${STYLES}${TALK_STYLES}</style>
      <ha-card>
        <div class="header" hidden></div>
        <div class="message" hidden></div>
        <div class="video" hidden>
          <img class="poster" alt="" hidden>
          <div class="stream"></div>
          <div class="overlay"></div>
        </div>
        <div class="chips-wrap" hidden></div>
        <div class="call" hidden></div>
        <div class="talk" hidden></div>
        <div class="doors" hidden></div>
        <div class="actuators" hidden></div>
        <div class="footer" hidden></div>
        <div class="sr" role="status" aria-live="polite"></div>
      </ha-card>`;
    const q = (sel) => root.querySelector(sel);
    this._els = {
      header: q(".header"),
      message: q(".message"),
      video: q(".video"),
      poster: q(".poster"),
      stream: q(".stream"),
      overlay: q(".overlay"),
      chips: q(".chips-wrap"),
      call: q(".call"),
      talk: q(".talk"),
      doors: q(".doors"),
      actuators: q(".actuators"),
      footer: q(".footer"),
      sr: q(".sr"),
    };

    root.addEventListener("click", (ev) => this._onClick(ev));
    root.addEventListener("pointerdown", (ev) => this._onPointerDown(ev));
    root.addEventListener("contextmenu", (ev) => {
      if (ev.target.closest && ev.target.closest(".confirm")) ev.preventDefault();
    });
    this._onPointerEnd = () => this._endHold();
    this._talk = new VimarTalkBack(this._els.talk, {
      toast: (message) => this._toast(message),
      announce: (text) => this._announce(text),
      streamEl: () => this._streamEl,
    });
  }

  /** Replace a section's DOM only when its markup changed; keep focus. */
  _section(name, html) {
    const el = this._els[name];
    if (this._last[name] === html) return;
    this._last[name] = html;
    const active = this.shadowRoot.activeElement;
    const key = active && el.contains(active) ? active.dataset.key : null;
    el.innerHTML = html;
    el.hidden = !html;
    if (key) {
      const again = [...el.querySelectorAll("[data-key]")].find((n) => n.dataset.key === key);
      if (again) again.focus();
    }
  }

  _computeWatchKey(hass) {
    const model = this._model;
    if (!model || model.error || hass.entities !== this._entitiesRef || hass.devices !== this._devicesRef) {
      return null;
    }
    const lang = language(hass);
    return lang + "|" + model.watched.map((id) => {
      const s = hass.states[id];
      return s ? `${s.state}@${s.last_updated}` : "-";
    }).join("|");
  }

  // ── state ──

  _state(entityId) {
    return entityId && this._hass ? this._hass.states[entityId] : undefined;
  }

  _inCall() {
    const s = this._model && this._state(this._model.inCall);
    return Boolean(s && s.state === "on");
  }

  _registered() {
    const s = this._model && this._state(this._model.registration);
    if (!s) return null;
    if (s.state === "on") return true;
    if (s.state === "off") return false;
    return null;
  }

  _panelName(ext) {
    if (ext == null) return null;
    const panel = this._model && this._model.panelByExt.get(String(ext));
    return panel ? panel.name : String(ext);
  }

  _ringActive() {
    const r = this._ring;
    return Boolean(r && !r.dismissed && Date.now() - r.at < RING_WINDOW_MS);
  }

  _reset() {
    this._gen += 1;
    this._unmount();
    this._view = "idle";
    this._origin = null;
    this._activePanel = null;
    this._selected = null;
    this._prevInCall = undefined;
    this._ringId = undefined;
    this._ring = null;
    this._last = {};
  }

  _syncRing() {
    const s = this._state(this._model.doorbell);
    const id = s && s.attributes.event_type === "ring" && !Number.isNaN(Date.parse(s.state)) ? s.state : null;
    if (id === this._ringId) return;
    const first = this._ringId === undefined;
    this._ringId = id;
    clearTimeout(this._timers.ring);
    if (!id) {
      this._ring = null;
      return;
    }
    // On the first look the event's own timestamp is all there is; after
    // that, the moment the card saw it change, which no clock skew between
    // browser and server can put in the past.
    const at = first ? Date.parse(id) : Date.now();
    this._ring = {
      panel: s.attributes.panel != null ? String(s.attributes.panel) : null,
      at,
      dismissed: false,
      duringCall: this._inCall(),
    };
    const left = RING_WINDOW_MS - (Date.now() - at);
    if (left > 0) {
      this._timers.ring = setTimeout(() => this._render(), left + 50);
      if (!first && !this._ring.duringCall) {
        this._announce(t(this._hass, "ringing", { panel: this._panelName(this._ring.panel) || "" }));
      }
    }
  }

  _syncCall() {
    const inCall = this._inCall();
    const was = this._prevInCall;
    this._prevInCall = inCall;
    if (inCall && this._view !== "idle") this._sawCall = true;
    if (was === undefined) return;
    if (inCall && !was) {
      // Answered - here, on the indoor unit or on a phone. Either way the
      // ring is over.
      if (this._ring && !this._ring.duringCall) this._ring.dismissed = true;
      // One still per call, once the first keyframe has had time to come.
      clearTimeout(this._timers.poster);
      this._timers.poster = setTimeout(() => this._refreshPoster(), 4000);
      if (this._view === "connecting" && this._streamEl) this._setView("live");
    } else if (!inCall && was) {
      if (this._view !== "idle" && this._sawCall) {
        // The call ended; the stream it fed ends with it.
        this._gen += 1;
        this._unmount();
        this._view = "idle";
        this._origin = null;
      }
      this._activePanel = null;
    }
  }

  // ── render ──

  _render() {
    if (!this._hass || !this._config) return;
    const hass = this._hass;
    const model = resolveModel(hass, this._config);
    this._model = model;
    this._entitiesRef = hass.entities;
    this._devicesRef = hass.devices;
    this._watchKey = this._computeWatchKey(hass);

    this._section("header", this._config.title ? esc(this._config.title) : "");
    if (model.error) {
      this._section("message", `${icon("mdi:alert-outline")}<span>${esc(t(hass, model.error))}</span>`);
      for (const name of ["chips", "call", "doors", "actuators", "footer"]) this._section(name, "");
      this._els.video.hidden = true;
      this._talk.update(hass, false);
      return;
    }
    this._section("message", "");

    this._syncRing();
    this._syncCall();
    this._confirmables = new Map();

    this._renderVideo();
    this._section("chips", this._chipsHtml());
    this._section("call", this._callHtml());
    // Talk-back while a call is being watched here; see VimarTalkBack.
    this._talk.update(hass, this._inCall() && this._view === "live");
    this._section("doors", this._doorsHtml());
    this._section("actuators", this._config.show_actuators === false ? "" : this._actuatorsHtml());
    this._section("footer", this._footerHtml());
    if (this._streamEl) this._streamEl.hass = hass;
    this._syncTicker();
  }

  _renderVideo() {
    const hass = this._hass;
    const model = this._model;
    const video = this._els.video;
    video.hidden = !model.camera;
    if (!model.camera) return;

    const cam = this._state(model.camera);
    const src = cam && cam.attributes.entity_picture;
    if (src && src !== this._posterSrc) {
      this._posterSrc = src;
      this._refreshPoster();
    }
    this._els.poster.hidden = !(this._posterOk && this._view === "idle");

    const registered = this._registered();
    const inCall = this._inCall();
    const ringing = this._ringActive() && !inCall;
    let html = "";
    if (this._view === "idle") {
      if (!cam || cam.state === "unavailable") {
        html = `<div class="center"><div class="note">${esc(t(hass, "unavailable"))}</div></div>`;
      } else if (ringing) {
        html = `<div class="center">${icon("mdi:doorbell-video")}<div class="note">${esc(t(hass, "video_on_answer"))}</div></div>`;
      } else if (registered === false && !inCall) {
        html = `<div class="center">${icon("mdi:lan-disconnect")}<div class="note">${esc(t(hass, "offline_video"))}</div></div>`;
      } else {
        const panel = this._selected || model.defaultPanel;
        const label = inCall
          ? t(hass, "watch_call")
          : panel
            ? t(hass, "watch", { panel: this._panelName(panel) })
            : t(hass, "watch_default");
        html = `<div class="center">
          <button class="play" data-action="watch" data-key="watch" aria-label="${esc(label)}">
            <span class="disc">${icon("mdi:play")}</span><span>${esc(label)}</span>
          </button></div>`;
      }
    } else {
      const stop = `<button class="corner" data-action="stop" data-key="stop" aria-label="${esc(t(hass, "stop"))}" title="${esc(t(hass, "stop"))}">${icon("mdi:stop")}</button>`;
      const panelName = this._panelName(this._activePanel);
      if (this._view === "connecting") {
        const text = this._origin === "call" && panelName
          ? t(hass, "calling", { panel: panelName })
          : t(hass, "connecting");
        html = `<div class="center"><div class="spinner" role="progressbar" aria-label="${esc(text)}"></div><div class="note">${esc(text)}</div></div>${stop}`;
      } else {
        const live = panelName ? `${t(hass, "live")} · ${panelName}` : t(hass, "live");
        html = `<div class="badge"><span class="dot"></span><span>${esc(live)}</span></div>${stop}`;
      }
    }
    this._section("overlay", html);
  }

  _chipsHtml() {
    const hass = this._hass;
    const model = this._model;
    if (!model.camera || model.panels.length < 2) return "";
    const selected = this._activePanel || this._selected || model.defaultPanel;
    const offline = this._registered() === false;
    const chips = model.panels.map((p) => {
      const pressed = p.ext === selected ? "true" : "false";
      const label = t(hass, "show_panel", { panel: p.name });
      return `<button class="chip" data-action="panel" data-panel="${esc(p.ext)}" data-key="panel-${esc(p.ext)}"
        aria-pressed="${pressed}" aria-label="${esc(label)}" ${offline ? "disabled" : ""}>
        ${icon("mdi:doorbell-video")}<span>${esc(p.name)}</span></button>`;
    }).join("");
    return `<div class="chips" role="group" aria-label="${esc(t(hass, "panels"))}">${chips}</div>`;
  }

  /** A confirm-to-run button (a door release), as a round action or a pill. */
  _confirmButton(key, spec, style) {
    const hass = this._hass;
    this._confirmables.set(key, spec);
    const armed = this._armed.has(key);
    const opened = this._feedback.has(key);
    const pending = this._pending.has(key);
    const cls = armed ? "armed" : opened ? "opened" : "";
    const text = opened ? t(hass, "opened") : pending ? t(hass, "opening") : armed ? t(hass, "tap_again") : spec.label;
    const glyph = opened ? "mdi:check" : spec.icon || "mdi:door-open";
    const aria = armed ? t(hass, "tap_again_for", { name: spec.name }) : `${spec.label}. ${t(hass, "confirm_hint")}`;
    const common = `data-action="confirm" data-key="${esc(key)}" aria-label="${esc(aria)}" ${this._armedStyle(key)} ${pending ? "disabled" : ""}`;
    if (style === "round") {
      return `<button class="action confirm ${cls}" ${common}>
        <span class="disc neutral">${icon(glyph)}</span><span>${esc(text)}</span></button>`;
    }
    return `<button class="pill neutral confirm ${cls}" ${common}>${icon(glyph)}<span>${esc(text)}</span></button>`;
  }

  _openSpecFor(ext) {
    const panel = ext != null && this._model.panelByExt.get(String(ext));
    if (!panel || !panel.open) return null;
    return {
      domain: "button",
      service: "press",
      entity: panel.open,
      label: t(this._hass, "open_door"),
      name: panel.name,
      icon: "mdi:door-open",
    };
  }

  _callHtml() {
    const hass = this._hass;
    const model = this._model;
    const inCall = this._inCall();
    const ring = this._ring;

    if (this._ringActive() && !inCall) {
      const name = this._panelName(ring.panel);
      const title = name ? t(hass, "ringing", { panel: name }) : t(hass, "ringing_generic");
      const open = this._openSpecFor(ring.panel);
      const answer = model.answer
        ? `<button class="action" data-action="answer" data-key="answer" aria-label="${esc(t(hass, "answer"))}" ${this._answering ? "disabled" : ""}>
             <span class="disc success">${icon("mdi:phone")}</span><span>${esc(t(hass, "answer"))}</span></button>`
        : "";
      return `<div class="ring" role="alert">
        <div class="ring-head">
          <span class="bell">${icon("mdi:bell-ring")}</span>
          <div><div class="ring-title">${esc(title)}</div><div class="ring-sub">${esc(t(hass, "ringing_hint"))}</div></div>
        </div>
        <div class="actions">
          <button class="action" data-action="dismiss" data-key="dismiss" aria-label="${esc(t(hass, "dismiss"))}">
            <span class="disc danger">${icon("mdi:close")}</span><span>${esc(t(hass, "dismiss"))}</span></button>
          ${open ? this._confirmButton(`open:${open.entity}`, open, "round") : ""}
          ${answer}
        </div></div>`;
    }

    if (inCall) {
      const name = this._panelName(this._activePanel);
      const title = name ? t(hass, "in_call_with", { panel: name }) : t(hass, "in_call");
      const open = this._openSpecFor(this._activePanel);
      const busyRing = this._ringActive() && ring.duringCall
        ? `<div class="incall-note">${esc(t(hass, "rang_during_call", { panel: this._panelName(ring.panel) || "" }))}</div>`
        : "";
      const hangup = model.hangup
        ? `<button class="pill danger" data-action="hangup" data-key="hangup" aria-label="${esc(t(hass, "hang_up"))}">${icon("mdi:phone-hangup")}<span>${esc(t(hass, "hang_up"))}</span></button>`
        : "";
      return `<div class="incall">
        <div class="incall-text">
          <div class="incall-title"><span class="dot live"></span><span>${esc(title)}</span></div>
          <div class="timer"></div>${busyRing}
        </div>
        <div class="incall-actions">${open ? this._confirmButton(`open:${open.entity}`, open, "pill") : ""}${hangup}</div>
      </div>`;
    }
    return "";
  }

  _doorsHtml() {
    const hass = this._hass;
    const doors = this._model.doors;
    if (!doors.length) return "";
    const buttons = doors.map((door) => {
      const spec = {
        domain: "lock",
        service: "unlock",
        entity: door.id,
        label: t(hass, "open_named", { name: door.name }),
        name: door.name,
        icon: door.icon || "mdi:door-open",
      };
      const key = door.id;
      this._confirmables.set(key, spec);
      const s = this._state(door.id);
      const unavailable = !s || s.state === "unavailable";
      const openNow = s && OPEN_LOCK_STATES.has(s.state);
      const armed = this._armed.has(key);
      const opened = this._feedback.has(key) || openNow;
      const pending = this._pending.has(key);
      const cls = armed ? "armed" : opened ? "opened" : "";
      const hint = opened
        ? t(hass, "opened")
        : pending
          ? t(hass, "opening")
          : armed
            ? t(hass, "tap_again")
            : t(hass, "confirm_hint");
      const aria = armed ? t(hass, "tap_again_for", { name: door.name }) : `${spec.label}. ${t(hass, "confirm_hint")}`;
      return `<button class="door confirm ${cls}" data-action="confirm" data-key="${esc(key)}"
          aria-label="${esc(aria)}" ${this._armedStyle(key)} ${unavailable || pending ? "disabled" : ""}>
          <span class="glyph">${icon(opened ? "mdi:check" : spec.icon)}</span>
          <span class="text"><span class="label">${esc(spec.label)}</span><span class="hint">${esc(unavailable ? t(hass, "unavailable") : hint)}</span></span>
        </button>`;
    }).join("");
    return `<div class="section"><h3 class="section-title">${esc(t(hass, "doors"))}</h3><div class="grid">${buttons}</div></div>`;
  }

  _actuatorsHtml() {
    const hass = this._hass;
    const acts = this._model.actuators;
    if (!acts.length) return "";
    const buttons = acts.map((a) => {
      const s = this._state(a.id);
      const unavailable = !s || s.state === "unavailable";
      const done = this._feedback.has(a.id);
      const pending = this._pending.has(a.id);
      return `<button class="act ${done ? "done" : ""}" data-action="actuator" data-entity="${esc(a.id)}" data-key="${esc(a.id)}"
          aria-label="${esc(done ? `${a.name}: ${t(hass, "done")}` : a.name)}" ${unavailable || pending ? "disabled" : ""}>
          ${icon(done ? "mdi:check" : a.icon)}<span>${esc(a.name)}</span></button>`;
    }).join("");
    return `<div class="section"><h3 class="section-title">${esc(t(hass, "controls"))}</h3><div class="acts">${buttons}</div></div>`;
  }

  _footerHtml() {
    const hass = this._hass;
    const model = this._model;
    if (!model.registration) return "";
    const registered = this._registered();
    const dot = registered === true ? "ok" : registered === false ? "bad" : "";
    const text = registered === true ? t(hass, "connected") : registered === false ? t(hass, "disconnected") : t(hass, "status_unknown");
    let action = "";
    if (registered !== true && model.reconnect) {
      const busy = this._feedback.has("reconnect") || this._pending.has("reconnect");
      const label = busy ? t(hass, "reconnecting") : t(hass, "reconnect");
      action = `<button class="link" data-action="reconnect" data-key="reconnect" aria-label="${esc(label)}" ${busy ? "disabled" : ""}>${icon("mdi:restart")}<span>${esc(label)}</span></button>`;
    }
    return `<span class="dot ${dot}"></span><span class="grow">${esc(text)}</span>${action}`;
  }

  /**
   * Fetch a still for the idle screen. The camera only has one while a
   * call is up, and a failed fetch keeps the last good picture: "who was
   * at the door" is still worth showing after the call.
   */
  _refreshPoster() {
    const src = this._posterSrc;
    if (!src) return;
    const img = new Image();
    img.onload = () => {
      this._els.poster.src = img.src;
      this._posterOk = true;
      this._els.poster.hidden = this._view !== "idle";
    };
    img.src = `${src}${src.includes("?") ? "&" : "?"}_=${Date.now()}`;
  }

  // ── call duration ──

  _syncTicker() {
    if (this._inCall() && this.isConnected) {
      if (!this._timers.tick) this._timers.tick = setInterval(() => this._tick(), 1000);
      this._tick();
    } else {
      this._stopTicker();
    }
  }

  _stopTicker() {
    clearInterval(this._timers.tick);
    this._timers.tick = null;
  }

  _tick() {
    const el = this._els.call.querySelector(".timer");
    const s = this._model && this._state(this._model.inCall);
    if (!el || !s || s.state !== "on") return;
    el.textContent = formatDuration(Date.now() - Date.parse(s.last_changed));
  }

  // ── input ──

  _onClick(ev) {
    const el = ev.target.closest && ev.target.closest("[data-action]");
    if (!el || el.disabled || !this.shadowRoot.contains(el)) return;
    const action = el.dataset.action;
    switch (action) {
      case "watch":
        this._watch(null);
        break;
      case "panel":
        this._watch(el.dataset.panel);
        break;
      case "stop":
        this._stop();
        break;
      case "answer":
        this._answer();
        break;
      case "dismiss":
        if (this._ring) this._ring.dismissed = true;
        this._render();
        break;
      case "hangup":
        this._hangUp();
        break;
      case "confirm":
        if (this._holdFired === el.dataset.key) {
          // The hold already opened it; this is the click that ends it.
          this._holdFired = null;
          break;
        }
        this._confirmTap(el.dataset.key);
        break;
      case "actuator":
        this._press(el.dataset.entity);
        break;
      case "reconnect":
        this._reconnect();
        break;
      default:
        break;
    }
  }

  _onPointerDown(ev) {
    if (ev.button !== undefined && ev.button !== 0) return;
    const el = ev.target.closest && ev.target.closest('[data-action="confirm"]');
    if (!el || el.disabled) return;
    const key = el.dataset.key;
    if (this._feedback.has(key) || this._pending.has(key)) return;
    this._endHold();
    el.classList.add("holding");
    const timer = setTimeout(() => {
      el.classList.remove("holding");
      this._hold = null;
      this._holdFired = key;
      this._disarm(key);
      this._runConfirmed(key);
    }, HOLD_MS);
    this._hold = { el, timer };
    window.addEventListener("pointerup", this._onPointerEnd, true);
    window.addEventListener("pointercancel", this._onPointerEnd, true);
  }

  _endHold() {
    window.removeEventListener("pointerup", this._onPointerEnd, true);
    window.removeEventListener("pointercancel", this._onPointerEnd, true);
    if (!this._hold) return;
    clearTimeout(this._hold.timer);
    this._hold.el.classList.remove("holding");
    this._hold = null;
  }

  _confirmTap(key) {
    const spec = this._confirmables.get(key);
    if (!spec || this._feedback.has(key) || this._pending.has(key)) return;
    if (this._armed.has(key)) {
      this._disarm(key);
      this._runConfirmed(key);
      return;
    }
    const timer = setTimeout(() => {
      this._armed.delete(key);
      this._render();
    }, CONFIRM_WINDOW_MS);
    this._armed.set(key, { timer, at: Date.now() });
    this._announce(t(this._hass, "tap_again_for", { name: spec.name }));
    this._render();
  }

  _disarm(key) {
    const armed = this._armed.get(key);
    if (armed) clearTimeout(armed.timer);
    this._armed.delete(key);
  }

  /**
   * Keeps the countdown bar on the real deadline when a re-render
   * replaces the button halfway through it.
   */
  _armedStyle(key) {
    const armed = this._armed.get(key);
    return armed ? `style="--vic-armed-elapsed: -${Date.now() - armed.at}ms"` : "";
  }

  async _runConfirmed(key) {
    const spec = this._confirmables.get(key);
    if (!spec || this._pending.has(key)) return;
    this._pending.add(key);
    this._render();
    const ok = await this._callService(spec.domain, spec.service, spec.entity, spec.label);
    this._pending.delete(key);
    if (ok) {
      this._flash(key, FEEDBACK_MS);
      this._announce(t(this._hass, "opened_named", { name: spec.name }));
    }
    this._render();
  }

  async _press(entityId) {
    if (!entityId || this._pending.has(entityId)) return;
    const name = entityName(this._hass, entityId, deviceName(this._hass, this._model.deviceId));
    this._pending.add(entityId);
    this._render();
    const ok = await this._callService("button", "press", entityId, name);
    this._pending.delete(entityId);
    if (ok) this._flash(entityId, 1500);
    this._render();
  }

  async _reconnect() {
    const id = this._model.reconnect;
    if (!id || this._pending.has("reconnect")) return;
    this._pending.add("reconnect");
    this._render();
    const ok = await this._callService("button", "press", id, t(this._hass, "reconnect"));
    this._pending.delete("reconnect");
    // Registration takes a few seconds; keep the button quiet meanwhile.
    if (ok) this._flash("reconnect", 8000);
    this._render();
  }

  _flash(key, ms) {
    clearTimeout(this._feedback.get(key));
    this._feedback.set(key, setTimeout(() => {
      this._feedback.delete(key);
      this._render();
    }, ms));
  }

  _announce(text) {
    const sr = this._els.sr;
    sr.textContent = "";
    // A change the screen reader notices even when the text repeats.
    setTimeout(() => {
      sr.textContent = text;
    }, 50);
  }

  _toast(message) {
    fire(this, "hass-notification", { message });
  }

  async _callService(domain, service, entityId, label) {
    try {
      // notifyOnError false: the card shows its own, labelled, toast.
      await this._hass.callService(domain, service, { entity_id: entityId }, undefined, false);
      return true;
    } catch (err) {
      const text = errorText(err);
      this._toast(label ? `${label}: ${text}` : text);
      return false;
    }
  }

  _waitFor(predicate, ms) {
    if (predicate()) return Promise.resolve(true);
    return new Promise((resolve) => {
      const waiter = { predicate, resolve };
      waiter.timer = setTimeout(() => {
        this._waiters.delete(waiter);
        resolve(false);
      }, ms);
      this._waiters.add(waiter);
    });
  }

  _checkWaiters() {
    for (const waiter of [...this._waiters]) {
      if (waiter.predicate()) {
        clearTimeout(waiter.timer);
        this._waiters.delete(waiter);
        waiter.resolve(true);
      }
    }
  }

  // ── video and calls ──

  _setView(view) {
    this._view = view;
    clearTimeout(this._timers.connecting);
    if (view === "connecting") {
      this._timers.connecting = setTimeout(() => {
        if (this._view === "connecting" && this._streamEl) this._setView("live");
      }, CONNECTING_MAX_MS);
    }
    this._render();
  }

  /**
   * Show a panel. With no panel given (the play button) that is the call
   * already up, or the selected panel. The default panel needs no call
   * button: opening the stream calls it (see the camera entity). Any other
   * panel is called first, and the stream opened once the call is up.
   */
  async _watch(ext) {
    const model = this._model;
    if (!model || !model.camera) return;
    if (ext != null && ext === this._activePanel && this._view === "connecting") return;
    const gen = ++this._gen;
    const inCall = this._inCall();
    if (ext != null) this._selected = ext;
    const target = ext != null ? ext : this._selected || model.defaultPanel;

    if (inCall) {
      if (ext == null || ext === this._activePanel) {
        if (!this._origin) this._origin = "join";
        if (this._view === "idle") this._mount("live");
        return;
      }
      // Another panel: this line holds one call at a time. The call about
      // to end is not the one this stream will show, so its ending must
      // not reset the card (see `_syncCall`).
      this._unmount();
      this._sawCall = false;
      this._setView("connecting");
      await this._callService("button", "press", model.hangup, t(this._hass, "hang_up"));
      await this._waitFor(() => !this._inCall(), HANGUP_WAIT_MS);
      if (gen !== this._gen) return;
    }

    const panel = model.panelByExt.get(String(target));
    this._activePanel = target;
    if (target === model.defaultPanel || !panel || !panel.call) {
      this._origin = "watch";
      this._mount("connecting");
      return;
    }

    this._origin = "call";
    this._setView("connecting");
    const ok = await this._callService("button", "press", panel.call, t(this._hass, "show_panel", { panel: panel.name }));
    if (gen !== this._gen) return;
    if (!ok) {
      this._toIdle();
      return;
    }
    const up = await this._waitFor(() => this._inCall(), CALL_SETUP_MS);
    if (gen !== this._gen) return;
    if (!up) {
      this._toast(t(this._hass, "call_failed", { panel: panel.name }));
      // Drop the attempt, so it cannot connect minutes later unseen.
      this._callService("button", "press", model.hangup, t(this._hass, "hang_up"));
      this._toIdle();
      return;
    }
    this._mount("live");
  }

  async _answer() {
    const model = this._model;
    if (!model.answer || this._answering) return;
    const gen = ++this._gen;
    const ring = this._ring;
    this._answering = true;
    this._render();
    const ok = await this._callService("button", "press", model.answer, t(this._hass, "answer"));
    this._answering = false;
    if (gen !== this._gen) return;
    if (!ok) {
      this._render();
      return;
    }
    if (ring) ring.dismissed = true;
    this._origin = "answer";
    this._activePanel = ring ? ring.panel : null;
    if (this._activePanel && model.panelByExt.has(this._activePanel)) this._selected = this._activePanel;
    if (!model.camera) {
      this._render();
      return;
    }
    this._setView("connecting");
    const up = await this._waitFor(() => this._inCall(), CALL_SETUP_MS);
    if (gen !== this._gen) return;
    if (!up) {
      this._toast(t(this._hass, "call_failed_generic"));
      this._toIdle();
      return;
    }
    this._mount("live");
  }

  async _mount(view) {
    const model = this._model;
    const gen = this._gen;
    this._sawCall = this._inCall();
    this._setView(view);
    const ok = await ensureCameraStream(model.camera);
    if (gen !== this._gen) return;
    if (!ok) {
      this._toast(t(this._hass, "stream_unavailable"));
      this._toIdle();
      return;
    }
    if (!this._streamEl) {
      const el = document.createElement("ha-camera-stream");
      el.muted = true;
      el.controls = true;
      el.allowExoPlayer = true;
      el.setAttribute("muted", "");
      el.setAttribute("controls", "");
      el.setAttribute("allow-exoplayer", "");
      this._streamEl = el;
    }
    this._streamEl.hass = this._hass;
    this._streamEl.stateObj = this._state(model.camera);
    if (!this._streamEl.isConnected) this._els.stream.appendChild(this._streamEl);
    // The call may have come up while the element loaded.
    if (this._view === "connecting" && this._inCall()) this._setView("live");
  }

  _unmount() {
    clearTimeout(this._timers.connecting);
    if (this._streamEl) {
      this._streamEl.remove();
      this._streamEl = null;
    }
  }

  _toIdle() {
    this._unmount();
    this._origin = null;
    this._setView("idle");
  }

  /** Stop watching. A call this card placed or answered ends with it. */
  _stop() {
    this._gen += 1;
    const origin = this._origin;
    const connecting = this._view === "connecting";
    this._toIdle();
    if ((origin === "call" || origin === "answer") && (this._inCall() || connecting) && this._model.hangup) {
      this._callService("button", "press", this._model.hangup, t(this._hass, "hang_up"));
    }
  }

  _hangUp() {
    this._gen += 1;
    this._toIdle();
    if (this._model && this._model.hangup) {
      this._callService("button", "press", this._model.hangup, t(this._hass, "hang_up"));
    }
  }
}

// ─── the visual editor ───────────────────────────────────────────────

class VimarIntercomCardEditor extends HTMLElement {
  constructor() {
    super();
    this._config = {};
    this._hass = null;
    this._form = null;
    this._loading = false;
  }

  setConfig(config) {
    this._config = { ...config };
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  _schema() {
    return [
      { name: "device_id", selector: { device: { filter: { integration: DOMAIN } } } },
      { name: "title", selector: { text: {} } },
      { name: "show_actuators", selector: { boolean: {} } },
      {
        name: "hidden_entities",
        selector: {
          entity: {
            multiple: true,
            include_entities: hideableEntities(this._hass, this._config),
          },
        },
      },
    ];
  }

  async _render() {
    if (!this._hass) return;
    if (!customElements.get("ha-form")) {
      if (this._loading) return;
      this._loading = true;
      const ok = await ensureHaForm();
      this._loading = false;
      if (!ok) {
        this.textContent = t(this._hass, "editor_unavailable");
        return;
      }
    }
    if (!this._form) {
      this._form = document.createElement("ha-form");
      this._form.addEventListener("value-changed", (ev) => this._changed(ev));
      this.textContent = "";
      this.appendChild(this._form);
    }
    const hass = this._hass;
    this._form.hass = hass;
    this._form.data = {
      show_actuators: true,
      ...this._config,
      device_id: this._config.device_id || intercomDevices(hass)[0],
    };
    this._form.schema = this._schema();
    this._form.computeLabel = (schema) => t(hass, `editor_${schema.name}`);
    this._form.computeHelper = (schema) =>
      schema.name === "hidden_entities" ? t(hass, "editor_hidden_help") : undefined;
  }

  _changed(ev) {
    ev.stopPropagation();
    const config = { ...this._config, ...ev.detail.value };
    if (!config.title) delete config.title;
    if (config.show_actuators !== false) delete config.show_actuators;
    if (!config.hidden_entities || !config.hidden_entities.length) delete config.hidden_entities;
    if (!config.device_id) delete config.device_id;
    this._config = config;
    fire(this, "config-changed", { config });
  }
}

// ─── registration ────────────────────────────────────────────────────

// Loaded once by the integration; a manual resource entry left over from
// testing must not make the second copy throw.
if (!customElements.get(CARD_TYPE)) customElements.define(CARD_TYPE, VimarIntercomCard);
if (!customElements.get(EDITOR_TYPE)) customElements.define(EDITOR_TYPE, VimarIntercomCardEditor);

window.customCards = window.customCards || [];
if (!window.customCards.some((card) => card.type === CARD_TYPE)) {
  const hass = document.querySelector("home-assistant")?.hass;
  window.customCards.push({
    type: CARD_TYPE,
    name: t(hass, "card_name"),
    description: t(hass, "card_description"),
    preview: true,
    documentationURL:
      "https://github.com/noiseheroes-lab/ha-custom-components/blob/main/custom_components/vimar_intercom/README.md#dashboard-card",
  });
}
