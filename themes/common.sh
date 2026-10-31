# Shared helpers for themes/*/build-assets.sh. Source it, don't run it.
#
# MKFONT: grub-mkfont to use (default: our build, then the system one).

THEMES_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -z "${MKFONT:-}" ]]; then
    MKFONT="$THEMES_DIR/../build/grub-efi/grub-mkfont"
    [[ -x "$MKFONT" ]] || MKFONT=grub-mkfont
fi

# Latin-1, plus bullets (masks), ellipsis and arrows.
FONT_RANGES=0x20-0x7E,0xA0-0xFF,0x2022-0x2022,0x2026-0x2026,0x2190-0x2193

# font FAMILY SIZE OUT.pf2 IN.ttf
# GRUB names the result "<FAMILY> <Regular|Bold|...> <SIZE>"; a theme's
# font = "..." must match that exactly. EXTRA_RANGES adds more characters.
font() {
    "$MKFONT" -s "$2" -r "$FONT_RANGES${EXTRA_RANGES:+,$EXTRA_RANGES}" -n "$1" -o "$3" "$4" 2>&1 |
        grep -v "unsupported font feature" || true
}

# png_rgb IN OUT: re-encode as plain 8-bit RGB PNG, which GRUB can read.
png_rgb() { magick "$1" -depth 8 -define png:color-type=2 -interlace none "$2"; }
