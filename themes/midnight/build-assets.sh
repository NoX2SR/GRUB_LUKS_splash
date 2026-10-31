#!/usr/bin/env bash
# Regenerate the binary assets of this theme (background, icon, fonts).
set -euo pipefail
cd "$(dirname "$0")"
source ../common.sh

magick -size 1920x1080 xc:'#070a14' \
    \( -size 1500x1500 radial-gradient:'#2f4690'-none \) -geometry -350-550 -composite \
    \( -size 1300x1300 radial-gradient:'#6a2f86'-none \) -geometry +1050+250 -composite \
    -blur 0x30 -depth 8 -define png:color-type=2 background.png

magick -background none -density 1200 \
    /usr/share/icons/Adwaita/symbolic/status/system-lock-screen-symbolic.svg \
    -resize 64x64 -fill white -colorize 100 -depth 8 PNG32:lock.png

font "Noto Sans" 32 noto-sans-bold-32.pf2 /usr/share/fonts/noto/NotoSans-Bold.ttf
font "Noto Sans" 20 noto-sans-regular-20.pf2 /usr/share/fonts/noto/NotoSans-Regular.ttf
font "Noto Sans" 16 noto-sans-regular-16.pf2 /usr/share/fonts/noto/NotoSans-Regular.ttf
