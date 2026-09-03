// Logs. Layout from the design mock (v2.dc.html:293-315): time, level, unit,
// text — with a source selector, because a bridge failure and a container
// failure look nothing alike and live in different places.

import { icon } from "../icons.js";
import { spinner, loading } from "../chrome/chrome.js";

const esc = (s) => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;");

const LEVEL_COLOUR = {
  ERR: "var(--tc-danger)", WARN: "var(--tc-warning)",
  INFO: "var(--tc-primary)", DEBUG: "var(--fg-off)",
};

function hhmmss(ts) {
  if (!ts) return "--:--:--";
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

export function logsScreen(s) {
  const m = s.modules.logs;
  const d = (m && m.data) || {};
  const lines = d.lines || [];
  const failed = d.failed_units || [];

  const srcLabel = d.source === "local" ? "Tracer"
                 : d.source === "host" ? "Headwaters host"
                 : "Headwaters container";

  // Show the count on each chip. Filtering cantomqtt to ERR yields an empty
  // list because it genuinely has no errors — without the counts that is
  // indistinguishable from a broken filter, which is exactly how it read.
  const counts = {
    ALL: d.total ?? 0,
    WARN: (d.warnings ?? 0) + (d.errors ?? 0),   // WARN means "warn and above"
    ERR: d.errors ?? 0,
  };
  const chips = ["ALL", "WARN", "ERR"].map((lv) => {
    const on = d.level === lv;
    const n = counts[lv];
    const dim = n === 0 && !on;
    return `<div style="font-size:11px;padding:2px 9px;border-radius:var(--r-badge);
      display:flex;align-items:center;gap:5px;
      border:1px solid ${on ? "var(--tc-primary-light)" : "var(--border)"};
      color:${on ? "var(--tc-primary-light)" : dim ? "var(--fg-off)" : "var(--fg-mute)"};">
      <span>${lv}</span>
      <span style="font-family:var(--mono);opacity:.8;">${n}</span>
    </div>`;
  }).join("");

  // A failed unit is usually the answer, and it is the one thing that will
  // never appear in whichever log you happened to open. Put it on top.
  const failedBanner = failed.length ? `
    <div style="margin-top:8px;padding:6px 10px;border-radius:8px;
                background:var(--bg-1);border:1px solid var(--tc-danger);
                display:flex;align-items:center;gap:8px;">
      <div style="color:var(--tc-danger);">${icon("alert-circle", 14)}</div>
      <div style="font-size:11px;color:var(--tc-danger);font-family:var(--mono);">
        systemd reports failed: ${esc(failed.join(", "))}
      </div>
    </div>` : "";

  const busy = m && m.busy;
  // Only show the error pane when there is genuinely nothing to fall back on.
  // If we still hold lines, keep showing them and mark them stale — throwing
  // away readable output because one refresh failed is the worse trade.
  const body = (busy && !lines.length) ? loading(m.busy_note || "Reading logs…")
  : (d.error && !lines.length) ? `
    <div style="flex:1;display:flex;flex-direction:column;align-items:center;
                justify-content:center;gap:8px;color:var(--fg-mute);">
      <div style="color:var(--tc-warning);">${icon("alert-circle", 30)}</div>
      <div style="font-size:13px;color:var(--fg);">Could not read ${esc(srcLabel)}</div>
      <div style="font-size:11px;font-family:var(--mono);">${esc(d.error)}</div>
    </div>`
  : lines.length ? lines.map((l, i) => {
      const on = i === s.row;
      return `
      <div data-idx="${i}" style="display:flex;gap:8px;padding:2px 0;
                  background:${on ? "var(--bg-2)" : "transparent"};">
        <div style="color:var(--fg-mute);flex-shrink:0;">${hhmmss(l.ts)}</div>
        <div style="color:${LEVEL_COLOUR[l.level] || "var(--fg-dim)"};
                    width:40px;flex-shrink:0;">${l.level}</div>
        <div style="color:var(--fg-dim);width:104px;flex-shrink:0;overflow:hidden;
                    text-overflow:ellipsis;white-space:nowrap;">${esc(l.unit)}</div>
        <div style="color:${on ? "var(--fg)" : "var(--fg-dim)"};flex:1;
                    overflow:hidden;text-overflow:ellipsis;
                    white-space:${on ? "normal" : "nowrap"};">${esc(l.text)}</div>
      </div>`;
    }).join("")
  : `<div style="display:flex;flex-direction:column;align-items:center;
                 justify-content:center;gap:6px;color:var(--fg-mute);
                 font-size:12px;padding:40px 12px;text-align:center;">
      <div>${d.query
        ? `Nothing matches "${esc(d.query)}"`
        : d.level === "ALL"
          ? `No log lines from ${esc(d.unit || "this source")}`
          : `No ${esc(d.level)} lines in ${esc(d.unit || "this source")}`}</div>
      ${d.level !== "ALL" && (d.total ?? 0) > 0
        ? `<div style="font-size:11px;">${d.total} line${d.total === 1 ? "" : "s"} at
             other levels — press X for ALL</div>` : ""}
    </div>`;

  return `
  <div style="height:420px;padding:12px 14px;display:flex;flex-direction:column;">
    <div style="display:flex;align-items:center;gap:8px;">
      <div style="font-size:15px;font-weight:500;">Logs</div>
      <div style="font-size:11px;color:var(--fg-mute);font-family:var(--mono);">
        ${esc(srcLabel)} · ${esc(d.unit || "--")}
      </div>
      <div style="flex:1"></div>
      ${busy ? spinner(13) : ""}
      ${chips}
    </div>
    ${failedBanner}
    <div style="display:flex;gap:10px;margin-top:6px;font-size:10px;color:var(--fg-mute);">
      <div>${d.shown ?? 0} of ${d.total ?? 0} lines</div>
      ${d.errors ? `<div style="color:var(--tc-danger);">${d.errors} errors</div>` : ""}
      ${d.warnings ? `<div style="color:var(--tc-warning);">${d.warnings} warnings</div>` : ""}
      ${d.query ? `<div style="color:var(--tc-info);">filter "${esc(d.query)}"</div>` : ""}
      ${d.stale ? `<div style="color:var(--tc-warning);">stale — last refresh failed</div>` : ""}
      ${d.age_s !== null && d.age_s !== undefined && d.age_s > 25
        ? `<div>updated ${d.age_s}s ago</div>` : ""}
    </div>
    <div data-scroll-clip style="flex:1;margin-top:6px;overflow:hidden;
                font-family:var(--mono);font-size:11px;line-height:16px;">
      <div data-scroll-list style="transition:transform var(--t-fast);">${body}</div>
    </div>
  </div>`;
}

export function logsRows(s) {
  const m = s.modules.logs;
  // Every fetched line is reachable; the clip decides what is visible, not a
  // slice. Capping this was the bug — the cursor stopped at 18 and the rest
  // of the log was unreachable.
  return ((m && m.data && m.data.lines) || []).length;
}

export function logsHints(s) {
  const m = s.modules.logs;
  const d = (m && m.data) || {};
  return [["Start", "Source"], ["Select", "Back"],
          ["X", `Level: ${d.level || "ALL"}`], ["Y", "Refresh"]];
}

// Cycles: Tracer -> each Headwaters host unit -> each container -> back.
// The CAN-to-MQTT bridge is NOT a container, so it is only reachable through
// the host list; leaving it out would hide the errors most worth seeing.
export function logsSources(d) {
  const h = (d && d.hosts) || { host_units: [], containers: [] };
  return [{ source: "local", unit: "" },
          ...h.host_units.map((u) => ({ source: "host", unit: u })),
          ...h.containers.map((c) => ({ source: "container", unit: c }))];
}
