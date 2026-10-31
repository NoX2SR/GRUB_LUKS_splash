#!/usr/bin/env bash
# Boot the test disk directly with QEMU (UEFI, Secure Boot off).
# Faster for the edit/rebuild/boot loop than going through libvirt.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/out"
DISK="$OUT/test-disk.raw"
[[ -f "$DISK" ]] || { echo "No disk, run vm/make-test-disk.sh first" >&2; exit 1; }

[[ -f "$OUT/OVMF_VARS.fd" ]] || cp /usr/share/edk2/x64/OVMF_VARS.4m.fd "$OUT/OVMF_VARS.fd"

exec qemu-system-x86_64 \
    -enable-kvm -machine q35 -cpu host -smp 2 -m 1024 \
    -drive if=pflash,format=raw,readonly=on,file=/usr/share/edk2/x64/OVMF_CODE.4m.fd \
    -drive if=pflash,format=raw,file="$OUT/OVMF_VARS.fd" \
    -drive file="$DISK",format=raw,if=virtio \
    -vga std -display gtk -nic none \
    "$@"
