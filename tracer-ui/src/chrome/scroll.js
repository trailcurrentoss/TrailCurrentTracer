// Cursor-driven scrolling inside a clipped region.
//
// There are no scrollbars anywhere in Tracer — `html, body { overflow: hidden }`
// and Chromium runs with --hide-scrollbars. A handheld with no pointer has
// nothing to drag, so the list follows the FOCUSED ROW instead: the cursor
// moves, and the viewport shifts only when the cursor would leave it.
//
// Offsets are measured from the real DOM (offsetTop / offsetHeight /
// clientHeight), never computed from an assumed row pitch. Rows here are not
// uniform — the focused log line un-truncates and grows taller, message
// payloads wrap — so any fixed-pitch maths would drift the moment a row
// changed height. This mirrors syncScroll() in the design mock.
//
// Usage: mark the clipping element `data-scroll-clip` and the moving element
// `data-scroll-list`, then call applyScroll(root, key, row) after each render.

const offsets = new Map();   // key -> current translateY in px

export function resetScroll(key) {
  offsets.delete(key);
}

export function applyScroll(root, key, rowIndex) {
  const clip = root.querySelector("[data-scroll-clip]");
  const list = root.querySelector("[data-scroll-list]");
  if (!clip || !list) return;

  const rows = list.children;
  if (!rows.length) {
    list.style.transform = "translateY(0px)";
    offsets.set(key, 0);
    return;
  }

  const idx = Math.max(0, Math.min(rows.length - 1, rowIndex | 0));
  const row = rows[idx];
  if (!row) return;

  let cur = offsets.get(key) || 0;
  const top = row.offsetTop;
  const bottom = top + row.offsetHeight;
  const view = clip.clientHeight;

  if (bottom > cur + view) cur = bottom - view;   // cursor below the fold
  else if (top < cur) cur = top;                  // cursor above it

  // Never scroll past the end, and never leave a gap at the top when the
  // content is shorter than the viewport.
  const max = Math.max(0, list.scrollHeight - view);
  cur = Math.max(0, Math.min(cur, max));

  offsets.set(key, cur);
  list.style.transform = `translateY(${-cur}px)`;
}
