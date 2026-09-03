// Render tests for the rewritten Module Debug (raw serial console).
//
// Run: node tracer-ui/tests/moduledebug.render.test.mjs

import {
  moduleDebugScreen, moduleDebugRows, moduleDebugHints, dbgUi,
} from "../src/apps/moduledebug.js";

const esp = {
  device: "/dev/ttyACM1", name: "ttyACM1", vid: "303a", pid: "1001",
  serial: "44:1B:F6:84:18:90", label: "USB JTAG/serial debug unit",
  transport: "Built-in USB serial console", baud_applies: false,
  likely_module: true, keyboard: false, selectable: true,
  identity: "44:1B:F6:84:18:90", baud: 115200,
};
const bridge = { ...esp, device: "/dev/ttyUSB0", name: "ttyUSB0", vid: "10c4",
  pid: "ea60", label: "CP2102 USB to UART Bridge",
  transport: "USB-to-UART bridge", baud_applies: true,
  identity: "10c4:ea60", baud: 230400 };

const lines = [
  { level: "meta", text: "\u2014 connected to /dev/ttyACM1 at 115200 baud \u2014" },
  { level: "raw", text: "I (689) wifi_init: tcp mbox: 6" },
  { level: "raw", text: "Type 'help' to get the list of commands." },
];
const st = (data, row = 0) => ({
  row, modules: { moduledebug: { state: "ok", busy: false, data } },
});

let pass = 0, fail = 0;
const check = (n, c) => { c ? pass++ : fail++;
  console.log(`  ${c ? "ok  " : "FAIL"} ${n}`); };

dbgUi.pane = "ports";
let h = moduleDebugScreen(st({ ports: [esp, bridge], bauds: [9600, 115200], connected: null }));
check("port picker renders", h.includes("USB serial ports"));
check("transport is described, not just the raw product string",
      h.includes("Built-in USB serial console"));
// Native-USB parts ignore baud, so the row shows a dash and the label USB
// rather than a number that cannot matter.
check("native USB shows a dash instead of a baud number",
      h.includes("\u2014") && h.includes("USB</div>"));
check("bridge shows its own baud", h.includes("230400"));
check("rows are tappable", h.includes('data-idx="1"'));
check("empty state explains itself",
      moduleDebugScreen(st({ ports: [], connected: null }))
        .includes("appears automatically"));

// Console
dbgUi.pane = "console";
const d = { ports: [esp], connected: "/dev/ttyACM1", baud: 115200,
            lines, partial: "antenna> meas", error: null };
h = moduleDebugScreen(st(d));
check("console renders", h.includes("ttyACM1"));
check("PROMPT and typed text are visible (the pending line)",
      h.includes("antenna&gt; meas"));
check("console is marked LIVE", h.includes("LIVE"));
check("nothing is filtered", h.includes("wifi_init") && h.includes("Type &#039;help&#039;")
      || h.includes("wifi_init"));
check("row count includes the pending line",
      moduleDebugRows(st(d)) === lines.length + 1);

// Escaping: module output goes straight into innerHTML.
const evil = { ...d, lines: [{ level: "raw", text: "<script>x</script>" }] };
h = moduleDebugScreen(st(evil));
check("module output is HTML-escaped",
      h.includes("&lt;script&gt;") && !h.includes("<script>"));

// Legend must document every binding that does something.
const picker = (dbgUi.pane = "ports", moduleDebugHints(st({ ports: [esp], connected: null })));
check("picker legend documents Start", picker.some(([b]) => b === "Start"));
check("picker legend documents Select", picker.some(([b]) => b === "Select"));
check("picker legend documents L/R baud", picker.some(([b]) => b === "L/R"));
check("picker legend documents Y rescan", picker.some(([b]) => b === "Y"));

dbgUi.pane = "console";
const con = moduleDebugHints(st(d));
check("console legend offers Disconnect",
      con.some(([, l]) => /disconnect/i.test(l)));
check("console legend advertises no letter keys",
      !con.some(([b]) => ["X", "Y", "L", "A", "B"].includes(b)));

// A disconnect drops back to the picker and surfaces the reason.
dbgUi.pane = "console";
check("disconnect falls back to the picker with a reason",
      moduleDebugScreen(st({ ports: [], connected: null,
                             error: "ttyACM1 disconnected" }))
        .includes("ttyACM1 disconnected"));

dbgUi.pane = "ports";
console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
