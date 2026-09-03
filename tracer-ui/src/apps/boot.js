// Boot screen. Matches the mock's composition (v2.dc.html:47-59).
//
// Note this is the GUI's own boot screen, shown while the daemon's modules
// resolve. The IMAGE boot splash is a separate, static TGA — see
// docs/boot.md for why those are deliberately different.

const LOGO = "./assets/boot-logo.png";

export function bootScreen(state) {
  const pct = Math.min(100, state.bootPct);
  return `
  <div style="position:absolute;inset:0;z-index:5;display:flex;flex-direction:column;
              align-items:center;justify-content:center;gap:18px;background:var(--bg-0);">
    <img src="${LOGO}" alt="TrailCurrent" style="width:128px;height:128px;">
    <div style="text-align:center;">
      <div style="font-size:26px;font-weight:500;letter-spacing:0.4px;">Tracer</div>
      <div style="font-size:11px;color:var(--fg-dim);letter-spacing:1.6px;
                  text-transform:uppercase;margin-top:5px;">
        TrailCurrent Field Debugger
      </div>
    </div>
    <div style="width:200px;height:3px;background:var(--bg-1);
                border-radius:var(--r-full);overflow:hidden;">
      <div style="width:${pct}%;height:3px;background:var(--tc-primary);
                  box-shadow:var(--glow);transition:width var(--t-slow);"></div>
    </div>
    <div style="font-size:11px;color:var(--fg-mute);font-family:var(--mono);">
      ${state.bootLine}
    </div>
  </div>`;
}

// Progress reflects real daemon readiness, not a timer. It counts modules
// that have left `starting`, so the bar means something.
export function bootProgress(state) {
  const caps = state.hello && state.hello.caps ? Object.keys(state.hello.caps) : [];
  if (!state.connected) {
    return { pct: 8, line: "waiting for tracerd" };
  }
  if (!caps.length) {
    return { pct: 20, line: "connected, awaiting modules" };
  }
  const resolved = caps.filter((n) => {
    const m = state.modules[n];
    return m && m.state !== "starting";
  }).length;
  const pct = Math.round(20 + (resolved / caps.length) * 80);
  const line = resolved < caps.length
    ? `starting modules ${resolved}/${caps.length}`
    : "ready";
  return { pct, line };
}
