# Tracer daemon ↔ UI API

**Status: IMPLEMENTED — this documents the shipped API.**

Per the build prompt's working method, this contract was written first and
reviewed before either side was built. Both sides now exist — `tracerd`
(Python, daemon `VERSION = "0.4.1"`, `tracerd/tracerd/__main__.py`) and
`tracer-ui` (JS) — and this document has been corrected to match what the code
actually does. Where the original draft claimed something the implementation
does not deliver, the claim is rewritten and marked with a **Gap:** note
rather than silently dropped.

Data shapes are lifted from the real Headwaters sources, not invented — see
[Provenance](#provenance) for the file and line behind each one.

---

## 0. Hard constraints

Two rules that override everything else in this document. Where a mock screen or
a build-plan feature conflicts with them, they win.

### C1 — Zero changes to any other repository

Tracer must work against the fleet **exactly as it exists today**. No new MQTT
topics, no new HTTP endpoints, no firmware changes on Bearing/Solstice/Tapper/
Switchback/Playbill, no schema changes, nothing added to Headwaters.

If a screen needs data that nothing currently exposes, the answer is to render
`--` and say so — **not** to add an endpoint upstream.

### C0 — Tracer debugs Headwaters, so it reads the SUBSTRATE

The most important constraint, because it overrides the obvious design.

**Tracer must not reach Headwaters through its HTTP API.** If it did, it could
only ever report what the PWA already reports — and when the backend is the
broken layer, an API-based tool is blind to exactly the bug you are hunting.

So Tracer talks to the layers *underneath* the backend:

| Want | Route | Not |
|---|---|---|
| Live traffic | MQTT directly | `/api/...` |
| Saved modules | MongoDB via `docker exec mongosh` over SSH | `GET /api/modules` |
| Container state | `docker ps` over SSH | a health endpoint |
| Container + system logs | `docker logs`, `journalctl` over SSH | a log endpoint |

Mongo is **deliberately not exposed on the network** — Headwaters'
`docker-compose.yml` maps no ports for it — so database access goes through
`docker exec` on the SSH channel, not a TCP connection.

This is why Tracer duplicates a little Headwaters logic (record building in
`modules/discovery.py`). That is not accidental coupling: to check a system you
need an independent expectation of what it should have done. **Where Tracer's
computed record and Headwaters' stored record disagree, that disagreement is
the finding.**

It also revises an earlier judgement in this document. I first argued Tracer
must never write what Headwaters could write, to avoid a split brain. That was
right for a *monitoring* tool and wrong for a *debugging* one — the split is
the instrument.

### C2 — Monitoring tool, and a headless console for Headwaters

C2 constrains what Tracer does **on its own**, not what a technician can do
through it. Two different things, and conflating them gets the design wrong:

- **Tracer's automated behaviour is observation-only.** Nothing polls by writing.
  No background task publishes, transmits, restarts, or reconfigures anything.
  Every module in [§4](#4-module-payloads) is a reader.
- **The Terminal is an operator-driven access path and is deliberately not
  restricted.** A core purpose of this device is replacing the keyboard, mouse
  and monitor you would otherwise hook up to a Headwaters box in a vehicle bay.
  That means a real interactive SSH session with the operator's own credentials
  and the operator's own authority.

An interactive shell can obviously do anything, so "monitoring tool" cannot mean
"the shell is read-only" — it means Tracer does not act on the fleet by itself.
Authority at the Terminal belongs to the human holding it.

The middle ground is one-touch fleet actions — as built: Discovery Onboard,
Firmware Deploy, MQTT Publish, Simulate Send. (The mock's CAN Send frame and
container Restart were never built — see [§5.1](#51-what-tracer-may-and-may-not-do).)
Those are Tracer acting, not a human typing.

### What C1 already changed in this document

Auditing the design against C1 found one hard blocker and one improvement:

- **`headwaters` cannot show per-container health.** The build plan called for
  polling "Docker/compose state and container health endpoints". Those do not
  exist. Headwaters' backend exposes exactly one health route
  (`index.js:64  app.get('/api/health')`) and no container-level API at all.
  Corrected in [§4.5](#45-headwaters--ssh-transport-with-graceful-fallback).
- **`discovery` asks Headwaters to browse — it does NOT browse itself.**
  I originally had Tracer running its own Zeroconf browse, on the grounds that
  it needs nothing from anyone. **That was wrong**, and the reason is worth
  stating: Headwaters is the system of record for what is on the rig. A device
  Tracer discovered and onboarded by itself would be registered nowhere that
  matters, and the two would disagree the moment either was restarted — a split
  brain in a diagnostic tool, which is the worst place for one.

  So Tracer is a **window onto Headwaters' discovery**, not a parallel
  implementation: it watches `discovery/browse/found`, triggers a sweep with
  `discovery/browse/start`, and asks Headwaters to onboard with
  `discovery/confirm/request` / `discovery/claim/request`. Every one of those
  topics already exists, so C1 still holds. See [§4.4](#44-discovery).

---

## 1. Transport

One origin, `http://127.0.0.1:8710`, loopback only, no TLS, no auth.

| | |
|---|---|
| WebSocket | The only push channel. Carries state and events. The UI connects to `/stream`, but the path is a convention, not a route — the server upgrades **any** GET carrying `Upgrade: websocket`. |
| `POST /rpc` | Single JSON-RPC-ish endpoint for all commands. |
| `GET /health` | Liveness. Body: `{"ok": true, "daemon": "<VERSION>", "mock": <bool>, "modules": {<name>: <state>, …}}`. Intended for `systemd`/scripts — nothing in the UI actually calls it. |
| `GET /hw/*` | Map-asset proxy to Headwaters (`mapproxy.py`, `PREFIX = "/hw/"`), so the page never has to trust the rig's private CA. |
| `GET /maps/*`, `/maps-static/*`, `/libs/*` | Passthrough prefixes proxied to Headwaters (the map style references its assets root-relative). `Range` requests are supported. |
| static files | With `--ui-dir`, the built `tracer-ui` bundle is served from the same origin, with an SPA fallback for unrouted GETs. |

There is no `GET /export/<id>` — the draft proposed one for capture files and
log dumps, but it was never built. Captures are loaded and replayed through
the daemon itself (`capture.load` / `play`), not streamed to the browser.

Binding to loopback is the whole authorization model: the GUI is unprivileged and
`tracerd` performs every privileged action. Nothing else on the device or the
network can reach 8710.

### Why a WebSocket *and* a REST endpoint

The socket is for state that changes on its own; `/rpc` is for things the operator
asks for. Commands need a per-call result and error, and multiplexing
request/response over a broadcast socket means inventing correlation IDs for no
benefit. The split keeps both halves simple.

---

## 2. Framing

Every WebSocket frame is a JSON object with a `t` discriminator.

Server → client:

```jsonc
{ "t": "hello",  "v": 1, "daemon": "0.4.1", "mock": false,
  "ts": 1756704000.0, "caps": { ... }, "apps": [ ... ] }
{ "t": "snap",   "m": "mqtt",  "seq": 41, "d": { ... } }   // full module state
{ "t": "apps",   "d": [ ... ] }                             // launcher tiles
{ "t": "ev",     "m": "input", "d": { ... } }              // transient, not state
{ "t": "toast",  "level": "info", "text": "Capture saved to /media/usb0" }
{ "t": "pong" }                                             // reply to a client ping
```

**There is no `patch` frame.** The draft proposed an RFC-6902 delta channel;
it was never built on either side — the hub only ever emits full `snap`
frames, and the UI has no patch handler. A snapshot per module per tick turned
out to be cheap enough over loopback that the delta machinery bought nothing.

**`snap` frames are per-module and independently sequenced.** `seq` increments
per module. If the UI sees a gap it can send `resync` for that module and get a
fresh `snap`. This is what keeps one stalled subsystem from corrupting another's
view — a direct requirement of acceptance criterion 3.

**`apps`** carries the launcher tile list, re-sent whenever any tile's derived
status changes (sending it only in `hello` froze the launcher at connect-time
state). Each tile is:

```jsonc
{ "id": "mqtt", "short": "MQTT Inspector", "icon": "pulse",
  "tint": "#52A441", "glyph": "#000",
  "status": "46 msg/s", "statusColor": "#7BC96A", "state": "ok" }
```

**`caps`** is `{<module name>: true}` for every registered module — a roster,
nothing more. There is no per-unit capability probing behind it (no backlight
detection, for example), and the UI currently never reads it. See the note in
[§3](#3-module-availability--the-degradation-contract).

Client → server:

```jsonc
{ "t": "resync", "m": "mqtt" }   // unknown or absent "m": full snapshot of EVERY module
{ "t": "ping" }                  // answered with {"t":"pong"}
```

Both client→server frames exist in the daemon, but the shipped UI sends
neither today: it relies on the socket closing to detect a dead daemon, and on
the fresh full snapshot every (re)connect delivers rather than per-module
resyncs.

### Rate discipline

The daemon coalesces rather than streaming raw, and the journal stays quiet at
rest. Concretely:

- Every module coalesces into a **single `snap` frame per 100 ms tick**, at
  most. Only the newest snapshot per module survives a tick.
- A module with nothing to say sends **nothing** — no empty heartbeat frames.
- `mqtt` at 46 msg/s (a mock fixture, not a measurement — see
  [§7](#7-mock-mode)) therefore produces ≤10 frames/s, not 46.
- **One exception:** terminal output bypasses the 100 ms tick via the hub's
  `broadcast_now()` — waiting up to 100 ms to see the character you just typed
  makes the shell feel broken. Safe because the terminal always sends its whole
  visible buffer, so a dropped or reordered frame cannot corrupt the view.
- `ev` frames are never coalesced (dropping a button press would make the
  device feel broken); they flush in order on the next tick.

---

## 3. Module availability — the degradation contract

This is the most important section, because acceptance criterion 3 turns on it.

Every module reports one of four states, and **`unavailable` is a normal state,
not an error**:

| `state` | Meaning | UI renders |
|---|---|---|
| `ok` | Working. | Live values. |
| `degraded` | Working with caveats (e.g. broker up, TLS failed back to plaintext). | Values plus a warning chip. |
| `unavailable` | Dependency absent — no CAN adapter, no gpsd, no broker, no route. | Values as `--`, labelled reason. |
| `starting` | Not yet resolved on this boot. | Skeleton, no `--` yet. |

```jsonc
{
  "t": "snap", "m": "can", "seq": 1,
  "d": {
    "state": "unavailable",
    "reason": "no can0 interface",     // short, human, lowercase, no punctuation
    "since": 1756704000.0,
    "busy": false,                      // long-running work in progress
    "busy_note": "",                    // what that work is ("scp package…")
    "data": null                        // always null unless state is ok/degraded
  }
}
```

`busy` / `busy_note` are set by operations that take real time (a firmware
deploy, a CA fetch) and are published immediately rather than on the next
tick, so the operator sees the work start.

Three rules that make the criterion testable rather than aspirational:

1. `reason` is written for a technician standing next to a vehicle. "no can0
   interface", not "ENODEV".
2. A module entering `unavailable` **keeps its polling loop alive** at a backoff
   interval and recovers on its own. It never exits, and it never blocks another
   module — each is an independent asyncio task with its own supervisor.
3. Transitions are logged at most **once per state change**, never once per poll.
   This is what keeps `journalctl -u tracerd` quiet at rest (criterion 5).

`caps` in the `hello` frame is just `{module: true}` for every registered
module — a roster. The draft intended per-unit capability reporting (e.g. hide
the Settings `Brightness` row outright when no backlight device exists), but
no such probing was built and the UI never reads `caps` at all. **Gap:** the
originally intended "hide controls that can never work on this unit" behaviour
is not implemented.

For testing this contract, `tracerd --force-unavailable MOD[,MOD…]` injects a
dependency-absent fault into the named modules: it replaces each module's
`poll()` with one that raises, while deliberately leaving the supervisor loop
untouched — the point is to prove the *real* degradation path runs, not to
bypass it.

---

## 4. Module payloads

Only the shapes with non-obvious provenance are given in full here. Field names
match the Headwaters wire format exactly wherever one exists — renaming them
would mean a translation layer in two places and a mismatch the first time
Headwaters changes.

### 4.1 `input` — the only input path

```jsonc
{ "t": "ev", "m": "input", "d": { "btn": "dpad_up", "phase": "down", "ts": 1756704000.123 } }
```

`btn` ∈ `dpad_up dpad_down dpad_left dpad_right a b x y l r start select`
`phase` ∈ `down` `up` `hold` — `hold` is the **kernel's autorepeat** (evdev
value 2) passed through, so timing follows the kernel's repeat settings. The
keymap file has `hold.threshold_ms` / `repeat_hz` fields; they are read but
**never used** — the draft's "500 ms then 8 Hz" was not implemented.

Per [hardware.md](hardware.md#input--a-usb-hid-keyboard-not-a-gamepad), these are
produced by translating evdev keycodes through a keymap loaded from the first
of `/var/lib/tracer/keymap.json`, `/var/lib/tracer/keymap.default.json`, or
the packaged `tracerd/keymap.default.json` (not `settings.json`). The evdev
device is auto-detected from `/proc/bus/input/devices` (override with
`--input-device`), not hardcoded to `event0`. **The GUI never sees a keycode.**

Loss of the keyboard is a normal state, not a fault — the RP2040 can drop into
BOOTSEL and strand every button while touch keeps working:

```jsonc
{ "t": "snap", "m": "input", "d": {
    "state": "unavailable", "reason": "keyboard not enumerated",
    "data": null } }        // data is ALWAYS null outside ok/degraded — §3
```

The UI must show this in the status bar and remain fully operable by touch.
Note the consequence of the §3 envelope rule: `data` is null here, so the UI
cannot learn `touch: true` from this frame — a known implementation gap; the
UI simply assumes touch keeps working.

#### Input is modal, and the daemon owns the mode

Six of the twelve buttons are bound to literal letter keys (A, B, X, Y, L, R —
captured from hardware). Those same keys have to type. So `input` runs in one of
two modes and reports which:

```jsonc
{ "t": "snap", "m": "input", "d": {
    "state": "ok",
    "data": { "mode": "nav", "touch": true, "keyboard": true } } }
```

| Mode | Emits |
|---|---|
| `nav` | `ev` button events only. Typeable keys are swallowed. |
| `text` | `ev` text events; letters pass through verbatim. Arrows still navigate within the field. |

```jsonc
{ "t": "ev", "m": "input", "d": { "text": "a", "ts": … } }               // printable
{ "t": "ev", "m": "input", "d": { "key": "backspace", "ts": … } }        // also: enter, escape, tab
{ "t": "ev", "m": "input", "d": { "text": "", "ctrl": true, "ts": … } }  // Ctrl-<key> as C0 char
```

The GUI **requests** a mode change when focus enters or leaves a text field, but
never assumes it took effect — it renders from the `mode` the daemon reports:

```jsonc
POST /rpc  { "id": "c9", "m": "input", "op": "set_mode",
             "args": { "mode": "text", "sink": "gui" } }
     200   { "id": "c9", "ok": true, "d": { "mode": "text", "sink": "gui" } }
```

`sink` ∈ `gui` `terminal` `moduledebug` — which consumer text events are
destined for. **Sharp edge:** `sink` defaults to `gui` when omitted, so
omitting it *resets* the sink; a caller that owns the sink must say so on
every `set_mode`.

One source of truth matters here more than usual: if the GUI believed it was in
text mode while the daemon was in nav mode, every keystroke would silently fire a
button action instead of typing. Leaving text mode is **GUI-driven** (the GUI
handles Escape and requests `nav`); in text mode `select` is forwarded like any
other button — the daemon does **not** force a return to `nav` on `select`,
contrary to the draft. The daemon-side safety net is different: it resets to
`nav` when the **last client disconnects**, so a crashed or reloaded GUI can
never strand the device in text mode with every button typing letters.

**There is no on-screen keyboard anywhere in Tracer** — the device has a real
QWERTY keyboard, so every text field takes physical input. The API therefore has
no virtual-key or character-picker operations, by design.

One dev-only operation exists: `input.press` (`args: {btn, phase}`) synthesizes
a button event, and is accepted **only in mock mode** — it lets `make mock`
drive the UI without hardware.

### 4.2 `mqtt`

Module-level `data`:

```jsonc
{ "connected": true, "status": "connected", "tls_verified": true,
  "paused": false,
  "rate": 46.0,                 // msg/s, sampled over ~1 s windows
  "total": 128841, "dropped": 0,
  "topics":   [ ... ],          // one node per topic, sorted by path
  "messages": [ ... ] }         // most recent messages, newest first
```

Each topic node:

```jsonc
{ "path": "local/energy/status", "count": 1841,
  "rate": 2.0,                                   // msg/s, same sampling window
  "retained": false, "qos": 0,
  "json": true,                                  // sibling of "last", not inside it
  "last": { "ts": 1756704127.114,
            "payload": "{\"batteryVoltage\":13.4,\"solarInput\":850}" } }
```

Each `messages[]` entry: `{ts, topic, payload, qos, retain}` — note the field
is `retain` (per-message flag) on message entries but `retained` on topic
nodes.

`payload` stays a **string**, always. The daemon reports whether it parsed as JSON
via `json`, but does not pre-parse it — the Inspector must be able to show
malformed payloads verbatim, which is precisely when the tool earns its keep.

Subscribes a plain `#`, unconditionally — there is no ignore list. Known topic
namespaces:

- `local/…` — 15 topics published by the CAN bridge (energy, water, gps/{latlon,alt,details,time}, level/{tilt,status,corners}, airquality/{status,temphumid,safety}, spoor/{0,1,2}/inputs)
- `can/inbound`, `can/outbound`
- `discovery/browse/{start,stop,found}`, `discovery/confirm/{request,response}`, `discovery/claim/{request,response}`
- `rv/…` — the cloud mirror namespace

### 4.3 `can`

Aggregated per arbitration ID, matching the mock's grouped view:

```jsonc
{ "id": "0x120", "dlc": 6, "data": "0D 68 03 52 00 00",
  "rate": 2.0, "first_seen": 1756703000.0, "last_seen": 1756704127.9,
  "count": 2254,
  "decoded": { "source": "dbc", "text": "Solstice MPPT · 13.4 V · 850 W",
               "signals": { "BatteryVoltage": 13.4, "SolarInput": 850 } } }
```

`decoded.source` ∈ `dbc` `map` `none`. When `none`, `text` is null and the UI
falls back to raw hex — the prompt's explicit requirement.

Decoding is driven by `TrailCurrentDocumentation/TrailCurrent.dbc` (88 messages,
258 signals — the real fleet DBC, not a subset). The YAML map ported from
`can-to-mqtt.py` is the fallback for IDs the DBC does not cover.

> **Note for review.** `can-to-mqtt.py` carries an explicit standing instruction
> that it is a wire-only passthrough with no per-ID filter, and that one must
> never be added. Tracer's `can` module is a *read-only observer* and does not
> change that. But the Capture module's topic filter and the CAN Sniffer's
> X=Freeze both hide frames from the operator's view. That is a display filter,
> not a bus filter, and I have kept them purely presentational for that reason.
> Flagging it explicitly so the distinction is deliberate and reviewed.

#### CAN detection is strictly passive — never probe by transmitting

**Tracer must never transmit a CAN frame to test the bus.** This is a correctness
requirement, not a preference.

A CAN transmitter needs at least one other node to ACK the frame. If Tracer is
alone on the bus — exactly the case when something is wrong and the operator is
diagnosing it — every transmit fails with an ACK error, the controller's TX error
counter climbs, and it escalates through error-passive to **bus-off**. A probe
intended to detect a fault would *create* one, and take the interface down while
doing it.

So every state below is derived without sending anything:

| State | Detected by | Needs traffic? |
|---|---|---|
| `no can0 interface` | `/sys/class/net/can0` absent | no |
| `can0 down` | `/sys/class/net/can0/operstate` | no |
| `bus-off` / `error-passive` / `error-warning` | netlink `IFLA_CAN_STATE` (`ip -details link show can0`) | no |
| bus error detail (ACK, bit, stuff, CRC, form) | SocketCAN **error frames** — set `CAN_ERR_MASK` on the socket and they arrive as ordinary reads | no |
| TX/RX error counters | netlink `IFLA_CAN_BERR_COUNTER` | no |
| frame rates, per-ID aggregation | ordinary reception | yes |

The error-frame mechanism is the important one: enabling `CAN_ERR_MASK` means the
controller *reports* bus faults to us as receivable frames. We learn about ACK
errors, bit errors and state transitions caused by **other** nodes' traffic,
without ever driving the bus ourselves.

##### The one genuinely ambiguous case

A quiet bus and a disconnected bus look identical to a passive listener: zero
frames, no errors, `error-active`. Nothing can distinguish them without
transmitting, and transmitting is forbidden.

Tracer must therefore **not guess**. `can0` up with no traffic reports exactly
that — `state: "degraded"`, `reason: "can0 up, no frames seen"` — and the UI shows
it as an observation, never as a verdict like "bus disconnected". Overclaiming
here would send a technician to re-terminate a bus that was merely idle.

For the same reason, any future checklist-style "CAN bus terminated, 0 error
frames" affordance must be worded as an operator confirmation: Tracer reports
the error-frame count, and the human confirms the physical termination. (The
design mock's Checklist app was not built.)

### 4.4 `discovery`

Field-for-field the payload `discovery-mdns.py` publishes to
`discovery/browse/found`, plus Tracer's own lifecycle fields:

```jsonc
{ "hostname": "bearing-3f2a", "type": "bearing", "fw": "1.4.2",
  "addr": 12, "canid": "0x150",          // optional — ESP32 MCUs only
  "deviceId": null, "canInstance": null,  // optional — Playbill only
  "target": null,                         // optional — Tapper variant
  "onboard": "confirm",                   // "confirm" (MCU) | "claim" (playbill)
  "name": "Bearing 12",                   // generated display name
  "first_seen": …, "last_seen": …,
  "state": "found",                       // found | onboarded | failed
  "detail": "" }                          // human-readable progress/error string
```

`state` ∈ `found` `onboarded` `failed`. There is no `ttl_expires` field on the
wire — the TTL is evaluated internally (an expired, un-onboarded device is
dropped from the list). `detail` carries onboarding progress or the failure
reason ("confirming on the device…", "device did not answer the confirm
marker"). `name` is a generated display name; when a second device of the same
type appears, the first sibling's name is back-filled with its address suffix
so the two stay distinguishable.

`onboard` is `claim` when `type == "playbill"`, `confirm` otherwise — mirroring
the daemon's own rule, so the two never disagree.

`addr`, `canid`, `deviceId`, `canInstance`, `target` are **genuinely optional** in
the source and must stay optional here. A Playbill TXT record has no `addr`; an
MCU has no `deviceId`. The UI renders whichever are present.

Browsed types: `_trailcurrent._tcp` plus `_mqtt._tcp`, `_ssh._tcp`, `_http._tcp`.

### 4.5 `headwaters` — SSH transport, with graceful fallback

Headwaters exposes no container API — its backend has exactly one health route
(`containers/backend/src/index.js:64`) and no Docker or compose endpoint.

But **both machines are full Linux**, so Tracer reads container state the same
way a technician would: over SSH, running `docker ps` and `docker logs` on the
Headwaters box. That satisfies C1 completely — no new endpoint, no new topic, not
one line changed in the Headwaters repo — and both commands are read-only, so it
satisfies C2 as well.

One thing already confirmed in the Headwaters image build makes this work without
`sudo`:

```
layer/trailcurrent-base.yaml:566
    chroot "$1" usermod -aG docker "$IGconf_device_user1"
```

The app user is in the `docker` group, so `docker ps` and `docker logs` run
directly.

#### Two tiers, degrading independently

The module reports which tier it achieved as a single module-level `tier` key,
so the UI never implies more certainty than it has:

| `tier` | Transport | Gives |
|---|---|---|
| **`"ssh"`** | one batched SSH command (`docker ps`, `docker inspect`, system stats) | Per-container state, restart count, uptime, plus the CPU/memory/disk/temp stats. Everything the mock draws. |
| **`"probe"`** | TCP connect to port 443 | **Reachable** only. No metrics → `--`, with a `note` saying why ("Set Headwaters Access in Settings to see system stats", or the SSH error). |

`tier` is module-level; there is **no per-field `source`** — the draft's
`source: "ssh" | "probe" | "mqtt"` on every field was never built. Nor was the
draft's third MQTT-only tier (`local/system/stats`): with neither key nor
credentials the module reports `tier: "probe"` and no metrics.

The distinction matters in the UI: a TCP probe proves a port accepts
connections, not that the service behind it is working. Probe tier must render
"Reachable", never "Healthy" — only the SSH tier can honestly say healthy,
because only it has read Docker's own state.

#### Operations

| Op | Does |
|---|---|
| `refresh` | Force a poll now instead of waiting out the interval. |
| `fetch_ca` | Copies the MQTT broker CA off Headwaters over SSH and installs it as Tracer's MQTT trust root. |
| `enrol_key` | Installs Tracer's public key in the app user's `authorized_keys` (below). |
| `clear_ca` | Clears the stored CA setting. |

#### Key enrolment — type the password once, ever

The SSH tier needs `sshd` running on Headwaters and Tracer's public key in the
app user's `~/.ssh/authorized_keys`. **This is deployment provisioning, not a repo
change** — nothing in the Headwaters codebase is modified, which is the line C1
actually draws.

Typing a password on a 3.5" thumb keyboard is miserable, and a technician would
be doing it on every reconnect. So the design is: **enter it once, then never
again.**

`Settings → Headwaters Access` holds Host (default `headwaters.local`), User
(default `trailcurrent`), and Password, plus the **Enrol key** action.

```jsonc
POST /rpc { "id":"c12", "m":"headwaters", "op":"enrol_key", "args": {} }
     200  { "id":"c12", "ok": true, "d": { "enrolled": true,
                                           "host": "headwaters.local",
                                           "user": "trailcurrent" } }
```

`enrol_key` **ignores its args entirely** — host, user, and password all come
from the stored settings (`headwaters_host`, `headwaters_user`,
`headwaters_password`, falling back to `mqtt_password`). If no password is
set, it fails with `"set the Headwaters password first"`. It takes no
`confirm` flag.

How the implementation actually behaves:

- Tracer generates an **ed25519** keypair on demand at
  `/var/lib/tracer/ssh/id_ed25519`, mode `0600`, on the one writable partition.
- The password is a **persisted setting**: `headwaters_password` is a settings
  key serialized to `/var/lib/tracer/settings.json` (written atomically, mode
  `0600`), masked on the wire — the GUI is only ever told whether it is set,
  never the value — and re-read from the store on every `enrol_key` and
  `fetch_ca`. **Gap:** the draft specified the password be used once and
  "never written to disk, never logged, never held after the call returns";
  at-rest persistence means that property is **not implemented**. The file
  permissions are the only protection.
- Enrolment is idempotent — re-running it repairs a broken `authorized_keys`
  without duplicating the entry.
- Host keys are handled with `StrictHostKeyChecking=accept-new`: the
  first-seen key is trusted **silently**, and a later mismatch makes the SSH
  call fail with a raw ssh error. **Gap:** the draft specified pinning on
  first enrolment with a clear mismatch warning in the UI; there is no
  pinning store and no friendly warning — a changed host key just surfaces as
  a failed operation.
- If enrolment never happens, the module simply runs at the probe tier. SSH is
  an enhancement, never a prerequisite.

The private key lives on Tracer alone; nothing is ever copied off the device.

##### Optional hardening, not the default

An `authorized_keys` forced command (`command="…",no-pty,no-port-forwarding …`)
would restrict the Tracer key to read-only Docker calls. That was my first
instinct and it is **wrong as a default here** — it would block the interactive
shell that is a core purpose of the device (see [C2](#c2--monitoring-tool-and-a-headless-console-for-headwaters)).

Kept as a documented option for operators who want a locked-down Tracer on a
shared rig, applied to a second, separate key. Not on by default.

Whether `openssh-server` ships enabled in a given Headwaters image is a per-rig
provisioning question — there is no explicit install in `layer/*.yaml`. Where
sshd is absent, enabling it is provisioning, not a repo change, and the module
runs at the probe tier until then. The SSH tier has been exercised against a
live Headwaters host (the installed unit names in `logs` were verified there).

### 4.6 `logs` — pulled tails, three sources, one screen

| `source` | Reads | How |
|---|---|---|
| `local` | Tracer's own journal | `journalctl` on this device |
| `host` | a Headwaters **host** unit (`cantomqtt`, `discovery-mdns`, `deployment-watcher`, `map-watcher`, `os-settings`, `time-from-bearing`, `can0`, `docker`) | `journalctl -u <unit>` over the [§4.5](#45-headwaters--ssh-transport-with-graceful-fallback) SSH channel |
| `container` | a Headwaters container (`mosquitto`, `backend`, `frontend`, `mongodb`, `photon`, `valhalla`) | `docker logs --tail` over SSH |

The `host` source matters because the CAN-to-MQTT bridge is **not** a
container — `docker logs` will never show it; `journalctl -u cantomqtt` is the
only way to see its errors. (Unit names are the *installed* names, which
differ from the repo filenames; verified against a live host.)

Every fetch is a bounded tail — the last `MAX_LINES = 400` lines — refreshed
periodically and republished as an ordinary module snapshot with level and
search filters applied on Tracer. Nothing is written to disk, nothing
streams: the fetch is one `journalctl`/`docker logs` to stdout per refresh,
read-only, Tier 1 under [§5.1](#51-what-tracer-may-and-may-not-do), and the
grep load stays on Tracer rather than the machine being diagnosed.

Operations:

```jsonc
POST /rpc { "id":"c20", "m":"logs", "op":"select",
            "args": { "source": "container", "unit": "mosquitto" } }
```

| Op | Args | Does |
|---|---|---|
| `select` | `source` ∈ `local` `host` `container`, `unit` | Switch view; old lines are discarded and a fresh tail is pulled. |
| `level` | `level` ∈ `ALL` `WARN` `ERR` (omitted: cycles) | Severity filter. Container lines have no priority field; severity is inferred from the text. |
| `search` | `query` | Substring filter, applied on Tracer. |
| `refresh` | — | Pull a fresh tail now. |

The draft's `fetch_remote` operation — copying whole logs to
`/var/lib/tracer/logs/<host>/…` with progress, cancellation, and truncation
reporting — was **never built**. What shipped is the bounded-tail model above.

### 4.7 Remaining modules

`net`, `gnss`, `capture`, `firmware`, `power`, `settings`, `terminal`,
`simulate`, `system`, and `moduledebug` follow the same envelope. Their
payloads track the mock's data blocks (`NET_STATS`, `NET_CHECKS`, `HW_STATS`,
`CONTAINERS`, `GNSS_FIELDS`, `LOG_LINES`, `CAPTURES`, `FW_TARGETS`,
`SETTINGS`); the module sources in `tracerd/tracerd/modules/` are the
authoritative shape reference — there are no separate schema files.

`terminal` takes a **target**: `local` (a pty on Tracer) or a saved SSH host,
reusing the enrolled key from [§4.5](#45-headwaters--ssh-transport-with-graceful-fallback)
so no password is typed. Reaching a Headwaters box without hooking up a keyboard,
mouse and monitor is a primary purpose of the device, so the host picker belongs
in the Terminal itself rather than buried in Settings. Terminal output is the
one path that bypasses the 100 ms tick — see
[Rate discipline](#rate-discipline).

Three modules exist that the draft never described:

- **`simulate`** — DBC-driven frame synthesis. `frame` builds a frame's
  signal set from the vendored fleet DBC; `send`† encodes it and publishes to
  MQTT (`can/inbound` or `can/outbound`). It never writes SocketCAN — see
  [§5.1](#51-what-tracer-may-and-may-not-do).
- **`system`** — timezone / NTP / time / locale / region configuration of
  Tracer itself. Ops: `regions`, `zones`, `locales`, `countries`,
  `set_timezone`, `set_ntp`, `set_time`, `set_locale`, `set_region`.
- **`moduledebug`** — a USB-serial console for attached TrailCurrent modules.
  Ops: `ports`, `rescan`, `open`, `close`, `clear`, `send`, `set_baud`. It is
  also an `input` sink (`set_mode` `sink: "moduledebug"`).

(`osconfig.py` in the same directory is a helper used by `system` and
`settings`, not a registered wire module.)

One undocumented event: pressing the physical power button (the daemon owns it
via `HandlePowerKey=ignore`) does not shut down — it emits

```jsonc
{ "t": "ev", "m": "power", "d": { "ev": "confirm_shutdown" } }
```

and the GUI asks the operator; the actual `power.shutdown` still requires
`confirm: true` like any other guarded op.

---

## 5. Commands

```jsonc
POST /rpc  { "id": "c1", "m": "net", "op": "connect",
             "args": { "ssid": "Airstream-27", "psk": "…" } }

     200   { "id": "c1", "ok": true,  "d": { … } }
     200   { "id": "c1", "ok": false, "err": { "code": "failed",
                                               "msg": "incorrect password" } }
```

A command failure is **always** HTTP 200 with `ok: false`. Non-2xx is reserved for
transport-level problems, so the UI never has to disambiguate "the daemon is gone"
from "the password was wrong".

`err.code` takes exactly three values from the daemon:

| Code | Meaning |
|---|---|
| `bad_json` | The request body was not valid JSON. |
| `no_module` | `m` names no registered module. |
| `failed` | The operation raised — `msg` carries the human-readable reason. |

The UI adds a fourth, client-side only: `transport`, when the fetch itself
failed. There is no `auth_failed` code. Note that a rejected confirm guard
arrives as `code: "failed"` with `msg: "needs_confirmation"` — a client
matching `err.code === "needs_confirmation"` will never fire; match the `msg`.

The implemented operations, by module (**†** = requires `"confirm": true`):

| Module | Ops |
|---|---|
| `input` | `set_mode` (`mode`, `sink`), `press` (mock only) |
| `mqtt` | `pause`, `clear`, `reconnect`, `publish`† |
| `discovery` | `browse`, `stop`, `onboard`† |
| `capture` | `start`, `stop`, `rename`, `delete`†, `load`, `play`, `pause`, `loop`, `seek` |
| `firmware` | `browse`, `browse_close`, `select`, `deploy`†, `verify` |
| `net` | `scan`, `connect`, `forget`†, `recheck` |
| `terminal` | `open`, `close`, `write`, `signal` |
| `logs` | `select`, `level`, `search`, `refresh` |
| `can` | *(none — read-only)* |
| `headwaters` | `refresh`, `fetch_ca`, `enrol_key`, `clear_ca` |
| `gnss` | `check_tiles` |
| `simulate` | `frame`, `send`† |
| `settings` | `get`, `set`, `options`, `set_system`, `reset`† |
| `system` | `regions`, `zones`, `locales`, `countries`, `set_timezone`, `set_ntp`, `set_time`, `set_locale`, `set_region` |
| `moduledebug` | `ports`, `rescan`, `open`, `close`, `clear`, `send`, `set_baud` |
| `power` | `set_brightness`, `reboot`†, `shutdown`† |

### 5.1 What Tracer may and may not do

Auditing every operation against [C2](#c2--monitoring-tool-and-a-headless-console-for-headwaters)
sorts them into three tiers. The first two were always uncontroversial; the
third was the draft's open question, and is now settled as described below.

**Tier 1 — pure observation. Unambiguously in scope.**
MQTT subscribe · CAN receive (passive only, [never transmit](#can-detection-is-strictly-passive--never-probe-by-transmitting)) ·
Zeroconf browse · journald read · `docker ps` / `docker logs` over SSH ·
`GET /api/health` · TCP reachability probes · gpsd read · system stats.
No writes anywhere. This is the bulk of the product.

**Tier 2 — writes to Tracer itself only. In scope.**
WiFi join/forget · brightness · capture to local disk or USB · settings ·
SSH key enrolment · reboot/shutdown *of Tracer*.
Nothing outside the device is touched.

**Tier 3 — Tracer acting on the fleet. As built:**

| Operation | Effect |
|---|---|
| `discovery.onboard` | Asks Headwaters to onboard a module (`discovery/confirm/request` or `claim`); changes its state |
| `firmware.deploy` | Deploys a Headwaters release package over SSH (see [§5.2](#52-what-firmwaredeploy-actually-is)) |
| `simulate.send` | Encodes a DBC frame and publishes it to `can/inbound` / `can/outbound` on the broker |
| `mqtt.publish` | Arbitrary write to the broker |

Two Tier 3 actions the mock drew were **never built**: `can.send_frame`
(direct CAN transmit) and `headwaters.restart` (container restart). Frame TX
exists only as `simulate.send`, which encodes via the vendored DBC and
publishes to MQTT — it never writes SocketCAN, so the
[passive-detection rule](#can-detection-is-strictly-passive--never-probe-by-transmitting)
is never at risk from it: nothing in Tracer drives the bus, operator-initiated
or otherwise.

Every one of these satisfies **C1** — they use contracts that already exist, and
need nothing changed in any other repo. But each is Tracer acting on the vehicle
rather than observing it, which is what C2 rules out.

**Decided: Tier 3 stays in the GUI — single press, no confirmation modal.**

This narrows C2 to its precise meaning: Tracer's **background** behaviour is
observation-only — no polling loop writes, ever. Operator-initiated actions are
the operator's authority, whether typed at the Terminal or pressed on a row. The
constraint is about what the device does unattended, not about what a technician
may do with it.

Feedback comes from toasts and per-module `busy_note` progress, so an action is
never silent.

Two things that follow, neither of which adds a step for the operator:

- **`confirm: true` stays in the wire format** for these operations, and the GUI
  supplies it on the press. It guards against a *programmatic* accident — a
  replayed RPC after a socket reconnect, a malformed request, a bug in a retry
  path — not against the human. Invisible in the UI.
- **Tier 3 operations are logged** with operator intent and outcome. When
  `firmware.deploy` pushes a release, the journal should say so; that is the
  record someone reads afterwards when the rig comes back wrong.

### 5.2 What `firmware.deploy` actually is

The draft (and the mock's "Firmware Push") imagined flashing MCUs across the
rig, over MQTT OTA or ESP-NOW relay. **Neither OTA path was built.** What
shipped deploys a **Headwaters release package**:

1. `browse` lists candidate packages, USB media first (`/media/usb0`,
   `/media/usb1`, `/mnt/usb`), then home and `/tmp` for packages copied over
   with scp.
2. `select` picks a `trailcurrent-deployment-X.Y.Z.zip`.
3. `deploy`† copies it over SSH (`scp` to the app user's home), unzips it, and
   runs `./deploy.sh` on the Headwaters box, streaming stage progress through
   `busy_note`.
4. `verify` checks the result.

No MCU is ever flashed by Tracer, and no CAN or ESP-NOW traffic is involved.

### The `confirm` argument

Exactly nine operations are confirm-guarded: `discovery.onboard`,
`firmware.deploy`, `mqtt.publish`, `simulate.send`, `net.forget`,
`power.reboot`, `power.shutdown`, `capture.delete`, and `settings.reset`.
Each fails without `"confirm": true` — on the wire as
`{"code": "failed", "msg": "needs_confirmation"}` (see the error-code table
in [§5](#5-commands)).

**This is not a user-facing confirmation step.** Per
[§5.1](#51-what-tracer-may-and-may-not-do) these fire on a single press with no
modal, and the GUI supplies the flag automatically. The guard exists so that a
replayed, malformed, or retried RPC cannot trigger a fleet action by accident —
it defends against the software, not the operator.

Server-side by design: the daemon never infers intent from the fact that a
request arrived.

---

## 6. Connection lifecycle

The GUI opens the socket, receives `hello`, then a `snap` per module. On drop it
shows the unmistakable "daemon offline" screen the prompt requires and reconnects
with backoff (250 ms → 8 s, ±20% jitter).

**The offline screen is not a toast.** It is full-screen and it stays until the
socket is back, because a field tool showing stale numbers is worse than one
showing none. Every value in the UI is owned by the daemon. (Strictly, the
UI's cached module state does persist in memory across a drop — but it is
never rendered while offline; the full-screen overlay covers the UI until the
socket is back and fresh snapshots arrive.)

---

## 7. Mock mode

`tracerd --mock` propagates a mock flag to every module: each serves synthetic
fixtures instead of touching hardware, brokers, or SSH — no dependencies
needed on a laptop. Two consequences for reading this document:

- Figures quoted from the mock (the MQTT topic node's `count: 1841`, the
  46 msg/s rate) are **fixtures, not measurements**.
- Mock mode unlocks `input.press`, so the UI can be driven without the
  physical keyboard.

The `hello` frame and `/health` both report `mock`, so a client can always
tell which it is looking at.

---

## Provenance

| Shape | Source |
|---|---|
| `can/inbound` payload, `data` as bit-arrays | `local_code/can-to-mqtt.py:209-214` |
| CAN bitrate 500 kbit/s | `local_code/can-to-mqtt.py:159` |
| `discovery/browse/found` payload | `local_code/discovery-mdns.py:134-168` |
| Discovery topic names | `local_code/discovery-mdns.py:29-39` |
| `local/…` topic list (15) | `containers/backend/src/services/can-bridge.js` |
| CAN ID → topic handlers (23 IDs) | `containers/backend/src/services/can-bridge.js:40-205` |
| `rv/…` cloud mirror map | `containers/backend/src/services/cloud-bridge.js:111-128` |
| DBC — 88 messages, 258 signals | `TrailCurrentDocumentation/TrailCurrent.dbc` |
| All UI data shapes | `design/PocketTerm35 OS v2.dc.html:509-719` |

---

## Open questions — all resolved

The four questions the draft raised for review were settled by the
implementation; none remain open.

1. **`patch` granularity — resolved: no patches.** The daemon sends full
   per-module snapshots, coalesced to the 100 ms tick ([§2](#2-framing)). The
   RFC-6902 machinery was never built on either side.
2. **`logs` volume — resolved: bounded pulled tails.** There is no live
   journal stream to burst: `logs` pulls the last 400 lines per refresh and
   republishes them as ordinary snapshots ([§4.6](#46-logs--pulled-tails-three-sources-one-screen)).
3. **Capture export — resolved: removed with `GET /export`.** Captures are
   loaded and replayed through the daemon (`capture.load`/`play`); no byte
   stream to the browser exists ([§1](#1-transport)).
4. **`firmware.push` scope — resolved: neither OTA path.** No MQTT OTA and no
   ESP-NOW; firmware deploys a Headwaters release package over SSH, sourced
   from USB ([§5.2](#52-what-firmwaredeploy-actually-is)).
