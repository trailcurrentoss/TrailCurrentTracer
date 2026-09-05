// Tracer GUI entry point. Pure ES modules — no build step, no framework.

import { state, update, subscribe, connect, onButton, onText, onTextKey, onPowerKey, rpc, toast } from "./store/store.js";
import { statusBar, hintBar, toastEl, offlineScreen } from "./chrome/chrome.js";
import { applyScroll, resetScroll } from "./chrome/scroll.js";
import { launcher, launcherMove } from "./apps/launcher.js";
import { bootScreen, bootProgress } from "./apps/boot.js";
import { networkScreen, networkRows } from "./apps/network.js";
import { mqttScreen, mqttRows, mqttHints } from "./apps/mqtt.js";
import { discoveryScreen, discoveryRows, discoveryHints } from "./apps/discovery.js";
import { logsScreen, logsRows, logsHints, logsSources } from "./apps/logs.js";
import { captureScreen, captureRows, captureHints, captureUi } from "./apps/capture.js";
import { firmwareScreen, firmwareRows, firmwareHints } from "./apps/firmware.js";
import { terminalScreen, terminalHints, termScroll, termFollow } from "./apps/terminal.js";
import { gnssScreen, gnssHints, ensureMap, mountMap, mapZoom, mapTeardown, positionMapHost } from "./apps/gnss.js";
import { simulateScreen, simulateRows, simulateHints, simUi, simReset } from "./apps/simulate.js";
import { wifi, wifiScreen, wifiPress, wifiScan, wifiText, wifiTextKey } from "./apps/wifi-setup.js";
import { settingsUi, settingsScreen, settingsHints, settingsPress, settingsText, settingsTextKey, bindSettings, reconcileSettings, settingsScrollRow } from "./apps/settings.js";
import { headwatersScreen, headwatersHints, headwatersRows }
  from "./apps/headwaters.js";
import { moduleDebugScreen, moduleDebugHints, moduleDebugRows, dbgUi }
  from "./apps/moduledebug.js";
import { icon } from "./icons.js";

const root = document.getElementById("app");

// ── dev-only keyboard bindings ───────────────────────────────────────
// NEVER enabled on device. On hardware every button arrives from tracerd's
// input module over the WebSocket; the GUI binds no raw keycodes.
const params = new URLSearchParams(location.search);
const DEV = params.has("dev");

// ?screen=<id> deep-links straight to a screen, mirroring the "Jump to screen"
// panel in the design mock. Needed for the screenshot-diff workflow: the boot
// screen would otherwise race every capture. Harmless in production — the
// daemon still owns every value, this only picks the starting view.
const INITIAL_SCREEN = params.get("screen");
// ?group=<n> opens a Settings group directly. Same rationale as ?screen= —
// the plan requires a screenshot of every screen diffed against the mock, and
// nested screens are otherwise unreachable from a cold page load.
const INITIAL_GROUP = params.get("group");
const INITIAL_ROW = params.get("row");

const DEV_KEYS = {
  ArrowUp: "dpad_up", ArrowDown: "dpad_down",
  ArrowLeft: "dpad_left", ArrowRight: "dpad_right",
  z: "a", x: "b", c: "x", v: "y",
  q: "l", e: "r",
  Enter: "start", Shift: "select",
};

if (DEV) {
  document.body.classList.add("dev-frame");
  window.addEventListener("keydown", (e) => {
    const btn = DEV_KEYS[e.key];
    if (!btn) return;
    e.preventDefault();
    press(btn, e.repeat ? "hold" : "down");
  });
}

// ── theme ────────────────────────────────────────────────────────────
// The daemon owns the value; the DOM just reflects it. Applied on every
// render so a theme change from Settings takes effect immediately without a
// reload.
// The panel has no kernel backlight device and no ddcutil, so brightness
// falls through to a compositing dim the GUI applies itself. It reduces
// emitted light and glare — which is the actual need at night beside a
// vehicle — but it does NOT save power, and the UI says "dim" rather than
// implying backlight control.
function applyBrightness(s) {
  const pw = s.modules.power;
  if (!pw || pw.state !== "ok" || !pw.data.brightness_software) {
    root.style.filter = "";
    return;
  }
  const pending = settingsUi.pending.brightness;
  const raw = pending !== undefined ? pending : pw.data.brightness;
  const pct = Math.max(5, Math.min(100, Number(raw ?? 100)));
  // Floor at 0.35 so the screen can never be dimmed to the point where the
  // brightness control itself is unreadable and unrecoverable.
  const f = 0.35 + 0.65 * (pct / 100);
  root.style.filter = `brightness(${f.toFixed(3)})`;
}

function applyTheme(s) {
  const st = s.modules.settings;
  const theme = settingsUi.pending.theme
    || (st && st.state === "ok" && st.data.values && st.data.values.theme)
    || "dark";
  if (document.documentElement.dataset.theme !== theme) {
    document.documentElement.dataset.theme = theme;
  }
}

// No cursor on the device, ever. The panel is a touchscreen and the operator
// has no mouse — a pointer arrow on it is a defect, not an affordance.
//
// This used to be a heuristic: listen for one mousemove, then reveal the
// cursor. It did the opposite of its intent on hardware. Chromium synthesises
// a compatibility mousemove for every touch, so the first tap latched the
// class on, and {once:true} meant there was no later event to take it off —
// the arrow then sat on the panel for the rest of the session. The RP2040
// also presents a Mouse HID collection, so even a pointerType filter would
// not reliably mean "someone is using a mouse".
//
// Hiding it is now unconditional and lives entirely in CSS (tokens.css).
// The one place a cursor is still wanted is ?dev, where the UI is driven
// with a real mouse over a tunnel — body.dev-frame re-enables it there.

// ── app screens not yet implemented ──────────────────────────────────
// Honest placeholder rather than a blank screen: says what it is, and B
// still returns to the launcher so the device never traps the operator.
function appStub(state) {
  const app = state.apps.find((a) => a.id === state.screen);
  const mod = state.modules[state.screen];
  const short = app ? app.short : state.screen;
  const st = mod ? mod.state : "unknown";
  const reason = mod && mod.reason ? mod.reason : "";

  const colour = st === "ok" ? "var(--tc-success)"
    : st === "degraded" ? "var(--tc-warning)"
    : "var(--fg-mute)";

  return `
  <div style="height:var(--body-h);padding:12px 14px;display:flex;flex-direction:column;">
    <div style="display:flex;align-items:center;gap:8px;">
      <div style="font-size:15px;font-weight:500;">${short}</div>
      <div style="flex:1"></div>
      <div style="font-size:11px;color:${colour};border:1px solid ${colour};
                  border-radius:var(--r-badge);padding:1px 8px;">${st}</div>
    </div>
    <div style="flex:1;display:flex;flex-direction:column;align-items:center;
                justify-content:center;gap:10px;color:var(--fg-mute);">
      <div style="opacity:.5">${icon(app ? app.icon : "settings", 40, "currentColor")}</div>
      <div style="font-size:13px;">Not implemented yet</div>
      ${reason ? `<div style="font-size:11px;font-family:var(--mono);">${reason}</div>` : ""}
      <div style="font-size:11px;">Press B to go back</div>
    </div>
  </div>`;
}

// ── render ───────────────────────────────────────────────────────────
function needsWifi(s) {
  // Gate on the daemon's own view of the association, never on a local flag.
  // `starting` is not "no wifi" — it is "not resolved yet", and gating on it
  // would flash the setup screen on every boot.
  const net = s.modules.net;
  return Boolean(net) && net.state === "unavailable";
}

// Which pane the Headwaters monitor has focused. The ROW cursor deliberately
// stays in state.row so applyScroll() and the universal touch handler need no
// special case for this screen.
const hwUi = { pane: "containers" };

function render(s) {
  // The monitor renders from this; keep it on the state object the screens see.
  s.hwUi = hwUi;
  reconcileSettings(s);
  applyTheme(s);
  applyBrightness(s);

  if (!s.connected && s.screen !== "boot") {
    root.innerHTML = offlineScreen();
    return;
  }

  if (s.screen === "boot") {
    root.innerHTML = bootScreen(s);
    return;
  }

  // Blocking: no network means no broker, no Headwaters, no discovery. The
  // launcher is not reachable until this is resolved.
  if (needsWifi(s)) {
    root.innerHTML = wifiScreen();
    bindWifiRows();
    return;
  }

  // WiFi picker opened from Settings — same component as the boot gate.
  if (wifiFromSettings) {
    root.innerHTML = wifiScreen();
    bindWifiRows();
    return;
  }

  let body;
  if (s.screen === "launcher") body = launcher(s);
  else if (s.screen === "net") body = networkScreen(s);
  else if (s.screen === "mqtt") body = mqttScreen(s);
  else if (s.screen === "discovery") body = discoveryScreen(s);
  else if (s.screen === "logs") body = logsScreen(s);
  else if (s.screen === "capture") body = captureScreen(s);
  else if (s.screen === "firmware") body = firmwareScreen(s);
  else if (s.screen === "terminal") body = terminalScreen(s);
  else if (s.screen === "gnss") body = gnssScreen(s);
  else if (s.screen === "headwaters") body = headwatersScreen(s);
  else if (s.screen === "moduledebug") body = moduleDebugScreen(s);
  else if (s.screen === "simulate") body = simulateScreen(s);
  else if (s.screen === "settings") body = settingsScreen(s);
  else body = appStub(s);

  let hints = null;
  if (s.screen === "settings") hints = settingsHints(s);
  else if (s.screen === "mqtt") hints = mqttHints(s);
  else if (s.screen === "discovery") hints = discoveryHints(s);
  else if (s.screen === "logs") hints = logsHints(s);
  else if (s.screen === "capture") hints = captureHints(s);
  else if (s.screen === "firmware") {
    const fd = (s.modules.firmware && s.modules.firmware.data) || {};
    hints = firmwareHints(s, fd.browser ? fwAtRoot(fd) : true);
  }
  else if (s.screen === "terminal") hints = terminalHints(s);
  else if (s.screen === "gnss") hints = gnssHints(s);
  else if (s.screen === "headwaters") hints = headwatersHints(s);
  else if (s.screen === "moduledebug") hints = moduleDebugHints(s);
  else if (s.screen === "simulate") hints = simulateHints(s);
  root.innerHTML = statusBar(s) + body + hintBar(s, hints) + toastEl(s)
                 + (prompt.open ? promptOverlay() : "")
                 + (confirmBox.open ? confirmOverlay() : "");

  if (s.screen === "launcher") bindTiles();
  if (s.screen === "settings") bindSettings(root, s, openWifiPicker);
  if (s.screen === "simulate") {
    const n = simulateRows(s);
    const mods = (s.modules.simulate && s.modules.simulate.data
                  && s.modules.simulate.data.modules) || [];
    if (btn === "dpad_up")   { simUi.row = Math.max(0, simUi.row - 1); update({}); return; }
    if (btn === "dpad_down") { simUi.row = Math.min(Math.max(0, n - 1), simUi.row + 1); update({}); return; }

    if (simUi.level === "modules") {
      if (btn === "a") {
        simUi.module = simUi.row; simUi.level = "frames"; simUi.row = 0;
        resetScroll("simulate"); update({}); return;
      }
      if (btn !== "b") return;
    } else if (simUi.level === "frames") {
      if (btn === "b") { simUi.level = "modules"; simUi.row = simUi.module;
                         resetScroll("simulate"); update({}); return; }
      if (btn === "a") {
        const f = (mods[simUi.module]?.frames || [])[simUi.row];
        if (!f) return;
        simUi.frame = simUi.row;
        rpc("simulate", "frame", { id: f.id }).then((res) => {
          if (!res.ok) { toast((res.err && res.err.msg) || "could not read frame"); return; }
          simUi.current = res.d;
          simUi.direction = res.d.direction;
          // Prefill from the DBC's own ranges so a send is valid by default.
          simUi.values = {};
          for (const sg of res.d.signals) simUi.values[sg.name] = sg.default;
          simUi.level = "form"; simUi.row = 0;
          resetScroll("simulate"); update({});
        });
        return;
      }
      if (btn !== "b") return;
    } else {
      const f = simUi.current;
      const sig = f && f.signals[simUi.row];
      if (btn === "b") { simUi.level = "frames"; simUi.row = simUi.frame;
                         simUi.current = null; resetScroll("simulate"); update({}); return; }
      if (btn === "x") {
        simUi.direction = (simUi.direction || f.direction) === "outbound"
          ? "inbound" : "outbound";
        toast(simUi.direction === "outbound"
          ? "Will publish to the REAL bus" : "Will emulate the module only");
        update({}); return;
      }
      if ((btn === "l" || btn === "r") && sig) {
        // Nudge without opening the keyboard — enums cycle, numbers step.
        const dir = btn === "r" ? 1 : -1;
        if (sig.choices) {
          const i = sig.choices.findIndex((c) => c.value === Number(simUi.values[sig.name]));
          const nx = sig.choices[(i + dir + sig.choices.length) % sig.choices.length];
          simUi.values[sig.name] = nx.value;
        } else {
          const step = sig.scale && sig.scale < 1 ? sig.scale * 10 : 1;
          let v = Number(simUi.values[sig.name] || 0) + dir * step;
          if (sig.min !== null) v = Math.max(sig.min, v);
          if (sig.max !== null) v = Math.min(sig.max, v);
          simUi.values[sig.name] = Math.round(v * 1000) / 1000;
        }
        update({}); return;
      }
      if (btn === "a" && sig) {
        promptValue(sig);
        return;
      }
      if (btn === "y" && f) {
        const dir = simUi.direction || f.direction;
        const send = () => rpc("simulate", "send",
            { id: f.id, direction: dir, values: simUi.values, confirm: true })
          .then((res) => toast(res.ok
            ? `Sent ${f.name} · ${res.d.bytes || "ok"}`
            : (res.err && res.err.msg) || "send failed", 5000));
        if (dir === "outbound") {
          askConfirm("Send to the real CAN bus?",
                     `${f.name} (${f.hex}) — physical modules will act on this`, send);
        } else { send(); }
        return;
      }
      if (btn !== "b") return;
    }
  }

  if (s.screen === "gnss") {
    const g = (s.modules.gnss && s.modules.gnss.data) || {};
    const theme = document.documentElement.dataset.theme || "dark";
    ensureMap(theme, () => update({}));
    mountMap(theme, g.lat, g.lon);
  } else {
    // The map host lives outside the re-rendered tree, so it has to be
    // hidden explicitly when another screen is showing.
    positionMapHost(false);
  }

  // After the DOM exists, shift the clipped list so the focused row is
  // visible. Must run post-render: it measures real element geometry.
  // Follow mode pins the cursor to the newest line. Done here rather than in
  // the reducer so an arriving line cannot fight a deliberate scroll: pressing
  // up/down clears `follow` first.
  // The console always shows its tail. A follow toggle was one more piece of
  // state that could be silently wrong.
  if (s.screen === "moduledebug" && dbgUi.pane === "console"
      && (s.modules.moduledebug || {}).data?.connected) {
    const n = moduleDebugRows(s);
    if (n > 0) s.row = n - 1;
  }
  // Settings keeps its own cursor (settingsUi.row, or the picker's) rather
  // than state.row, so the shared scroller has to be told which one to follow.
  // Without this the time-zone picker scrolls against row 0 forever and the
  // selection leaves the screen after five presses.
  applyScroll(root, s.screen,
              s.screen === "settings" ? settingsScrollRow() : s.row);
}

// Reusing the boot gate as the Settings network picker means one flow to get
// right, not two — same scan, same password entry, same connect path.
// Y in the browser toggles between the current folder and the shortlist of
// likely starting points, so a USB stick is one press away from anywhere.
// Where the browser was opened. Back walks up to here and only then leaves —
// so drilling in three folders takes three Backs to undo, which is what
// "back" means everywhere else.
let fwRoot = "";

function fwAtRoot(d) {
  const b = d.browser;
  if (!b) return true;
  return !b.parent || b.path === fwRoot || b.path === "/";
}

function showPlaces(d) {
  const places = d.places || [];
  if (!places.length) return;
  const cur = d.browser && d.browser.path;
  const i = places.findIndex((p) => p.path === cur);
  const next = places[(i + 1) % places.length];
  fwRoot = next.path;
  resetScroll("firmware");
  rpc("firmware", "browse", { path: next.path });
  toast(next.label);
  update({ row: 0 });
}

let wifiFromSettings = false;
function openWifiPicker() {
  wifiFromSettings = true;
  wifi.stage = "scan";
  wifiScan();
  update({});
}
function closeWifiPicker() {
  wifiFromSettings = false;
  update({});
}

function bindWifiRows() {
  for (const el of root.querySelectorAll(".wifi-row")) {
    el.addEventListener("click", async () => {
      wifi.sel = Number(el.dataset.idx);
      update({});
      await wifiPress("a");
    });
  }
}

// Touch parity: everything reachable by button is tappable. This is not a
// convenience — the buttons hang off an RP2040 that can strand itself in
// BOOTSEL, and touch is the only recovery path. See docs/controls.md.
function bindTiles() {
  for (const el of root.querySelectorAll(".tile")) {
    el.addEventListener("click", () => {
      const idx = Number(el.dataset.idx);
      update({ focus: idx });
      openApp(el.dataset.app);
    });
  }
}

// ── Universal touch ──────────────────────────────────────────────────
// Bound ONCE and delegated, so it survives innerHTML replacement and covers
// screens written later. Touch was previously added per screen (bindTiles,
// bindWifiRows, settings) which meant every app built afterwards silently
// had none. This makes tappability a property of the system instead.
//
// A tap sets the screen's cursor to the tapped row and then issues the SAME
// open that Start issues, so thumb and D-pad share one code path and cannot
// drift apart.
//
// Cursor location is read out of the code, not assumed: the majority of
// screens keep it in state.row; the two exceptions are launcher (state.focus)
// and simulate (simUi.row). Settings and the WiFi picker already bind their
// own rows, so those are skipped here to avoid handling a tap twice.
const OWN_BINDER = ".tile, .wifi-row, .set-row, .set-group";

function setCursor(screen, n) {
  if (screen === "simulate") { simUi.row = n; update({}); return; }
  if (screen === "launcher") { update({ focus: n }); return; }
  update({ row: n });
}

root.addEventListener("click", async (ev) => {
  // The hint bar is checked BEFORE the modal guard, and goes straight to
  // press() rather than through setCursor.
  //
  // Both are deliberate. press() is the physical buttons' own entry point and
  // already arbitrates modals — it routes "a"/"b" to the confirm box, the WiFi
  // gate or the screen underneath as appropriate. Sending taps anywhere else
  // would fork navigation into a touch path and a button path that drift; the
  // modal guard below exists for taps on rows BEHIND an overlay, which is a
  // different problem and still handled.
  //
  // It also means the on-screen Start/Select keep working while a confirm
  // dialog is up, which is the one moment an operator with a wedged keyboard
  // most needs them.
  const hint = ev.target.closest("[data-hint]");
  if (hint && root.contains(hint)) {
    await press(hint.dataset.hint);
    return;
  }

  // A modal owns every input while it is up. Without this, a tap landing on
  // a row BEHIND the overlay would move the cursor and fire "a" — which the
  // modal handler reads as Confirm. That would let a stray touch delete a
  // recording or put a frame on the real CAN bus.
  if (prompt.open || confirmBox.open) return;
  const el = ev.target.closest("[data-idx]");
  if (!el || !root.contains(el) || el.closest(OWN_BINDER)) return;
  const n = Number(el.dataset.idx);
  if (!Number.isFinite(n)) return;
  // Panes declare themselves so a tap lands in the pane touched, rather than
  // moving the cursor of whichever pane happened to have focus.
  if (el.dataset.pane && el.dataset.pane !== hwUi.pane) {
    hwUi.pane = el.dataset.pane;
    resetScroll(state.screen);
  }
  setCursor(state.screen, n);
  await press("a");
});

// ── input ────────────────────────────────────────────────────────────
function openApp(id) {
  if (id === "settings") { settingsUi.mode = "groups"; settingsUi.row = 0; }
  if (id === "capture") captureUi.pane = "sessions";
  if (id === "simulate") simReset();
  resetScroll(id);
  if (id === "terminal") {
    termFollow();
    // The shell needs every key. Select stays non-typeable, so Back survives.
    // sink:"terminal" makes tracerd write keystrokes straight to the pty —
    // without it each character became an HTTP POST (and a fresh TCP
    // connection), which is why typing crawled.
    rpc("terminal", "open");
    rpc("input", "set_mode", { mode: "text", sink: "terminal" });
  }
  update({ screen: id, row: 0 });
}

function leaveApp() {
  if (state.screen === "terminal") rpc("input", "set_mode", { mode: "nav" });
  // Module Debug routes the keyboard to the serial port while typing. If that
  // is not released here the daemon stays in text mode with the serial sink,
  // and every button types into a port that is no longer on screen — leaving
  // no way to navigate anywhere.
  if (state.screen === "moduledebug") {
    rpc("input", "set_mode", { mode: "nav", sink: "gui" });
    rpc("moduledebug", "close");
    dbgUi.pane = "ports";
  }
  update({ screen: "launcher", row: 0 });
}

async function press(btn, phase = "down") {
  if (phase === "up") return;
  const s = state;

  // ── Universal navigation ───────────────────────────────────────────
  // Start = Open/Confirm, Select = Back. EVERYWHERE — launcher, inside an
  // app, in a text field.
  //
  // These two are the only buttons that carry no character, so they are the
  // only pair that can mean the same thing in every mode. A and B are the
  // letter keys A and B: they work as shortcuts while navigating, but the
  // instant a text field has focus they must type. Binding the universal
  // pair to A/B semantics here means every existing handler works unchanged
  // and the operator never has to learn a second navigation model.
  if (btn === "start") btn = "a";
  else if (btn === "select") btn = "b";

  // The WiFi gate swallows every button while it is up.
  if (needsWifi(s) && s.screen !== "boot") {
    await wifiPress(btn);
    return;
  }

  // Settings' WiFi picker is dismissible (unlike the boot gate).
  if (wifiFromSettings) {
    if (btn === "b" && (wifi.stage === "list" || wifi.stage === "failed")) {
      closeWifiPicker();
      return;
    }
    await wifiPress(btn);
    if (s.modules.net && s.modules.net.state === "ok" && wifi.stage === "connecting") {
      closeWifiPicker();
    }
    return;
  }

  if (confirmBox.open) {
    if (btn === "a") {
      const fn = confirmBox.onYes;
      confirmBox.open = false; confirmBox.onYes = null;
      if (fn) await fn();
      update({});
      return;
    }
    if (btn === "b") { confirmBox.open = false; confirmBox.onYes = null; update({}); return; }
    return;
  }

  if (prompt.open) {
    if (btn === "a") { await promptCommit(); return; }
    if (btn === "b") { promptCancel(); return; }
    return;
  }

  if (s.screen === "boot") {
    if (btn === "a" || btn === "b") update({ screen: "launcher" });
    return;
  }

  if (s.screen === "launcher") {
    if (btn === "a") {
      const app = s.apps[s.focus];
      if (app) openApp(app.id);
      return;
    }
    // B at the launcher does nothing — it is the root.
    if (btn === "b") return;
    if (btn === "x") { toast("Search apps"); return; }
    if (btn === "y") {
      const app = s.apps[s.focus];
      if (app) toast(`${app.short} pinned to top row`);
      return;
    }
    const next = launcherMove(s, btn);
    if (next !== s.focus) update({ focus: next });
    return;
  }

  if (s.screen === "settings") {
    const handled = await settingsPress(btn, s, openWifiPicker);
    if (handled) return;
    update({ screen: "launcher", row: 0 });
    return;
  }

  if (s.screen === "simulate") {
    const n = simulateRows(s);
    const mods = (s.modules.simulate && s.modules.simulate.data
                  && s.modules.simulate.data.modules) || [];
    if (btn === "dpad_up")   { simUi.row = Math.max(0, simUi.row - 1); update({}); return; }
    if (btn === "dpad_down") { simUi.row = Math.min(Math.max(0, n - 1), simUi.row + 1); update({}); return; }

    if (simUi.level === "modules") {
      if (btn === "a") {
        simUi.module = simUi.row; simUi.level = "frames"; simUi.row = 0;
        resetScroll("simulate"); update({}); return;
      }
      if (btn !== "b") return;
    } else if (simUi.level === "frames") {
      if (btn === "b") { simUi.level = "modules"; simUi.row = simUi.module;
                         resetScroll("simulate"); update({}); return; }
      if (btn === "a") {
        const f = (mods[simUi.module]?.frames || [])[simUi.row];
        if (!f) return;
        simUi.frame = simUi.row;
        rpc("simulate", "frame", { id: f.id }).then((res) => {
          if (!res.ok) { toast((res.err && res.err.msg) || "could not read frame"); return; }
          simUi.current = res.d;
          simUi.direction = res.d.direction;
          // Prefill from the DBC's own ranges so a send is valid by default.
          simUi.values = {};
          for (const sg of res.d.signals) simUi.values[sg.name] = sg.default;
          simUi.level = "form"; simUi.row = 0;
          resetScroll("simulate"); update({});
        });
        return;
      }
      if (btn !== "b") return;
    } else {
      const f = simUi.current;
      const sig = f && f.signals[simUi.row];
      if (btn === "b") { simUi.level = "frames"; simUi.row = simUi.frame;
                         simUi.current = null; resetScroll("simulate"); update({}); return; }
      if (btn === "x") {
        simUi.direction = (simUi.direction || f.direction) === "outbound"
          ? "inbound" : "outbound";
        toast(simUi.direction === "outbound"
          ? "Will publish to the REAL bus" : "Will emulate the module only");
        update({}); return;
      }
      if ((btn === "l" || btn === "r") && sig) {
        // Nudge without opening the keyboard — enums cycle, numbers step.
        const dir = btn === "r" ? 1 : -1;
        if (sig.choices) {
          const i = sig.choices.findIndex((c) => c.value === Number(simUi.values[sig.name]));
          const nx = sig.choices[(i + dir + sig.choices.length) % sig.choices.length];
          simUi.values[sig.name] = nx.value;
        } else {
          const step = sig.scale && sig.scale < 1 ? sig.scale * 10 : 1;
          let v = Number(simUi.values[sig.name] || 0) + dir * step;
          if (sig.min !== null) v = Math.max(sig.min, v);
          if (sig.max !== null) v = Math.min(sig.max, v);
          simUi.values[sig.name] = Math.round(v * 1000) / 1000;
        }
        update({}); return;
      }
      if (btn === "a" && sig) {
        promptValue(sig);
        return;
      }
      if (btn === "y" && f) {
        const dir = simUi.direction || f.direction;
        const send = () => rpc("simulate", "send",
            { id: f.id, direction: dir, values: simUi.values, confirm: true })
          .then((res) => toast(res.ok
            ? `Sent ${f.name} · ${res.d.bytes || "ok"}`
            : (res.err && res.err.msg) || "send failed", 5000));
        if (dir === "outbound") {
          askConfirm("Send to the real CAN bus?",
                     `${f.name} (${f.hex}) — physical modules will act on this`, send);
        } else { send(); }
        return;
      }
      if (btn !== "b") return;
    }
  }

  if (s.screen === "gnss") {
    if (btn === "a") { toast("Rechecking map tiles"); rpc("gnss", "check_tiles"); return; }
    if (btn === "dpad_up")   { mapZoom(+1); update({}); return; }
    if (btn === "dpad_down") { mapZoom(-1); update({}); return; }
    if (btn === "b") { mapTeardown(); }
    if (btn !== "b") return;
  }

  if (s.screen === "terminal") {
    if (btn === "b") { termFollow(); leaveApp(); return; }
    const total = ((s.modules.terminal && s.modules.terminal.data
                    && s.modules.terminal.data.lines) || []).length;
    if (btn === "dpad_up")   { termScroll(+3, total); update({}); return; }
    if (btn === "dpad_down") { termScroll(-3, total); update({}); return; }
    if (btn === "a") { termFollow(); update({}); return; }   // Start = follow live
    // Back must NOT be swallowed here — it falls through to the generic
    // handler at the end so Select always leaves the app. A bare
    // `return` here is what stopped Select working once a screen had
    // handled its own keys.
    if (btn !== "b") return;
  }

  if (s.screen === "capture") {
    const d = (s.modules.capture && s.modules.capture.data) || {};
    const pb = d.playback || {};
    if (captureUi.pane === "playback") {
      if (btn === "b") { rpc("capture", "pause"); captureUi.pane = "sessions"; update({}); return; }
      if (btn === "a") { rpc("capture", pb.playing ? "pause" : "play"); return; }
      if (btn === "x") { rpc("capture", "seek", { pos: 0 }); return; }
      if (btn === "y") {
        // Y was Close, which duplicated Select=Back. Loop is the useful thing
        // to spend a key on: a short capture repeated is how you actually
        // study an intermittent fault.
        rpc("capture", "loop");
        toast(pb.loop ? "Loop off" : "Loop on");
        return;
      }
      return;
    }
    const n = captureRows(s);
    if (btn === "dpad_up")   { update({ row: Math.max(0, s.row - 1) }); return; }
    if (btn === "dpad_down") { update({ row: Math.min(Math.max(0, n - 1), s.row + 1) }); return; }
    if (btn === "x") {
      if (d.recording) { promptName("capture", "stop", "Name this capture"); }
      else { rpc("capture", "start"); toast("Recording MQTT"); }
      return;
    }
    if (btn === "a") {
      const c = (d.sessions || [])[s.row];
      if (!c) return;
      captureUi.pane = "playback";
      resetScroll("capture");
      rpc("capture", "load", { file: c.file });
      update({ row: 0 });
      return;
    }
    if (btn === "y") {
      const c = (d.sessions || [])[s.row];
      if (c) promptName("capture", "rename", "Rename capture", { file: c.file });
      return;
    }
    if (btn === "l") {
      const c = (d.sessions || [])[s.row];
      if (!c) return;
      askConfirm("Delete capture?", c.name, async () => {
        const res = await rpc("capture", "delete", { file: c.file, confirm: true });
        toast(res.ok ? `Deleted ${c.name}` : (res.err && res.err.msg) || "delete failed");
        update({ row: 0 });
      });
      return;
    }
    // Back must NOT be swallowed here — it falls through to the generic
    // handler at the end so Select always leaves the app. A bare
    // `return` here is what stopped Select working once a screen had
    // handled its own keys.
    if (btn !== "b") return;
  }

  if (s.screen === "firmware") {
    const d = (s.modules.firmware && s.modules.firmware.data) || {};
    const n = firmwareRows(s);
    if (btn === "dpad_up")   { update({ row: Math.max(0, s.row - 1) }); return; }
    if (btn === "dpad_down") { update({ row: Math.min(Math.max(0, n - 1), s.row + 1) }); return; }

    if (d.browser) {
      const e = (d.browser.entries || [])[s.row];
      const up = () => {
        resetScroll("firmware");
        rpc("firmware", "browse", { path: d.browser.parent });
        update({ row: 0 });
      };
      if (btn === "b") {
        // Back means "undo the last navigation". Having it exit the browser
        // from three folders deep left no way back up the hierarchy at all.
        if (!fwAtRoot(d)) { up(); return; }
        rpc("firmware", "browse_close");
        return;
      }
      // One job per key. Select already walks up the tree, so X duplicating
      // it was just noise; X jumps between starting points instead, and Y is
      // the explicit way out from any depth.
      if (btn === "x") { showPlaces(d); return; }
      if (btn === "y") { rpc("firmware", "browse_close"); return; }
      if (btn === "a" && e) {
        resetScroll("firmware");
        if (e.dir) { rpc("firmware", "browse", { path: e.path }); update({ row: 0 }); }
        else { rpc("firmware", "select", { path: e.path }); toast(`Selected ${e.name}`); update({ row: 0 }); }
        return;
      }
      return;
    }

    if (btn === "x") {
      const start = (d.places && d.places[0] && d.places[0].path) || "/media";
      fwRoot = start;
      resetScroll("firmware");
      rpc("firmware", "browse", { path: start });
      update({ row: 0 });
      return;
    }
    if (btn === "y") { toast("Verifying Headwaters"); rpc("firmware", "verify"); return; }
    if (btn === "a" && !d.busy) {
      const p = (d.packages || [])[s.row];
      if (!p) return;
      // Naming the package in the dialog is the mitigation for a mis-indexed
      // tap: the list can re-render between paint and touch, so the operator
      // confirms WHAT is being deployed, not merely that they meant to act.
      askConfirm("Deploy firmware?", p.name, () => {
        toast(`Deploying ${p.name} — several minutes`, 5000);
        rpc("firmware", "deploy", { path: p.path, confirm: true });
      });
      return;
    }
    // Back must NOT be swallowed here — it falls through to the generic
    // handler at the end so Select always leaves the app. A bare
    // `return` here is what stopped Select working once a screen had
    // handled its own keys.
    if (btn !== "b") return;
  }

  if (s.screen === "logs") {
    const n = logsRows(s);
    const d = (s.modules.logs && s.modules.logs.data) || {};
    if (btn === "dpad_up")   { update({ row: Math.max(0, s.row - 1) }); return; }
    if (btn === "dpad_down") { update({ row: Math.min(Math.max(0, n - 1), s.row + 1) }); return; }
    if (btn === "x") { rpc("logs", "level"); return; }
    if (btn === "y") { toast("Refreshing"); rpc("logs", "refresh"); return; }
    if (btn === "a") {
      const list = logsSources(d);
      const i = list.findIndex((x) => x.source === d.source && (x.unit || "") === (d.unit || ""));
      const next = list[(i + 1) % list.length];
      toast(`${next.source === "local" ? "Tracer" : next.unit}`);
      rpc("logs", "select", next);
      update({ row: 0 });
      return;
    }
    // Back must NOT be swallowed here — it falls through to the generic
    // handler at the end so Select always leaves the app. A bare
    // `return` here is what stopped Select working once a screen had
    // handled its own keys.
    if (btn !== "b") return;
  }

  if (s.screen === "moduledebug") {
    const md = (s.modules.moduledebug || {}).data || {};

    // Connected == the keyboard belongs to the module. tracerd routes every
    // keystroke straight to the port (sink:"moduledebug"), so this handler
    // only ever sees Select — the one button carrying no character. There is
    // no typing mode to be on the wrong side of.
    if (dbgUi.pane === "console" && (md.connected || md.waiting)) {
      if (btn === "b") {
        rpc("input", "set_mode", { mode: "nav", sink: "gui" });
        rpc("moduledebug", "close");
        dbgUi.pane = "ports";
        resetScroll("moduledebug");
        update({ row: 0 });
        return;
      }
      return;
    }

    // Port picker.
    const n = moduleDebugRows(s);
    if (btn === "dpad_down") { update({ row: Math.min(Math.max(0, n - 1), s.row + 1) }); return; }
    if (btn === "dpad_up")   { update({ row: Math.max(0, s.row - 1) }); return; }
    if (btn === "y") {
      // Report the result, not just the intent. "Rescanning…" with an
      // unchanged screen is indistinguishable from a button that does nothing.
      rpc("moduledebug", "rescan").then((r) => {
        const n = (r && r.d && r.d.ports) || 0;
        toast(n ? `${n} port${n === 1 ? "" : "s"} found` : "No modules found");
      });
      return;
    }
    if (btn === "l" || btn === "r") {
      // Baud belongs to the DEVICE, not the app: a rig can hold one module on
      // native USB (where baud is ignored) and another on a bridge at a
      // different rate. Stored against the module's USB identity.
      const p = (md.ports || [])[s.row];
      if (!p) return;
      if (p.baud_applies === false) {
        toast(`${p.label} uses built-in USB — baud does not apply`);
        return;
      }
      const list = md.bauds || [115200];
      const cur = list.indexOf(p.baud);
      const next = list[((cur < 0 ? 1 : cur) + (btn === "r" ? 1 : list.length - 1))
                        % list.length];
      rpc("moduledebug", "set_baud", { identity: p.identity, baud: next });
      toast(`${p.label}: ${next} baud`);
      return;
    }
    if (btn === "a") {
      const p = (md.ports || [])[s.row];
      if (!p) return;
      toast(`Connecting to ${p.name}`);
      rpc("moduledebug", "open", { port: p.device }).then((r) => {
        if (!r.ok) { toast((r.err && r.err.msg) || "could not open port", 4000); return; }
        if (r.d && r.d.ok === false) { toast(r.d.error, 4000); return; }
        dbgUi.pane = "console";
        // Hand the keyboard to the port immediately. Connecting IS the intent
        // to interact; a separate step to enable typing was the single most
        // confusing thing about the previous version.
        rpc("input", "set_mode", { mode: "text", sink: "moduledebug" });
        resetScroll("moduledebug");
        update({ row: 0 });
      });
      return;
    }
    if (btn !== "b") return;
  }

  if (s.screen === "headwaters") {
    const n = headwatersRows(s);
    if (btn === "dpad_down") { update({ row: Math.min(Math.max(0, n - 1), s.row + 1) }); return; }
    if (btn === "dpad_up")   { update({ row: Math.max(0, s.row - 1) }); return; }
    // X swaps panes. Each pane keeps its own scroll offset reset so the
    // cursor never lands off-screen in the pane being entered.
    if (btn === "x") {
      hwUi.pane = hwUi.pane === "containers" ? "procs" : "containers";
      resetScroll("headwaters");
      update({ row: 0 });
      return;
    }
    if (btn === "y") { toast("Refreshing"); rpc("headwaters", "refresh"); return; }
    if (btn !== "b") return;
  }

  if (s.screen === "discovery") {
    const n = discoveryRows(s);
    const d = (s.modules.discovery && s.modules.discovery.data) || {};
    if (btn === "dpad_up")   { update({ row: Math.max(0, s.row - 1) }); return; }
    if (btn === "dpad_down") { update({ row: Math.min(Math.max(0, n - 1), s.row + 1) }); return; }
    if (btn === "x") {
      const op = d.browsing ? "stop" : "browse";
      toast(d.browsing ? "Stopping scan" : "Asked Headwaters to scan");
      rpc("discovery", op);
      return;
    }
    if (btn === "a") {
      const dev = (d.devices || [])[s.row];
      if (!dev) return;
      if (dev.state === "onboarded") { toast(`${dev.hostname} already onboarded`); return; }
      // Devices appear and disappear during a scan, so the row under a finger
      // may not be the row that was painted. The hostname in the dialog is
      // what makes that visible before a module joins the fleet.
      const verb = dev.onboard === "claim" ? "Claim" : "Confirm";
      askConfirm(`${verb} this module?`, dev.hostname, () => {
        // Headwaters does the onboarding; we are asking it to.
        toast(`Asked Headwaters to ${verb.toLowerCase()} ${dev.hostname}`);
        rpc("discovery", "onboard", { hostname: dev.hostname, confirm: true });
      });
      return;
    }
    if (btn === "y") {
      const dev = (d.devices || [])[s.row];
      if (dev) toast(`${dev.hostname} · ${dev.type} · ${dev.fw || "fw --"}`, 4000);
      return;
    }
    // Back must NOT be swallowed here — it falls through to the generic
    // handler at the end so Select always leaves the app. A bare
    // `return` here is what stopped Select working once a screen had
    // handled its own keys.
    if (btn !== "b") return;
  }

  if (s.screen === "mqtt") {
    const n = mqttRows(s);
    if (btn === "dpad_up")   { update({ row: Math.max(0, s.row - 1) }); return; }
    if (btn === "dpad_down") { update({ row: Math.min(Math.max(0, n - 1), s.row + 1) }); return; }
    if (btn === "a") {
      const m = s.modules.mqtt;
      if (!m || !m.data || !m.data.connected) {
        toast("Reconnecting to broker");
        rpc("mqtt", "reconnect");
      }
      return;
    }
    if (btn === "x") { rpc("mqtt", "pause"); return; }
    if (btn === "y") { toast("Cleared"); rpc("mqtt", "clear"); update({ row: 0 }); return; }
    // Back must NOT be swallowed here — it falls through to the generic
    // handler at the end so Select always leaves the app. A bare
    // `return` here is what stopped Select working once a screen had
    // handled its own keys.
    if (btn !== "b") return;
  }

  if (s.screen === "net") {
    const n = networkRows(s);
    if (!n) return;
    if (btn === "dpad_up")   { update({ row: Math.max(0, s.row - 1) }); return; }
    if (btn === "dpad_down") { update({ row: Math.min(n - 1, s.row + 1) }); return; }
    if (btn === "l") { update({ row: 0 }); return; }
    if (btn === "r") { update({ row: n - 1 }); return; }
    if (btn === "a") { toast("Rechecking endpoints"); rpc("net", "recheck"); return; }
    if (btn === "x") { toast("Rejoining " + (s.modules.net.data.ssid || "network")); return; }
    if (btn === "y") { toast("Static IP not implemented"); return; }
  }

  // Generic Back, LAST. It has to come after every per-screen handler:
  // screens with their own hierarchy (the firmware file browser, capture
  // playback) need to claim Back first so it means "up one level" there.
  // Running this early made those handlers unreachable, and Back always
  // jumped straight to the launcher no matter what the hint bar said.
  if (btn === "b") { leaveApp(); return; }
}

// A small shared text prompt. Capture needs a name and firmware may need one
// later; both use the same physical-keyboard text mode as everything else.
const prompt = { open: false, title: "", value: "", module: "", op: "", extra: null };
// warn/yesLabel/noLabel default to the destructive-delete wording every
// existing caller relies on. Shutdown needs different words — it is not a
// delete and it IS undoable, you just turn the unit back on — so they are
// overridable rather than baked into the overlay.
const confirmBox = {
  open: false, title: "", detail: "", onYes: null,
  warn: "This cannot be undone", yesLabel: "Delete", noLabel: "Keep",
};

function askConfirm(title, detail, onYes, opts = {}) {
  confirmBox.open = true; confirmBox.title = title;
  confirmBox.detail = detail; confirmBox.onYes = onYes;
  confirmBox.warn = opts.warn ?? "This cannot be undone";
  confirmBox.yesLabel = opts.yesLabel ?? "Delete";
  confirmBox.noLabel = opts.noLabel ?? "Keep";
  update({});
}

function confirmOverlay() {
  return `
  <div style="position:absolute;inset:0;z-index:7;background:var(--bg-0);
              display:flex;flex-direction:column;align-items:center;
              justify-content:center;gap:14px;padding:24px;">
    <div style="color:var(--tc-danger);">${icon("alert-circle", 52)}</div>
    <div style="font-size:28px;font-weight:500;">${confirmBox.title}</div>
    <div style="font-size:19px;color:var(--fg);font-family:var(--mono);
                text-align:center;word-break:break-all;max-width:580px;
                line-height:25px;">${confirmBox.detail}</div>
    <div style="font-size:17px;color:var(--tc-warning);">
      ${confirmBox.warn}
    </div>
    <div style="display:flex;gap:26px;margin-top:12px;">
      <div style="display:flex;align-items:center;gap:9px;">
        <!-- White on red: #000 on --tc-danger fails contrast in both themes,
             and this is the destructive choice, so it has to be legible. -->
        <div style="padding:5px 14px;border-radius:var(--r-full);
                    background:var(--tc-danger);color:#fff;font-size:15px;
                    font-weight:700;">Start</div>
        <div style="font-size:19px;">${confirmBox.yesLabel}</div>
      </div>
      <div style="display:flex;align-items:center;gap:9px;">
        <div style="padding:5px 14px;border-radius:var(--r-full);
                    background:var(--bg-3);color:var(--fg);font-size:15px;
                    font-weight:700;">Select</div>
        <div style="font-size:19px;">${confirmBox.noLabel}</div>
      </div>
    </div>
  </div>`;
}

// Editing one signal by hand. Numeric entry uses the physical keyboard, like
// every other text field in the product.
function promptValue(sig) {
  const range = sig.min !== null ? ` (${sig.min} to ${sig.max}${sig.unit ? " " + sig.unit : ""})` : "";
  prompt.open = true;
  prompt.title = `${sig.name}${range}`;
  prompt.value = String(simUi.values[sig.name] ?? "");
  prompt.module = "__sim"; prompt.op = sig.name; prompt.extra = null;
  rpc("input", "set_mode", { mode: "text" });
  update({});
}

function promptName(module, op, title, extra) {
  prompt.open = true; prompt.title = title; prompt.value = "";
  prompt.module = module; prompt.op = op; prompt.extra = extra || null;
  rpc("input", "set_mode", { mode: "text" });
  update({});
}

async function promptCommit() {
  const p = { ...prompt };
  prompt.open = false;
  if (p.module === "__sim") {
    // Local edit, no RPC — the value is only sent when Y is pressed.
    const n = Number(p.value);
    simUi.values[p.op] = Number.isFinite(n) ? n : p.value;
    await rpc("input", "set_mode", { mode: "nav" });
    update({});
    return;
  }
  await rpc("input", "set_mode", { mode: "nav" });
  const res = await rpc(p.module, p.op, { name: p.value, ...(p.extra || {}) });
  toast(res.ok ? `Saved ${p.value || "capture"}`
               : (res.err && res.err.msg) || "failed");
  update({});
}

function promptCancel() {
  prompt.open = false;
  rpc("input", "set_mode", { mode: "nav" });
  update({});
}

function promptOverlay() {
  return `
  <div style="position:absolute;inset:0;z-index:6;background:var(--bg-0);
              display:flex;flex-direction:column;padding:12px 14px;">
    <div style="font-size:15px;font-weight:500;">${prompt.title}</div>
    <div style="margin-top:12px;padding:12px;background:var(--bg-1);
                border:2px solid var(--focus-border);box-shadow:var(--glow);
                border-radius:10px;font-family:var(--mono);font-size:15px;
                min-height:44px;word-break:break-all;">
      ${prompt.value || '<span style="color:var(--fg-off)">capture-name</span>'}<span
        style="display:inline-block;width:8px;height:15px;vertical-align:-2px;
               background:var(--tc-primary-light);
               animation:tcpulse 1s ease-in-out infinite;"></span>
    </div>
    <div style="margin-top:10px;font-size:11px;color:var(--fg-dim);">
      Enter or Start to save · Esc or Select to cancel
    </div>
  </div>`;
}

onButton(press);

// The physical power button. logind is set to HandlePowerKey=ignore in the
// image, so a short press arrives here as an event instead of powering the
// unit off where it stands — this is a handheld tool that gets carried in a
// bag, and a brushed button used to kill it mid-capture with no warning.
//
// Guarded against re-entry: the daemon emits one event per key-down, but a
// second prompt stacking on the first would leave a dialog nobody dismissed.
// If anything else already owns the screen (a delete confirmation, a text
// prompt), that wins — the operator is mid-decision and should finish it.
onPowerKey(() => {
  if (confirmBox.open || prompt.open) return;
  askConfirm("Shut down?", "The device will power off", async () => {
    toast("Shutting down…");
    // confirm:true is required — power.py refuses the operation without it,
    // which is what makes this dialog load-bearing rather than decorative.
    await rpc("power", "shutdown", { confirm: true });
  }, { warn: "Unsaved captures will be lost",
       yesLabel: "Shut down", noLabel: "Cancel" });
});

// Text mode: the daemon sends characters instead of buttons once a field has
// focus. Six of the twelve buttons are letter keys, so this switch is what
// lets the same physical key type "a" and press A in different contexts.
onText((ch) => {
  if (prompt.open) { prompt.value += ch; update({}); return; }
  // Characters no longer arrive here while the Terminal is focused — the
  // daemon sinks them directly. This stays as a fallback if the sink is off.
  if (state.screen === "terminal") { rpc("terminal", "write", { data: ch }); return; }
  if (wifiFromSettings || needsWifi(state)) wifiText(ch);
  else if (state.screen === "settings") settingsText(ch, state);
});
onTextKey((k) => {
  if (prompt.open) {
    if (k === "backspace") { prompt.value = prompt.value.slice(0, -1); update({}); }
    else if (k === "enter") promptCommit();
    else if (k === "escape") promptCancel();
    return;
  }
  if (state.screen === "terminal") {
    const map = { enter: "\n", backspace: "\x7f", tab: "\t", escape: "\x1b" };
    if (map[k]) rpc("terminal", "write", { data: map[k] });
    return;
  }
  if (wifiFromSettings || needsWifi(state)) wifiTextKey(k);
  else if (state.screen === "settings") settingsTextKey(k, state);
});

// ── boot sequencing ──────────────────────────────────────────────────
// Leaves the boot screen when the daemon reports its modules resolved, or
// after a hard timeout so a wedged module can never trap the operator on the
// splash with no way forward.
let bootDone = Boolean(INITIAL_SCREEN);
const BOOT_TIMEOUT_MS = 8000;
const bootStarted = Date.now();

if (INITIAL_SCREEN) state.screen = INITIAL_SCREEN;
if (INITIAL_GROUP !== null && INITIAL_SCREEN === "settings") {
  settingsUi.mode = "rows";
  settingsUi.group = Number(INITIAL_GROUP) || 0;
  if (INITIAL_ROW !== null) settingsUi.row = Number(INITIAL_ROW) || 0;
}

subscribe((s) => {
  if (bootDone || s.screen !== "boot") return;
  const { pct, line } = bootProgress(s);
  if (pct !== s.bootPct || line !== s.bootLine) {
    update({ bootPct: pct, bootLine: line });
  }
  if (pct >= 100 || Date.now() - bootStarted > BOOT_TIMEOUT_MS) {
    bootDone = true;
    setTimeout(() => update({ screen: "launcher" }), 250);
  }
});

// Kick off a scan the moment the gate becomes necessary.
let wifiScanStarted = false;
subscribe((s) => {
  if (needsWifi(s) && !wifiScanStarted) {
    wifiScanStarted = true;
    wifiScan();
  } else if (!needsWifi(s) && s.modules.net && s.modules.net.state === "ok") {
    wifiScanStarted = false;
  }
});

subscribe(render);
connect();
render(state);
