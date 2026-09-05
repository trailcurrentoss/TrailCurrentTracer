// Launcher — a single non-paginated 3x4 grid of all twelve apps.
// Geometry from the design mock: content fills --body-h, padding 14/14/16,
// gap 10px. (The mock's content box was 420px, when the hint bar was a 30px
// legend rather than a touch target — see --chrome-bottom in tokens.css.)

import { icon } from "../icons.js";

export function launcher(state) {
  const tiles = state.apps.map((app, i) => {
    const focused = i === state.focus;
    const bg = focused ? "var(--bg-2)" : "var(--bg-1)";
    const border = focused ? "var(--tc-primary-light)" : "var(--border)";
    // Glow ONLY on the focused tile — engaged state, nothing else.
    const glow = focused ? "var(--glow)" : "none";

    return `
    <div class="tile" data-idx="${i}" data-app="${app.id}"
         style="display:flex;align-items:center;gap:11px;padding:8px 11px;
                background:${bg};border:2px solid ${border};box-shadow:${glow};
                border-radius:var(--r-card);cursor:pointer;
                transition:background var(--t-fast),border-color var(--t-fast),
                           box-shadow var(--t-fast);">
      <div style="width:48px;height:48px;border-radius:11px;background:${app.tint};
                  display:flex;align-items:center;justify-content:center;flex-shrink:0;">
        ${icon(app.icon, 26, app.glyph)}
      </div>
      <div style="min-width:0;">
        <div style="font-size:13px;font-weight:500;line-height:17px;">${app.short}</div>
        <div style="font-size:11px;line-height:14px;font-family:var(--mono);
                    color:${app.statusColor};overflow:hidden;text-overflow:ellipsis;
                    white-space:nowrap;">${app.status}</div>
      </div>
    </div>`;
  }).join("");

  return `
  <div id="launcher-grid"
       style="height:var(--body-h);padding:14px 14px 16px;display:grid;
              grid-template-columns:repeat(3,1fr);grid-auto-rows:1fr;gap:10px;">
    ${tiles}
  </div>`;
}

// D-pad movement across the 3-wide grid. Clamps rather than wrapping — the
// mock clamps, and wrapping makes it easy to overshoot past the edge.
export function launcherMove(state, btn) {
  const max = state.apps.length - 1;
  if (max < 0) return state.focus;
  const cols = 3;
  const clamp = (n) => Math.max(0, Math.min(max, n));
  switch (btn) {
    case "dpad_left":  return clamp(state.focus - 1);
    case "dpad_right": return clamp(state.focus + 1);
    case "dpad_up":    return clamp(state.focus - cols);
    case "dpad_down":  return clamp(state.focus + cols);
    case "l":          return 0;
    case "r":          return max;
    default:           return state.focus;
  }
}
