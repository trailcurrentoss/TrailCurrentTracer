// Tiny signal store + daemon connection. No framework.
//
// Everything rendered by the GUI is owned by the daemon. There is no
// client-side cache that could outlive the connection — a field tool showing
// stale numbers is worse than one showing none.

const listeners = new Set();

export const state = {
  connected: false,
  hello: null,
  screen: "boot",       // boot | launcher | <appId>
  focus: 0,             // launcher tile index
  row: 0,               // cursor row inside an app
  apps: [],
  modules: {},          // name -> snapshot
  toast: "",
  bootPct: 0,
  bootLine: "starting tracerd",
};

export function subscribe(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

let queued = false;
export function update(patch) {
  Object.assign(state, patch);
  // Coalesce renders to one per frame. Button repeat at 8 Hz plus module
  // snapshots at 10 Hz would otherwise cause redundant full re-renders.
  if (!queued) {
    queued = true;
    requestAnimationFrame(() => {
      queued = false;
      for (const fn of listeners) fn(state);
    });
  }
}

let toastTimer = null;
export function toast(text, ms = 1700) {
  update({ toast: text });
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => update({ toast: "" }), ms);
}

// ── daemon connection ────────────────────────────────────────────────
const BASE = `${location.hostname || "127.0.0.1"}:${location.port || 8710}`;

let ws = null;
let backoff = 250;
const buttonHandlers = new Set();
const textHandlers = new Set();
const textKeyHandlers = new Set();
const powerKeyHandlers = new Set();

export function onButton(fn) {
  buttonHandlers.add(fn);
  return () => buttonHandlers.delete(fn);
}

// Characters, emitted only while tracerd is in text mode.
export function onText(fn) {
  textHandlers.add(fn);
  return () => textHandlers.delete(fn);
}

// Named editing keys in text mode (backspace, enter).
export function onTextKey(fn) {
  textKeyHandlers.add(fn);
  return () => textKeyHandlers.delete(fn);
}

// The physical power button. This is NOT one of the PocketTerm35 buttons —
// the daemon watches the Pi's own gpio-keys device and forwards a short press
// here, because the image sets HandlePowerKey=ignore so logind no longer
// powers the unit off the instant the button is brushed in a bag. Holding the
// button still forces a poweroff through logind, independently of this.
export function onPowerKey(fn) {
  powerKeyHandlers.add(fn);
  return () => powerKeyHandlers.delete(fn);
}

export function connect() {
  try {
    ws = new WebSocket(`ws://${BASE}/stream`);
  } catch {
    return scheduleReconnect();
  }

  ws.onopen = () => {
    backoff = 250;
    update({ connected: true });
  };

  ws.onmessage = (e) => {
    let f;
    try { f = JSON.parse(e.data); } catch { return; }
    handleFrame(f);
  };

  ws.onclose = () => {
    update({ connected: false });
    scheduleReconnect();
  };

  ws.onerror = () => { try { ws.close(); } catch {} };
}

function scheduleReconnect() {
  // 250 ms -> 8 s with jitter, so a daemon restart doesn't get hammered.
  const jitter = backoff * (0.8 + Math.random() * 0.4);
  setTimeout(connect, jitter);
  backoff = Math.min(backoff * 2, 8000);
}

function handleFrame(f) {
  switch (f.t) {
    case "hello":
      update({ hello: f, apps: f.apps || [] });
      break;
    case "apps":
      // Tile statuses follow module state; without this the launcher shows
      // whatever was true at connect time, forever.
      update({ apps: f.d || [] });
      break;
    case "snap": {
      const modules = { ...state.modules, [f.m]: f.d };
      update({ modules });
      break;
    }
    case "ev":
      if (f.m === "terminal") {
        // The pty pushes as it produces output; merge into the module snapshot
        // so the screen renders from one place.
        const cur = state.modules.terminal || {};
        update({ modules: { ...state.modules,
          terminal: { ...cur, data: { ...(cur.data || {}), ...f.d } } } });
        break;
      }
      if (f.m === "input") {
        if (f.d.btn) for (const fn of buttonHandlers) fn(f.d.btn, f.d.phase);
        else if (f.d.text) for (const fn of textHandlers) fn(f.d.text);
        else if (f.d.key) for (const fn of textKeyHandlers) fn(f.d.key);
        break;
      }
      if (f.m === "power" && f.d.ev === "confirm_shutdown") {
        for (const fn of powerKeyHandlers) fn();
      }
      break;
    case "toast":
      toast(f.text);
      break;
  }
}

export async function rpc(m, op, args = {}) {
  try {
    const res = await fetch(`http://${BASE}/rpc`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: String(Date.now()), m, op, args }),
    });
    return await res.json();
  } catch (err) {
    return { ok: false, err: { code: "transport", msg: String(err) } };
  }
}
