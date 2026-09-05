// Device Discovery. Layout from the design mock (v2.dc.html:133-160).
//
// Every action here is a REQUEST TO HEADWATERS, not something Tracer does
// itself. Headwaters browses and Headwaters onboards; Tracer shows the result
// and asks. The screen says so, because an operator needs to know which box
// is actually doing the work when it goes wrong.

import { icon } from "../icons.js";
import { spinner } from "../chrome/chrome.js";

const TYPE_ICON = {
  bearing: "location", solstice: "pulse", tapper: "swap-vertical",
  playbill: "server", spoor: "scan", torrent: "pulse",
  switchback: "swap-vertical", picket: "scan", borealis: "pulse",
};
const TYPE_TINT = {
  bearing: "#48E6FE", solstice: "#FFC107", tapper: "#7BC96A",
  playbill: "#52A441", spoor: "#4a4a4a",
};

const esc = (s) => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;");

function meta(d) {
  const bits = [];
  if (d.fw) bits.push(`fw ${d.fw}`);
  if (d.addr !== undefined && d.addr !== null) bits.push(`addr ${d.addr}`);
  if (d.canid) bits.push(`canid ${d.canid}`);
  if (d.deviceId) bits.push(`deviceId ${d.deviceId}`);
  if (d.canInstance) bits.push(`canInstance ${d.canInstance}`);
  if (d.target) bits.push(`target ${d.target}`);
  bits.push(d.onboard === "claim" ? "claim" : "confirm");
  return bits.join(" · ");
}

function stateChip(d) {
  switch (d.state) {
    case "onboarded": return { label: "Onboarded", colour: "var(--tc-success)" };
    case "pending":   return { label: "Asking…",   colour: "var(--tc-warning)" };
    case "failed":    return { label: "Failed",    colour: "var(--tc-danger)" };
    default:
      return d.onboard === "claim"
        ? { label: "Claim", colour: "var(--tc-info)" }
        : { label: "Confirm", colour: "var(--fg-dim)" };
  }
}

export function discoveryScreen(s) {
  const m = s.modules.discovery;
  const d = (m && m.data) || {};
  const devices = d.devices || [];
  const degraded = m && m.state === "degraded";

  const sweep = d.browsing
    ? `<div style="color:var(--tc-primary);animation:tcspin 1s linear infinite;">
         ${icon("sync-outline", 14)}</div>
       <div style="font-size:11px;color:var(--fg-dim);">${d.browse_remaining}s left</div>`
    : `<div style="font-size:11px;color:var(--fg-mute);">X to scan</div>`;

  const rows = devices.length ? devices.map((dev, i) => {
    const on = i === s.row;
    const chip = stateChip(dev);
    const tint = TYPE_TINT[dev.type] || "#4a4a4a";
    return `
    <div class="disco-row" data-idx="${i}"
         style="display:flex;align-items:center;gap:12px;padding:10px 12px;
                background:${on ? "var(--bg-2)" : "var(--bg-1)"};
                border:2px solid ${on ? "var(--focus-border)" : "var(--border)"};
                box-shadow:${on ? "var(--glow)" : "none"};
                border-radius:10px;">
      <div style="width:36px;height:36px;border-radius:9px;background:${tint};
                  display:flex;align-items:center;justify-content:center;flex-shrink:0;">
        ${icon(TYPE_ICON[dev.type] || "scan", 20, "#000")}
      </div>
      <div style="flex:1;min-width:0;">
        <div style="display:flex;align-items:baseline;gap:7px;">
          <div style="font-family:var(--mono);font-size:13px;font-weight:500;">${esc(dev.hostname)}</div>
          <div style="font-size:11px;color:var(--fg-dim);">${esc(dev.type)}</div>
        </div>
        <div style="font-size:11px;color:var(--fg-mute);font-family:var(--mono);
                    overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">
          ${esc(dev.detail || meta(dev))}
        </div>
      </div>
      <div style="font-size:11px;color:${chip.colour};border:1px solid ${chip.colour};
                  border-radius:var(--r-badge);padding:2px 9px;flex-shrink:0;">${chip.label}</div>
    </div>`;
  }).join("") : `
    <div style="flex:1;display:flex;flex-direction:column;align-items:center;
                justify-content:center;gap:8px;color:var(--fg-mute);">
      <div style="opacity:.5">${icon("scan", 34)}</div>
      <div style="font-size:13px;">${
        degraded ? "Waiting for the broker"
        : d.scanned ? "Headwaters found no devices"
        : "Nothing discovered yet"}</div>
      <div style="font-size:11px;max-width:440px;text-align:center;line-height:15px;">${
        degraded ? esc(m.reason || "")
        : d.scanned
          ? "The scan completed and no modules answered. Check they are powered, "
            + "on this network, and that discovery-mdns is running on Headwaters."
          : "Press X to ask Headwaters to scan the rig"}</div>
    </div>`;

  return `
  <div style="height:var(--body-h);padding:12px 14px;display:flex;flex-direction:column;">
    <div style="display:flex;align-items:center;gap:8px;">
      <div style="font-size:15px;font-weight:500;">Device Discovery</div>
      <div style="font-size:11px;color:var(--fg-mute);font-family:var(--mono);">
        via Headwaters
      </div>
      <div style="flex:1"></div>
      ${m && m.busy ? spinner(13, m.busy_note) : ""}
      ${sweep}
    </div>
    <div data-scroll-clip style="margin-top:12px;flex:1;overflow:hidden;">
      <div data-scroll-list style="display:flex;flex-direction:column;gap:8px;
           transition:transform var(--t-fast);">${rows}</div>
    </div>
  </div>`;
}

export function discoveryRows(s) {
  const m = s.modules.discovery;
  return (m && m.data && m.data.devices) ? m.data.devices.length : 0;
}

export function discoveryHints(s) {
  const m = s.modules.discovery;
  const d = (m && m.data) || {};
  const dev = (d.devices || [])[s.row];
  const act = dev && dev.state === "onboarded" ? "Onboarded"
            : dev && dev.onboard === "claim" ? "Claim" : "Confirm";
  return [["Start", act], ["Select", "Back"],
          ["X", d.browsing ? "Stop" : "Scan"], ["Y", "Details"]];
}
