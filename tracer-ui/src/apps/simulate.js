// Simulate — emulate any module's CAN traffic.
//
// Three levels: module -> frame -> field form. Everything shown is generated
// from the fleet DBC by the daemon, so this file never hardcodes a signal
// name, range or unit.

import { icon } from "../icons.js";

const esc = (s) => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;");

export const simUi = {
  level: "modules",     // modules | frames | form
  module: 0,
  frame: 0,
  row: 0,
  current: null,        // frame detail from the daemon
  values: {},
  direction: null,
};

export function simReset() {
  simUi.level = "modules"; simUi.module = 0; simUi.frame = 0;
  simUi.row = 0; simUi.current = null; simUi.values = {}; simUi.direction = null;
}

function modules(s) {
  const m = s.modules.simulate;
  return (m && m.data && m.data.modules) || [];
}

const rowIdx = (on, idx, inner) => row(on, inner, idx);

function row(on, inner, idx) {
  return `<div data-idx="${idx}" style="display:flex;align-items:center;gap:11px;padding:9px 12px;
     background:${on ? "var(--bg-2)" : "var(--bg-1)"};
     border:2px solid ${on ? "var(--focus-border)" : "var(--border)"};
     box-shadow:${on ? "var(--glow)" : "none"};border-radius:10px;">${inner}</div>`;
}

// Inbound and outbound are very different acts; the badge says which.
function dirBadge(dir) {
  const out = dir === "outbound";
  const c = out ? "var(--tc-danger)" : "var(--tc-info)";
  return `<div style="font-size:10px;color:${c};border:1px solid ${c};
     border-radius:var(--r-badge);padding:1px 7px;white-space:nowrap;">
     ${out ? "TO BUS" : "EMULATE"}</div>`;
}

export function simulateScreen(s) {
  const mods = modules(s);
  const d = (s.modules.simulate && s.modules.simulate.data) || {};

  if (!mods.length) {
    return `<div style="height:var(--body-h);display:flex;align-items:center;
       justify-content:center;color:var(--fg-mute);font-size:12px;">
       Loading CAN database…</div>`;
  }

  // ── level 3: the field form ──
  if (simUi.level === "form" && simUi.current) {
    const f = simUi.current;
    const dir = simUi.direction || f.direction;
    const fields = f.signals.map((sig, i) => {
      const on = i === simUi.row;
      const v = simUi.values[sig.name];
      const choice = sig.choices && sig.choices.find((c) => c.value === Number(v));
      const shown = choice ? `${choice.label} (${v})` : (v ?? "--");
      return rowIdx(on, i, `
        <div style="flex:1;min-width:0;">
          <div style="font-size:12px;">${esc(sig.name)}</div>
          <div style="font-size:10px;color:var(--fg-mute);">
            ${sig.length}-bit${sig.signed ? " signed" : ""}
            ${sig.min !== null ? ` · ${sig.min} to ${sig.max}` : ""}
            ${sig.unit ? ` · ${esc(sig.unit)}` : ""}
            ${sig.choices ? ` · ${sig.choices.length} options` : ""}
          </div>
        </div>
        <div style="font-size:13px;font-family:var(--mono);
                    color:${on ? "var(--fg)" : "var(--fg-dim)"};
                    max-width:180px;overflow:hidden;text-overflow:ellipsis;
                    white-space:nowrap;">${esc(shown)}</div>`);
    }).join("");

    return `
    <div style="height:var(--body-h);padding:12px 14px;display:flex;flex-direction:column;">
      <div style="display:flex;align-items:center;gap:8px;">
        <div style="font-size:15px;font-weight:500;">${esc(f.name)}</div>
        <div style="font-size:11px;color:var(--fg-mute);font-family:var(--mono);">${f.hex}</div>
        <div style="flex:1"></div>
        ${dirBadge(dir)}
      </div>
      <div style="font-size:10px;color:var(--fg-mute);margin-top:3px;line-height:14px;">
        ${dir === "outbound"
          ? "Publishes to can/outbound — this goes on the REAL bus and physical modules will act on it."
          : "Publishes to can/inbound — Headwaters decodes it as if a real module sent it. Nothing reaches the bus."}
      </div>
      <div data-scroll-clip style="flex:1;margin-top:8px;overflow:hidden;">
        <div data-scroll-list style="display:flex;flex-direction:column;gap:5px;">${fields}</div>
      </div>
    </div>`;
  }

  // ── level 2: frames for a module ──
  if (simUi.level === "frames") {
    const mod = mods[simUi.module];
    if (!mod) { simReset(); return simulateScreen(s); }
    const rows = mod.frames.map((f, i) => row(i === simUi.row, `
      <div style="font-family:var(--mono);font-size:11px;color:var(--tc-primary-light);
                  width:46px;flex-shrink:0;">${f.hex}</div>
      <div style="flex:1;min-width:0;">
        <div style="font-size:12px;overflow:hidden;text-overflow:ellipsis;
                    white-space:nowrap;">${esc(f.name)}</div>
        <div style="font-size:10px;color:var(--fg-mute);">
          ${f.signals} field${f.signals === 1 ? "" : "s"} · ${f.dlc} bytes</div>
      </div>
      ${dirBadge(f.direction)}`, i)).join("");
    return `
    <div style="height:var(--body-h);padding:12px 14px;display:flex;flex-direction:column;">
      <div style="display:flex;align-items:center;gap:8px;">
        <div style="font-size:15px;font-weight:500;">${esc(mod.label)}</div>
        <div style="flex:1"></div>
        <div style="font-size:11px;color:var(--fg-mute);">${mod.count} frames</div>
      </div>
      <div data-scroll-clip style="flex:1;margin-top:10px;overflow:hidden;">
        <div data-scroll-list style="display:flex;flex-direction:column;gap:5px;">${rows}</div>
      </div>
    </div>`;
  }

  // ── level 1: modules ──
  const rows = mods.map((mod, i) => row(i === simUi.row, `
    <div style="flex:1;">
      <div style="font-size:12px;">${esc(mod.label)}</div>
      <div style="font-size:10px;color:var(--fg-mute);">${mod.count} frames</div>
    </div>
    <div style="color:var(--fg-mute);">${icon("chevron-forward", 15)}</div>`, i)).join("");

  return `
  <div style="height:var(--body-h);padding:12px 14px;display:flex;flex-direction:column;">
    <div style="display:flex;align-items:center;gap:8px;">
      <div style="font-size:15px;font-weight:500;">Simulate</div>
      <div style="font-size:11px;color:var(--fg-mute);">
        ${d.total_frames} frames from the fleet DBC</div>
      <div style="flex:1"></div>
      ${d.connected ? "" : `<div style="font-size:10px;color:var(--tc-warning);
         border:1px solid var(--tc-warning);border-radius:var(--r-badge);
         padding:1px 7px;">NO BROKER</div>`}
    </div>
    <div style="font-size:10px;color:var(--fg-mute);margin-top:3px;">
      Emulate a module that is not fitted, or send deliberately wrong values.
    </div>
    <div data-scroll-clip style="flex:1;margin-top:8px;overflow:hidden;">
      <div data-scroll-list style="display:flex;flex-direction:column;gap:5px;">${rows}</div>
    </div>
  </div>`;
}

export function simulateRows(s) {
  const mods = modules(s);
  if (simUi.level === "form") return (simUi.current?.signals || []).length;
  if (simUi.level === "frames") return (mods[simUi.module]?.frames || []).length;
  return mods.length;
}

export function simulateHints(s) {
  if (simUi.level === "form") {
    const dir = simUi.direction || simUi.current?.direction;
    return [["Start", "Edit"], ["Select", "Back"],
            ["X", dir === "outbound" ? "→ Emulate" : "→ To bus"], ["Y", "Send"]];
  }
  return [["Start", "Open"], ["Select", "Back"]];
}
