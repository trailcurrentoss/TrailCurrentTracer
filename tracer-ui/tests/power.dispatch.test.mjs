// Dispatch tests for the power button.
//
// The physical power button no longer powers the unit off by itself: the image
// sets HandlePowerKey=ignore so a brushed button in a bag cannot kill a capture
// (image/layer/tracer-base.yaml). tracerd watches the Pi's gpio-keys device and
// forwards a short press as {"t":"ev","m":"power","d":{"ev":"confirm_shutdown"}},
// and the GUI turns that into a confirmation dialog.
//
// That makes this wiring load-bearing in a way the old path was not. If the
// frame stops reaching a handler, the button silently does nothing at all and
// the only remaining way to power down is a long press — which most operators
// will never discover. Worth a test that drives the REAL frame path rather than
// calling an internal function.
//
// Run: node tracer-ui/tests/power.dispatch.test.mjs

globalThis.location = { hostname: "127.0.0.1", port: "8710" };
// Snapshot frames call update(), which batches renders through rAF. Node has
// no such thing; run the callback inline so a "snap" frame does not throw and
// mask the assertion it was meant to set up.
globalThis.requestAnimationFrame = (fn) => { fn(); return 0; };

// Minimal WebSocket stub. connect() constructs one and assigns the handlers,
// so capturing the instance gives a handle to feed frames through onmessage —
// the same route a real daemon frame takes.
let sock = null;
globalThis.WebSocket = class {
  constructor(url) { this.url = url; sock = this; }
  close() {}
};

const { connect, onPowerKey, onButton } = await import("../src/store/store.js");

connect();

function feed(frame) {
  sock.onmessage({ data: JSON.stringify(frame) });
}

let pass = 0, fail = 0;
function ok(name, cond) {
  if (cond) { pass++; console.log(`  ok   ${name}`); }
  else { fail++; console.log(`  FAIL ${name}`); }
}

// ── the frame reaches a handler ──────────────────────────────────────
let fired = 0;
onPowerKey(() => { fired++; });

feed({ t: "ev", m: "power", d: { ev: "confirm_shutdown" } });
ok("a power-key frame invokes the handler", fired === 1);

// ── it does not fire on anything else ────────────────────────────────
feed({ t: "ev", m: "power", d: { ev: "something_else" } });
ok("an unrelated power event does not prompt", fired === 1);

feed({ t: "snap", m: "power", d: { brightness: 80 } });
ok("a power snapshot does not prompt", fired === 1);

// A shutdown prompt raised by a stray input frame would be alarming, and the
// two share the "ev" frame type.
feed({ t: "ev", m: "input", d: { btn: "a", phase: "down" } });
ok("a button press does not prompt", fired === 1);

// ── input dispatch still works (regression) ──────────────────────────
// The power case was added alongside the input case in the same switch arm.
// Breaking button dispatch would brick every screen, so prove it survived.
let btns = [];
onButton((b, phase) => { btns.push(`${b}:${phase}`); });
feed({ t: "ev", m: "input", d: { btn: "start", phase: "down" } });
ok("button frames still dispatch", btns.length === 1 && btns[0] === "start:down");

// ── repeats ──────────────────────────────────────────────────────────
// The daemon filters autorepeat and key-up, so one press is one frame. If that
// ever regresses, the UI guard (confirmBox.open) is the second line of defence
// — but the handler itself should still be called once per frame, not coalesced.
feed({ t: "ev", m: "power", d: { ev: "confirm_shutdown" } });
ok("a second press is delivered, not swallowed", fired === 2);

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
