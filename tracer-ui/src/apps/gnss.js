// GNSS & Map. Layout from the design mock (v2.dc.html:386-407): 250px field
// panel on the left, map area on the right.
//
// Real MapLibre tiles, loaded the same way the PWA loads them (PMTiles over
// HTTP Range) but through tracerd's /hw/ proxy rather than the Headwaters
// API — so it still renders when the PWA will not load, which is the point.

import { icon } from "../icons.js";
import { spinner } from "../chrome/chrome.js";

const esc = (s) => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;");
const dash = (v) => (v === null || v === undefined || v === "" ? "--" : v);
const n = (v, d = 0) => (typeof v === "number" ? v.toFixed(d) : "--");

export function gnssScreen(s) {
  const m = s.modules.gnss;
  const d = (m && m.data) || {};
  const t = d.tiles;
  const hasFix = Boolean(d.has_fix);

  const fields = [
    ["Fix", dash(d.fix_type), hasFix],
    ["Satellites", d.sats === undefined ? "--" : String(d.sats), (d.sats || 0) >= 4],
    ["Latitude", typeof d.lat === "number" ? d.lat.toFixed(5) : "--", null],
    ["Longitude", typeof d.lon === "number" ? d.lon.toFixed(5) : "--", null],
    ["Altitude", d.alt_m === undefined ? "--" : `${n(d.alt_m)} m`, null],
    ["Speed", d.speed === undefined ? "--" : `${n(d.speed, 1)} kn`, null],
    ["Course", d.course === undefined ? "--" : `${n(d.course)}°`, null],
    ["Updated", d.age_s === null || d.age_s === undefined ? "--" : `${d.age_s}s ago`,
     d.age_s !== null && d.age_s !== undefined && d.age_s < 10],
  ].map(([label, value, good]) => `
    <div style="display:flex;justify-content:space-between;align-items:baseline;
                padding:5px 0;border-bottom:1px solid var(--bg-1);">
      <div style="font-size:11px;color:var(--fg-mute);text-transform:uppercase;
                  letter-spacing:0.5px;">${label}</div>
      <div style="font-size:13px;font-family:var(--mono);
                  color:${good === true ? "var(--tc-success)"
                        : good === false ? "var(--tc-warning)" : "var(--fg)"};">${value}</div>
    </div>`).join("");

  // Position marker, placed inside the tile bounds when we know them so the
  // dot means something rather than sitting decoratively in the middle.
  let left = "50%", top = "50%", inBounds = null;
  if (hasFix && t && t.bounds) {
    const [w, so, e, no] = t.bounds;
    if (e > w && no > so) {
      const x = (d.lon - w) / (e - w);
      const y = 1 - (d.lat - so) / (no - so);
      inBounds = x >= 0 && x <= 1 && y >= 0 && y <= 1;
      left = `${Math.max(2, Math.min(98, x * 100))}%`;
      top = `${Math.max(4, Math.min(96, y * 100))}%`;
    }
  }

  const tilePanel = m && m.busy && !t
    ? `<div style="position:absolute;inset:0;display:flex;align-items:center;
           justify-content:center;">${spinner(22, "checking map tiles")}</div>`
    : d.tiles_error
    ? `<div style="position:absolute;inset:0;display:flex;flex-direction:column;
           align-items:center;justify-content:center;gap:8px;padding:16px;
           text-align:center;">
         <div style="color:var(--tc-danger);">${icon("alert-circle", 30)}</div>
         <div style="font-size:12px;color:var(--fg);">Map tiles unreachable</div>
         <div style="font-size:11px;color:var(--fg-mute);font-family:var(--mono);
                     word-break:break-all;">${esc(d.tiles_error)}</div>
         <div style="font-size:10px;color:var(--fg-mute);">Start to retry</div>
       </div>`
    : t
    ? `
      ${mapStatus().error ? `
        <div style="position:absolute;inset:0;display:flex;flex-direction:column;
                    align-items:center;justify-content:center;gap:6px;
                    background:var(--bg-0);text-align:center;padding:14px;">
          <div style="color:var(--tc-warning);">${icon("alert-circle", 26)}</div>
          <div style="font-size:12px;">Tiles did not render</div>
          <div style="font-size:11px;color:var(--fg-mute);font-family:var(--mono);
                      word-break:break-all;">${esc(mapStatus().error)}</div>
        </div>` : ""}
      <div style="position:absolute;left:10px;top:10px;padding:6px 9px;
                  background:var(--chrome-bg);border:1px solid var(--border);
                  border-radius:8px;font-size:10px;line-height:14px;">
        <div style="color:var(--tc-success);display:flex;align-items:center;gap:5px;">
          ${icon("checkmark-circle", 12)} tiles served by nginx${t.range_supported ? " · range ok" : ""}
        </div>
        <div style="color:var(--fg-mute);font-family:var(--mono);">
          z${t.min_zoom}–${t.max_zoom} · ${(t.addressed_tiles / 1e6).toFixed(1)}M tiles · ${t.ms} ms
        </div>
        ${t.tls_verified ? "" : `<div style="color:var(--tc-warning);">TLS unverified</div>`}
      </div>
      <div style="position:absolute;left:10px;bottom:10px;padding:6px 9px;
                  background:var(--chrome-bg);border:1px solid var(--border);
                  border-radius:8px;font-family:var(--mono);font-size:11px;
                  color:var(--fg-dim);">
        ${hasFix ? `${d.lat.toFixed(5)}, ${d.lon.toFixed(5)}` : "no position"}
      </div>
      ${inBounds === false ? `
        <div style="position:absolute;right:10px;bottom:10px;padding:4px 8px;
                    background:var(--chrome-bg);border:1px solid var(--tc-warning);
                    border-radius:8px;font-size:10px;color:var(--tc-warning);">
          position is outside the installed map</div>` : ""}`
    : `<div style="position:absolute;inset:0;display:flex;align-items:center;
           justify-content:center;color:var(--fg-mute);font-size:12px;">
         Map tiles not checked yet</div>`;

  return `
  <div style="height:var(--body-h);display:flex;">
    <div style="width:250px;padding:12px 14px;border-right:1px solid var(--border);
                display:flex;flex-direction:column;">
      <div style="display:flex;align-items:center;gap:8px;">
        <div style="font-size:15px;font-weight:500;">GNSS</div>
        <div style="flex:1"></div>
        ${hasFix
          ? `<div style="font-size:10px;color:var(--tc-success);
               border:1px solid var(--tc-success);border-radius:var(--r-badge);
               padding:1px 7px;">LIVE</div>`
          : `<div style="font-size:10px;color:var(--tc-warning);
               border:1px solid var(--tc-warning);border-radius:var(--r-badge);
               padding:1px 7px;">NO FIX</div>`}
      </div>
      <div style="margin-top:10px;">${fields}</div>
      <div style="flex:1"></div>
      <div style="font-size:10px;color:var(--fg-mute);line-height:13px;">
        Position from <span style="font-family:var(--mono);">local/gps/*</span>
        on MQTT — not the Headwaters API.
      </div>
    </div>
    <div style="flex:1;position:relative;background:var(--bg-0);overflow:hidden;">
      ${tilePanel}
    </div>
  </div>`;
}

// ── map rendering ────────────────────────────────────────────────────
// Everything is fetched through tracerd's loopback proxy: same origin, no
// CORS, and no need for the browser to trust the rig's private CA.
const HW = "/hw";
let mapState = { loading: false, ready: false, error: "", map: null, marker: null };
// Exposed deliberately: this is a diagnostic tool, and "the map pane is
// blank" is unanswerable from the outside without seeing this.
if (typeof window !== "undefined") window.__tracerMap = mapState;

function loadOnce(tag, attrs, timeoutMs = 20000) {
  return new Promise((resolve, reject) => {
    // Without this a stalled asset leaves ensureMap awaiting forever with
    // loading=true and no error — a blank pane and nothing to diagnose.
    const timer = setTimeout(
      () => reject(new Error(`timed out loading ${attrs.src || attrs.href}`)),
      timeoutMs);
    const done = (fn, arg) => { clearTimeout(timer); fn(arg); };
    const sel = tag === "script" ? `script[src="${attrs.src}"]`
                                 : `link[href="${attrs.href}"]`;
    if (document.querySelector(sel)) return done(resolve);
    const el = document.createElement(tag);
    Object.assign(el, attrs);
    el.onload = () => done(resolve);
    el.onerror = () => done(reject,
      new Error(`failed to load ${attrs.src || attrs.href}`));
    document.head.appendChild(el);
  });
}

// MapLibre binds its canvas to a container element. Our render loop replaces
// root.innerHTML wholesale, which DETACHES that element — the map then paints
// into an orphaned node and the panel looks empty with no error. So the map
// lives in its own host div, appended to #app once and never touched by a
// re-render; we only move and show/hide it.
function mapHost() {
  let host = document.getElementById("tracer-map-host");
  if (!host) {
    host = document.createElement("div");
    host.id = "tracer-map-host";
    host.style.cssText = "position:fixed;display:none;overflow:hidden;z-index:5;";
    // BODY, not #app. #app is the element whose innerHTML the render loop
    // replaces, so anything parented to it — including MapLibre's canvas —
    // is destroyed on every render. That was the blank pane: the map was
    // being rebuilt into a node that had already been thrown away.
    document.body.appendChild(host);
  }
  return host;
}

export function positionMapHost(show) {
  const host = mapHost();
  if (!show) { host.style.display = "none"; return host; }
  // Positioned against the viewport from #app's real rect, so it lines up
  // whether or not the dev 640x480 frame is centring the panel.
  const app = document.getElementById("app");
  const r = app.getBoundingClientRect();
  Object.assign(host.style, {
    position: "fixed",
    left: `${Math.round(r.left + 250)}px`,
    top: `${Math.round(r.top + 30)}px`,
    width: `${Math.round(r.width - 250)}px`,
    height: `${Math.round(r.height - 60)}px`,   // minus status + hint bars
    display: "block",
    overflow: "hidden",
    zIndex: "5",
  });
  if (mapState.map) mapState.map.resize();
  return host;
}

export function webglAvailable() {
  try {
    const c = document.createElement("canvas");
    return Boolean(c.getContext("webgl2") || c.getContext("webgl"));
  } catch { return false; }
}

export async function ensureMap(theme, onReady) {
  if (mapState.loading || mapState.ready) return;
  mapState.loading = true;
  mapState.error = "";
  try {
    if (!webglAvailable()) {
      // MapLibre is WebGL-only. Say so plainly rather than showing an empty
      // pane that looks like a tile problem.
      throw new Error("WebGL unavailable — cannot render vector tiles");
    }
    await loadOnce("link", { rel: "stylesheet", href: `${HW}/libs/maplibre/maplibre-gl.css` });
    if (!window.maplibregl) await loadOnce("script", { src: `${HW}/libs/maplibre/maplibre-gl.js` });
    if (!window.pmtiles) await loadOnce("script", { src: `${HW}/libs/pmtiles/pmtiles.js` });
    if (!window.maplibregl || !window.pmtiles) throw new Error("map libraries did not load");

    // Register pmtiles:// so MapLibre resolves the style's source URL to
    // Range reads against the proxied archive.
    if (!mapState._proto) {
      const proto = new window.pmtiles.Protocol();
      window.maplibregl.addProtocol("pmtiles", proto.tile);
      mapState._proto = proto;
    }
    mapState.ready = true;
    onReady && onReady();
  } catch (err) {
    mapState.error = String(err.message || err);
  } finally {
    mapState.loading = false;
  }
}

export function mountMap(theme, lat, lon) {
  const el = positionMapHost(true);
  if (!mapState.ready || !window.maplibregl) return;
  const styleName = theme === "light" ? "3d" : "3d-dark";
  const styleUrl = `${HW}/maps-static/styles/${styleName}/style.json`;

  if (!mapState.map) {
    mapState.map = new window.maplibregl.Map({
      container: el,
      style: styleUrl,
      center: [lon ?? -98.5795, lat ?? 39.8283],
      zoom: lat ? 12 : 4,
      attributionControl: false,
      // The panel has no pointer and the D-pad drives everything, so the
      // built-in drag/zoom handlers would only fight the button model.
      interactive: false,
    });
    mapState.map.on("error", (e) => {
      mapState.error = (e && e.error && e.error.message) || "tile error";
    });
  } else if (mapState._style !== styleUrl) {
    mapState.map.setStyle(styleUrl);
  }
  mapState._style = styleUrl;

  if (lat !== undefined && lon !== undefined && lat !== null && lon !== null) {
    mapState.map.jumpTo({ center: [lon, lat], zoom: mapState._zoom || 12 });
    if (!mapState.marker) {
      const dot = document.createElement("div");
      dot.style.cssText = "width:14px;height:14px;border-radius:9999px;"
        + "background:var(--tc-primary);box-shadow:var(--glow);";
      mapState.marker = new window.maplibregl.Marker({ element: dot })
        .setLngLat([lon, lat]).addTo(mapState.map);
    } else {
      mapState.marker.setLngLat([lon, lat]);
    }
  }
}

export function mapZoom(delta) {
  if (!mapState.map) return;
  mapState._zoom = Math.max(1, Math.min(16, (mapState._zoom || 12) + delta));
  mapState.map.jumpTo({ zoom: mapState._zoom });
}

export function mapTeardown() {
  // Hide rather than destroy: rebuilding the map (and re-reading the PMTiles
  // directory) on every visit is slow, and the operator moves between apps
  // constantly.
  positionMapHost(false);
}

export function mapStatus() { return mapState; }

export function gnssRows() { return 0; }

export function gnssHints() {
  return [["Start", "Recheck tiles"], ["Select", "Back"]];
}
