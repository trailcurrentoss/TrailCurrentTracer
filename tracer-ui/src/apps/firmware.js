// Firmware — deploy a Headwaters package the documented manual way.
// scp -> unzip -o -> ./deploy.sh  (Headwaters PI_DEPLOYMENT.md:172-190)

import { icon } from "../icons.js";
const esc = (s) => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;");
// Keep the tail of a long path — the folder you are in matters more than the
// root. Done in JS because CSS `direction:rtl` relocates the leading slash.
function shortPath(p, max = 62) {
  p = String(p || "");
  return p.length <= max ? p : "…" + p.slice(-(max - 1));
}

const mb = (b) => b >= 1e9 ? `${(b / 1e9).toFixed(2)} GB`
                : b >= 1e6 ? `${(b / 1e6).toFixed(0)} MB` : `${(b / 1e3).toFixed(0)} kB`;

export function firmwareScreen(s) {
  const m = s.modules.firmware;
  const d = (m && m.data) || {};

  // ── file browser ──
  if (d.browser) {
    const b = d.browser;
    const rows = (b.entries || []).map((e, i) => {
      const on = i === s.row;
      return `
      <div data-idx="${i}" style="display:flex;align-items:center;gap:10px;padding:7px 11px;
                  background:${on ? "var(--bg-2)" : "var(--bg-1)"};
                  border:2px solid ${on ? "var(--focus-border)" : "var(--border)"};
                  box-shadow:${on ? "var(--glow)" : "none"};border-radius:9px;">
        <div style="color:${e.dir ? "var(--fg-dim)" : "var(--tc-primary-light)"};">
          ${icon(e.dir ? "document-text" : "cloud-upload", 15)}</div>
        <div style="flex:1;font-family:var(--mono);font-size:12px;overflow:hidden;
                    text-overflow:ellipsis;white-space:nowrap;
                    color:${e.dir ? "var(--fg)" : "var(--tc-primary-light)"};">
          ${esc(e.name)}${e.dir ? "/" : ""}</div>
        ${e.dir ? "" : `<div style="font-size:11px;color:var(--fg-mute);">${mb(e.bytes)}</div>`}
      </div>`;
    }).join("") || `<div style="color:var(--fg-mute);font-size:12px;padding:16px;
        text-align:center;">Nothing here — no folders and no .zip files</div>`;

    return `
    <div style="height:var(--body-h);padding:12px 14px;display:flex;flex-direction:column;">
      <div style="display:flex;align-items:center;gap:8px;">
        <div style="font-size:15px;font-weight:500;">Choose a package</div>
        <div style="flex:1"></div>
        <div style="font-size:10px;color:var(--fg-mute);">
          ${b.parent ? "Select to go up · " : ""}${b.zips} zip${b.zips === 1 ? "" : "s"} here</div>
      </div>
      <div style="margin-top:4px;font-size:11px;color:var(--fg-dim);
                  font-family:var(--mono);white-space:nowrap;overflow:hidden;
                  ">${esc(shortPath(b.path))}</div>
      <div data-scroll-clip style="flex:1;margin-top:8px;overflow:hidden;">
        <div data-scroll-list style="display:flex;flex-direction:column;gap:5px;">${rows}</div>
      </div>
    </div>`;
  }

  if (d.busy || (d.log || []).length) {
    const log = (d.log || []).map((l) => `
      <div style="color:${/FAILED|error|Error/.test(l) ? "var(--tc-danger)" : "var(--fg-dim)"};
                  white-space:pre-wrap;word-break:break-all;">${esc(l)}</div>`).join("");
    const r = d.last_result;
    const checks = (r && r.checks || []).map((c) => `
      <div style="display:flex;align-items:center;gap:8px;font-size:11px;">
        <div style="color:${c.ok ? "var(--tc-success)" : "var(--tc-danger)"};">
          ${icon(c.ok ? "checkmark-circle" : "alert-circle", 13)}</div>
        <div style="width:120px;">${esc(c.name)}</div>
        <div style="color:var(--fg-mute);font-family:var(--mono);flex:1;overflow:hidden;
                    text-overflow:ellipsis;white-space:nowrap;">${esc(c.detail)}</div>
      </div>`).join("");
    return `
    <div style="height:var(--body-h);padding:12px 14px;display:flex;flex-direction:column;">
      <div style="display:flex;align-items:center;gap:8px;">
        <div style="font-size:15px;font-weight:500;">Firmware</div>
        <div style="flex:1"></div>
        ${d.busy ? `<div style="color:var(--tc-primary);animation:tcspin 1s linear infinite;">
              ${icon("sync-outline", 14)}</div>` : ""}
        <div style="font-size:11px;color:${d.busy ? "var(--tc-warning)"
             : r && r.ok ? "var(--tc-success)" : "var(--tc-danger)"};">
          ${esc(d.stage || (r && r.ok ? "done" : "failed"))}</div>
      </div>
      ${checks ? `<div style="margin-top:8px;padding:8px 10px;background:var(--bg-1);
           border:1px solid var(--border);border-radius:8px;display:flex;
           flex-direction:column;gap:4px;">${checks}</div>` : ""}
      <div data-scroll-clip style="flex:1;margin-top:8px;overflow:hidden;
           font-family:var(--mono);font-size:10px;line-height:14px;">
        <div data-scroll-list>${log}</div>
      </div>
    </div>`;
  }

  const pkgs = (d.packages || []).map((p, i) => {
    const on = i === s.row;
    return `
    <div data-idx="${i}" style="display:flex;align-items:center;gap:11px;padding:10px 12px;
                background:${on ? "var(--bg-2)" : "var(--bg-1)"};
                border:2px solid ${on ? "var(--focus-border)" : "var(--border)"};
                box-shadow:${on ? "var(--glow)" : "none"};border-radius:10px;">
      <div style="color:var(--tc-primary-light);">${icon("cloud-upload", 18)}</div>
      <div style="flex:1;min-width:0;">
        <div style="font-family:var(--mono);font-size:12px;overflow:hidden;
                    text-overflow:ellipsis;white-space:nowrap;">${esc(p.name)}</div>
        <div style="font-size:10px;color:var(--fg-mute);">${esc(p.where)}</div>
      </div>
      <div style="font-size:11px;color:var(--fg-mute);">${mb(p.bytes)}</div>
    </div>`;
  }).join("") || `
    <div style="flex:1;display:flex;flex-direction:column;align-items:center;
                justify-content:center;gap:8px;color:var(--fg-mute);">
      <div style="opacity:.5">${icon("cloud-upload", 32)}</div>
      <div style="font-size:13px;">No deployment package found</div>
      <div style="font-size:11px;text-align:center;max-width:460px;line-height:15px;">
        Put a <span style="font-family:var(--mono);">trailcurrent-deployment-*.zip</span>
        on a USB stick, or press X to browse for one.<br>
        Searched: ${esc((d.search_dirs || []).join(", "))}</div>
    </div>`;

  return `
  <div style="height:var(--body-h);padding:12px 14px;display:flex;flex-direction:column;">
    <div style="display:flex;align-items:center;gap:8px;">
      <div style="font-size:15px;font-weight:500;">Firmware</div>
      <div style="font-size:11px;color:var(--fg-mute);font-family:var(--mono);">
        scp → unzip → ./deploy.sh</div>
      <div style="flex:1"></div>
    </div>
    <div style="margin-top:6px;font-size:10px;color:var(--fg-mute);line-height:14px;">
      Runs the manual developer flow on Headwaters, not the PWA uploader —
      so it still works when the uploader is the thing that is broken.
    </div>
    <div data-scroll-clip style="flex:1;margin-top:10px;overflow:hidden;">
      <div data-scroll-list style="display:flex;flex-direction:column;gap:7px;">${pkgs}</div>
    </div>
  </div>`;
}

export function firmwareRows(s) {
  const m = s.modules.firmware;
  const d = (m && m.data) || {};
  if (d.browser) return (d.browser.entries || []).length;
  if (d.busy || (d.log || []).length) return 0;
  return (d.packages || []).length;
}

export function firmwareHints(s, atRoot = true) {
  const d = (s.modules.firmware && s.modules.firmware.data) || {};
  if (d.browser) {
    // Say which one Back will do, so leaving the browser is never a surprise.
    return [["Start", "Open / Pick"],
            ["Select", atRoot ? "Close" : "Up"],
            ["X", "Places"], ["Y", "Close"]];
  }
  if (d.busy) return [["Select", "Back"], ["Y", "Verify"]];
  if ((d.log || []).length) return [["Select", "Back"], ["X", "Clear"], ["Y", "Verify"]];
  return [["Start", "Deploy"], ["Select", "Back"],
          ["X", "Browse…"], ["Y", "Verify"]];
}
