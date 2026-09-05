// Fixed chrome: status bar (top) and hint bar (bottom). Both heights come from
// --chrome-top / --chrome-bottom in tokens.css, which the app bodies size
// themselves against via --body-h — do not write either number here.
// Colours are lifted from the design mock.

import { icon } from "../icons.js";

const TITLES = {
  launcher: "Tracer",
  mqtt: "MQTT Inspector", discovery: "Device Discovery", capture: "Capture",
  firmware: "Firmware", net: "Network", terminal: "Terminal", logs: "Logs",
  can: "CAN Sniffer", headwaters: "Headwaters", gnss: "GNSS & Map",
  simulate: "Simulate", settings: "Settings",
  moduledebug: "Module Debug",
};

// Per-app hints.
//
// The legend leads with Start/Select, NOT A/B. Start and Select are the only
// buttons that mean the same thing in every mode — including inside a text
// field, where A and B necessarily type letters. Showing "A Open" taught a
// model that stops being true the moment the operator edits anything, so the
// legend now shows the pair that always works. A and B still function as
// shortcuts while navigating; they are simply not what the legend advertises.
const HINTS = {
  launcher:  [["Start", "Open"], ["X", "Search"], ["Y", "Pin"]],
  mqtt:      [["Start", "Expand"], ["Select", "Back"], ["X", "Pause"], ["Y", "Clear"]],
  discovery: [["Start", "Confirm"], ["Select", "Back"], ["X", "Scan"], ["Y", "Details"]],
  capture:   [["Start", "Record"], ["Select", "Back"], ["X", "Filter"], ["Y", "Upload"]],
  firmware:  [["Start", "Toggle"], ["Select", "Back"], ["X", "Push"], ["Y", "Manifest"]],
  net:       [["Start", "Recheck"], ["Select", "Back"], ["X", "Rejoin"], ["Y", "Static IP"]],
  terminal:  [["Select", "Back"], ["X", "Ctrl-C"], ["Y", "Paste"], ["L/R", "Tab"]],
  logs:      [["Start", "Expand"], ["Select", "Back"], ["X", "Level"], ["Y", "Follow"]],
  can:       [["Start", "Decode"], ["Select", "Back"], ["X", "Freeze"], ["Y", "Send frame"]],
  headwaters:[["Start", "Container"], ["Select", "Back"], ["X", "Restart"], ["Y", "Logs"]],
  gnss:      [["Start", "Center"], ["Select", "Back"], ["X", "Sat view"], ["Y", "Mark"]],
  simulate:  [["Start", "Open"], ["Select", "Back"]],
  moduledebug: [["Start", "Connect"], ["Select", "Back"], ["L/R", "Baud"], ["Y", "Rescan"]],
  settings:  [["Start", "Open"], ["Select", "Back"], ["X", "Search"], ["Y", "Reboot"]],
};

const BTN_COLORS = {
  // White on the red pill — black on #FF5453 is hard to read on the panel,
  // and B/Back is exactly the label you reach for when something is wrong.
  A: ["#52A441", "#000"], B: ["#FF5453", "#fff"],
  X: ["#48E6FE", "#000"], Y: ["#FFC107", "#000"],
  "L/R": ["#2a2a2a", "#aaa"],
  // Start/Select are the text-field Accept/Cancel — neutral, not action-green,
  // so they read as chrome rather than competing with A.
  Start: ["#7BC96A", "#000"], Select: ["#4a4a4a", "#fff"],
  Esc: ["#2a2a2a", "#aaa"],
  "Ctrl+C": ["#2a2a2a", "#aaa"], Enter: ["#2a2a2a", "#aaa"],
};

function clock() {
  const d = new Date();
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

// Values the daemon has no data for render as `--`, per the copy convention.
function dash(v) {
  return (v === null || v === undefined || v === "") ? "--" : v;
}

// One spinner, used everywhere. A screen that is fetching must say so — the
// alternative is an empty list that is indistinguishable from a broken app,
// which is exactly how the slow log fetches read.
export function spinner(size = 14, note = "") {
  return `<div style="display:flex;align-items:center;gap:6px;">
    <div style="color:var(--tc-primary);animation:tcspin 1s linear infinite;
                display:flex;">${icon("sync-outline", size)}</div>
    ${note ? `<div style="font-size:11px;color:var(--fg-dim);">${note}</div>` : ""}
  </div>`;
}

// Full-panel version, for a screen with nothing to show yet.
export function loading(note = "Loading…") {
  return `<div style="flex:1;display:flex;flex-direction:column;
              align-items:center;justify-content:center;gap:10px;">
    <div style="color:var(--tc-primary);animation:tcspin 1s linear infinite;
                display:flex;">${icon("sync-outline", 30)}</div>
    <div style="font-size:12px;color:var(--fg-dim);">${note}</div>
  </div>`;
}

export function statusBar(state) {
  const net = state.modules.net;
  const hw = state.modules.headwaters;

  const netOk = net && net.state === "ok";
  const ssid = netOk ? dash(net.data.ssid) : "--";
  const netColor = netOk ? "var(--tc-success)" : "var(--fg-mute)";

  const hwOk = hw && hw.state === "ok";
  const hwLabel = hwOk ? dash(hw.data.host) : "--";
  const hwColor = hwOk ? "var(--tc-success)" : "var(--fg-mute)";

  // NO BATTERY INDICATOR — deliberately, and it should stay that way unless
  // the hardware changes.
  //
  // The PocketTerm35 gives the Pi no way to read charge state. Verified on
  // the device rather than assumed:
  //   * /sys/class/power_supply/ is empty — no battery or charger driver
  //   * no fuel-gauge module is loaded, and nothing answers on i2c-1
  //   * the devices on i2c-13 (0x37 0x3a 0x4a 0x4b 0x50) are the HDMI
  //     DDC/EDID bus, not power
  //   * the onboard RP2040 does expose a CDC port (/dev/ttyACM0) but emits
  //     nothing unprompted, and Waveshare documents no protocol, command set
  //     or firmware source for it
  // Waveshare's own docs describe battery state only as four front-panel
  // LEDs driven by the UPS board.
  //
  // A gauge that always reads "--" is worse than no gauge: on a handheld it
  // reads as a flat or failing battery, which is exactly the wrong thing to
  // tell a technician mid-diagnosis. If a future carrier exposes a real
  // supply, reinstate this from /sys/class/power_supply rather than guessing.

  return `
  <div style="height:var(--chrome-top);display:flex;align-items:center;gap:10px;
              padding:0 12px;background:var(--chrome-bg);backdrop-filter:blur(10px);
              border-bottom:1px solid var(--border);">
    <div style="font-size:12px;font-weight:500;letter-spacing:0.4px;">${clock()}</div>
    <div style="font-size:11px;color:var(--fg-mute);">${TITLES[state.screen] || "Tracer"}</div>
    <div style="flex:1"></div>
    ${Object.values(state.modules || {}).some((m) => m && m.busy)
      ? `<div style="color:var(--tc-primary);animation:tcspin 1s linear infinite;
             display:flex;" title="working">${icon("sync-outline", 12)}</div>` : ""}
    <div style="display:flex;align-items:center;gap:5px;color:${netColor};font-size:11px;">
      ${icon("wifi", 14)}<span>${ssid}</span>
    </div>
    <div style="display:flex;align-items:center;gap:5px;color:${hwColor};font-size:11px;">
      ${icon("server", 13)}<span>${hwLabel}</span>
    </div>
  </div>`;
}

// Which legend entries are also BUTTONS you can press with a thumb.
//
// The hint bar used to be a pure legend — it told you Start opens and Select
// goes back, and did nothing when touched. On a touchscreen that is a trap:
// the labels look like controls, so they get tapped, and nothing happening
// reads as a frozen device rather than as a caption.
//
// It matters more than polish here. The buttons hang off an RP2040 that can
// strand itself in BOOTSEL (docs/controls.md), and when it does, touch is the
// only way to drive the unit — including Back, which had no on-screen
// equivalent anywhere.
//
// Entries absent from this map stay captions, because they are not single
// buttons the daemon can be told about: "L/R" names a pair, and Esc / Enter /
// Ctrl+C are keystrokes the terminal consumes in text mode, not nav buttons.
// Rendering those as tappable would be the same lie in the other direction.
const HINT_PRESS = {
  Start: "start", Select: "select",
  A: "a", B: "b", X: "x", Y: "y",
};

export function hintBar(state, override) {
  const hints = override || HINTS[state.screen] || [];
  const items = hints.map(([btn, label]) => {
    const [bg, fg] = BTN_COLORS[btn] || ["#2a2a2a", "#aaa"];
    const press = HINT_PRESS[btn];
    // The whole cell is the hit area, not the pill. The pill is 20px — a
    // 2mm target on this panel — so hanging the tap off it would be a
    // touch affordance you cannot reliably touch. Padding is what makes
    // the target, so it is on the tappable element itself.
    return `
      <div ${press ? `data-hint="${press}" role="button" tabindex="-1"` : ""}
           style="display:flex;align-items:center;gap:6px;height:100%;
                  padding:0 8px;border-radius:var(--r-btn);
                  ${press ? "" : "opacity:0.75;"}">
        <div style="height:20px;min-width:20px;padding:${btn.length > 2 ? "0 7px" : "0"};
                    border-radius:var(--r-full);background:${bg};
                    color:${fg};font-size:11px;font-weight:700;display:flex;
                    align-items:center;justify-content:center;">${btn}</div>
        <div style="font-size:11px;color:var(--fg-dim);white-space:nowrap;">${label}</div>
      </div>`;
  }).join("");

  return `
  <div style="position:absolute;left:0;right:0;bottom:0;height:var(--chrome-bottom);
              display:flex;align-items:stretch;gap:4px;padding:6px 8px;
              background:var(--chrome-bg);
              backdrop-filter:blur(10px);border-top:1px solid var(--border);">
    ${items}
  </div>`;
}

export function toastEl(state) {
  if (!state.toast) return "";
  return `
  <div style="position:absolute;left:50%;bottom:calc(var(--chrome-bottom) + 18px);
              transform:translateX(-50%);
              background:var(--bg-2);border:1px solid var(--border);
              border-radius:var(--r-badge);padding:6px 14px;font-size:12px;
              color:var(--fg-dim);z-index:3;">${state.toast}</div>`;
}

// Unmistakable, full-screen, and it stays until the socket is back. Not a
// toast: stale numbers on a diagnostic tool are worse than no numbers.
export function offlineScreen() {
  return `
  <div style="position:absolute;inset:0;z-index:10;display:flex;flex-direction:column;
              align-items:center;justify-content:center;gap:14px;background:var(--bg-0);">
    <div style="color:var(--tc-danger);">${icon("alert-circle", 48)}</div>
    <div style="font-size:20px;font-weight:500;">Daemon offline</div>
    <div style="font-size:12px;color:var(--fg-mute);font-family:var(--mono);">
      reconnecting to 127.0.0.1:8710
    </div>
  </div>`;
}
