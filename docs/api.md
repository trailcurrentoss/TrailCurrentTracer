# Tracer daemon ↔ UI API

**Status: DRAFT — for review before either side is implemented.**

Per the build prompt's working method, this contract is written first and reviewed
before `tracerd` or `tracer-ui` is built. Nothing below is implemented yet.

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

What remains genuinely open is the middle ground: the mock's one-touch fleet
actions (Discovery Confirm, Firmware Push, CAN Send frame, container Restart).
Those are Tracer acting, not a human typing. See
[§5.1](#51-what-tracer-may-and-may-not-do).

### What C1 already changed in this document

Auditing the design against C1 found one hard blocker and one improvement:

- **`headwaters` cannot show per-container health.** The build plan called for
  polling "Docker/compose state and container health endpoints". Those do not
  exist. Headwaters' backend exposes exactly one health route
  (`index.js:64  app.get('/api/health')`) and no container-level API at all.
  Corrected in [§4.6](#45-headwaters--ssh-transport-with-graceful-fallback).
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
| `GET /stream` | WebSocket. The only push channel. Carries state and events. |
| `POST /rpc` | Single JSON-RPC-ish endpoint for all commands. |
| `GET /health` | Liveness for `systemd` and for the UI's pre-connect check. |
| `GET /export/<id>` | Byte streams too large for the socket (capture files, log dumps). |

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

```jsonc
{ "t": "hello",  "v": 1, "daemon": "0.4.1", "mock": false, "caps": { ... } }
{ "t": "snap",   "m": "mqtt",  "seq": 41, "d": { ... } }   // full module state
{ "t": "patch",  "m": "mqtt",  "seq": 42, "d": [ ... ] }   // RFC-6902 subset
{ "t": "ev",     "m": "input", "d": { ... } }              // transient, not state
{ "t": "toast",  "level": "info", "text": "Capture saved to /media/usb0" }
```

**`snap` / `patch` are per-module and independently sequenced.** `seq` increments
per module. If the UI sees a gap it sends `resync` for that module only, and gets
a fresh `snap`. This is what keeps one stalled subsystem from corrupting another's
view — a direct requirement of acceptance criterion 3.

Patches use a deliberately small subset of RFC 6902: `replace`, `add`, `remove`,
with paths no deeper than three segments. Enough for tree and list deltas, small
enough to implement in ~40 lines on each side without a dependency.

### Rate discipline

The prompt requires the daemon stream deltas, never the whole tree, and that the
journal stay quiet at rest. Concretely:

- Every module coalesces into a **single frame per 100 ms tick**, at most.
- A module with nothing to say sends **nothing** — no empty heartbeat frames.
- `mqtt` at 46 msg/s (the mock's figure) therefore produces ≤10 frames/s, not 46.
- The socket itself has no application-level keepalive; the WebSocket ping frame
  already covers liveness.

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
    "data": null                        // always null unless state is ok/degraded
  }
}
```

Three rules that make the criterion testable rather than aspirational:

1. `reason` is written for a technician standing next to a vehicle. "no can0
   interface", not "ENODEV".
2. A module entering `unavailable` **keeps its polling loop alive** at a backoff
   interval and recovers on its own. It never exits, and it never blocks another
   module — each is an independent asyncio task with its own supervisor.
3. Transitions are logged at most **once per state change**, never once per poll.
   This is what keeps `journalctl -u tracerd` quiet at rest (criterion 5).

`caps` in the `hello` frame lets the UI know at connect time which modules can
ever be `ok` on this unit — so, for example, the Settings `Brightness` row is
hidden outright when no backlight device exists, rather than shown as an inert
control. See the open item in [hardware.md](hardware.md#still-to-verify-on-hardware).

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
`phase` ∈ `down` `up` `hold` (`hold` fires once at 500 ms, then repeats at 8 Hz)

Per [hardware.md](hardware.md#input--a-usb-hid-keyboard-not-a-gamepad), these are
produced by translating evdev keycodes from `/dev/input/event0` through a keymap
in `settings.json`. **The GUI never sees a keycode.**

Loss of the keyboard is a normal state, not a fault — the RP2040 can drop into
BOOTSEL and strand every button while touch keeps working:

```jsonc
{ "t": "snap", "m": "input", "d": {
    "state": "unavailable", "reason": "keyboard not enumerated",
    "data": { "touch": true } } }
```

The UI must show this in the status bar and remain fully operable by touch.

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
{ "t": "ev", "m": "input", "d": { "text": "a", "ts": … } }        // text mode
```

The GUI **requests** a mode change when focus enters or leaves a text field, but
never assumes it took effect — it renders from the `mode` the daemon reports:

```jsonc
POST /rpc  { "id": "c9", "m": "input", "op": "set_mode", "args": { "mode": "text" } }
```

One source of truth matters here more than usual: if the GUI believed it was in
text mode while the daemon was in nav mode, every keystroke would silently fire a
button action instead of typing. `start` and `select` are the only non-typeable
buttons, so `select` always forces a return to `nav` regardless of GUI state —
the escape hatch cannot itself be swallowed.

**There is no on-screen keyboard anywhere in Tracer** — the device has a real
QWERTY keyboard, so every text field takes physical input. The API therefore has
no virtual-key or character-picker operations, by design.

### 4.2 `mqtt`

Topic tree nodes, streamed as deltas:

```jsonc
{ "path": "local/energy/status", "depth": 1, "count": 1841,
  "rate": 2.0,                                   // EWMA, msg/s
  "retained": false, "qos": 0,
  "last": { "ts": 1756704127.114,
            "payload": "{\"batteryVoltage\":13.4,\"solarInput\":850}",
            "json": true } }
```

`payload` stays a **string**, always. The daemon reports whether it parsed as JSON
via `json`, but does not pre-parse it — the Inspector must be able to show
malformed payloads verbatim, which is precisely when the tool earns its keep.

Subscribes `#` minus a configurable ignore list. Known topic namespaces:

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

The Checklist's "CAN bus terminated, 0 error frames" row is worded as an operator
confirmation for the same reason: Tracer reports the error-frame count, and the
human confirms the physical termination.

### 4.4 `discovery`

Field-for-field the payload `discovery-mdns.py` publishes to
`discovery/browse/found`, plus Tracer's own lifecycle fields:

```jsonc
{ "hostname": "bearing-3f2a", "type": "bearing", "fw": "1.4.2",
  "addr": 12, "canid": "0x150",          // optional — ESP32 MCUs only
  "deviceId": null, "canInstance": null,  // optional — Playbill only
  "target": null,                         // optional — Tapper variant
  "onboard": "confirm",                   // "confirm" (MCU) | "claim" (playbill)
  "first_seen": …, "last_seen": …, "ttl_expires": …, "state": "present" }
```

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

#### Three tiers, degrading independently

The module reports which tier it achieved, so the UI never implies more certainty
than it has:

| Tier | Transport | Gives |
|---|---|---|
| **A — SSH** | `docker ps --format …`, `docker logs --tail` | Per-container state, image version, uptime, restart count, logs. Everything the mock draws. |
| **B — probe** | TCP connect per known port + `GET /api/health` | Per-service **reachable/unreachable**, gateway up. No version, uptime, or restarts → `--`. |
| **C — MQTT only** | `local/system/stats` | The four stat tiles (CPU, memory, disk, temp) only. |

Tier C works with no Headwaters access at all, so the stat tiles are always live.
Tiers fall back automatically and the daemon reports `source: "ssh" | "probe" |
"mqtt"` on every field.

The distinction matters in the UI: a TCP probe proves a port accepts connections,
not that the service behind it is working. Tier B must render "Reachable", never
"Healthy" — only Tier A can honestly say healthy, because only Tier A has read
Docker's own health status.

#### Key enrolment — type the password once, ever

Tier A needs `sshd` running on Headwaters and Tracer's public key in the app
user's `~/.ssh/authorized_keys`. **This is deployment provisioning, not a repo
change** — nothing in the Headwaters codebase is modified, which is the line C1
actually draws.

Typing a password on a 3.5" thumb keyboard is miserable, and a technician would
be doing it on every reconnect. So the design is: **enter it once, then never
again.**

`Settings → Headwaters Access`:

| Field | Notes |
|---|---|
| Host | default `headwaters.local`; discovered hosts offered as a picker |
| User | default `trailcurrent` |
| Key status | `Not enrolled` / `Enrolled ✓` / fingerprint |
| **Enrol key** | prompts for the password **once**, installs the public key, verifies, done |
| Test connection | runs `docker ps` and reports the tier reached |

```jsonc
POST /rpc { "id":"c12", "m":"headwaters", "op":"enrol_key",
            "args": { "host":"headwaters.local", "user":"trailcurrent",
                      "password":"…", "confirm": true } }
```

Rules the daemon enforces:

- Tracer generates an **ed25519** keypair on first boot at
  `/var/lib/tracer/ssh/id_ed25519`, mode `0600`, on the one writable partition.
- The password is used for exactly one `ssh-copy-id`-equivalent operation and is
  **never written to disk**, never logged, and never held after the call returns.
  It exists only as a local in the enrol coroutine.
- Enrolment is idempotent — re-running it repairs a broken `authorized_keys`
  without duplicating the entry.
- The host key is pinned on first enrolment and a later mismatch surfaces as a
  clear warning, not a silent failure. A field tool that quietly accepts a
  changed host key is worse than one that refuses.
- If enrolment never happens, the module simply runs at Tier B/C. Tier A is an
  enhancement, never a prerequisite.

The private key lives on Tracer alone; nothing is ever copied off the device.

##### Optional hardening, not the default

An `authorized_keys` forced command (`command="…",no-pty,no-port-forwarding …`)
would restrict the Tracer key to read-only Docker calls. That was my first
instinct and it is **wrong as a default here** — it would block the interactive
shell that is a core purpose of the device (see [C2](#c2--monitoring-tool-and-a-headless-console-for-headwaters)).

Kept as a documented option for operators who want a locked-down Tracer on a
shared rig, applied to a second, separate key. Not on by default.

**Unverified:** I could not confirm `openssh-server` ships in the Headwaters
image. There is no explicit install in `layer/*.yaml`, and `build.sh` advertises
that setup needs "no SSH, keyboard, or monitor". It may come from the base layer.
If sshd is absent by default, Tier A requires enabling it per rig — still no repo
change, but a bigger provisioning ask, and worth checking on a real Headwaters
unit before committing to Tier A as the primary path.

### 4.6 `logs` — local journal, plus pulled Headwaters logs

Two sources, one screen.

**Local:** journald on Tracer via `systemd.journal`, level filter and text search
across `tracerd` and its units. Live.

**Remote, pulled:** the same SSH channel as [§4.6](#45-headwaters--ssh-transport-with-graceful-fallback)
copies logs off Headwaters to Tracer, where they are read and searched locally.

```jsonc
POST /rpc { "id":"c20", "m":"logs", "op":"fetch_remote",
            "args": { "source": "journal" | "container",
                      "unit": "cantomqtt" | "mosquitto",
                      "since": "2h", "confirm": true } }
```

Landing in `/var/lib/tracer/logs/<host>/<source>-<timestamp>.log`, alongside
captures, and exportable to USB by the same path.

Why pull rather than stream:

- **Analysis load stays on Tracer.** Grepping a 200 MB journal over a live SSH
  pipe does the work on the Headwaters box, which is the machine the technician
  is trying to diagnose. Copying once and searching locally leaves it alone.
- **It survives the link.** Once pulled, the logs are readable with Headwaters
  powered down, off the rig entirely, or back at the bench.
- **It is still read-only** — `journalctl`/`docker logs` to stdout, copied off.
  Tier 1 under [§5.1](#51-what-tracer-may-and-may-not-do).

Large fetches report progress like a capture and can be cancelled. The daemon
caps a single fetch and reports what it truncated rather than filling the
writable partition — silent truncation would read as "that is all the logs",
which is exactly the wrong thing to tell someone chasing an intermittent fault.

### 4.7 Remaining modules

`net`, `gnss`, `capture`, `firmware`, `power`, `settings`, and `terminal`
follow the same envelope.

`terminal` takes a **target**: `local` (a pty on Tracer) or a saved SSH host,
reusing the enrolled key from [§4.6](#45-headwaters--ssh-transport-with-graceful-fallback)
so no password is typed. Reaching a Headwaters box without hooking up a keyboard,
mouse and monitor is a primary purpose of the device, so the host picker belongs
in the Terminal itself rather than buried in Settings.
 Their payloads are direct transcriptions of the mock's
data blocks (`NET_STATS`, `NET_CHECKS`, `HW_STATS`, `CONTAINERS`, `GNSS_FIELDS`,
`LOG_LINES`, `CAPTURES`, `FW_TARGETS`, `SETTINGS`) and are specified in full in
the schema files rather than duplicated here.

---

## 5. Commands

```jsonc
POST /rpc  { "id": "c1", "m": "net", "op": "connect",
             "args": { "ssid": "Airstream-27", "psk": "…" } }

     200   { "id": "c1", "ok": true,  "d": { … } }
     200   { "id": "c1", "ok": false, "err": { "code": "auth_failed",
                                               "msg": "incorrect password" } }
```

A command failure is **always** HTTP 200 with `ok: false`. Non-2xx is reserved for
transport-level problems, so the UI never has to disambiguate "the daemon is gone"
from "the password was wrong".

Operations map 1:1 onto the mock's per-app X/Y semantics — `mqtt.pause`,
`disco.rescan`, `disco.confirm`, `capture.start`, `firmware.push`,
`headwaters.restart`, `check.rerun`, and so on.

### 5.1 What Tracer may and may not do

Auditing every operation against [C2](#c2--monitoring-tool-and-a-headless-console-for-headwaters)
sorts them into three tiers. The first two are settled; the third is the open
question.

**Tier 1 — pure observation. Unambiguously in scope.**
MQTT subscribe · CAN receive (passive only, [never transmit](#can-detection-is-strictly-passive--never-probe-by-transmitting)) ·
Zeroconf browse · journald read · `docker ps` / `docker logs` over SSH ·
`GET /api/health` · TCP reachability probes · gpsd read · system stats.
No writes anywhere. This is the bulk of the product.

**Tier 2 — writes to Tracer itself only. In scope.**
WiFi join/forget · brightness · capture to local disk or USB · settings ·
SSH key enrolment · reboot/shutdown *of Tracer*.
Nothing outside the device is touched.

**Tier 3 — Tracer acting on the fleet. Open question.**

| Operation | Mock binding | Effect |
|---|---|---|
| `disco.confirm` / `claim` | Discovery, **A** | Onboards a module; changes its state |
| `firmware.push` | Firmware, **X** | Flashes MCUs across the rig |
| `can.send_frame` | CAN Sniffer, **Y** | Drives the vehicle bus |
| `headwaters.restart` | Headwaters, **X** | Restarts a container |
| `mqtt.publish` | — | Arbitrary write to the broker |

Every one of these satisfies **C1** — they use contracts that already exist, and
need nothing changed in any other repo. But each is Tracer acting on the vehicle
rather than observing it, which is what C2 rules out.

They are also each one button-press from a focused row, on a handheld device with
no confirmation modal in the mock. `can.send_frame` in particular writes to a live
vehicle bus, and `firmware.push` reflashes modules.

**Decided: Tier 3 stays in the GUI, exactly as the mock draws it — single press,
no confirmation modal.**

I raised the concern above; the call is to keep them. So the mock's button
semantics are authoritative and unchanged: Discovery **A** = Confirm, Firmware
**X** = Push, CAN **Y** = Send frame, Headwaters **X** = Restart. Build them as
drawn.

This narrows C2 to its precise meaning: Tracer's **background** behaviour is
observation-only — no polling loop writes, ever. Operator-initiated actions are
the operator's authority, whether typed at the Terminal or pressed on a row. The
constraint is about what the device does unattended, not about what a technician
may do with it.

Feedback comes from the toast the mock already specifies ("Confirm marker sent to
bearing-3f2a", "Published local/ota/trigger to 2 targets"), so an action is never
silent.

Two things that follow, neither of which adds a step for the operator:

- **`confirm: true` stays in the wire format** for these operations, and the GUI
  supplies it on the press. It guards against a *programmatic* accident — a
  replayed RPC after a socket reconnect, a malformed request, a bug in a retry
  path — not against the human. Invisible in the UI.
- **Tier 3 operations are logged** with operator intent and outcome. When
  `firmware.push` reflashes four modules, the journal should say so; that is the
  record someone reads afterwards when a module comes back wrong.

`can.send_frame` deserves one implementation note: it is the only Tier 3 action
that drives the bus directly, and it must not bypass the
[passive-detection rule](#can-detection-is-strictly-passive--never-probe-by-transmitting).
That rule forbids *Tracer* probing on its own initiative; an operator explicitly
sending a frame is a different act. The daemon must still surface the resulting
error counters honestly if the send fails for lack of an ACK.

### The `confirm` argument

`disco.confirm`, `firmware.push`, `can.send_frame`, `headwaters.restart`,
`mqtt.publish`, `power.reboot`, `power.shutdown`, and `net.forget` each take a
`"confirm": true` argument and fail `needs_confirmation` without it.

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
showing none. Every value in the UI is owned by the daemon; there is no
client-side cache that could outlive the connection.

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

## Open questions for review

1. **`patch` granularity.** Is an RFC-6902 subset worth it over
   "send the changed list items"? It is more code on both sides; it is meaningfully
   cheaper only for the MQTT topic tree, which is the one place it matters most.
   Happy to simplify to per-item replacement everywhere if you'd rather.
2. **`logs` volume.** journald can burst far faster than 10 frames/s during a
   fault — exactly when the operator is looking. Proposal: cap at 200 lines per
   tick and report `dropped: n` rather than falling behind or growing unbounded.
3. **Capture export.** `GET /export/<id>` streams from disk. For a 22 MB capture
   (the mock's largest) over loopback that is fine; flagging it only because it is
   the one place the UI touches something other than the socket.
4. **`firmware.push` scope.** The prompt lists both MQTT OTA topics and ESP-NOW
   relay. Headwaters has `trigger_ota_mqtt.py` and `trigger_ota_wireless.py`, so
   both paths exist upstream — confirm Tracer should drive both, or MQTT only for
   now.
