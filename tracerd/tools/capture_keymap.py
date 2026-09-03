#!/usr/bin/env python3
"""Capture PocketTerm35 keycodes without evtest or python-evdev.

Reads raw input_event structs from /dev/input/event0 and logs every key press
with its numeric code and KEY_* name. Does NOT grab the device, so the keyboard
keeps working normally while this runs.

Usage: capture_keymap.py <seconds> <outfile>
"""
import re
import struct
import sys
import time

DEV = "/dev/input/event0"
HDR = "/usr/include/linux/input-event-codes.h"

# struct input_event on 64-bit: struct timeval (2x long) + __u16 + __u16 + __s32
FMT = "llHHi"
SZ = struct.calcsize(FMT)

EV_KEY = 0x01
EV_MSC = 0x04
MSC_SCAN = 0x04


def load_names():
    """code -> KEY_* / BTN_* name, parsed from the kernel header."""
    names = {}
    pat = re.compile(r"^#define\s+((?:KEY|BTN)_\w+)\s+(0x[0-9a-fA-F]+|\d+)")
    try:
        with open(HDR) as fh:
            for line in fh:
                m = pat.match(line)
                if m:
                    names.setdefault(int(m.group(2), 0), m.group(1))
    except OSError:
        pass
    return names


def main():
    dur = float(sys.argv[1])
    out = sys.argv[2]
    names = load_names()
    deadline = time.time() + dur
    pending_scan = None

    with open(DEV, "rb", buffering=0) as dev, open(out, "w", buffering=1) as log:
        log.write(f"# capture start {time.strftime('%H:%M:%S')} "
                  f"({len(names)} keycode names loaded)\n")
        while time.time() < deadline:
            data = dev.read(SZ)
            if not data or len(data) < SZ:
                continue
            _sec, _usec, etype, code, value = struct.unpack(FMT, data)

            # MSC_SCAN carries the raw HID usage, emitted just before the key
            # event. Very useful for telling apart keys the kernel maps alike.
            if etype == EV_MSC and code == MSC_SCAN:
                pending_scan = value
                continue

            if etype == EV_KEY and value == 1:  # press only, ignore release/repeat
                name = names.get(code, "UNKNOWN")
                scan = f"0x{pending_scan:x}" if pending_scan is not None else "--"
                log.write(f"{time.strftime('%H:%M:%S')}  code={code:<4} "
                          f"{name:<20} hid_usage={scan}\n")
                pending_scan = None

        log.write("# capture end\n")


if __name__ == "__main__":
    main()
