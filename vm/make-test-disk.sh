#!/usr/bin/env bash
# Build a UEFI test disk that mirrors the real setup, without root:
#
#   p1 ESP (FAT32)
#      /EFI/BOOT/BOOTX64.EFI          stage 1 GRUB: boot menu (Linux / Windows)
#      /EFI/BOOT/grub.cfg
#      /EFI/encrypted/grubx64.efi     stage 2 GRUB: asks for the passphrase,
#                                     unlocks p2, loads its grub.cfg
#   p2 LUKS2 (aes-xts-plain64) -> ext4
#      /boot/grub/grub.cfg            "unlocked" confirmation menu
#
# Usage: vm/make-test-disk.sh [--pbkdf pbkdf2|argon2id] [--grub-dir DIR] [--theme DIR]
#   --grub-dir   GRUB build directory (modules + grub-mkimage); default: system
#                GRUB, or build/grub-efi when --theme is given
#   --theme      unlock theme directory; stage 2 then uses unlock_theme instead
#                of the plain cryptomount prompt
#   --layout     embedded (default): stage 1 chainloads stage 2, whose built-in
#                config runs cryptomount
#                premenu: a standalone GRUB at EFI/cachyos-menu whose grub.cfg
#                menu entries run cryptomount (stage 2 stays on the ESP unused)
# Env: PASSPHRASE (default: test), ITER_TIME in ms (default: 200)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/out"
PASSPHRASE="${PASSPHRASE:-test}"
ITER_TIME="${ITER_TIME:-200}"
PBKDF=pbkdf2
GRUB_DIR=
THEME_DIR=
LAYOUT=embedded

while [[ $# -gt 0 ]]; do
    case "$1" in
        --pbkdf) PBKDF="$2"; shift 2 ;;
        --grub-dir) GRUB_DIR="$(realpath "$2")"; shift 2 ;;
        --theme) THEME_DIR="$(realpath "$2")"; shift 2 ;;
        --layout) LAYOUT="$2"; shift 2 ;;
        *) echo "unknown option: $1" >&2; exit 1 ;;
    esac
done

if [[ -z "$GRUB_DIR" && -n "$THEME_DIR" ]]; then
    GRUB_DIR="$HERE/../build/grub-efi"
fi
if [[ -n "$GRUB_DIR" ]]; then
    GRUB_MODDIR="$GRUB_DIR/grub-core"
    MKIMAGE="$GRUB_DIR/grub-mkimage"
else
    GRUB_MODDIR=/usr/lib/grub/x86_64-efi
    MKIMAGE=grub-mkimage
fi

ESP_MB=64
LUKS_MB=64
LUKS_UUID=5e1f0c2a-0000-4000-8000-00000000c0de
FS_UUID=5e1f0c2a-0000-4000-8000-0000000000f5

rm -rf "$OUT/work"
mkdir -p "$OUT/work"
W="$OUT/work"

# --- encrypted partition --------------------------------------------------
mkdir -p "$W/root/boot/grub"
cat > "$W/root/boot/grub/grub.cfg" <<'EOF'
set timeout=-1
menuentry "Unlocked: the encrypted partition was decrypted successfully" { true }
menuentry "Reboot" { reboot }
menuentry "Power off" { halt }
EOF

PLAIN_MB=$((LUKS_MB - 16))   # LUKS2 header takes the first 16 MiB
/usr/bin/mkfs.ext4 -q -F -L cryptroot -U "$FS_UUID" -d "$W/root" "$W/plain.ext4" "${PLAIN_MB}M"

truncate -s "${LUKS_MB}M" "$W/luks.img"
# A short iter time keeps the test loop fast; GRUB derives keys several times
# slower than cryptsetup on the host, so real disks take noticeably longer.
printf '%s' "$PASSPHRASE" | cryptsetup luksFormat -q --type luks2 --pbkdf "$PBKDF" \
    --iter-time "$ITER_TIME" --sector-size 512 --uuid "$LUKS_UUID" --key-file - "$W/luks.img"
printf '%s' "$PASSPHRASE" | cryptsetup luksDump -q --dump-volume-key \
    --volume-key-file "$W/vk.bin" --key-file - "$W/luks.img" >/dev/null
OFFSET=$(cryptsetup luksDump --dump-json-metadata "$W/luks.img" |
    python3 -c 'import json,sys; print(json.load(sys.stdin)["segments"]["0"]["offset"])')
python3 -I "$HERE/luks_fill.py" "$W/luks.img" "$W/vk.bin" "$OFFSET" "$W/plain.ext4"
rm -f "$W/vk.bin"

# --- GRUB images ----------------------------------------------------------
STAGE2_EXTRA=()
if [[ -n "$THEME_DIR" ]]; then
    UNLOCK="unlock_theme -t \$prefix/unlock-theme/theme.txt -u $LUKS_UUID"
    STAGE2_EXTRA=(unlock_theme)
else
    UNLOCK="cryptomount -u $LUKS_UUID"
fi
cat > "$W/stage2-early.cfg" <<EOF
$UNLOCK
search --no-floppy --fs-uuid --set=root $FS_UUID
configfile /boot/grub/grub.cfg
EOF

"$MKIMAGE" -d "$GRUB_MODDIR" -O x86_64-efi -p /EFI/BOOT -o "$W/BOOTX64.EFI" \
    part_gpt fat normal configfile echo sleep chain efifwsetup reboot halt \
    efi_gop gfxterm search
"$MKIMAGE" -d "$GRUB_MODDIR" -O x86_64-efi -p /EFI/encrypted -c "$W/stage2-early.cfg" \
    -o "$W/grubx64.efi" \
    part_gpt fat ext2 cryptodisk luks luks2 gcry_rijndael gcry_sha256 gcry_sha512 \
    pbkdf2 argon2 normal configfile echo search search_fs_uuid reboot halt \
    efi_gop gfxterm font png jpeg tga "${STAGE2_EXTRA[@]}"

cat > "$W/stage1.cfg" <<'EOF'
set timeout=10
menuentry "Linux (encrypted)" { chainloader /EFI/encrypted/grubx64.efi }
menuentry "Windows (placeholder)" { echo "Placeholder entry, nothing to boot."; sleep 3 }
menuentry "UEFI Firmware Settings" { fwsetup }
EOF

# --- ESP ------------------------------------------------------------------
mkfs.fat -F 32 -n ESP -C "$W/esp.img" $((ESP_MB * 1024)) >/dev/null
mmd -i "$W/esp.img" ::/EFI ::/EFI/BOOT ::/EFI/encrypted
if [[ "$LAYOUT" == premenu ]]; then
    # A grub-mkstandalone menu whose entries run cryptomount themselves, with
    # a grub.cfg "generated" from grub.d templates (like grub-premenu-mkconfig).
    mkdir -p "$OUT/premenu/grub.d"
    cat > "$W/memdisk.cfg" <<'EOF'
search --no-floppy --file --set=root /EFI/cachyos-menu/grub.cfg
configfile ($root)/EFI/cachyos-menu/grub.cfg
EOF
    grub-mkstandalone -O x86_64-efi --modules="part_gpt fat" -o "$W/menu.efi" \
        "boot/grub/grub.cfg=$W/memdisk.cfg"
    cat > "$W/menu.cfg" <<EOF
#
# DO NOT EDIT THIS FILE
#
# It is automatically generated by grub-mkconfig using templates
# from $OUT/premenu/grub.d and settings from /etc/grub-premenu/default
#

### BEGIN $OUT/premenu/grub.d/00_header ###
insmod part_gpt
insmod fat
if loadfont \$config_directory/unicode.pf2 ; then
  set gfxmode=auto
  insmod efi_gop
  insmod gfxterm
fi
terminal_output gfxterm
set timeout=10
### END $OUT/premenu/grub.d/00_header ###

### BEGIN $OUT/premenu/grub.d/10_linux ###
menuentry 'Linux (encrypted)' {
	insmod cryptodisk
	insmod luks2
	insmod gcry_rijndael
	insmod gcry_sha256
	cryptomount -u $LUKS_UUID
	search --no-floppy --fs-uuid --set=root $FS_UUID
	configfile /boot/grub/grub.cfg
}
### END $OUT/premenu/grub.d/10_linux ###
EOF
    mmd -i "$W/esp.img" ::/EFI/cachyos-menu
    mcopy -i "$W/esp.img" "$W/menu.efi" ::/EFI/BOOT/BOOTX64.EFI
    mcopy -i "$W/esp.img" "$W/menu.efi" ::/EFI/cachyos-menu/grubx64.efi
    mcopy -i "$W/esp.img" "$W/menu.cfg" ::/EFI/cachyos-menu/grub.cfg
    mcopy -i "$W/esp.img" /usr/share/grub/unicode.pf2 ::/EFI/cachyos-menu/
else
    mcopy -i "$W/esp.img" "$W/BOOTX64.EFI" "$W/stage1.cfg" ::/EFI/BOOT/
    mren -i "$W/esp.img" ::/EFI/BOOT/stage1.cfg ::/EFI/BOOT/grub.cfg
fi
mcopy -i "$W/esp.img" "$W/grubx64.efi" ::/EFI/encrypted/
if [[ -n "$THEME_DIR" ]]; then
    mmd -i "$W/esp.img" ::/EFI/encrypted/unlock-theme
    find "$THEME_DIR" -maxdepth 1 -type f ! -name '*.sh' \
        -exec mcopy -i "$W/esp.img" {} ::/EFI/encrypted/unlock-theme/ \;
fi

# --- assemble GPT disk ----------------------------------------------------
DISK="$OUT/test-disk.raw"
truncate -s 0 "$DISK"
truncate -s $((1 + ESP_MB + LUKS_MB + 1))M "$DISK"
sfdisk -q "$DISK" <<EOF
label: gpt
start=1MiB, size=${ESP_MB}MiB, type=C12A7328-F81F-11D2-BA4B-00A0C93EC93B, name="EFI system"
size=${LUKS_MB}MiB, type=CA7D7CCB-63ED-4C53-861C-1742536059CC, name="cryptroot"
EOF
dd if="$W/esp.img" of="$DISK" bs=1M seek=1 conv=notrunc status=none
dd if="$W/luks.img" of="$DISK" bs=1M seek=$((1 + ESP_MB)) conv=notrunc status=none

rm -rf "$W"
echo "Built $DISK (LUKS2, pbkdf=$PBKDF, passphrase: '$PASSPHRASE'${THEME_DIR:+, theme: $THEME_DIR})"
