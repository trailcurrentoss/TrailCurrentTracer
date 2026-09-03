"""Minimal DBC reader and frame encoder for the Simulate app.

Only the subset TrailCurrent.dbc actually uses:
  BO_   message: id, name, dlc, sender
  SG_   signal:  start bit, length, byte order, sign, scale, offset,
                 min, max, unit, receivers
  VAL_  enumerations
  CM_   comments

Every signal in the fleet DBC is `@0` (Motorola / big-endian) — verified:
246 unsigned and 12 signed, zero Intel signals. That makes encoding uniform,
and it lines up exactly with the wire format Headwaters already uses:
can-to-mqtt.py carries `data` as eight 8-bit arrays, MSB first. In that
representation a big-endian signal is simply a run of consecutive bits, so
encoding is a direct write with no byte-swapping.

Intel signals would need different placement. Rather than write untested
code for a case the fleet does not have, `parse()` records them and the
Simulate module refuses to encode them — a clear refusal beats a silent
mis-encode that puts wrong values on a vehicle bus.
"""

from __future__ import annotations

import re
from pathlib import Path

_BO = re.compile(r"^BO_\s+(\d+)\s+(\w+)\s*:\s*(\d+)\s+(\S+)")
_SG = re.compile(
    r"^\s*SG_\s+(\w+)\s*:\s*(\d+)\|(\d+)@([01])([+-])\s*"
    r"\(([^,]+),([^)]+)\)\s*\[([^|]*)\|([^\]]*)\]\s*\"([^\"]*)\"\s*(.*)$"
)
_VAL = re.compile(r'^VAL_\s+(\d+)\s+(\w+)\s+(.*);')
_CM_BO = re.compile(r'^CM_\s+BO_\s+(\d+)\s+"(.*)"\s*;', re.S)
_CM_SG = re.compile(r'^CM_\s+SG_\s+(\d+)\s+(\w+)\s+"(.*)"\s*;', re.S)


def default_path() -> Path:
    return Path(__file__).resolve().parent / "data" / "TrailCurrent.dbc"


def parse(path: Path | None = None) -> dict:
    text = (path or default_path()).read_text(errors="replace")
    messages: dict[int, dict] = {}
    current: dict | None = None

    for line in text.splitlines():
        m = _BO.match(line)
        if m:
            mid = int(m.group(1))
            current = {
                "id": mid,
                "hex": f"0x{mid:03x}",
                "name": m.group(2),
                "dlc": int(m.group(3)),
                "sender": m.group(4),
                "signals": [],
                "comment": "",
            }
            messages[mid] = current
            continue
        s = _SG.match(line)
        if s and current is not None:
            lo, hi = s.group(8).strip(), s.group(9).strip()
            current["signals"].append({
                "name": s.group(1),
                "start": int(s.group(2)),
                "length": int(s.group(3)),
                "big_endian": s.group(4) == "0",
                "signed": s.group(5) == "-",
                "scale": float(s.group(6)),
                "offset": float(s.group(7)),
                "min": float(lo) if lo else None,
                "max": float(hi) if hi else None,
                "unit": s.group(10),
                "receivers": [r for r in s.group(11).split(",") if r and r != "Vector__XXX"],
                "values": {},
                "comment": "",
            })
            continue
        if line.startswith("VAL_ "):
            v = _VAL.match(line)
            if v:
                mid, sig = int(v.group(1)), v.group(2)
                pairs = re.findall(r'(-?\d+)\s+"([^"]*)"', v.group(3))
                msg = messages.get(mid)
                if msg:
                    for sg in msg["signals"]:
                        if sg["name"] == sig:
                            sg["values"] = {int(k): val for k, val in pairs}

    # Comments can wrap lines, so run them over the whole text.
    for mid, txt in _CM_BO.findall(text):
        if int(mid) in messages:
            messages[int(mid)]["comment"] = txt.replace("\n", " ").strip()
    for mid, sig, txt in _CM_SG.findall(text):
        msg = messages.get(int(mid))
        if msg:
            for sg in msg["signals"]:
                if sg["name"] == sig:
                    sg["comment"] = txt.replace("\n", " ").strip()

    return messages


class EncodeError(Exception):
    pass


def encode(message: dict, values: dict) -> list[list[int]]:
    """Encode signal values into eight MSB-first bit arrays.

    Returns the exact `data` shape can-to-mqtt.py expects — eight lists of
    eight bits, MSB first — so the frame is indistinguishable on the wire
    from one Headwaters itself produced.
    """
    bits = [0] * 64
    for sig in message["signals"]:
        if sig["name"] not in values:
            continue
        if not sig["big_endian"]:
            raise EncodeError(
                f"{sig['name']} is a little-endian signal; encoding is not "
                "implemented and guessing it could put a wrong value on the bus")

        raw = _to_raw(sig, values[sig["name"]])
        start = sig["start"]
        # MSB-first array index for DBC bit position: byte b, bit 7-n.
        idx = (start // 8) * 8 + (7 - (start % 8))
        if idx + sig["length"] > 64:
            raise EncodeError(f"{sig['name']} runs past the end of the frame")
        for i in range(sig["length"]):
            bits[idx + i] = (raw >> (sig["length"] - 1 - i)) & 1

    return [bits[i * 8:(i + 1) * 8] for i in range(8)]


def _to_raw(sig: dict, value) -> int:
    try:
        phys = float(value)
    except (TypeError, ValueError):
        raise EncodeError(f"{sig['name']}: {value!r} is not a number")

    scale = sig["scale"] or 1.0
    raw = round((phys - sig["offset"]) / scale)

    span = 1 << sig["length"]
    if sig["signed"]:
        lo, hi = -(span >> 1), (span >> 1) - 1
        if not lo <= raw <= hi:
            raise EncodeError(
                f"{sig['name']}: {value} does not fit in {sig['length']} signed bits")
        if raw < 0:
            raw += span
    else:
        if not 0 <= raw < span:
            raise EncodeError(
                f"{sig['name']}: {value} does not fit in {sig['length']} bits")
    return raw
