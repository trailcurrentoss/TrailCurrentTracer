# Vendored Waveshare device tree overlays

Unmodified upstream binaries, vendored so the image build is self-contained and
reproducible without a network fetch from Waveshare at build time.

**Source:** https://files.waveshare.com/wiki/common/3.5HDMI_E_DTBO.zip
**Guide:** https://docs.waveshare.com/PocketTerm35/Software-Guide
**Retrieved:** 2026-09-01

| File | sha256 |
|---|---|
| `waveshare-35dpi-3b.dtbo` | `1bec92239d3ca95374cc36452812101947a1827d629cf153e2fa3c681a81f253` |
| `waveshare-35dpi-4b.dtbo` | `ab96c555880dce0a17dffad80dacbbb588efbca8209bd6d518e3c49aaf996b91` |
| `waveshare-35dpi-5b.dtbo` | `b86a6e34a54c86eacac8ab4a4a135b9217307b2cafa59b59fa5e70fd0605377e` |

## Verification chain

All three checks were run and passed on 2026-09-01:

1. Upstream zip contents == the copy in `DOCS/3.5HDMI_E_DTBO/` — identical.
2. `waveshare-35dpi-5b.dtbo` == `/boot/firmware/overlays/waveshare-35dpi-5b.dtbo`
   on the live development unit — identical.

So what is vendored here is exactly what upstream ships and exactly what is
currently running on the hardware.

## What these actually contain

**Touch only.** Despite the `35dpi` name and the `3.5HDMI_E` archive name, these
overlays contain no display node of any kind — just a Goodix GT911 on `i2c1`.
The panel is plain HDMI at native 640×480 and needs no overlay. Confirmed by
decompilation and corroborated by Waveshare's own Software Guide, which adds no
display, framebuffer, or video-mode configuration whatsoever:

```
dtparam=i2c_arm=on
dtoverlay=waveshare-35dpi-4b
dtoverlay=waveshare-35dpi-5b
dtoverlay=dwc2,dr_mode=host
```

(Waveshare instructs users to enable both `-4b` and `-5b` regardless of board.
On a Pi 5 only `-5b` applies.)

Variant selection is by Pi generation, and only the address set differs:

| Overlay | Board | GT911 address(es) |
|---|---|---|
| `-3b` | Pi 3B | 0x5d |
| `-4b` | Pi 4B | 0x14 |
| `-5b` | Pi 5 | 0x14 **and** 0x5d |

## Tracer does not use these at runtime

`-5b` declares the controller at both addresses so one overlay covers panels
strapped either way. Only 0x5d is populated on this unit, so the 0x14 node fails
`-EBUSY` and logs two error lines on every boot.

Tracer therefore ships [`image/overlays/tracer-gt911-overlay.dts`](../../overlays/tracer-gt911-overlay.dts)
— the `-5b` overlay with the 0x14 node removed and nothing else changed. The
0x5d node's property values are byte-identical to upstream (verified by
decompile-and-diff).

These vendored blobs are kept as the **fallback** for a replacement panel
strapped to 0x14, and as the provenance record for the derived overlay. See
[docs/hardware.md](../../../docs/hardware.md).
