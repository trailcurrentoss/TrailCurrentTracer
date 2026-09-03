"""power — battery, brightness, and clean shutdown.

BRIGHTNESS ON THIS HARDWARE
---------------------------
Verified on the board 2026-09-01: `/sys/class/backlight` is **empty** and
`ddcutil` is not installed. The panel is HDMI, and small HDMI panels frequently
expose no backlight control at all.

So brightness is resolved in tiers, and the daemon reports which one is live so
the UI can be honest about what the control actually does:

  1. `backlight` — a real kernel backlight device. Genuine backlight control,
     saves power. Not present on this unit.
  2. `ddc`       — DDC/CI over the connector's I2C bus via ddcutil. Real
     backlight control if the panel implements VCP 0x10. Untested here;
     ddcutil is in the image package list so this can be tried on hardware.
  3. `software`  — a compositing dim applied by the GUI. This does NOT reduce
     backlight power; it reduces emitted light and glare, which is the actual
     need for a technician working at night next to a vehicle.

Tier 3 always works, so the control is never dead. It is labelled as a
software dim in the UI rather than being passed off as backlight control.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import struct
from pathlib import Path

from .base import Module, Unavailable

log = logging.getLogger("tracerd.mod")

BACKLIGHT_DIR = Path("/sys/class/backlight")

# ── Power button ────────────────────────────────────────────────────────
# The image sets HandlePowerKey=ignore (image/layer/tracer-base.yaml), so
# logind no longer powers the unit off the instant the button is brushed.
# Instead we watch the key ourselves and ask the GUI to confirm.
#
# This is a DIFFERENT input device from the PocketTerm35 keyboard that
# inputmod reads. The Pi 5's button is a gpio-keys device that reports
# KEY_POWER, so it is matched by name here rather than reusing the keymap.
INPUT_DEVICES = Path("/proc/bus/input/devices")
KEY_POWER = 116
EV_KEY = 0x01
# struct input_event on 64-bit: two 64-bit timeval words, then type, code, value
_EV_FMT = "llHHi"
_EV_SIZE = struct.calcsize(_EV_FMT)
# Names the Pi's power button presents as. Checked in order; first match wins.
_POWER_DEV_HINTS = ("pwr_button", "power_button", "gpio-keys")


def _power_button_device() -> str | None:
    """Find the evdev node for the power button, or None if absent.

    Absent is a normal state, not an error: a bench unit driven over HDMI with
    no carrier board has no such device, and the GUI shutdown item must keep
    working there.
    """
    try:
        blob = INPUT_DEVICES.read_text()
    except OSError:
        return None
    for block in blob.split("\n\n"):
        low = block.lower()
        if not any(h in low for h in _POWER_DEV_HINTS):
            continue
        for tok in block.split():
            if tok.startswith("event"):
                return f"/dev/input/{tok}"
    return None


def _backlight_device() -> Path | None:
    try:
        entries = sorted(BACKLIGHT_DIR.iterdir())
    except OSError:
        return None
    for e in entries:
        if (e / "brightness").exists() and (e / "max_brightness").exists():
            return e
    return None


class PowerModule(Module):
    name = "power"
    interval = 10.0

    def __init__(self, hub, mock: bool = False):
        super().__init__(hub)
        self.mock = mock
        self._method: str | None = None
        self._bl: Path | None = None
        self._pwr_task: asyncio.Task | None = None

    async def setup(self) -> None:
        if not self.mock and self._pwr_task is None:
            self._pwr_task = asyncio.create_task(
                self._power_key_loop(), name="power:key"
            )
        self._bl = _backlight_device()
        if self._bl:
            self._method = "backlight"
        elif shutil.which("ddcutil"):
            self._method = "ddc"
        else:
            self._method = "software"

    def _brightness_setting(self) -> int:
        st = self.hub.modules.get("settings")
        if st and getattr(st, "values", None):
            try:
                return int(st.values.get("brightness", 70))
            except (TypeError, ValueError):
                pass
        return 70

    async def poll(self):
        brightness = self._brightness_setting()
        data = {
            "brightness": brightness,
            "brightness_method": self._method,
            # The UI applies the dim itself when there is no hardware path.
            "brightness_software": self._method == "software",
        }

        if self.mock:
            data.update({"percent": 78, "charging": False, "supply": "mock"})
            return data

        # No power-management IC has been identified on this carrier. Report
        # `--` rather than inventing a battery percentage — a fabricated
        # charge level on a field tool is worse than an absent one.
        base = "/sys/class/power_supply"
        try:
            for d in sorted(os.listdir(base)):
                cap = os.path.join(base, d, "capacity")
                if os.path.isfile(cap):
                    with open(cap) as fh:
                        data["percent"] = int(fh.read().strip())
                    data["supply"] = d
                    status = os.path.join(base, d, "status")
                    if os.path.isfile(status):
                        with open(status) as fh:
                            data["charging"] = fh.read().strip() == "Charging"
                    break
        except OSError:
            pass
        return data

    async def set_brightness(self, pct: int) -> dict:
        pct = max(5, min(100, int(pct)))

        if self._method == "backlight" and self._bl:
            try:
                mx = int((self._bl / "max_brightness").read_text().strip())
                (self._bl / "brightness").write_text(str(round(mx * pct / 100)))
                await self.refresh()
                return {"brightness": pct, "method": "backlight"}
            except OSError as exc:
                # Usually root-owned. Fall through to the software dim rather
                # than failing — the operator still gets a working control.
                self._method = "software"

        if self._method == "ddc":
            try:
                proc = await asyncio.create_subprocess_exec(
                    "ddcutil", "setvcp", "10", str(pct),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                rc = await asyncio.wait_for(proc.wait(), timeout=8.0)
                if rc == 0:
                    await self.refresh()
                    return {"brightness": pct, "method": "ddc"}
            except (OSError, asyncio.TimeoutError):
                pass
            self._method = "software"

        # Software dim: the value is carried in state and the GUI applies it.
        await self.refresh()
        return {"brightness": pct, "method": "software"}

    async def handle(self, op: str, args: dict):
        if op == "set_brightness":
            pct = args.get("value")
            st = self.hub.modules.get("settings")
            if st:
                # Persist through settings so it survives a reboot; that also
                # validates and atomically writes it.
                await st.handle("set", {"key": "brightness", "value": pct})
            return await self.set_brightness(pct)
        if op in ("reboot", "shutdown"):
            if not args.get("confirm"):
                raise Unavailable("needs_confirmation")
            if self.mock:
                return {"would": op}
            cmd = ["systemctl", "reboot" if op == "reboot" else "poweroff"]
            proc = await asyncio.create_subprocess_exec(*cmd)
            await proc.wait()
            return {op: True}
        raise Unavailable(f"power has no operation {op!r}")

    # ── power button ─────────────────────────────────────────────────
    async def _power_key_loop(self) -> None:
        """Watch the power button and ask the GUI to confirm a shutdown.

        Its own task, like inputmod's reader, so a missing or disappearing
        button never blocks poll(). Emits an event rather than powering off
        directly — the decision belongs to the operator, and power.handle()
        still refuses to act without confirm=true.

        Long-press is deliberately NOT handled here. logind keeps
        HandlePowerKeyLongPress=poweroff, so holding the button forces a
        poweroff even if this daemon or the GUI is wedged. That escape hatch
        is the reason it is safe for us to swallow the short press.
        """
        loop = asyncio.get_running_loop()
        while True:
            dev = _power_button_device()
            if not dev:
                await asyncio.sleep(5.0)
                continue
            try:
                fh = await loop.run_in_executor(
                    None, lambda: open(dev, "rb", buffering=0)
                )
            except OSError as exc:
                log.warning("power: cannot open %s (%s); retrying", dev, exc)
                await asyncio.sleep(5.0)
                continue
            try:
                while True:
                    raw = await loop.run_in_executor(None, fh.read, _EV_SIZE)
                    if not raw or len(raw) < _EV_SIZE:
                        break
                    _, _, etype, code, value = struct.unpack(_EV_FMT, raw)
                    # value 1 == key down. Ignore key-up (0) and autorepeat (2)
                    # so holding the button raises exactly one prompt.
                    if etype == EV_KEY and code == KEY_POWER and value == 1:
                        log.info("power: button pressed, asking GUI to confirm")
                        self.hub.broadcast_now(
                            "power", {"ev": "confirm_shutdown"}
                        )
            except OSError as exc:
                log.warning("power: read on %s failed (%s); reopening", dev, exc)
            finally:
                try:
                    fh.close()
                except OSError:
                    pass
            await asyncio.sleep(1.0)

    async def stop(self):
        if self._pwr_task:
            self._pwr_task.cancel()
            self._pwr_task = None
        await super().stop()

    def tile_status(self):
        return ("--", "#666")
