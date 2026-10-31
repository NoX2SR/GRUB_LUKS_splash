"""Find EFI system partitions, the GRUB images on them, and boot entries."""

import os
import re
import shutil
import subprocess

SECURE_BOOT_VAR = "/sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c"
MAX_DEPTH = 4


def esp_mounts():
    """Mount points of FAT file systems (where ESPs live)."""
    mounts = []
    with open("/proc/self/mounts") as f:
        for line in f:
            _dev, mnt, fstype = line.split()[:3]
            if fstype in ("vfat", "msdos"):
                # /proc/mounts escapes spaces and friends as octal.
                mnt = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), mnt)
                mounts.append(mnt)
    return mounts


def efi_files(root):
    """*.efi files under ROOT, as paths relative to ROOT."""
    out = []
    root = os.path.abspath(root)
    for dirpath, dirs, names in os.walk(root, onerror=_raise):
        depth = dirpath[len(root):].count(os.sep)
        if depth >= MAX_DEPTH:
            dirs[:] = []
        for name in names:
            if name.lower().endswith(".efi"):
                out.append(os.path.relpath(os.path.join(dirpath, name), root))
    return sorted(out)


def _raise(err):
    raise err


def boot_entries():
    """{lower-case ESP-relative path: [labels]} from efibootmgr, if available."""
    if not shutil.which("efibootmgr"):
        return {}
    try:
        out = subprocess.run(["efibootmgr"], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    entries = {}
    for line in out.splitlines():
        m = re.match(r"^Boot([0-9A-Fa-f]{4})\*?\s+(.*?)\t(.*)$", line)
        if not m:
            continue
        path = re.search(r"\\[^\t()]*?\.efi", m.group(3), re.IGNORECASE)
        if path:
            key = path.group(0).replace("\\", "/").lstrip("/").lower()
            entries.setdefault(key, []).append(m.group(2).strip())
    return entries


def secure_boot_enabled():
    """True, False, or None if unknown (not booted via UEFI)."""
    try:
        with open(SECURE_BOOT_VAR, "rb") as f:
            data = f.read()
    except OSError:
        return None
    return bool(data[-1]) if data else None
