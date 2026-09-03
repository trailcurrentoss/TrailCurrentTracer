# Vendored CAN database

`TrailCurrent.dbc` is copied verbatim from `TrailCurrentDocumentation`.

Vendored rather than read across repos so the image is self-contained — a
flashed Tracer must not need another checkout present to know the frame
layouts (docs/api.md C1).

**It is a copy, not a fork.** Re-copy it whenever the fleet DBC changes; do
not edit it here. `make verify-dbc` re-parses it and fails if any message or
signal stops decoding.

Used by `tracerd/modules/simulate.py` to generate the Simulate app's forms and
to encode frames. Every field the UI shows — signal names, bit layout, scale,
offset, min/max, units, enumerations — comes from this file, so the simulator
cannot drift from the fleet definition.
