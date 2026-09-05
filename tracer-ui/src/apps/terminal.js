// Terminal — a real local shell, contained in the app.
//
// While this screen has focus tracerd is in TEXT mode, so the letter keys type
// instead of acting as buttons. That makes Select the only way out, which is
// exactly the universal Back binding.

const esc = (s) => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;");

// 21, not 22: the hint bar takes 30px and the live prompt occupies a line of
// its own. Sizing for 22 pushed the newest line under the hint bar, where it
// was clipped and unreadable — the one line you most want to see.
const ROWS = 21;

// How far back from the newest line the view is parked. 0 == following live
// output; anything higher means the operator has scrolled up to read
// something and new output must NOT yank the view away from them.
export const termView = { back: 0 };

export function termScroll(delta, total) {
  const max = Math.max(0, total - ROWS);
  termView.back = Math.max(0, Math.min(max, termView.back + delta));
  return termView.back;
}

export function termFollow() { termView.back = 0; }

export function terminalScreen(s) {
  const m = s.modules.terminal;
  const d = (m && m.data) || {};
  const all = d.lines || [];
  const total = all.length;
  const back = Math.min(termView.back, Math.max(0, total - ROWS));
  const endIdx = total - back;
  const lines = all.slice(Math.max(0, endIdx - ROWS), endIdx);
  const following = back === 0;

  const body = lines.map((l) => `
    <div style="white-space:pre;color:var(--fg-dim);">${esc(l) || "&nbsp;"}</div>`).join("");

  return `
  <div style="height:var(--body-h);padding:10px 12px 12px;display:flex;
              flex-direction:column;font-family:var(--mono);font-size:12px;
              line-height:17px;overflow:hidden;">
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;">
      <div style="font-size:13px;font-weight:500;font-family:var(--font);">Terminal</div>
      <div style="font-size:11px;color:${d.alive ? "var(--tc-success)" : "var(--fg-mute)"};
                  font-family:var(--font);">
        ${d.alive ? "shell running" : "starting…"}</div>
      <div style="flex:1"></div>
      ${following ? "" : `<div style="font-size:10px;color:var(--tc-warning);
           font-family:var(--font);">scrolled up ${back} line${back === 1 ? "" : "s"}
           · Start to follow</div>`}
      <div style="font-size:10px;color:var(--fg-mute);font-family:var(--font);">
        Select to leave · Ctrl+C interrupts</div>
    </div>
    <!-- flex-end keeps the newest output pinned to the bottom of the
         available space, so a short session does not float mid-panel and a
         full one never spills under the hint bar. -->
    <div data-scroll-clip style="flex:1;overflow:hidden;display:flex;
         flex-direction:column;justify-content:flex-end;">
      <div data-scroll-list>
        ${body}
        ${following ? `<div style="display:flex;gap:0;white-space:pre;">
          <span style="color:var(--fg);">${esc(d.partial || "")}</span>
          <span style="display:inline-block;width:7px;height:14px;
                background:var(--tc-primary-light);
                animation:tcpulse 1s ease-in-out infinite;"></span>
        </div>` : ""}
      </div>
    </div>
  </div>`;
}

export function terminalHints() {
  // X cannot be a shortcut here — in text mode X types the letter "x". Use
  // the real Ctrl key; advertising a binding that cannot fire is worse than
  // advertising nothing.
  return [["Select", "Back"], ["Ctrl+C", "Interrupt"], ["Enter", "Run"]];
}
