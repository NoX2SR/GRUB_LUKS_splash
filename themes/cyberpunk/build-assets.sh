#!/usr/bin/env bash
# Regenerate the background (neon grid, skyline, scanlines) and fonts.
set -euo pipefail
cd "$(dirname "$0")"
source ../common.sh

W=1920 H=1080 HORIZON=640 CX=960

# Perspective grid below the horizon.
grid=""
for i in $(seq -24 24); do
    grid+="line $((CX + i * 14)),$HORIZON $((CX + i * 170)),$H "
done
for k in $(seq 1 14); do
    y=$((HORIZON + (H - HORIZON) * k * k / 196))
    grid+="line 0,$y $W,$y "
done

# Skyline silhouette with a few lit windows (fixed seed: same image every run).
RANDOM=2077
city="" windows=""
x=0
while ((x < W)); do
    w=$((40 + RANDOM % 90)) h=$((40 + RANDOM % 230))
    city+="rectangle $x,$((HORIZON - h)) $((x + w)),$HORIZON "
    for _ in 1 2 3; do
        wx=$((x + 6 + RANDOM % (w - 12))) wy=$((HORIZON - h + 8 + RANDOM % (h - 12)))
        windows+="rectangle $wx,$wy $((wx + 3)),$((wy + 2)) "
    done
    x=$((x + w + RANDOM % 14))
done

magick -size ${W}x${H} gradient:'#0b0016'-'#2a0a3c' \
    \( -size ${W}x$((H - HORIZON)) gradient:'#1a0626'-'#05000a' \) -geometry +0+$HORIZON -composite \
    \( -size ${W}x${H} xc:none -stroke none -fill '#ff2bd6' -draw "ellipse $CX,$HORIZON 900,60 0,360" -blur 0x60 \) -composite \
    \( -size ${W}x${H} xc:none -stroke '#ff2bd6' -strokewidth 5 -fill none -draw "$grid" -blur 0x6 \) -composite \
    \( -size ${W}x${H} xc:none -stroke '#ff5ce1' -strokewidth 1.5 -fill none -draw "$grid" \) -composite \
    \( -size ${W}x${H} xc:none -stroke none -fill '#07010d' -draw "$city" \) -composite \
    \( -size ${W}x${H} xc:none -stroke none -fill '#00f0ff' -draw "$windows" \) -composite \
    \( -size 1x3 xc:none -fill '#00000070' -draw 'point 0,2' -write mpr:line +delete \
       -size ${W}x${H} tile:mpr:line \) -composite \
    -depth 8 -define png:color-type=2 background.png

# Block elements and geometric shapes for masks and HUD marks.
EXTRA_RANGES=0x2580-0x259F,0x25A0-0x25FF
font "Fira Sans Compressed" 64 fira-compressed-64.pf2 /usr/share/fonts/TTF/FiraSansCompressed-Heavy.ttf
font "Hack" 20 hack-bold-20.pf2 /usr/share/fonts/TTF/Hack-Bold.ttf
font "Hack" 16 hack-regular-16.pf2 /usr/share/fonts/TTF/Hack-Regular.ttf
font "Hack" 13 hack-regular-13.pf2 /usr/share/fonts/TTF/Hack-Regular.ttf
