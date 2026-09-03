// Render tests for the Settings screen, focused on the OS-owned group.
//
// These rows are the only way to fix a clock, time zone or locale once an
// image is flashed — the device boots into a kiosk with nothing behind it. So
// the things worth testing are the ones that would leave an operator stuck:
// a value read from the wrong place, a picker that cannot be paged, or a hint
// bar advertising buttons that type instead of act.
//
// Run: node tracer-ui/tests/settings.render.test.mjs

// settings.js imports the store, which derives the daemon's address from
// `location` at module load. Stub it BEFORE importing — hence the dynamic
// import, since static ones are hoisted above any setup. Nothing else in the
// store runs at load time: the WebSocket is only opened by connect().
globalThis.location = { hostname: "127.0.0.1", port: "8710" };

const {
  settingsUi, settingsScreen, settingsHints, settingsScrollRow,
} = await import("../src/apps/settings.js");

// The System group as the daemon ships it (tracerd/modules/settings.py).
const systemGroup = {
  meta: { id: "system", title: "Date & Time", icon: "time",
          sub: "Clock, time zone, locale, region" },
  searchIndex: [
    { label: "Time zone", kw: "timezone", anchor: "timezone", key: "timezone",
      type: "picker", source: "os", options: "timezones", help: "Sets the clock." },
    { label: "Automatic time", kw: "ntp", anchor: "ntp", key: "ntp",
      type: "choice", source: "os", choices: ["on", "off"], help: "Syncs the clock." },
    { label: "Date and time", kw: "date", anchor: "time", key: "time",
      type: "text", source: "os", example: "2026-09-01 14:30", help: "Manual clock." },
    { label: "Locale", kw: "locale", anchor: "locale", key: "locale",
      type: "picker", source: "os", options: "locales", help: "Language." },
  ],
};

const st = (system) => ({
  row: 0,
  modules: {
    settings: {
      state: "ok", busy: false,
      data: {
        groups: [systemGroup],
        values: {},
        readonly: {},
        system,
      },
    },
  },
});

const SYNCED = { timezone: "America/Denver", ntp: true, ntp_synced: true,
                 time: "2026-09-01 14:30:00", locale: "en_US.UTF-8",
                 keymap: "us", wifi_region: "US", errors: {} };

let pass = 0, fail = 0;
const check = (n, c) => { c ? pass++ : fail++;
  console.log(`  ${c ? "ok  " : "FAIL"} ${n}`); };

// ── OS-owned values come from data.system, not data.values ──────────
settingsUi.mode = "rows";
settingsUi.group = 0;
settingsUi.row = 0;
settingsUi.picker = null;
settingsUi.editing = null;

let h = settingsScreen(st(SYNCED));
check("time zone shows the system value", h.includes("America/Denver"));
check("clock shows the system value", h.includes("2026-09-01 14:30:00"));
check("locale shows the system value", h.includes("en_US.UTF-8"));

// NTP being ON says nothing about whether the clock is actually right, and an
// unsynced clock is what breaks the broker's TLS. The row has to distinguish.
check("automatic time reports synced", h.includes("on · synced"));
h = settingsScreen(st({ ...SYNCED, ntp_synced: false }));
check("automatic time distinguishes not-yet-synced",
      h.includes("on · not synced"));
h = settingsScreen(st({ ...SYNCED, ntp: false }));
check("automatic time off renders as off",
      h.includes(">off<") || h.includes("off</div>"));

// A board with no raspi-config, or a locale read that failed, must render as
// unknown rather than blank or "undefined".
h = settingsScreen(st({ ...SYNCED, timezone: null, locale: null }));
check("an unreadable OS value renders as --", h.includes("--"));
check("an unreadable OS value never renders undefined",
      !h.includes("undefined"));

// ── the picker ──────────────────────────────────────────────────────
settingsUi.picker = {
  key: "timezone", label: "Time zone", query: "", row: 1, loading: false,
  error: "", current: "America/Denver",
  options: ["Africa/Cairo", "America/Denver", "America/New_York", "Europe/London"],
};
h = settingsScreen(st(SYNCED));
check("picker lists its options", h.includes("America/New_York"));
check("picker marks the value already in force", h.includes("CURRENT"));
check("picker shows how many of how many", h.includes("4 of 4"));
check("picker rows are tappable", h.includes('data-idx="2"'));

// Typing filters. With several hundred time zones this is the only usable way
// in, and matching has to hit the CITY, which sits after the region.
settingsUi.picker.query = "denver";
settingsUi.picker.row = 0;
h = settingsScreen(st(SYNCED));
check("filter matches mid-string, not just the prefix",
      h.includes("America/Denver") && !h.includes("Europe/London"));
check("filter reports the narrowed count", h.includes("1 of 4"));

settingsUi.picker.query = "zzz";
h = settingsScreen(st(SYNCED));
check("an empty filter result says so", h.includes("Nothing matches"));

// A failed read must explain itself rather than showing an empty list, which
// is indistinguishable from "this system has no time zones".
settingsUi.picker = { key: "locale", label: "Locale", query: "", row: 0,
  loading: false, error: "localectl is not installed on this image",
  current: "", options: [] };
h = settingsScreen(st(SYNCED));
check("a failed option list shows the reason",
      h.includes("localectl is not installed"));

settingsUi.picker = { key: "locale", label: "Locale", query: "", row: 0,
  loading: true, error: "", current: "", options: [] };
h = settingsScreen(st(SYNCED));
check("a slow list says it is loading", h.toLowerCase().includes("reading"));

// ── hints ───────────────────────────────────────────────────────────
// The query field has focus in the picker, so A/B/X/Y/L/R type letters. A hint
// bar naming them would be advertising inert bindings.
settingsUi.picker = { key: "timezone", label: "Time zone", query: "", row: 0,
  loading: false, error: "", current: "", options: ["UTC"] };
const hints = settingsHints(st(SYNCED));
check("picker hints offer Start and Select",
      hints.some(([b]) => b === "Start") && hints.some(([b]) => b === "Select"));
check("picker hints advertise no letter keys",
      !hints.some(([b]) => ["A", "B", "X", "Y", "L", "R", "L/R"].includes(b)));
check("picker hints document paging", hints.some(([, l]) => /page/i.test(l)));

// ── scrolling ───────────────────────────────────────────────────────
// The shared scroller follows one index. Settings keeps its own cursor, and
// the picker keeps another; returning the wrong one scrolls against row 0
// forever and the selection leaves the screen.
settingsUi.picker.row = 7;
check("scroll follows the picker cursor while it is open",
      settingsScrollRow() === 7);
settingsUi.picker = null;
settingsUi.row = 3;
check("scroll follows the settings cursor otherwise", settingsScrollRow() === 3);

settingsUi.mode = "groups";
settingsUi.row = 0;
console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
