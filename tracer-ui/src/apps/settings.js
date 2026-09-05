// Settings — grouped and searchable, matching the Headwaters PWA pattern.
//
// The daemon owns the groups and their searchIndex; this file only renders
// them and routes edits back. Adding a setting means editing
// tracerd/modules/settings.py, not this file.
//
// Text entry uses the PHYSICAL keyboard via tracerd's text mode. There is no
// on-screen keyboard anywhere in Tracer.

import { icon } from "../icons.js";
import { rpc, update, state, toast } from "../store/store.js";
import { resetScroll } from "../chrome/scroll.js";

export const settingsUi = {
  mode: "groups",   // groups | rows | search | edit
  group: 0,
  row: 0,
  query: "",
  editing: null,    // {key, label, type, value, secret}
  // Open list picker. Time zones number in the hundreds, so cycling a choice
  // row is useless and a sub-screen is the only workable shape. Reuses the
  // search interaction the operator already knows from X on the group list:
  // type to filter, d-pad to move, Start to apply.
  // {key, label, options, query, row, loading, error, current}
  picker: null,
  results: [],
  busy: null,
  // Optimistic overrides, keyed by setting. The daemon stays authoritative —
  // these are cleared the moment its snapshot confirms the value. Without
  // them a slider only moves after a full RPC round trip, which at 8 Hz key
  // repeat feels like the input was dropped.
  pending: {},
};

function effective(s, key, fallback) {
  if (key in settingsUi.pending) return settingsUi.pending[key];
  const v = values(s)[key];
  return v === undefined ? fallback : v;
}

// Drop an optimistic value once the daemon reports the same thing.
export function reconcileSettings(s) {
  const v = values(s);
  let changed = false;
  for (const k of Object.keys(settingsUi.pending)) {
    if (String(v[k]) === String(settingsUi.pending[k])) {
      delete settingsUi.pending[k];
      changed = true;
    }
  }
  return changed;
}

function groups(s) {
  const m = s.modules.settings;
  return (m && m.state === "ok" && m.data.groups) ? m.data.groups : [];
}

function values(s) {
  const m = s.modules.settings;
  return (m && m.state === "ok" && m.data.values) ? m.data.values : {};
}

function readonly(s) {
  const m = s.modules.settings;
  return (m && m.state === "ok" && m.data.readonly) ? m.data.readonly : {};
}

// OS-owned values (clock, time zone, locale, WiFi region). Read live by the
// daemon every poll and never stored, so there is nothing here to reconcile
// against a local copy — see tracerd/modules/osconfig.py.
function system(s) {
  const m = s.modules.settings;
  return (m && m.state === "ok" && m.data.system) ? m.data.system : {};
}

// Which row the clipped list should follow. The picker has its own cursor,
// and its list is long enough that getting this wrong means the selection
// scrolls off screen.
export function settingsScrollRow() {
  return settingsUi.picker ? settingsUi.picker.row : settingsUi.row;
}

function pickerMatches(p) {
  const q = (p.query || "").trim().toLowerCase();
  if (!q) return p.options;
  // Match anywhere, not just the prefix: an operator looking for a time zone
  // types the CITY ("denver"), which sits after the region in every name.
  return p.options.filter((o) => o.toLowerCase().includes(q));
}

function rowsOf(s) {
  if (settingsUi.mode === "search") return settingsUi.results;
  const g = groups(s)[settingsUi.group];
  return g ? g.searchIndex : [];
}

// Flattened search across every group's index — the same model the PWA uses.
function runSearch(s) {
  const q = settingsUi.query.trim().toLowerCase();
  if (!q) { settingsUi.results = []; return; }
  const all = [];
  for (const g of groups(s)) {
    for (const item of g.searchIndex) all.push({ ...item, group: g });
  }
  settingsUi.results = all.filter(
    (i) => i.label.toLowerCase().includes(q) || (i.kw || "").includes(q)
  );
  settingsUi.row = 0;
}

function displayValue(s, item) {
  const v = values(s);
  const ro = readonly(s);

  // OS-owned rows read from a different place than the JSON store.
  if (item.source === "os") {
    const sys = system(s);
    const raw = sys[item.key];
    if (item.key === "ntp") {
      if (raw === null || raw === undefined) return "--";
      // "on" alone is a half-truth while the clock is still wrong. NTP being
      // enabled says nothing about whether it has reached a server yet, and
      // an unsynced clock is exactly what breaks the broker's TLS.
      if (!raw) return "off";
      return sys.ntp_synced ? "on · synced" : "on · not synced";
    }
    if (raw === null || raw === undefined || raw === "") return "--";
    return String(raw);
  }

  if (item.type === "readonly") return ro[item.key] ?? "--";
  if (item.type === "action") {
    if (item.key === "wifi") {
      const net = s.modules.net;
      return (net && net.state === "ok" && net.data.ssid) ? net.data.ssid : "Not connected";
    }
    if (item.key === "fetch_ca") {
      if (settingsUi.busy === "fetch_ca") return "Fetching…";
      // An action row shows what pressing A will DO, not a value.
      const mq = s.modules.mqtt;
      if (mq && mq.data && mq.data.tls_verified) return "Installed · verified";
      return readonly(s).ca_installed ? "Installed" : "Not installed";
    }
    return "";
  }
  if (item.type === "secret") return v[item.key] ? "Set" : "Not set";
  if (item.type === "slider") {
    const pw = s.modules.power;
    const method = pw && pw.state === "ok" ? pw.data.brightness_method : null;
    const suffix = method === "software" ? "% (dim)" : "%";
    return `${effective(s, item.key, "--")}${suffix}`;
  }
  const val = v[item.key];
  return (val === "" || val === undefined || val === null) ? "--" : String(val);
}

// The hint bar shows what the FOCUSED row actually does, not a fixed set for
// the screen. A slider whose adjust keys are invisible reads as broken.
export function settingsHints(s) {
  // In a text field the letter buttons type, so only start/select can act.
  if (settingsUi.editing) return [["Start", "Save"], ["Select", "Cancel"], ["Esc", "Cancel"]];
  // Same constraint in the picker: typing filters, so A-Z cannot be bindings.
  if (settingsUi.picker) return [["Start", "Select"], ["Select", "Cancel"],
                                 ["Up/Down", "Move"], ["Left/Right", "Page"]];
  if (settingsUi.mode === "search") return [["Start", "Done"], ["Select", "Back"]];
  if (settingsUi.mode === "groups") return null;
  const item = rowsOf(s)[settingsUi.row];
  if (item && item.type === "slider") {
    return [["L/R", "Adjust"], ["Select", "Back"], ["X", "Search"], ["Y", "Reboot"]];
  }
  if (item && item.type === "choice") {
    return [["Start", "Toggle"], ["Select", "Back"], ["X", "Search"], ["Y", "Reboot"]];
  }
  if (item && item.type === "action") {
    return [["Start", "Open"], ["Select", "Back"], ["X", "Search"], ["Y", "Reboot"]];
  }
  return null;
}

export function settingsScreen(s) {
  const gs = groups(s);
  if (!gs.length) {
    return `<div style="height:var(--body-h);display:flex;align-items:center;
      justify-content:center;color:var(--fg-mute);font-size:12px;">Loading settings…</div>`;
  }

  if (settingsUi.picker) return pickerOverlay(s);
  if (settingsUi.editing) return editOverlay(s);

  // ── group list ──
  if (settingsUi.mode === "groups") {
    const items = gs.map((g, i) => {
      const on = i === settingsUi.group;
      return `
      <div class="set-group" data-idx="${i}"
           style="display:flex;align-items:center;gap:11px;padding:9px 12px;
                  background:${on ? "var(--bg-2)" : "var(--bg-1)"};
                  border:2px solid ${on ? "var(--focus-border)" : "var(--border)"};
                  box-shadow:${on ? "var(--glow)" : "none"};
                  border-radius:10px;cursor:pointer;">
        <div style="color:var(--fg-dim);">${icon(g.meta.icon, 18)}</div>
        <div style="flex:1;min-width:0;">
          <div style="font-size:12px;">${g.meta.title}</div>
          <div style="font-size:10px;color:var(--fg-mute);">${g.meta.sub}</div>
        </div>
        <div style="color:var(--fg-mute);">${icon("chevron-forward", 15)}</div>
      </div>`;
    }).join("");
    return `
    <div style="height:var(--body-h);padding:12px 14px;display:flex;flex-direction:column;">
      <div style="display:flex;align-items:center;gap:8px;">
        <div style="font-size:15px;font-weight:500;">Settings</div>
        <div style="flex:1"></div>
        <div style="font-size:11px;color:var(--fg-mute);">X to search</div>
      </div>
      <div style="display:flex;flex-direction:column;gap:5px;margin-top:12px;">${items}</div>
    </div>`;
  }

  // ── search ──
  if (settingsUi.mode === "search") {
    const rows = settingsUi.results.length
      ? settingsUi.results.map((item, i) => rowHtml(s, item, i, item.group.meta.title)).join("")
      : `<div style="color:var(--fg-mute);font-size:12px;padding:12px 2px;">
           No settings match "${settingsUi.query}".</div>`;
    return `
    <div style="height:var(--body-h);padding:12px 14px;display:flex;flex-direction:column;">
      <div style="display:flex;align-items:center;gap:8px;">
        <div style="font-size:15px;font-weight:500;">Search settings</div>
      </div>
      <div style="margin-top:10px;padding:9px 12px;background:var(--bg-1);
                  border:2px solid var(--focus-border);box-shadow:var(--glow);
                  border-radius:10px;font-family:var(--mono);font-size:13px;">
        ${settingsUi.query || '<span style="color:var(--fg-mute)">type to search…</span>'}<span
          style="display:inline-block;width:7px;height:14px;vertical-align:-2px;
                 background:var(--tc-primary-light);animation:tcpulse 1s ease-in-out infinite;"></span>
      </div>
      <div style="margin-top:8px;display:flex;flex-direction:column;gap:5px;
                  overflow:hidden;">${rows}</div>
    </div>`;
  }

  // ── rows in a group ──
  const g = gs[settingsUi.group];
  const rows = g.searchIndex.map((item, i) => rowHtml(s, item, i)).join("");
  return `
  <div style="height:var(--body-h);padding:12px 14px;display:flex;flex-direction:column;">
    <div style="display:flex;align-items:center;gap:8px;">
      <div style="color:var(--fg-dim);">${icon(g.meta.icon, 16)}</div>
      <div style="font-size:15px;font-weight:500;">${g.meta.title}</div>
      <div style="flex:1"></div>
      <div style="font-size:11px;color:var(--fg-mute);">B back</div>
    </div>
    <div data-scroll-clip style="flex:1;margin-top:12px;overflow:hidden;">
      <div data-scroll-list style="display:flex;flex-direction:column;gap:5px;
           transition:transform var(--t-fast);">${rows}</div>
    </div>
  </div>`;
}

function rowHtml(s, item, i, groupLabel) {
  const on = i === settingsUi.row;
  const val = displayValue(s, item);
  const isSlider = item.type === "slider";
  const pct = isSlider ? Number(effective(s, item.key, 0)) : 0;

  return `
  <div class="set-row" data-idx="${i}"
       style="display:flex;align-items:center;gap:11px;padding:9px 12px;
              background:${on ? "var(--bg-2)" : "var(--bg-1)"};
              border:2px solid ${on ? "var(--focus-border)" : "var(--border)"};
              box-shadow:${on ? "var(--glow)" : "none"};
              border-radius:10px;cursor:pointer;">
    <div style="flex:1;min-width:0;">
      <div style="font-size:12px;">${item.label}</div>
      ${groupLabel ? `<div style="font-size:10px;color:var(--fg-mute);">${groupLabel}</div>` : ""}
      ${on && item.help ? `<div style="font-size:10px;color:var(--fg-mute);
        line-height:13px;margin-top:2px;white-space:normal;">${item.help}</div>` : ""}
      ${isSlider ? `
        <div style="display:flex;align-items:center;gap:8px;margin-top:6px;">
          <div style="height:4px;background:var(--bg-3);border-radius:var(--r-full);
                      overflow:hidden;width:180px;">
            <div style="width:${pct}%;height:4px;background:var(--tc-primary);"></div>
          </div>
          ${on ? `<div style="font-size:10px;color:var(--fg-mute);">L / R to adjust</div>` : ""}
        </div>` : ""}
    </div>
    <div style="font-size:12px;font-family:var(--mono);
                color:${item.type === "action" && on ? "var(--tc-primary-light)"
                       : on ? "var(--fg)" : "var(--fg-dim)"};">${val}</div>
    ${item.type === "readonly" ? "" :
      `<div style="color:var(--fg-mute);">${icon("chevron-forward", 15)}</div>`}
  </div>`;
}

function pickerOverlay(s) {
  const p = settingsUi.picker;

  if (p.loading) {
    return `
    <div style="height:var(--body-h);padding:12px 14px;display:flex;flex-direction:column;
                align-items:center;justify-content:center;gap:10px;">
      <div style="color:var(--tc-primary);animation:tcspin 1s linear infinite;">
        ${icon("sync-outline", 22)}</div>
      <div style="font-size:12px;color:var(--fg-dim);">Reading ${p.label.toLowerCase()}…</div>
    </div>`;
  }
  if (p.error) {
    return `
    <div style="height:var(--body-h);padding:12px 14px;display:flex;flex-direction:column;">
      <div style="font-size:15px;font-weight:500;">${p.label}</div>
      <div style="margin-top:12px;padding:9px 12px;border-radius:8px;
                  background:rgba(255,84,83,0.14);border:1px solid var(--tc-danger);
                  font-size:12px;color:var(--tc-danger);line-height:16px;">${p.error}</div>
      <div style="flex:1"></div>
      <div style="font-size:11px;color:var(--fg-mute);">Select to go back</div>
    </div>`;
  }

  const matches = pickerMatches(p);
  // The count is not decoration: with 400 time zones behind a filter, it is
  // the only feedback that says whether to keep typing.
  const rows = matches.length
    ? matches.map((opt, i) => {
        const on = i === p.row;
        const cur = opt === p.current;
        return `
        <div class="set-pick" data-idx="${i}"
             style="display:flex;align-items:center;gap:9px;padding:7px 11px;
                    background:${on ? "var(--bg-2)" : "var(--bg-1)"};
                    border:2px solid ${on ? "var(--focus-border)" : "var(--border)"};
                    box-shadow:${on ? "var(--glow)" : "none"};
                    border-radius:9px;cursor:pointer;">
          <div style="flex:1;min-width:0;font-size:12px;font-family:var(--mono);
                      overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${opt}</div>
          ${cur ? `<div style="font-size:9px;padding:1px 7px;border-radius:var(--r-full);
                     background:var(--tc-primary);color:#000;">CURRENT</div>` : ""}
        </div>`;
      }).join("")
    : `<div style="color:var(--fg-mute);font-size:12px;padding:12px 2px;">
         Nothing matches "${p.query}".</div>`;

  return `
  <div style="height:var(--body-h);padding:12px 14px;display:flex;flex-direction:column;">
    <div style="display:flex;align-items:center;gap:8px;">
      <div style="font-size:15px;font-weight:500;">${p.label}</div>
      <div style="flex:1"></div>
      <div style="font-size:10px;color:var(--fg-mute);">
        ${matches.length} of ${p.options.length}</div>
    </div>
    <div style="margin-top:9px;padding:8px 11px;background:var(--bg-1);
                border:2px solid var(--focus-border);box-shadow:var(--glow);
                border-radius:10px;font-family:var(--mono);font-size:13px;">
      ${p.query || '<span style="color:var(--fg-mute)">type to filter…</span>'}<span
        style="display:inline-block;width:7px;height:14px;vertical-align:-2px;
               background:var(--tc-primary-light);animation:tcpulse 1s ease-in-out infinite;"></span>
    </div>
    <div data-scroll-clip style="flex:1;margin-top:8px;overflow:hidden;">
      <div data-scroll-list style="display:flex;flex-direction:column;gap:4px;">${rows}</div>
    </div>
  </div>`;
}

function editOverlay(s) {
  const e = settingsUi.editing;
  const shown = e.secret ? "•".repeat(e.value.length) : (e.value || "");
  // A field labelled only "Headwaters password" with "Type on the keyboard"
  // tells the operator nothing about what to type. Every field carries a
  // help string from the daemon; show it here, where the question is asked.
  const help = e.help
    ? `<div style="font-size:11px;color:var(--fg-dim);margin-top:6px;
                   line-height:15px;">${e.help}</div>` : "";
  const example = e.example
    ? `<div style="font-size:11px;color:var(--fg-mute);margin-top:4px;
                   font-family:var(--mono);">e.g. ${e.example}</div>` : "";
  const optional = e.optional
    ? `<div style="font-size:11px;color:var(--tc-info);margin-top:4px;">
         Optional — leave blank to skip.</div>` : "";
  return `
  <div style="height:var(--body-h);padding:12px 14px;display:flex;flex-direction:column;">
    <div style="font-size:15px;font-weight:500;">${e.label}</div>
    ${help}${example}${optional}
    <div style="margin-top:12px;padding:12px;background:var(--bg-1);
                border:2px solid var(--focus-border);box-shadow:var(--glow);
                border-radius:10px;font-family:var(--mono);font-size:15px;
                min-height:44px;word-break:break-all;">
      ${shown || `<span style="color:var(--fg-off)">${e.secret ? "" : (e.example || "")}</span>`}<span
        style="display:inline-block;width:8px;height:15px;
        background:var(--tc-primary-light);vertical-align:-2px;
        animation:tcpulse 1s ease-in-out infinite;"></span>
    </div>
    <div style="display:flex;gap:14px;align-items:center;margin-top:10px;">
      <div style="display:flex;align-items:center;gap:5px;">
        <div style="padding:1px 7px;border-radius:var(--r-full);background:var(--tc-primary-light);
                    color:#000;font-size:10px;font-weight:700;">Enter</div>
        <div style="font-size:11px;color:var(--fg-dim);">Save</div>
      </div>
      <div style="display:flex;align-items:center;gap:5px;">
        <div style="padding:1px 7px;border-radius:var(--r-full);background:var(--bg-3);
                    color:var(--fg);font-size:10px;font-weight:700;">Esc</div>
        <div style="font-size:11px;color:var(--fg-dim);">Cancel</div>
      </div>
      <div style="flex:1"></div>
      <div style="font-size:10px;color:var(--tc-warning);">B types a letter here</div>
    </div>
    ${e.error ? `<div style="margin-top:8px;font-size:12px;color:var(--tc-danger);">${e.error}</div>` : ""}
  </div>`;
}

// ── input ────────────────────────────────────────────────────────────
async function beginEdit(item, s) {
  const source = item.source === "os" ? "os" : "store";
  const current = source === "os"
    ? String(system(s)[item.key] ?? "")
    : String(values(s)[item.key] ?? "");
  settingsUi.editing = {
    key: item.key, label: item.label, type: item.type, source,
    help: item.help || "", example: item.example || "",
    optional: Boolean(item.optional),
    secret: item.type === "secret",
    // Never prefill a secret — the GUI does not have the value, only whether
    // one is set. Typing replaces it outright.
    value: item.type === "secret" ? "" : current,
    error: "",
  };
  await rpc("input", "set_mode", { mode: "text" });
  update({});
}

async function beginPicker(item, s) {
  settingsUi.picker = {
    key: item.key, label: item.label, options: [], query: "", row: 0,
    loading: true, error: "", current: String(system(s)[item.key] ?? ""),
  };
  // Text mode BEFORE the fetch: the list can take a moment on a cold cache,
  // and a keystroke arriving as a navigation button in that window would walk
  // the operator out of the screen they just opened.
  await rpc("input", "set_mode", { mode: "text" });
  update({});

  const res = await rpc("settings", "options", { key: item.options });
  if (!settingsUi.picker || settingsUi.picker.key !== item.key) return;  // cancelled
  if (res.ok) {
    settingsUi.picker.options = (res.d && res.d.options) || [];
    // Start on the current value rather than at the top. On a 3.5" screen the
    // alternative is scrolling past 200 time zones to see what is already set.
    const at = settingsUi.picker.options.indexOf(settingsUi.picker.current);
    settingsUi.picker.row = at >= 0 ? at : 0;
    if (!settingsUi.picker.options.length) {
      settingsUi.picker.error = "This system reported no options for that setting.";
    }
  } else {
    settingsUi.picker.error = (res.err && res.err.msg) || "could not read the list";
  }
  settingsUi.picker.loading = false;
  resetScroll("settings");
  update({});
}

async function cancelPicker() {
  settingsUi.picker = null;
  await rpc("input", "set_mode", { mode: "nav" });
  resetScroll("settings");
  update({});
}

async function commitPicker() {
  const p = settingsUi.picker;
  if (!p || p.loading) return;
  const choice = pickerMatches(p)[p.row];
  if (choice === undefined) return;
  if (choice === p.current) { await cancelPicker(); return; }

  // Locale generation genuinely takes seconds. Say so, or the screen looks
  // frozen and the operator presses Start again.
  p.loading = true;
  update({});
  const res = await rpc("settings", "set_system", { key: p.key, value: choice });
  if (res.ok) {
    settingsUi.picker = null;
    await rpc("input", "set_mode", { mode: "nav" });
    resetScroll("settings");
    toast(`${p.label}: ${choice}`);
  } else {
    p.loading = false;
    p.error = (res.err && res.err.msg) || "could not apply";
  }
  update({});
}

// One place that leaves the editor, so text mode can never be left dangling.
async function cancelEdit() {
  settingsUi.editing = null;
  await rpc("input", "set_mode", { mode: "nav" });
  update({});
}

async function commitEdit() {
  const e = settingsUi.editing;
  if (!e) return;
  const res = e.source === "os"
    ? await rpc("settings", "set_system", { key: e.key, value: e.value })
    : await rpc("settings", "set", { key: e.key, value: e.value });
  await rpc("input", "set_mode", { mode: "nav" });
  if (res.ok) {
    settingsUi.editing = null;
    toast(`${e.label} saved`);
  } else {
    e.error = (res.err && res.err.msg) || "could not save";
  }
  update({});
}

export async function settingsPress(btn, s, openWifi) {
  // Editing overlay. B CANNOT cancel here — B is KEY_B, a letter the operator
  // needs to type. start and select are the only two buttons that carry no
  // character, so they are the only possible Accept/Cancel.
  // Normalised upstream: Start arrives as "a" (confirm), Select as "b"
  // (back). The literal A and B keys cannot reach here — in text mode they
  // type — so these can only have come from Start/Select.
  if (settingsUi.editing) {
    if (btn === "a") { await commitEdit(); return true; }
    if (btn === "b") { await cancelEdit(); return true; }
    return true;
  }

  // Picker. Same constraint as the editor: typing filters the list, so the
  // letter buttons cannot carry actions and only Start/Select are available.
  if (settingsUi.picker) {
    const p = settingsUi.picker;
    if (btn === "b") { await cancelPicker(); return true; }
    if (p.loading) return true;              // ignore input mid-apply
    if (p.error) { await cancelPicker(); return true; }
    const n = pickerMatches(p).length;
    if (btn === "dpad_up")   { p.row = Math.max(0, p.row - 1); update({}); return true; }
    if (btn === "dpad_down") { p.row = Math.min(n - 1, p.row + 1); update({}); return true; }
    // Page with LEFT/RIGHT, not L/R. The query field has focus, so L and R
    // are letter keys that type — binding them here would be inert, and the
    // d-pad is the only thing besides Start/Select that reaches text mode as
    // a button. One row at a time through 400 time zones is not a way to move.
    if (btn === "dpad_left")  { p.row = Math.max(0, p.row - 10); update({}); return true; }
    if (btn === "dpad_right") { p.row = Math.min(n - 1, p.row + 10); update({}); return true; }
    if (btn === "a") { await commitPicker(); return true; }
    return true;
  }

  if (settingsUi.mode === "groups") {
    const n = groups(s).length;
    if (btn === "dpad_up")   { settingsUi.group = Math.max(0, settingsUi.group - 1); update({}); return true; }
    if (btn === "dpad_down") { settingsUi.group = Math.min(n - 1, settingsUi.group + 1); update({}); return true; }
    if (btn === "a") { settingsUi.mode = "rows"; settingsUi.row = 0; update({}); return true; }
    if (btn === "x") {
      settingsUi.mode = "search"; settingsUi.query = ""; settingsUi.results = [];
      await rpc("input", "set_mode", { mode: "text" });
      update({}); return true;
    }
    if (btn === "b") return false;    // let main.js return to the launcher
    return true;
  }

  if (settingsUi.mode === "search") {
    if (btn === "b") {
      settingsUi.mode = "groups"; settingsUi.query = ""; settingsUi.results = [];
      await rpc("input", "set_mode", { mode: "nav" });
      update({}); return true;
    }
    if (btn === "a") { await rpc("input", "set_mode", { mode: "nav" }); update({}); return true; }
  }

  const rows = rowsOf(s);
  if (btn === "dpad_up")   { settingsUi.row = Math.max(0, settingsUi.row - 1); update({}); return true; }
  if (btn === "dpad_down") { settingsUi.row = Math.min(rows.length - 1, settingsUi.row + 1); update({}); return true; }

  const item = rows[settingsUi.row];

  // Adjust a slider in place — no sub-screen for brightness.
  //
  // BOTH the d-pad and the L/R buttons work. L/R is what people reach for on
  // a handheld ("shoulder = adjust"), and the d-pad is what the row layout
  // suggests. Binding only one guarantees half of users press the other and
  // conclude it is broken. L/R's usual "jump to first/last" is near-worthless
  // in a 2-row group, so nothing of value is displaced.
  const DEC = ["dpad_left", "l"];
  const INC = ["dpad_right", "r"];
  if (item && item.type === "slider" && (DEC.includes(btn) || INC.includes(btn))) {
    const cur = Number(effective(s, item.key, 70));
    const next = Math.max(5, Math.min(100, cur + (INC.includes(btn) ? 5 : -5)));
    // Paint first, then tell the daemon. The bar moves on the keypress.
    settingsUi.pending[item.key] = next;
    update({});
    const res = await rpc("power", "set_brightness", { value: next });
    if (!res.ok) {
      delete settingsUi.pending[item.key];   // snap back to the truth
      toast((res.err && res.err.msg) || "brightness failed");
      update({});
    }
    return true;
  }

  if (btn === "b") {
    if (settingsUi.mode === "search") await rpc("input", "set_mode", { mode: "nav" });
    settingsUi.mode = "groups"; settingsUi.row = 0; update({});
    return true;
  }

  if (btn === "a" && item) {
    if (item.type === "readonly") { toast(displayValue(s, item)); return true; }
    if (item.type === "action" && item.key === "wifi") { openWifi(); return true; }
    if (item.type === "action" && item.key === "fetch_ca") {
      settingsUi.busy = "fetch_ca";
      update({});
      const res = await rpc("headwaters", "fetch_ca");
      settingsUi.busy = null;
      if (res.ok) {
        // Show the fingerprint. The fetch is trust-on-first-use — we cannot
        // verify Headwaters until we have its CA — so the operator needs
        // something to check it against.
        toast(`CA installed · ${res.d.fingerprint}`, 6000);
      } else {
        toast((res.err && res.err.msg) || "CA fetch failed", 5000);
      }
      update({});
      return true;
    }
    if (item.type === "picker") { await beginPicker(item, s); return true; }
    if (item.type === "choice" && item.source === "os") {
      // Same cycle as a stored choice, but the current value comes from the
      // system and the write goes back to it. No optimistic override: the
      // daemon re-reads the OS after the change, and showing "on" before the
      // OS agrees is how a failed write becomes invisible.
      const sys = system(s);
      const cur = item.key === "ntp" ? (sys.ntp ? "on" : "off")
                                     : String(sys[item.key] ?? item.choices[0]);
      const next = item.choices[(item.choices.indexOf(cur) + 1) % item.choices.length];
      const res = await rpc("settings", "set_system", { key: item.key, value: next });
      if (!res.ok) toast((res.err && res.err.msg) || "failed", 5000);
      else toast(`${item.label}: ${next}`);
      update({});
      return true;
    }
    if (item.type === "choice") {
      // Cycle rather than open a sub-screen — two options do not warrant one.
      const cur = effective(s, item.key, item.choices[0]);
      const idx = item.choices.indexOf(cur);
      const next = item.choices[(idx + 1) % item.choices.length];
      settingsUi.pending[item.key] = next;   // theme flips on the keypress
      update({});
      const res = await rpc("settings", "set", { key: item.key, value: next });
      if (!res.ok) {
        delete settingsUi.pending[item.key];
        toast((res.err && res.err.msg) || "failed");
        update({});
      }
      return true;
    }
    if (item.type === "slider") { toast("Left/Right to adjust"); return true; }
    await beginEdit(item, s);
    return true;
  }
  return true;
}

export async function settingsText(ch, s) {
  if (settingsUi.editing) { settingsUi.editing.value += ch; update({}); return; }
  if (settingsUi.picker) {
    const p = settingsUi.picker;
    if (p.loading) return;
    p.query += ch;
    // The filter changes what row 0 means, so the cursor has to go back to
    // the top or it points at whatever happens to be at that index now.
    p.row = 0;
    resetScroll("settings");
    update({});
    return;
  }
  if (settingsUi.mode === "search") { settingsUi.query += ch; runSearch(s); update({}); }
}

export async function settingsTextKey(key, s) {
  if (settingsUi.picker) {
    const p = settingsUi.picker;
    if (key === "escape") { await cancelPicker(); return; }
    if (p.loading) return;
    if (key === "enter") { await commitPicker(); return; }
    if (key === "backspace") {
      p.query = p.query.slice(0, -1);
      p.row = 0;
      resetScroll("settings");
      update({});
    }
    return;
  }
  if (settingsUi.editing) {
    if (key === "backspace") { settingsUi.editing.value = settingsUi.editing.value.slice(0, -1); update({}); }
    else if (key === "enter") await commitEdit();
    else if (key === "escape") await cancelEdit();
    return;
  }
  if (settingsUi.mode === "search" && key === "escape") {
    settingsUi.mode = "groups"; settingsUi.query = ""; settingsUi.results = [];
    await rpc("input", "set_mode", { mode: "nav" });
    update({});
    return;
  }
  if (settingsUi.mode === "search") {
    if (key === "backspace") { settingsUi.query = settingsUi.query.slice(0, -1); runSearch(s); update({}); }
    else if (key === "enter") { await rpc("input", "set_mode", { mode: "nav" }); update({}); }
  }
}

export function bindSettings(root, s, openWifi) {
  // A tap in the picker selects that row and applies it, matching the tap
  // behaviour of every other list in the product (tap = move cursor + A).
  for (const el of root.querySelectorAll(".set-pick")) {
    el.addEventListener("click", async () => {
      if (!settingsUi.picker || settingsUi.picker.loading) return;
      settingsUi.picker.row = Number(el.dataset.idx);
      update({});
      await commitPicker();
    });
  }
  for (const el of root.querySelectorAll(".set-group")) {
    el.addEventListener("click", () => {
      settingsUi.group = Number(el.dataset.idx);
      settingsUi.mode = "rows"; settingsUi.row = 0;
      update({});
    });
  }
  for (const el of root.querySelectorAll(".set-row")) {
    el.addEventListener("click", async () => {
      settingsUi.row = Number(el.dataset.idx);
      update({});
      await settingsPress("a", s, openWifi);
    });
  }
}
