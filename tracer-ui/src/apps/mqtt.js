// MQTT Inspector. Layout from the design mock (v2.dc.html:98-130):
// 196px topic tree on the left, message stream on the right.

import { icon } from "../icons.js";

export const mqttUi = { pane: "messages", topicRow: 0 };

function hhmmss(ts) {
  const d = new Date(ts * 1000);
  const p = (n, w = 2) => String(n).padStart(w, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}.${p(d.getMilliseconds(), 3)}`;
}

function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// Render `local/energy/status` as an indented leaf under a `local/` head,
// the way the mock groups the tree.
function treeRows(topics) {
  const out = [];
  let lastHead = null;
  for (const t of topics) {
    const i = t.path.indexOf("/");
    const head = i === -1 ? "" : t.path.slice(0, i + 1);
    const leaf = i === -1 ? t.path : t.path.slice(i + 1);
    if (head && head !== lastHead) {
      out.push({ head: true, name: head });
      lastHead = head;
    }
    out.push({ head: false, name: leaf || t.path, t });
  }
  return out;
}

export function mqttScreen(s) {
  const m = s.modules.mqtt;
  const d = (m && m.data) || {};
  const connected = Boolean(d.connected);
  const topics = d.topics || [];
  const messages = d.messages || [];

  // Connection trouble is the whole story — show it instead of an empty grid.
  if (!connected) {
    return `
    <div style="height:var(--body-h);padding:12px 14px;display:flex;flex-direction:column;">
      <div style="display:flex;align-items:center;gap:8px;">
        <div style="font-size:15px;font-weight:500;">MQTT Inspector</div>
        <div style="flex:1"></div>
        <div style="font-size:10px;color:var(--tc-warning);border:1px solid var(--tc-warning);
                    border-radius:var(--r-badge);padding:1px 7px;">OFFLINE</div>
      </div>
      <div style="flex:1;display:flex;flex-direction:column;align-items:center;
                  justify-content:center;gap:10px;color:var(--fg-mute);">
        <div style="color:var(--tc-warning);">${icon("alert-circle", 34)}</div>
        <div style="font-size:13px;color:var(--fg);">Not connected to the broker</div>
        <div style="font-size:11px;font-family:var(--mono);max-width:520px;
                    text-align:center;">${esc(d.status || (m && m.reason) || "--")}</div>
        <div style="font-size:11px;">Start to retry · Settings › MQTT for credentials</div>
      </div>
    </div>`;
  }

  const rows = treeRows(topics).map((r) => {
    if (r.head) {
      return `<div style="display:flex;align-items:center;gap:6px;padding:5px 12px;
                   border-left:3px solid var(--tc-primary);">
        <div style="flex:1;font-family:var(--mono);font-size:11px;color:var(--fg);">${esc(r.name)}</div>
      </div>`;
    }
    const rate = r.t.rate >= 0.1 ? `${r.t.rate.toFixed(r.t.rate < 10 ? 1 : 0)}/s` : "--";
    return `<div style="display:flex;align-items:center;gap:6px;padding:5px 12px;
                 border-left:3px solid transparent;">
      <div style="flex:1;font-family:var(--mono);font-size:11px;color:var(--fg-dim);
                  padding-left:10px;overflow:hidden;text-overflow:ellipsis;
                  white-space:nowrap;">${esc(r.name)}</div>
      <div style="font-size:10px;color:var(--fg-mute);">${rate}</div>
    </div>`;
  }).join("");

  const msgs = messages.map((msg, i) => {
    const on = i === s.row;
    const full = msg.payload || "";
    const shown = on ? full : (full.length > 74 ? full.slice(0, 74) + "…" : full);
    const flags = `q${msg.qos}${msg.retain ? " R" : ""}`;
    return `
    <div data-idx="${i}" style="padding:5px 12px;background:${on ? "var(--bg-2)" : "transparent"};
                border-bottom:1px solid var(--bg-1);">
      <div style="display:flex;gap:8px;align-items:baseline;font-family:var(--mono);font-size:11px;">
        <div style="color:var(--fg-mute);">${hhmmss(msg.ts)}</div>
        <div style="flex:1;color:${on ? "var(--tc-primary-light)" : "var(--tc-info)"};
                    overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(msg.topic)}</div>
        <div style="color:var(--fg-mute);">${flags}</div>
      </div>
      <div style="font-family:var(--mono);font-size:11px;
                  color:${on ? "var(--fg)" : "var(--fg-dim)"};margin-top:2px;
                  white-space:pre-wrap;word-break:break-all;">${esc(shown)}</div>
    </div>`;
  }).join("") || `<div style="padding:18px;color:var(--fg-mute);font-size:12px;">
      Connected — waiting for messages…</div>`;

  const live = d.paused
    ? { label: "PAUSED", colour: "var(--tc-warning)" }
    : { label: "LIVE", colour: "var(--tc-success)" };

  // An unverified TLS link still shows traffic, but must never look trusted.
  const tls = d.tls_verified ? "" : `
    <div style="font-size:10px;color:var(--tc-warning);border:1px solid var(--tc-warning);
                border-radius:var(--r-badge);padding:1px 7px;">TLS UNVERIFIED</div>`;

  return `
  <div style="display:flex;height:var(--body-h);">
    <div style="width:196px;border-right:1px solid var(--border);padding:10px 0;
                overflow:hidden;">
      <div style="font-size:10px;color:var(--fg-mute);letter-spacing:1px;
                  text-transform:uppercase;padding:0 12px 8px;">
        Topics · ${topics.length}
      </div>
      ${rows || `<div style="padding:0 12px;font-size:11px;color:var(--fg-mute);">none yet</div>`}
    </div>
    <div style="flex:1;display:flex;flex-direction:column;min-width:0;">
      <div style="display:flex;align-items:center;gap:8px;padding:8px 12px;
                  border-bottom:1px solid var(--border);">
        <div style="font-size:13px;font-weight:500;">MQTT Inspector</div>
        <div style="font-size:10px;color:${live.colour};border:1px solid ${live.colour};
                    border-radius:var(--r-badge);padding:1px 7px;">${live.label}</div>
        ${tls}
        <div style="flex:1"></div>
        <div style="font-size:10px;color:var(--fg-mute);font-family:var(--mono);">
          ${d.rate ?? 0} msg/s · ${d.total ?? 0}
        </div>
      </div>
      <div data-scroll-clip style="flex:1;overflow:hidden;">
        <div data-scroll-list style="transition:transform var(--t-fast);">${msgs}</div>
      </div>
    </div>
  </div>`;
}

export function mqttRows(s) {
  const m = s.modules.mqtt;
  if (!m || !m.data || !m.data.connected) return 0;
  return (m.data.messages || []).length;
}

export function mqttHints(s) {
  const m = s.modules.mqtt;
  if (!m || !m.data || !m.data.connected) {
    return [["Start", "Retry"], ["Select", "Back"]];
  }
  const paused = m.data.paused;
  return [["Start", "Expand"], ["Select", "Back"],
          ["X", paused ? "Resume" : "Pause"], ["Y", "Clear"]];
}
