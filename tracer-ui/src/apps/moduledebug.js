// Module Debug — a raw USB serial console for ANY attached board.
//
// Generic by design: ESP32, Arduino, Teensy, Pico, an unbranded CH340 clone.
// Nothing here assumes a particular firmware or output format.
//
// Rewritten to match the Terminal app's model, which has been stable. Two
// screens, one job each:
//
//   ports    pick a device (Start connects, L/R sets that device's baud)
//   console  raw bytes in and out; the keyboard is LIVE the whole time
//
// Typing is not a mode. The previous version had a Start-to-type toggle, and
// its commonest failure was sitting in the console pressing keys with nothing
// happening because the toggle was off — indistinguishable from a dead link.
// While connected, every key goes to the module and Select is the way out;
// Select carries no character, so it is the one button that can always mean
// "leave", even mid-command.
//
// Nothing is filtered, classified or reordered. Only ANSI escapes are removed,
// because the GUI renders HTML and they would otherwise appear as "[0;32m".

import { icon } from "../icons.js";
import { spinner, loading } from "../chrome/chrome.js";

export const dbgUi = {
  pane: "ports",        // ports | console
};

function esc(t) {
  return String(t)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function portRow(p, i, on) {
  return `
  <div data-idx="${i}"
       style="display:flex;align-items:center;gap:10px;padding:8px 11px;
              background:${on ? "var(--bg-2)" : "var(--bg-1)"};
              border:2px solid ${on ? "var(--focus-border)" : "var(--border)"};
              box-shadow:${on ? "var(--glow)" : "none"};border-radius:9px;">
    <div style="color:${p.likely_module ? "var(--tc-success)" : "var(--fg-dim)"};
                display:flex;">${icon("checkmark-circle", 17)}</div>
    <div style="flex:1;min-width:0;">
      <div style="font-size:12px;overflow:hidden;text-overflow:ellipsis;
                  white-space:nowrap;">${esc(p.label)}</div>
      <div style="font-size:10px;color:var(--fg-mute);overflow:hidden;
                  text-overflow:ellipsis;white-space:nowrap;">
        ${p.name} · ${esc(p.transport || "USB serial")}</div>
    </div>
    <div style="text-align:right;flex-shrink:0;">
      <div style="font-size:11px;font-family:var(--mono);
                  color:${on ? "var(--tc-primary)" : "var(--fg-dim)"};">
        ${p.baud_applies === false ? "—" : (p.baud || 115200)}</div>
      <div style="font-size:8px;color:var(--fg-mute);letter-spacing:0.4px;">
        ${p.baud_applies === false ? "USB" : "BAUD"}</div>
    </div>
  </div>`;
}

export function moduleDebugScreen(s) {
  const mod = s.modules.moduledebug;
  if (!mod || mod.state === "starting") return loading("Scanning USB…");
  const d = mod.data || {};

  // Re-attaching after a power-cycle: stay on the console. The scrollback is
  // why the operator is here, and unplugging the cable IS how you reboot this
  // hardware — bouncing to the picker every time would be hostile.
  if (dbgUi.pane === "console" && !d.connected && d.waiting) {
    const kept = (d.lines || []).map((l, i) => `
      <div data-idx="${i}" style="padding:0 5px;font-size:11px;
           font-family:var(--mono);white-space:pre-wrap;word-break:break-all;
           line-height:1.34;opacity:0.65;
           color:${l.level === "meta" ? "var(--tc-primary)" : "var(--fg)"};">
        ${esc(l.text) || "&nbsp;"}</div>`).join("");
    return `
    <div style="height:var(--body-h);padding:9px 11px 34px;display:flex;
                flex-direction:column;">
      <div style="display:flex;align-items:center;gap:7px;">
        <div style="color:var(--tc-warning);display:flex;
                    animation:tcspin 1s linear infinite;">
          ${icon("sync-outline", 13)}</div>
        <div style="font-size:11px;color:var(--tc-warning);flex:1;">
          Waiting for the module to power up…</div>
        <div style="font-size:9px;color:var(--fg-mute);">Select to stop</div>
      </div>
      <div data-scroll-clip style="flex:1;overflow:hidden;margin-top:7px;">
        <div data-scroll-list>${kept}</div>
      </div>
    </div>`;
  }

  // ── port picker ────────────────────────────────────────────────────
  if (dbgUi.pane === "ports" || !d.connected) {
    const ports = d.ports || [];
    const body = ports.length
      ? ports.map((p, i) => portRow(p, i, i === s.row)).join("")
      : `<div style="flex:1;display:flex;flex-direction:column;
                     align-items:center;justify-content:center;gap:9px;">
           <div style="color:var(--fg-mute);">${icon("terminal", 34)}</div>
           <div style="font-size:12px;color:var(--fg-dim);text-align:center;
                       line-height:1.5;">
             No serial devices connected.<br>
             Plug a board into the USB-A port — it appears automatically.
           </div>
         </div>`;

    return `
    <div style="height:var(--body-h);padding:11px 13px 34px;display:flex;
                flex-direction:column;">
      <div style="display:flex;align-items:center;gap:8px;">
        <div style="font-size:15px;font-weight:500;">Module Debug</div>
        <div style="flex:1"></div>
        ${mod.busy ? spinner(12) : ""}
      </div>
      ${d.error ? `
        <div style="margin-top:8px;padding:5px 9px;border-radius:7px;
                    background:rgba(255,84,83,0.14);
                    border:1px solid var(--tc-danger);
                    font-size:11px;color:var(--tc-danger);">${esc(d.error)}</div>`
        : ""}
      <div style="font-size:10px;color:var(--fg-mute);margin:9px 0 7px;
                  letter-spacing:0.8px;text-transform:uppercase;">
        USB serial ports</div>
      <div data-scroll-clip style="flex:1;overflow:hidden;">
        <div data-scroll-list style="display:flex;flex-direction:column;gap:7px;">
          ${body}
        </div>
      </div>
    </div>`;
  }

  // ── console ────────────────────────────────────────────────────────
  const lines = d.lines || [];
  const rows = lines.map((l, i) => `
    <div data-idx="${i}" style="padding:0 5px;font-size:11px;
         font-family:var(--mono);white-space:pre-wrap;word-break:break-all;
         line-height:1.34;
         color:${l.level === "meta" ? "var(--tc-primary)" : "var(--fg)"};">
      ${esc(l.text) || "&nbsp;"}</div>`).join("");

  // The line still arriving: the prompt, and every character echoed back as
  // it is typed. Neither ends in a newline, so without this the console looks
  // dead exactly while it is being used.
  const pending = `
    <div data-idx="${lines.length}"
         style="padding:0 5px;font-size:11px;font-family:var(--mono);
                white-space:pre-wrap;word-break:break-all;line-height:1.34;">
      ${esc(d.partial || "")}<span
        style="background:var(--tc-success);color:#000;">&nbsp;</span></div>`;

  return `
  <div style="height:var(--body-h);padding:9px 11px 34px;display:flex;
              flex-direction:column;">
    <div style="display:flex;align-items:center;gap:7px;">
      <div style="width:7px;height:7px;border-radius:50%;
                  background:var(--tc-success);"></div>
      <div style="font-size:12px;font-family:var(--mono);">
        ${(d.connected || "").replace("/dev/", "")}</div>
      <div style="font-size:10px;color:var(--fg-mute);font-family:var(--mono);">
        ${d.baud}</div>
      <div style="flex:1"></div>
      <div style="font-size:9px;padding:1px 7px;border-radius:var(--r-full);
                  background:var(--tc-success);color:#000;">LIVE</div>
    </div>
    <div data-scroll-clip style="flex:1;overflow:hidden;margin-top:7px;">
      <div data-scroll-list>${rows}${pending}</div>
    </div>
  </div>`;
}

export function moduleDebugRows(s) {
  const d = (s.modules.moduledebug || {}).data || {};
  if (dbgUi.pane === "console" && !d.connected && d.waiting) {
    return (d.lines || []).length;
  }
  if (dbgUi.pane === "ports" || !d.connected) return (d.ports || []).length;
  // +1 for the pending line, so following the cursor reaches the bottom.
  return (d.lines || []).length + 1;
}

export function moduleDebugHints(s) {
  const d = (s.modules.moduledebug || {}).data || {};
  if (dbgUi.pane === "console" && !d.connected && d.waiting) {
    return [["Select", "Stop waiting"]];
  }
  if (dbgUi.pane === "ports" || !d.connected) {
    return [["Start", "Connect"], ["Select", "Back"], ["L/R", "Baud"],
            ["Y", "Rescan"]];
  }
  // Connected: every letter key is going to the module, so only the buttons
  // carrying no character are advertised. Listing bindings the operator would
  // find inert is worse than listing none.
  return [["Select", "Disconnect"], ["Enter", "Send"]];
}
