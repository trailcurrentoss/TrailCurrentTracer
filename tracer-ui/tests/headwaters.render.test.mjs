// Render tests for the Headwaters monitor.
//
// WHY THESE EXIST
// A screen function is only executed when something renders it, so a typo in
// a branch nobody hit stays invisible until an operator opens the app in a
// vehicle bay. That has already happened twice on this product: `_EDIT_KEYS`
// was undefined and text entry was simply dead, and a KeyError inside
// tile_status() killed the whole WebSocket handler rather than one tile.
// `node --check` catches neither — both parse fine.
//
// The cases below are the ones that actually differ in shape, not a
// restatement of the happy path: the first poll (CPU is null because a rate
// needs two samples), a container whose age is unknown because the rig's
// clock moved backwards, an unreachable rig, and a reachable rig with no SSH
// credentials.
//
// Run: node tracer-ui/tests/headwaters.render.test.mjs

import {
  headwatersScreen, headwatersRows, headwatersHints,
} from "../src/apps/headwaters.js";

const metrics = {
  cpu_total: 15.5, cpu_cores: [62.9, 2.6, 9.4, 9.7],
  mem_total: 4.2e9, mem_used: 1.4e9, mem_percent: 32.5,
  swap_total: 209e6, swap_used: 0,
  load: [1.63, 1.52, 1.46], uptime: 131000, temp_c: 52.9,
  disks: [
    { mount: "/", total: 60e9, used: 16e9, percent: 26.8 },
    { mount: "/boot/firmware", total: 500e6, used: 247e6, percent: 49.5 },
  ],
  procs: [
    { pid: 7532, cpu: 22.1, mem: 0.0, rss: 4e6, name: "irq/177-spi0.0" },
    { pid: 995, cpu: 5.1, mem: 5.2, rss: 220e6, name: "MainThread" },
  ],
  containers: [
    // up_seconds null == started after the rig's own clock. Real case: the
    // clock jumped ~36 days backwards while the stack stayed up.
    { name: "trailcurrent-backend-1", status: "Up", up_seconds: null, restarts: 0, up: true },
    { name: "trailcurrent-mongodb-1", status: "Up", up_seconds: 131000, restarts: 2, up: true },
  ],
  healthy: 6, total: 6,
  clock_skew: 3182751.5,
  clock_warning: "Headwaters clock is 36.8 days behind Tracer",
};

const state = (data, over = {}) => ({
  row: 0, hwUi: { pane: "containers" },
  modules: { headwaters: { state: "ok", busy: false, data } }, ...over,
});

let pass = 0, fail = 0;
const check = (name, cond) => {
  cond ? pass++ : fail++;
  console.log(`  ${cond ? "ok  " : "FAIL"} ${name}`);
};

const html = headwatersScreen(state({ host: "headwaters.local", tier: "ssh", metrics }));
check("renders without throwing", typeof html === "string" && html.length > 500);
check("clock-skew banner is shown", html.includes("36.8 days behind"));
check("per-core bars are drawn", html.includes(">c0<") && html.includes(">c3<"));
check("restart count is visible", html.includes("↻2"));
check("unknown container age shows --, never 0m", html.includes(">--<"));
check("rows carry data-idx (touch)", html.includes('data-idx="0"'));
check("rows declare their pane", html.includes('data-pane="containers"'));
// applyScroll() binds the FIRST [data-scroll-clip]; two would strand a pane.
check("exactly one scroll clip is active",
      (html.match(/data-scroll-clip/g) || []).length === 1);

check("reachable-but-no-SSH renders",
      headwatersScreen(state({ host: "x", tier: "probe", metrics: null,
                               note: "Set Headwaters Access" })).includes("reachable"));
check("unreachable renders", headwatersScreen({
  row: 0, hwUi: { pane: "containers" },
  modules: { headwaters: { state: "unavailable", reason: "not reachable" } },
}).includes("unreachable"));
check("starting renders",
      headwatersScreen({ row: 0, modules: { headwaters: { state: "starting" } } }).length > 50);

// First poll: cpu_total is null. A naive `.toFixed()` throws here.
const first = { ...metrics, cpu_total: null, cpu_cores: [] };
check("first poll with null CPU renders",
      headwatersScreen(state({ host: "h", tier: "ssh", metrics: first })).includes("measuring"));

check("row count follows the containers pane", headwatersRows(state({ metrics })) === 2);
const procPane = state({ metrics }, { hwUi: { pane: "procs" } });
check("row count follows the procs pane", headwatersRows(procPane) === 2);
check("hints flip with the pane",
      headwatersHints(procPane).some((h) => h[1] === "Containers"));

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
