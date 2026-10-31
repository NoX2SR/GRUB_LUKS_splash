#!/usr/bin/env bash
# Read-only report of the GRUB setup on the ESP(s), for troubleshooting.
# Usage: sudo scripts/diagnose.sh > diagnose.txt
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "== install.py --list"
"$ROOT/install.py" --list 2>&1

echo
echo "== GRUB images: prefix and embedded config"
python3 - "$ROOT/installer" <<'EOF'
import os, sys
sys.path.insert(0, sys.argv[1])
import esp, grubimage
for mnt in esp.esp_mounts():
    for rel in esp.efi_files(mnt):
        path = os.path.join(mnt, rel)
        img = grubimage.read_image(path)
        if img is None:
            continue
        print(f"--- {path}")
        print(f"sha256: {grubimage.sha256(path)}")
        print(f"prefix: {img.prefix!r}")
        print(f"modules: {' '.join(img.modules)}")
        print("config:")
        print("    " + "\n    ".join(img.config.splitlines()))
EOF

echo
echo "== ESP files"
for mnt in $(awk '$3 == "vfat" {print $2}' /proc/self/mounts); do
    find "$mnt" -maxdepth 4 \( -iname '*.efi' -o -iname '*.cfg' -o -iname '*.json' \
        -o -name 'unlock-theme' -o -iname '*.pre-unlock-theme' \) -printf '%TY-%Tm-%Td %TH:%TM  %10s  %p\n' | sort -k3
done

echo
echo "== grub.cfg files on the ESP"
for mnt in $(awk '$3 == "vfat" {print $2}' /proc/self/mounts); do
    find "$mnt" -maxdepth 4 -iname 'grub.cfg' | while read -r f; do
        echo "--- $f"
        cat "$f"
    done
done

echo
echo "== unlock-theme manifests"
for mnt in $(awk '$3 == "vfat" {print $2}' /proc/self/mounts); do
    find "$mnt" -maxdepth 4 -name 'unlock-theme.json' | while read -r f; do
        echo "--- $f"
        cat "$f"
        echo "theme dir:"
        ls -la "$(dirname "$f")/unlock-theme" 2>&1
    done
done
