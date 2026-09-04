# Design reference

The authoritative visual and interaction spec for Tracer OS. Layout, colour, type
size, scroll behaviour, and per-app button semantics are lifted from here
verbatim rather than approximated.

| File | What it is |
|---|---|
| `PocketTerm35 OS v2.dc.html` | **Authoritative.** Launcher plus all twelve apps, with real data shapes. |
| `PocketTerm35 OS.dc.html` | Earlier revision. Kept for history — **do not build from it.** |
| `support.js` | Runtime the `.dc.html` mocks need. Required for them to open. |
| `assets/boot-logo.png` | Boot logo used by the mock's splash screen. |
| `PROVENANCE-claude-design.md` | Which Headwaters source files each screen was derived from. |
| `thumbnail.webp` | Preview image from the design export. |

Open `PocketTerm35 OS v2.dc.html` in a browser — it is interactive. Arrows drive
the D-pad, Enter/Esc are A/B, and X/Y/Q/E are the remaining buttons.

## Where this came from

Exported from Claude Design, originally at
`DOCS/Waveshare PocketTerm35 OS design/`. Moved here so the repo has a single
lowercase `docs/` directory — `DOCS/` and `docs/` differ only in case, which
collides on macOS and Windows checkouts.

One file was not carried over: `uploads/boot_logo-1788221255605-656n.png`, a
byte-identical duplicate of `assets/boot-logo.png` (md5 `8f4d5b47…`).

## The one place the mock is not followed

The **boot splash**. The mock draws an animated progress bar and a changing
status line; the shipped splash is a static TGA matching Headwaters' composition
with the Tracer icon. That is a deliberate decision, not an oversight — see
[../docs/boot.md](../docs/boot.md#3-splash).

Everything else should match the mock. Checking is manual today: `make dev-shot`
captures the panel at 640×480 into `./panel.png` for side-by-side comparison —
there is no automated screenshot diff yet.
