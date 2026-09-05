// Headwaters system monitor — btop-style, for the rig's own box.
//
// Everything here is read off the substrate over SSH (/proc, df, ps, docker),
// never through the Headwaters API. That is the whole point: when the PWA
// stops answering, this still shows what the machine is actually doing.
// See docs/api.md C0.
//
// LAYOUT — 640x420 between the two 30px chrome bars, and it must not scroll
// as a page. Panes clip internally instead, because a page that scrolls hides
// the fact that there is more to see on a screen with no scrollbars.

import { icon } from "../icons.js";
import { spinner, loading } from "../chrome/chrome.js";

const PANES = ["containers", "procs"];

// Thresholds shared by every bar so colour means one thing across the screen.
function loadColor(pct) {
  if (pct === null || pct === undefined) return "var(--fg-mute)";
  return pct >= 90 ? "var(--tc-danger)"
       : pct >= 70 ? "var(--tc-warning)"
       : "var(--tc-success)";
}

function bytes(n) {
  if (!n && n !== 0) return "--";
  const u = ["B", "K", "M", "G", "T"];
  let i = 0, v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v >= 100 || i === 0 ? Math.round(v) : v.toFixed(1)}${u[i]}`;
}

function duration(sec) {
  if (sec === null || sec === undefined) return "--";
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
}

// A bar with the value written on it. btop draws braille gradients; at this
// size and DPI a solid fill with the number beside it reads far better.
function bar(pct, w, h = 9) {
  const p = Math.max(0, Math.min(100, pct ?? 0));
  const known = pct !== null && pct !== undefined;
  return `
    <div style="width:${w}px;height:${h}px;border-radius:3px;
                background:var(--bg-2);overflow:hidden;flex-shrink:0;">
      <div style="width:${known ? p : 0}%;height:100%;
                  background:${loadColor(pct)};"></div>
    </div>`;
}

function statTile(label, value, color) {
  return `
    <div style="flex:1;min-width:0;padding:6px 8px;background:var(--bg-1);
                border:1px solid var(--border);border-radius:8px;">
      <div style="font-size:9px;color:var(--fg-mute);text-transform:uppercase;
                  letter-spacing:0.5px;">${label}</div>
      <div style="font-size:14px;font-family:var(--mono);margin-top:2px;
                  color:${color || "var(--fg)"};white-space:nowrap;
                  overflow:hidden;text-overflow:ellipsis;">${value}</div>
    </div>`;
}

export function headwatersScreen(s) {
  const mod = s.modules.headwaters;
  if (!mod || mod.state === "starting") return loading("Reaching Headwaters…");
  if (mod.state !== "ok") {
    return `
    <div style="height:var(--body-h);display:flex;flex-direction:column;
                align-items:center;justify-content:center;gap:10px;padding:24px;">
      <div style="color:var(--tc-danger);">${icon("alert-circle", 44)}</div>
      <div style="font-size:15px;">Headwaters unreachable</div>
      <div style="font-size:12px;color:var(--fg-dim);text-align:center;">
        ${mod.reason || "--"}
      </div>
    </div>`;
  }

  const d = mod.data || {};
  const m = d.metrics;

  // Reachable but no SSH: say what to do about it rather than showing an
  // empty dashboard that looks broken.
  if (!m) {
    return `
    <div style="height:var(--body-h);display:flex;flex-direction:column;
                align-items:center;justify-content:center;gap:10px;padding:30px;">
      <div style="color:var(--tc-warning);">${icon("server", 40)}</div>
      <div style="font-size:14px;">${d.host || "Headwaters"} is reachable</div>
      <div style="font-size:12px;color:var(--fg-dim);text-align:center;
                  line-height:1.5;">${d.note || "No system stats available."}</div>
    </div>`;
  }

  const ui = s.hwUi || { pane: "containers" };
  const cores = m.cpu_cores || [];
  const containers = m.containers || [];
  const procs = m.procs || [];

  // ── clock skew banner ──────────────────────────────────────────────
  // Deliberately the first thing on the screen. A wrong clock on the rig
  // invalidates TLS and makes every timestamp below untrustworthy, so it
  // outranks any utilisation number.
  const skew = m.clock_warning
    ? `<div style="display:flex;align-items:center;gap:8px;padding:6px 9px;
                   background:rgba(255,84,83,0.14);border:1px solid var(--tc-danger);
                   border-radius:8px;margin-bottom:8px;">
         <div style="color:var(--tc-danger);display:flex;">${icon("alert-circle", 15)}</div>
         <div style="font-size:11px;color:var(--tc-danger);flex:1;min-width:0;
                     overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">
           ${m.clock_warning} — TLS and log timestamps unreliable
         </div>
       </div>`
    : "";

  // ── CPU ────────────────────────────────────────────────────────────
  const coreBars = cores.map((c, i) => `
    <div style="display:flex;align-items:center;gap:5px;">
      <div style="font-size:9px;color:var(--fg-mute);font-family:var(--mono);
                  width:22px;">c${i}</div>
      ${bar(c, 96, 7)}
      <div style="font-size:9px;font-family:var(--mono);width:30px;
                  color:${loadColor(c)};text-align:right;">${c.toFixed(0)}%</div>
    </div>`).join("");

  const cpuTotal = m.cpu_total;
  const cpuBlock = `
    <div style="flex:1;min-width:0;padding:8px 9px;background:var(--bg-1);
                border:1px solid var(--border);border-radius:9px;">
      <div style="display:flex;align-items:baseline;gap:6px;">
        <div style="font-size:9px;color:var(--fg-mute);text-transform:uppercase;
                    letter-spacing:0.6px;">CPU</div>
        <div style="font-size:17px;font-family:var(--mono);
                    color:${loadColor(cpuTotal)};">
          ${cpuTotal === null || cpuTotal === undefined
            ? "--" : cpuTotal.toFixed(1) + "%"}</div>
        <div style="flex:1"></div>
        <div style="font-size:9px;color:var(--fg-mute);font-family:var(--mono);">
          ${(m.load || []).map((l) => l.toFixed(2)).join("  ") || "--"}</div>
      </div>
      <div style="display:flex;flex-direction:column;gap:3px;margin-top:6px;">
        ${coreBars || `<div style="font-size:10px;color:var(--fg-mute);">
           measuring…</div>`}
      </div>
    </div>`;

  // ── memory + disks ─────────────────────────────────────────────────
  const memPct = m.mem_percent;
  const swapPct = m.swap_total
    ? Math.round(100 * (m.swap_used || 0) / m.swap_total) : null;

  const diskRows = (m.disks || []).slice(0, 3).map((dk) => `
    <div style="display:flex;align-items:center;gap:5px;">
      <div style="font-size:9px;color:var(--fg-dim);width:74px;overflow:hidden;
                  text-overflow:ellipsis;white-space:nowrap;
                  font-family:var(--mono);">${dk.mount}</div>
      ${bar(dk.percent, 74, 7)}
      <div style="font-size:9px;font-family:var(--mono);width:34px;
                  text-align:right;color:${loadColor(dk.percent)};">
        ${dk.percent.toFixed(0)}%</div>
    </div>`).join("");

  const memBlock = `
    <div style="flex:1;min-width:0;padding:8px 9px;background:var(--bg-1);
                border:1px solid var(--border);border-radius:9px;">
      <div style="display:flex;align-items:center;gap:6px;">
        <div style="font-size:9px;color:var(--fg-mute);text-transform:uppercase;
                    letter-spacing:0.6px;width:34px;">MEM</div>
        ${bar(memPct, 96, 8)}
        <div style="font-size:10px;font-family:var(--mono);
                    color:${loadColor(memPct)};">
          ${bytes(m.mem_used)}/${bytes(m.mem_total)}</div>
      </div>
      ${m.swap_total ? `
      <div style="display:flex;align-items:center;gap:6px;margin-top:4px;">
        <div style="font-size:9px;color:var(--fg-mute);text-transform:uppercase;
                    letter-spacing:0.6px;width:34px;">SWAP</div>
        ${bar(swapPct, 96, 8)}
        <div style="font-size:10px;font-family:var(--mono);color:var(--fg-dim);">
          ${bytes(m.swap_used)}</div>
      </div>` : ""}
      <div style="display:flex;flex-direction:column;gap:3px;margin-top:6px;">
        ${diskRows}
      </div>
    </div>`;

  // ── containers ─────────────────────────────────────────────────────
  const focused = ui.pane === "containers";
  const cRows = containers.length ? containers.map((c, i) => {
    const on = focused && i === s.row;
    const restarted = typeof c.restarts === "number" && c.restarts > 0;
    return `
    <div data-idx="${i}" data-pane="containers"
         style="display:flex;align-items:center;gap:7px;padding:4px 7px;
                background:${on ? "var(--bg-2)" : "transparent"};
                border-left:2px solid ${on ? "var(--focus-border)" : "transparent"};
                border-radius:4px;">
      <div style="width:6px;height:6px;border-radius:50%;flex-shrink:0;
                  background:${c.up ? "var(--tc-success)" : "var(--tc-danger)"};"></div>
      <div style="flex:1;min-width:0;font-size:10px;overflow:hidden;
                  text-overflow:ellipsis;white-space:nowrap;">
        ${c.name.replace(/^trailcurrent-/, "")}</div>
      <div style="font-size:9px;font-family:var(--mono);color:var(--fg-mute);
                  width:44px;text-align:right;">${duration(c.up_seconds)}</div>
      <div style="font-size:9px;font-family:var(--mono);width:26px;
                  text-align:right;
                  color:${restarted ? "var(--tc-warning)" : "var(--fg-mute)"};">
        ${typeof c.restarts === "number" ? "↻" + c.restarts : "--"}</div>
    </div>`;
  }).join("")
    : `<div style="font-size:10px;color:var(--fg-mute);padding:6px 7px;">
         No containers reported — docker may not be installed on this rig.
       </div>`;

  // ── processes ──────────────────────────────────────────────────────
  const pFocused = ui.pane === "procs";
  const pRows = procs.map((p, i) => {
    const on = pFocused && i === s.row;
    return `
    <div data-idx="${i}" data-pane="procs"
         style="display:flex;align-items:center;gap:6px;padding:3px 7px;
                background:${on ? "var(--bg-2)" : "transparent"};
                border-left:2px solid ${on ? "var(--focus-border)" : "transparent"};
                border-radius:4px;">
      <div style="font-size:9px;font-family:var(--mono);color:var(--fg-mute);
                  width:44px;">${p.pid}</div>
      <div style="flex:1;min-width:0;font-size:10px;overflow:hidden;
                  text-overflow:ellipsis;white-space:nowrap;">${p.name}</div>
      <div style="font-size:9px;font-family:var(--mono);width:34px;
                  text-align:right;color:${loadColor(p.cpu)};">
        ${p.cpu.toFixed(1)}</div>
      <div style="font-size:9px;font-family:var(--mono);width:40px;
                  text-align:right;color:var(--fg-dim);">${bytes(p.rss)}</div>
    </div>`;
  }).join("");

  const paneHead = (title, count, active) => `
    <div style="display:flex;align-items:center;gap:6px;padding:0 7px 3px;">
      <div style="font-size:9px;text-transform:uppercase;letter-spacing:0.7px;
                  color:${active ? "var(--tc-primary)" : "var(--fg-mute)"};">
        ${title}</div>
      <div style="font-size:9px;color:var(--fg-mute);font-family:var(--mono);">
        ${count}</div>
    </div>`;

  return `
  <div style="height:var(--body-h);padding:8px 10px 34px;display:flex;
              flex-direction:column;gap:7px;overflow:hidden;">
    ${skew}
    <div style="display:flex;align-items:center;gap:8px;">
      <div style="font-size:13px;font-weight:500;">${d.host || "Headwaters"}</div>
      <div style="font-size:10px;color:var(--fg-mute);font-family:var(--mono);">
        up ${duration(m.uptime)}</div>
      <div style="flex:1"></div>
      ${mod.busy ? spinner(11) : ""}
      <div style="font-size:10px;font-family:var(--mono);
                  color:${loadColor(m.temp_c ? m.temp_c : null)};">
        ${m.temp_c ? m.temp_c.toFixed(1) + "°C" : "--"}</div>
    </div>

    <div style="display:flex;gap:7px;">${cpuBlock}${memBlock}</div>

    <div style="flex:1;display:flex;gap:7px;min-height:0;">
      <div style="flex:1;min-width:0;display:flex;flex-direction:column;
                  background:var(--bg-1);border:1px solid ${focused
                    ? "var(--focus-border)" : "var(--border)"};
                  border-radius:9px;padding:6px 0 4px;overflow:hidden;">
        ${paneHead("Containers", `${m.healthy ?? 0}/${m.total ?? 0}`, focused)}
        <div ${focused ? "data-scroll-clip" : ""} style="flex:1;overflow:hidden;">
          <div ${focused ? "data-scroll-list" : ""}>${cRows}</div>
        </div>
      </div>
      <div style="flex:1;min-width:0;display:flex;flex-direction:column;
                  background:var(--bg-1);border:1px solid ${pFocused
                    ? "var(--focus-border)" : "var(--border)"};
                  border-radius:9px;padding:6px 0 4px;overflow:hidden;">
        ${paneHead("Processes", `${procs.length}`, pFocused)}
        <div ${pFocused ? "data-scroll-clip" : ""} style="flex:1;overflow:hidden;">
          <div ${pFocused ? "data-scroll-list" : ""}>${pRows}</div>
        </div>
      </div>
    </div>
  </div>`;
}

export function headwatersRows(s) {
  const m = ((s.modules.headwaters || {}).data || {}).metrics;
  if (!m) return 0;
  const ui = s.hwUi || { pane: "containers" };
  return ui.pane === "containers"
    ? (m.containers || []).length
    : (m.procs || []).length;
}

export function headwatersHints(s) {
  const ui = s.hwUi || { pane: "containers" };
  return [
    ["Select", "Back"],
    ["X", ui.pane === "containers" ? "Processes" : "Containers"],
    ["Y", "Refresh"],
  ];
}

export { PANES as headwatersPanes };
