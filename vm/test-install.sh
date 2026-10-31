#!/usr/bin/env bash
# End-to-end test of install.py on the test disk, without root:
#   1. build a stock disk (system GRUB, plain cryptomount prompt)
#   2. copy its ESP to vm/out/esp
#   3. run install.py on that directory
#   4. write the ESP back into the disk image
# Then boot it with vm/run-vm.sh or vm/smoke-test.sh.
#
# Usage: vm/test-install.sh [install|theme|uninstall] [script arguments...]
#   install:   fresh stock disk, then install.py ARGS (default: --theme midnight --yes)
#   theme:     install.py ARGS on the current disk (e.g. --theme terminal --yes)
#   uninstall: uninstall.py ARGS on the current disk (e.g. --yes)
# Env: LAYOUT=embedded|premenu (see make-test-disk.sh, default: embedded)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/out"
ESP_DIR="$OUT/esp"
DISK="$OUT/test-disk.raw"
ESP_MB=64
LAYOUT="${LAYOUT:-embedded}"
if [[ "$LAYOUT" == premenu ]]; then
    IMAGE=EFI/cachyos-menu/grubx64.efi
else
    IMAGE=EFI/encrypted/grubx64.efi
fi

action="${1:-install}"
shift || true
args=("$@")
[[ ${#args[@]} -gt 0 || "$action" != install ]] || args=(--theme midnight --yes)

case "$action" in
    install) "$HERE/make-test-disk.sh" --layout "$LAYOUT"; script=install.py ;;
    theme) script=install.py ;;
    uninstall) script=uninstall.py ;;
    *) echo "unknown action: $action" >&2; exit 1 ;;
esac

rm -rf "$ESP_DIR"
mkdir -p "$ESP_DIR"
mcopy -s -i "$DISK@@1M" ::/EFI "$ESP_DIR/"

"$HERE/../$script" "${args[@]}" --esp "$ESP_DIR" --grub-location "$IMAGE"

# Recreate the FAT file system from the directory and put it back.
ESP_IMG="$OUT/esp.img"
rm -f "$ESP_IMG"
mkfs.fat -F 32 -n ESP -C "$ESP_IMG" $((ESP_MB * 1024)) >/dev/null
mcopy -s -i "$ESP_IMG" "$ESP_DIR/EFI" ::/
dd if="$ESP_IMG" of="$DISK" bs=1M seek=1 conv=notrunc status=none
rm -f "$ESP_IMG"
echo "ESP written back to $DISK"
