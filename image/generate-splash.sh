#!/bin/bash
# Generate the Tracer boot splash (TGA) for rpi-image-gen's splash layer.
#
# Ported from TrailCurrentHeadwaters/CM5/image/generate-splash.sh. Same
# composition, same colours, same TGA constraints — two deliberate changes:
#
#   1. 640x480, not 1920x1080. That is the PocketTerm35 panel's native mode
#      (verified: HDMI-A-1 reports 640x480 as preferred). Rendering at 1080p
#      and letting the firmware scale would soften the icon on a 3.5" screen.
#      Note this is 4:3, not 16:9, so the layout is re-derived rather than
#      scaled from the Headwaters numbers.
#
#   2. The Tracer product icon instead of the TrailCurrent house icon.
#
# Requirements: ImageMagick (convert)
# Usage: ./generate-splash.sh [output.tga]
#
# Environment overrides:
#   MARKETING_DIR  — path to the TrailCurrent Marketing directory
#   ICON_SVG       — path to the icon SVG
#   WORDMARK       — text under the icon (default "Tracer")

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Marketing lives at TrailCurrent/Marketing, two levels up from the repo root
# (Product/TrailCurrentTracer -> Product -> TrailCurrent).
MARKETING_DIR="${MARKETING_DIR:-$(cd "$REPO_ROOT/../../Marketing" 2>/dev/null && pwd || echo "")}"
ICON_SVG="${ICON_SVG:-${MARKETING_DIR}/ProductNaming/brand/icons/svg/tc_tracer.svg}"
WORDMARK="${WORDMARK:-Tracer}"

OUTPUT="${1:-${SCRIPT_DIR}/splash/tracer-splash.tga}"
mkdir -p "$(dirname "$OUTPUT")"

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

# ── Dependencies ────────────────────────────────────────────────────
if ! command -v convert &>/dev/null; then
    echo "Error: ImageMagick is required but 'convert' not found."
    echo "Install with: sudo apt install imagemagick"
    exit 1
fi

if [ ! -f "$ICON_SVG" ]; then
    echo "Error: Icon SVG not found: $ICON_SVG"
    echo "Set MARKETING_DIR or ICON_SVG to override."
    exit 1
fi

echo "Generating Tracer boot splash (640x480)..."
echo "  icon:     $ICON_SVG"
echo "  wordmark: $WORDMARK"

# ── Layout (640x480) ────────────────────────────────────────────────
# Panel is 3.5", so proportions are driven by legibility at that size rather
# than by scaling the 1080p Headwaters numbers down.
BG_COLOR="#1a1a2e"        # identical to Headwaters
FG_COLOR="#52a441"        # TrailCurrent primary green
ICON_PX=180               # ~37% of height, matching Headwaters' proportion
ICON_OFFSET=-40           # nudge up to leave room for the wordmark
TEXT_OFFSET=+96
POINTSIZE=34

# The icon is a self-contained vector with no <text> elements (verified), so
# ImageMagick renders it correctly. If a future icon revision adds text, render
# it with Inkscape instead — ImageMagick mangles SVG text.
convert -background none -density 300 "$ICON_SVG" \
    -resize ${ICON_PX}x${ICON_PX} "$TMPDIR/icon.png"

FONT="DejaVu-Sans-Bold"
if ! convert -list font 2>/dev/null | grep -qi "DejaVu-Sans-Bold"; then
    FONT="Helvetica-Bold"
fi

convert -size 640x480 "xc:${BG_COLOR}" \
    \( "$TMPDIR/icon.png" \) -gravity center -geometry +0${ICON_OFFSET} -composite \
    -font "$FONT" -pointsize ${POINTSIZE} \
    -fill "$FG_COLOR" -gravity center -annotate +0${TEXT_OFFSET} "$WORDMARK" \
    "$TMPDIR/composed.png"

# Keep a PNG next to the TGA — useful for eyeballing the result without a
# TGA viewer, and for the docs.
cp "$TMPDIR/composed.png" "${OUTPUT%.tga}.png"

# ── TGA conversion ──────────────────────────────────────────────────
# Splash-screen requirements: 24-bit, max 224 colours, uncompressed.
# The -flip is required — boot splash TGAs are stored bottom-to-top.
convert "$TMPDIR/composed.png" \
    -depth 8 \
    -colors 224 \
    -type truecolor \
    -flip \
    -compress None \
    "$OUTPUT"

echo ""
echo "Splash created: $OUTPUT"
echo "Preview PNG:    ${OUTPUT%.tga}.png"
file "$OUTPUT"
identify "$OUTPUT" 2>/dev/null || true
