#!/usr/bin/env bash
# Regenerate the fonts of this theme.
set -euo pipefail
cd "$(dirname "$0")"
source ../common.sh

font "Noto Sans" 18 noto-sans-regular-18.pf2 /usr/share/fonts/noto/NotoSans-Regular.ttf
font "Noto Sans" 14 noto-sans-regular-14.pf2 /usr/share/fonts/noto/NotoSans-Regular.ttf
