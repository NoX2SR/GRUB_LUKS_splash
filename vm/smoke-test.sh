#!/usr/bin/env bash
# Boot the test disk headless, walk through the unlock flow and save
# screenshots to vm/out/shots/:
#   1-menu       stage 1 menu
#   2-prompt     passphrase screen, nothing typed
#   3-typing     some characters typed
#   4-busy       right after submitting a wrong passphrase
#   5-error      after the wrong passphrase was rejected
#   6-unlocked   after the right passphrase
# Env: PASSPHRASE (default: test), UNLOCK_WAIT seconds (default: 8)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE/out"
PASSPHRASE="${PASSPHRASE:-test}"
SHOTS=shots
mkdir -p "$SHOTS"
rm -f "$SHOTS"/*.png m.sock
cp /usr/share/edk2/x64/OVMF_VARS.4m.fd vars-smoke.fd

qemu-system-x86_64 -enable-kvm -machine q35 -cpu host -m 1024 \
    -drive if=pflash,format=raw,readonly=on,file=/usr/share/edk2/x64/OVMF_CODE.4m.fd \
    -drive if=pflash,format=raw,file=vars-smoke.fd \
    -drive file=test-disk.raw,format=raw,if=virtio,snapshot=on \
    -vga std -display none -nic none -monitor unix:m.sock,server,nowait &
QPID=$!
trap 'kill $QPID 2>/dev/null; rm -f vars-smoke.fd m.sock' EXIT

mon() {
    python3 -c '
import socket, sys, time
s = socket.socket(socket.AF_UNIX)
s.connect("m.sock")
time.sleep(0.2)
s.recv(4096)
s.send(sys.argv[1].encode() + b"\n")
time.sleep(0.3)' "$1"
}
shot() { mon "screendump $SHOTS/$1.ppm"; magick "$SHOTS/$1.ppm" "$SHOTS/$1.png"; rm -f "$SHOTS/$1.ppm"; }
type_text() {
    local i c
    for ((i = 0; i < ${#1}; i++)); do
        c="${1:i:1}"
        mon "sendkey $c"
    done
}

for _ in $(seq 20); do [[ -S m.sock ]] && break; sleep 0.5; done
sleep 6;  shot 1-menu
mon "sendkey ret"; sleep 4; shot 2-prompt
type_text "nope"; sleep 1; shot 3-typing
mon "sendkey ret"; sleep 0.5; shot 4-busy
sleep "${UNLOCK_WAIT:-8}"; shot 5-error
type_text "$PASSPHRASE"; mon "sendkey ret"; sleep "${UNLOCK_WAIT:-8}"; shot 6-unlocked
ls "$SHOTS"
