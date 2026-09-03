// Network app. Layout from the design mock (v2.dc.html:249-276).

import { icon } from "../icons.js";
import { spinner } from "../chrome/chrome.js";

const dash = (v) => (v === null || v === undefined || v === "" ? "--" : v);

export function networkScreen(state) {
  const mod = state.modules.net;
  const ok = mod && mod.state === "ok";
  const d = ok ? mod.data : {};

  const upLabel = ok ? `${d.iface || "wlan0"} up` : (mod ? mod.reason : "--");
  const upColor = ok ? "var(--tc-success)" : "var(--fg-mute)";

  // Signal is real dBm from /proc/net/wireless. If the driver did not report
  // it we show `--` rather than relabelling nmcli's 0-100 quality as dBm.
  const sig = d.signal_dbm;
  const stats = [
    { label: "SSID",    value: dash(d.ssid),    color: "var(--fg)" },
    { label: "IP",      value: dash(d.ip),      color: "var(--fg)" },
    { label: "Signal",  value: sig === null || sig === undefined ? "--" : `${sig} dBm`,
      color: sig === null || sig === undefined ? "var(--fg-mute)"
           : sig > -60 ? "var(--tc-success)" : sig > -75 ? "var(--tc-warning)" : "var(--tc-danger)" },
    { label: "Gateway", value: dash(d.gateway), color: "var(--fg)" },
  ];

  const tiles = stats.map((n) => `
    <div style="padding:10px;background:var(--bg-1);border:1px solid var(--border);
                border-radius:10px;min-width:0;">
      <div style="font-size:10px;color:var(--fg-mute);text-transform:uppercase;
                  letter-spacing:0.5px;">${n.label}</div>
      <div style="font-size:15px;margin-top:3px;color:${n.color};
                  font-family:var(--mono);overflow:hidden;text-overflow:ellipsis;
                  white-space:nowrap;">${n.value}</div>
    </div>`).join("");

  const checks = (d.checks || []);
  const rows = checks.length
    ? checks.map((c, i) => {
        const on = i === state.row;
        return `
        <div class="net-row" data-idx="${i}"
             style="display:flex;align-items:center;gap:11px;padding:9px 12px;
                    background:${on ? "var(--bg-2)" : "var(--bg-1)"};
                    border:2px solid ${on ? "var(--focus-border)" : "var(--border)"};
                    box-shadow:${on ? "var(--glow)" : "none"};
                    border-radius:10px;">
          <div style="color:${c.ok ? "var(--tc-success)" : "var(--tc-danger)"};">
            ${icon(c.ok ? "checkmark-circle" : "alert-circle", 17)}
          </div>
          <div style="flex:1;font-size:12px;min-width:0;overflow:hidden;
                      text-overflow:ellipsis;white-space:nowrap;">${c.name}</div>
          <div style="font-size:11px;color:var(--fg-mute);font-family:var(--mono);">${c.detail}</div>
          <div style="font-size:11px;color:${c.ok ? "var(--tc-success)" : "var(--tc-danger)"};">${c.result}</div>
        </div>`;
      }).join("")
    : `<div style="flex:1;display:flex;flex-direction:column;align-items:center;
                   justify-content:center;gap:8px;">
         <div style="color:var(--tc-primary);animation:tcspin 1s linear infinite;
                     display:flex;">${icon("sync-outline", 24)}</div>
         <div style="color:var(--fg-dim);font-size:12px;">Running checks…</div>
       </div>`;

  return `
  <div style="height:420px;padding:12px 14px 16px;display:flex;flex-direction:column;">
    <div style="display:flex;align-items:center;gap:8px;">
      <div style="font-size:15px;font-weight:500;">Network</div>
      <div style="flex:1"></div>
      ${mod && mod.busy ? spinner(13) : ""}
      <div style="font-size:11px;color:${upColor};">${upLabel}</div>
    </div>
    <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:12px;">
      ${tiles}
    </div>
    <div style="font-size:10px;color:var(--fg-mute);letter-spacing:1px;
                text-transform:uppercase;margin:14px 0 8px;">Reachability</div>
    <div style="flex:1;display:flex;flex-direction:column;gap:7px;
                justify-content:space-between;">${rows}</div>
  </div>`;
}

export function networkRows(state) {
  const mod = state.modules.net;
  return (mod && mod.state === "ok" && mod.data.checks) ? mod.data.checks.length : 0;
}
