# Build manifest — every device-side change, and where it is captured

**Purpose: guarantee a flashed image behaves exactly like the board we have
been iterating on.** Everything below was discovered by running on real
hardware. Each row names the fix, where it lives in the repo, and the
bake-time check that fails the build if it goes missing.

The failure mode this exists to prevent: a change is made live on the board,
never captured in the image layer, and the first real build silently
regresses to behaviour we already debugged.

**Nothing here is applied by hand on the device.** Every item is in the repo
and installed by `image/layer/tracer-base.yaml`. The dev board is disposable.

---

## Verified at bake time

`tracer-base.yaml` ends with a verification hook that **fails the build** if
any of these is missing. They are all cases where the image would otherwise
boot, look healthy, and be quietly broken.

| # | What | Where it lives | Build check | Why it bites |
|---|---|---|---|---|
| 1 | GT911 touch overlay | `image/overlays/tracer-gt911-overlay.dts` | `.dtbo` present | Vendor overlay logs two `err` lines every boot; ours drops the unpopulated 0x14 node |
| 2 | `config.txt` uses **our** overlay | layer hook | `grep dtoverlay=tracer-gt911` | Reverting to Waveshare's reintroduces the boot noise |
| 3 | `dtparam=i2c_arm=on` | layer hook | grep | Touch never probes — and touch is the only recovery path when the RP2040 strands itself |
| 4 | `dtoverlay=dwc2,dr_mode=host` | layer hook | grep | **No keyboard at all** |
| 5 | `kernel.sysrq=0` | layer hook → `/etc/sysctl.d/10-tracer.conf` | grep | Select is `KEY_SYSRQ`; stock `438` enables reboot/poweroff, so Alt+Select hard-reboots the device |
| 6 | NetworkManager polkit rule | `image/layer/files/49-tracer-networkmanager.rules` | file present | Without it the WiFi scan silently returns a **stale cache** showing only the connected SSID |
| 7 | Hardware-captured keymap | `tracerd/keymap.default.json` | file non-empty | No button does anything |
| 8 | `tracerd` | `tracerd/` → `/opt/tracer/tracerd` | `__main__.py` present | No daemon |
| 9 | `tracer-ui` | `tracer-ui/` → `/opt/tracer/tracer-ui` | `index.html` present | Blank screen |
| 10 | Generated icon subset | `tracer-ui/src/icons.js` (committed) | non-empty | Every icon on every screen renders blank |
| 11 | `_EDIT_KEYS` defined | `tracerd/modules/inputmod.py` | grep | Text entry dies silently — this exact bug shipped once |
| 12 | Units enabled | `image/systemd/*.service` | wants-symlink present | Boots to a blank screen |
| 13 | MQTT client (stdlib) | `tracerd/mqttclient.py` | file present | No broker connection; Inspector, Discovery and CAN feed all dead |
| 14 | MQTT module | `tracerd/modules/mqtt.py` | file present | as above |
| 15 | MQTT Inspector screen | `tracer-ui/src/apps/mqtt.js` | file present | Screen falls back to the "not implemented" stub |
| 16 | Per-topic rate is windowed | `modules/mqtt.py` | `grep def sample` | Instantaneous `1/dt` reported `can/inbound` at 8904/s against a real 142 msg/s |
| 17 | SSH substrate helpers | `tracerd/sshcopy.py` | file present | No CA fetch, no Mongo read, no `docker logs` — every Headwaters path goes through this |
| 18 | Discovery module | `tracerd/modules/discovery.py` | file present | No device registry, no scan trigger |
| 19 | Discovery screen | `tracer-ui/src/apps/discovery.js` | file present | Falls back to the not-implemented stub |

---

## Behaviour captured in code (not hand-applied)

Fixes found on hardware that live in normal source files. Listed so a future
reader knows they were deliberate, and can find the reasoning.

| Fix | File | Why |
|---|---|---|
| `Module.refresh()` — re-poll before publishing | `modules/base.py` | `publish()` serialises `self.data`, which only `poll()` writes. A handler that mutated state and published broadcast the **previous** poll's data — theme changes took up to 30 s to appear |
| Settings saved off the event loop, debounced 600 ms | `modules/settings.py` | Two `fsync`s inline stalled every module and the WebSocket; L/R repeat at 8 Hz turned that into a stall storm |
| Secrets redacted on the wire | `modules/settings.py` | The GUI receives `true`/`false` for `mqtt_password`, never the value; store is `0600` |
| SSID from the active AP, not the profile name | `modules/net.py` | netplan names profiles `netplan-wlan0-Quigon`; using the name showed that as the network name and marked saved networks unsaved |
| Scan results merged by SSID, strongest wins | `modules/net.py` | 16 APs broadcast "Quigon"; keeping the first row reported the connected network as not-connected |
| Scan authorization checked and surfaced | `modules/net.py` | `nmcli` returns non-zero and then serves a cache — the failure is invisible unless the exit code is read |
| Start = Accept, Select = Cancel in text fields | `inputmod.py`, `apps/settings.js`, `apps/wifi-setup.js` | A/B/X/Y/L/R are literal letter keys and must type, so **B cannot cancel**. Start and Select are the only non-typeable buttons |
| Text mode resets when the last client disconnects | `inputmod.py` | A GUI crash mid-edit would leave every button typing letters with no way back |
| Discovery reads `system_config.mcu_modules`, NOT `db.modules` | `modules/discovery.py` | The `modules` collection exists but is empty; the real registry is a field on the `_id:"main"` system_config document (`routes/discovery.js:253,270`). Querying the obvious collection returns `[]` and looks like "no devices" |
| A scan is THREE broadcasts, not one | `modules/discovery.py` | CAN 0x02 + `local/discovery/trigger` + `discovery/browse/start` (`routes/discovery.js:180-182`). The CAN frame is what puts un-onboarded modules into discovery mode; the mDNS browse alone finds nothing |
| Onboarding writes Mongo directly, not via the API | `modules/discovery.py` | `handleConfirmResponse` only resolves a promise the HTTP route created — publishing the MQTT request alone confirms the device but persists nothing. And the API is the layer under test. See C0 |
| MQTT payloads stay strings, binary shown as hex | `modules/mqtt.py` | The Inspector must show malformed and binary payloads verbatim — that is exactly when it earns its keep |
| Optimistic UI for slider and theme | `apps/settings.js` | Input must paint on the keypress, then reconcile against the daemon |
| Brightness tiers (backlight → DDC → software dim) | `modules/power.py` | No kernel backlight device exists on this panel; the control says `(dim)` rather than implying backlight control |
| Battery renders `--`, never `undefined%` | `chrome/chrome.js` | `power` reports `ok` for brightness even with no battery, so module state alone is not enough |
| Theme-aware chrome via `--chrome-bg` | `chrome/chrome.js` | Hardcoded `rgba(0,0,0,0.9)` left the clock invisible in light theme |
| Icon wrapper uses `color` + `fill=currentColor` | `scripts/gen-icons.mjs` | Forcing `stroke` visibly thickened every solid icon |

---

## Regression tests

`make test` — 13 tests over the input dispatch path, the layer the
`_EDIT_KEYS` bug hid in. `compileall` passed while text entry was dead,
because the name was only referenced *inside a function*. **Anything reached
only by a real keypress needs a test that presses a key.**

---

## Deliberately NOT in the image

| | Why |
|---|---|
| `openssh-server` | Tracer is a handheld tool, not a host to log into. Add it to a debug variant if needed |
| `tracer-ui/node_modules` | Only needed to regenerate `icons.js`; the output is committed |
| `tracerd/tests` | Stripped at install |
| `__pycache__`, `*.pyc` | Stripped — a stale `.pyc` on the dev board once made a fix look like it had not deployed |
| Anything under `~/tracer-dev` | `make dev` scratch only. It installs no unit and writes nothing outside `$HOME`, so a dev deploy can never leak into a build |

---

## Deliberately deferred

Decisions taken knowingly, recorded so they are not mistaken for oversights.

### ~~MQTT TLS is unverified~~ — RESOLVED 2026-09-01

Was deferred; then solved properly rather than worked around. Settings ›
Headwaters Access › **Fetch CA certificate** copies `~/data/keys/ca.pem` off
Headwaters over `scp`, using the credentials already stored for Headwaters
access, and installs it as the MQTT trust root. Verified on hardware:
`CN=TrailCurrent-CA`, and the broker session reconnects with
`tls_verified: true`.

Two properties worth keeping if this is ever refactored:

- **The certificate is parsed before it is written.** A malformed download
  never lands on disk, where it would quietly break every future broker
  connection with a confusing TLS error.
- **The fingerprint is returned and shown**, so the operator can check it
  against Headwaters. Copying a trust root over an unverified channel is
  trust-on-first-use; showing the fingerprint makes that a checkable decision
  rather than a blind one.

Still to do before a customer image: make an unverified broker connection a
**build failure** rather than only an amber badge.

---

## Still not captured

Honest list — these are known gaps, not oversights:

- **`tracerd` runs as the device account, not a dedicated user.** That account
  is chosen at image build time (see [building.md](building.md#device-account));
  the units name it via `@TRACER_USER@` and it is never baked into the repo.
  tracerd only needs `input`, `netdev`, and `video`, so a dedicated `tracer`
  service user would be tighter than reusing the login account.
- **`/var/log` tmpfs and the read-only rootfs overlay** are designed in
  [boot.md](boot.md#7-read-only-rootfs) but not yet in the layer.
- **No image has been built or booted yet.** Every check above is written
  against a build that has not run. The first `sudo make image` is where this
  manifest gets tested — expect to fix things, and add rows here when you do.
