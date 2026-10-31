#!/usr/bin/env bash
# Regenerate the background (scanlines + vignette) and font of this theme.
set -euo pipefail
cd "$(dirname "$0")"
source ../common.sh

magick -size 1920x1080 xc:'#061006' \
    \( -size 1920x1080 radial-gradient:'#0d2a12'-'#020502' \) -compose over -composite \
    \( -size 1x3 xc:none -fill '#00000060' -draw 'point 0,2' -write mpr:line +delete \
       -size 1920x1080 tile:mpr:line \) -compose over -composite \
    -depth 8 -define png:color-type=2 background.png

font "Noto Sans Mono" 18 noto-sans-mono-18.pf2 /usr/share/fonts/noto/NotoSansMono-Regular.ttf
