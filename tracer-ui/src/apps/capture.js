// Capture — record MQTT traffic, name it, replay it.
// Layout follows the mock (v2.dc.html:163-205): record panel over a session list.

import { icon } from "../icons.js";
import { spinner } from "../chrome/chrome.js";
const esc = (s) => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;");
const mb = (b) => b >= 1e6 ? `${(b / 1e6).toFixed(1)} MB`
                : b >= 1e3 ? `${(b / 1e3).toFixed(0)} kB` : `${b || 0} B`;
const clock = (s) => `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;

export const captureUi = { pane: "sessions" };   // sessions | playback

export function captureScreen(s) {
  const m = s.modules.capture;
  const d = (m && m.data) || {};
  const pb = d.playback || {};
  const rec = d.recording;

  if (captureUi.pane === "playback" && pb.file) {
    const rows = (pb.rows || []).map((r, i) => `
      <div style="display:flex;gap:8px;padding:2px 0;
                  background:${i === 0 ? "var(--bg-2)" : "transparent"};">
        <div style="color:var(--fg-mute);flex-shrink:0;">
          ${new Date(r.ts * 1000).toLocaleTimeString()}</div>
        <div style="color:var(--tc-info);width:150px;flex-shrink:0;overflow:hidden;
                    text-overflow:ellipsis;white-space:nowrap;">${esc(r.topic)}</div>
        <div style="color:var(--fg-dim);flex:1;overflow:hidden;
                    text-overflow:ellipsis;white-space:nowrap;">${esc(r.payload)}</div>
      </div>`).join("") || `<div style="color:var(--fg-mute);padding:14px;">
        Press Start to play</div>`;
    const pct = pb.total ? Math.round((pb.pos / pb.total) * 100) : 0;
    return `
    <div style="height:420px;padding:12px 14px;display:flex;flex-direction:column;">
      <div style="display:flex;align-items:center;gap:8px;">
        <div style="font-size:15px;font-weight:500;">Playback</div>
        <div style="font-size:11px;color:var(--fg-mute);font-family:var(--mono);">${esc(pb.file)}</div>
        <div style="flex:1"></div>
        ${pb.loop ? `<div style="font-size:10px;color:var(--tc-primary-light);
             border:1px solid var(--tc-primary-light);border-radius:var(--r-badge);
             padding:1px 8px;">LOOP${pb.loops ? ` ×${pb.loops}` : ""}</div>` : ""}
        <div style="font-size:11px;color:${pb.playing ? "var(--tc-info)" : "var(--fg-mute)"};
                    border:1px solid ${pb.playing ? "var(--tc-info)" : "var(--border)"};
                    border-radius:var(--r-badge);padding:1px 8px;">
          ${pb.playing ? "PLAYING" : "PAUSED"}</div>
      </div>
      <div style="height:4px;background:var(--bg-3);border-radius:var(--r-full);
                  margin-top:10px;overflow:hidden;">
        <div style="width:${pct}%;height:4px;
                    background:${pb.loop ? "var(--tc-primary-light)" : "var(--tc-info)"};
                    transition:width var(--t-fast);"></div>
      </div>
      <div style="display:flex;gap:12px;margin-top:5px;font-size:10px;color:var(--fg-mute);">
        <div>${pb.pos} / ${pb.total} messages</div><div>${pct}%</div>
        ${pb.loops ? `<div>${pb.loops} loop${pb.loops === 1 ? "" : "s"}</div>` : ""}
        <div style="flex:1"></div>
        <div>replay only — nothing is published to the rig</div>
      </div>
      <div data-scroll-clip style="flex:1;margin-top:8px;overflow:hidden;
           font-family:var(--mono);font-size:11px;line-height:16px;">
        <div data-scroll-list>${rows}</div>
      </div>
    </div>`;
  }

  const sessions = (d.sessions || []).map((c, i) => {
    const on = i === s.row;
    return `
    <div data-idx="${i}" style="display:flex;align-items:center;gap:11px;padding:9px 12px;
                background:${on ? "var(--bg-2)" : "var(--bg-1)"};
                border:2px solid ${on ? "var(--focus-border)" : "var(--border)"};
                box-shadow:${on ? "var(--glow)" : "none"};border-radius:10px;">
      <div style="color:var(--fg-dim);">${icon("document-text", 16)}</div>
      <div style="flex:1;font-family:var(--mono);font-size:12px;overflow:hidden;
                  text-overflow:ellipsis;white-space:nowrap;">${esc(c.name)}</div>
      <div style="font-size:11px;color:var(--fg-mute);">${mb(c.bytes)}</div>
      <div style="font-size:10px;color:var(--fg-mute);">
        ${new Date(c.mtime * 1000).toLocaleDateString()}</div>
    </div>`;
  }).join("") || `<div style="color:var(--fg-mute);font-size:12px;padding:14px;
      text-align:center;">No captures yet — press X to record</div>`;

  return `
  <div style="height:420px;padding:12px 14px;display:flex;flex-direction:column;">
    <div style="display:flex;align-items:center;gap:8px;">
      <div style="font-size:15px;font-weight:500;">Capture</div>
      ${m && m.busy ? spinner(13, m.busy_note) : ""}
      <div style="flex:1"></div>
      <div style="font-size:11px;color:var(--fg-mute);font-family:var(--mono);">${esc(d.dir || "")}</div>
    </div>
    <div style="margin-top:10px;padding:12px;border-radius:var(--r-card);
                background:${rec ? "var(--bg-2)" : "var(--bg-1)"};
                border:2px solid ${rec ? "var(--tc-danger)" : "var(--border)"};
                box-shadow:${rec ? "var(--glow-danger)" : "none"};">
      <div style="display:flex;align-items:center;gap:10px;">
        <div style="width:11px;height:11px;border-radius:var(--r-full);
                    background:${rec ? "var(--tc-danger)" : "var(--fg-off)"};
                    ${rec ? "animation:tcpulse 1s ease-in-out infinite;" : ""}"></div>
        <div style="font-size:13px;font-weight:500;">
          ${rec ? "Recording" : "Ready to record"}</div>
        <div style="flex:1"></div>
        <div style="font-size:11px;color:var(--fg-mute);font-family:var(--mono);">
          ${rec ? esc(d.name || "unnamed") : "X to start"}</div>
      </div>
      <div style="display:flex;align-items:flex-end;gap:26px;margin-top:8px;">
        <div style="font-size:30px;line-height:34px;font-family:var(--mono);">
          ${clock(d.elapsed || 0)}</div>
        <div><div style="font-size:10px;color:var(--fg-mute);text-transform:uppercase;">Messages</div>
             <div style="font-size:15px;">${d.count ?? 0}</div></div>
        <div><div style="font-size:10px;color:var(--fg-mute);text-transform:uppercase;">Size</div>
             <div style="font-size:15px;">${mb(d.bytes)}</div></div>
        <div><div style="font-size:10px;color:var(--fg-mute);text-transform:uppercase;">Dropped</div>
             <div style="font-size:15px;color:${d.dropped ? "var(--tc-warning)" : "var(--fg)"};">
               ${d.dropped ?? 0}</div></div>
      </div>
    </div>
    <div style="font-size:10px;color:var(--fg-mute);letter-spacing:1px;
                text-transform:uppercase;margin:12px 0 6px;">Saved sessions</div>
    <div data-scroll-clip style="flex:1;overflow:hidden;">
      <div data-scroll-list style="display:flex;flex-direction:column;gap:6px;">${sessions}</div>
    </div>
  </div>`;
}

export function captureRows(s) {
  const m = s.modules.capture;
  if (captureUi.pane === "playback") return 0;
  return ((m && m.data && m.data.sessions) || []).length;
}

export function captureHints(s) {
  const d = (s.modules.capture && s.modules.capture.data) || {};
  if (captureUi.pane === "playback") {
    const pb = d.playback || {};
    return [["Start", pb.playing ? "Pause" : "Play"], ["Select", "Back"],
            ["X", "Restart"], ["Y", pb.loop ? "Loop: on" : "Loop: off"]];
  }
  return [["Start", "Load"], ["Select", "Back"],
          ["X", d.recording ? "Stop & name" : "Record"],
          ["Y", "Rename"], ["L", "Delete"]];
}
