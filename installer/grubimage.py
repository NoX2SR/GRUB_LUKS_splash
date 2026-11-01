"""Read GRUB EFI core images and rebuild them with grub-mkimage.

A core image (e.g. grubx64.efi) is a PE file whose "mods" section holds a
module info header ("mimg") followed by objects: ELF modules, the embedded
early config, the prefix, and so on (see grub/include/grub/kernel.h).
"""

import hashlib
import os
import shutil
import struct
import subprocess
import tempfile
from dataclasses import dataclass, field

MODULE_INFO_MAGIC = b"mimg"

(OBJ_ELF, OBJ_MEMDISK, OBJ_CONFIG, OBJ_PREFIX, OBJ_GPG_PUBKEY, OBJ_X509_PUBKEY,
 OBJ_DTB, OBJ_DISABLE_SHIM_LOCK, OBJ_DISABLE_CLI) = range(9)

OBJ_NAMES = {
    OBJ_GPG_PUBKEY: "GPG public key",
    OBJ_X509_PUBKEY: "X.509 public key",
    OBJ_DTB: "device tree",
}

PE_TARGETS = {0x8664: "x86_64-efi", 0x014C: "i386-efi", 0xAA64: "arm64-efi"}


class ImageError(Exception):
    pass


@dataclass
class GrubImage:
    path: str
    target: str
    modules: list = field(default_factory=list)
    module_data: dict = field(default_factory=dict)   # name -> embedded ELF object
    config: str = ""
    prefix: str = ""
    memdisk: bytes = None
    sbat: bytes = None
    disable_shim_lock: bool = False
    disable_cli: bool = False
    unsupported: list = field(default_factory=list)


def _cstr(data, offset=0):
    end = data.find(b"\0", offset)
    return data[offset:end if end >= 0 else len(data)].decode("utf-8", "replace")


def _pe_sections(data):
    """Returns (machine, {name: (file offset, size)})."""
    if data[:2] != b"MZ":
        raise ImageError("not a PE file")
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        raise ImageError("not a PE file")
    machine, nsections = struct.unpack_from("<HH", data, pe + 4)
    opt_size = struct.unpack_from("<H", data, pe + 20)[0]
    sections = {}
    off = pe + 24 + opt_size
    for _ in range(nsections):
        name = data[off:off + 8].rstrip(b"\0").decode("ascii", "replace")
        vsize, _vaddr, rawsize, rawptr = struct.unpack_from("<IIII", data, off + 8)
        sections[name] = (rawptr, min(vsize or rawsize, rawsize))
        off += 40
    return machine, sections


def _elf_modname(elf):
    if elf[:4] != b"\x7fELF":
        raise ImageError("module is not an ELF object")
    if elf[4] == 2:
        shoff = struct.unpack_from("<Q", elf, 0x28)[0]
        shentsize, shnum, shstrndx = struct.unpack_from("<HHH", elf, 0x3A)

        def section(i):
            base = shoff + i * shentsize
            name = struct.unpack_from("<I", elf, base)[0]
            offset, size = struct.unpack_from("<QQ", elf, base + 0x18)
            return name, offset, size
    else:
        shoff = struct.unpack_from("<I", elf, 0x20)[0]
        shentsize, shnum, shstrndx = struct.unpack_from("<HHH", elf, 0x2E)

        def section(i):
            base = shoff + i * shentsize
            name = struct.unpack_from("<I", elf, base)[0]
            offset, size = struct.unpack_from("<II", elf, base + 0x10)
            return name, offset, size

    _, strtab, _ = section(shstrndx)
    for i in range(shnum):
        name, offset, size = section(i)
        if _cstr(elf, strtab + name) == ".modname":
            return _cstr(elf[offset:offset + size])
    raise ImageError("module without .modname")


def _parse_objects(img, data, info, wide):
    if wide:
        _magic, _pad, offset, size = struct.unpack_from("<IIQQ", data, info)
    else:
        _magic, offset, size = struct.unpack_from("<III", data, info)
    pos, end = info + offset, info + size
    if offset < 8 or end > len(data):
        raise ImageError("bad module info")
    while pos < end:
        kind, objsize = struct.unpack_from("<II", data, pos)
        if objsize < 8 or pos + objsize > end:
            raise ImageError("bad module header")
        body = data[pos + 8:pos + objsize]
        if kind == OBJ_ELF:
            name = _elf_modname(body)
            img.modules.append(name)
            img.module_data[name] = body
        elif kind == OBJ_CONFIG:
            img.config = _cstr(body)
        elif kind == OBJ_PREFIX:
            img.prefix = _cstr(body)
        elif kind == OBJ_MEMDISK:
            img.memdisk = body
        elif kind == OBJ_DISABLE_SHIM_LOCK:
            img.disable_shim_lock = True
        elif kind == OBJ_DISABLE_CLI:
            img.disable_cli = True
        else:
            img.unsupported.append(OBJ_NAMES.get(kind, f"object type {kind}"))
        pos += objsize


def read_image(path):
    """Returns a GrubImage, or None if PATH is not a GRUB core image."""
    with open(path, "rb") as f:
        data = f.read()
    try:
        machine, sections = _pe_sections(data)
    except (ImageError, struct.error):
        return None
    target = PE_TARGETS.get(machine)
    if target is None:
        return None

    start, end = sections.get("mods", (0, len(data)))
    end += start
    wide = machine in (0x8664, 0xAA64)
    while True:
        info = data.find(MODULE_INFO_MAGIC, start, end)
        if info < 0:
            return None
        start = info + 4
        img = GrubImage(path=path, target=target)
        try:
            _parse_objects(img, data, info, wide)
        except (ImageError, struct.error):
            continue
        if not img.modules:
            continue
        if ".sbat" in sections:
            offset, size = sections[".sbat"]
            img.sbat = data[offset:offset + size].rstrip(b"\0")
        return img


def same_grub(a, b, ignore=()):
    """True if images A and B were built from the same GRUB: grub-mkimage
    embeds module files unchanged, so their shared modules are identical."""
    shared = (set(a.module_data) & set(b.module_data)) - set(ignore)
    return bool(shared) and all(a.module_data[m] == b.module_data[m] for m in shared)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def grub_version(mkimage):
    """'2.16' from 'grub-mkimage (GRUB) 2:2.16-1.1' or '... 2.16'."""
    out = subprocess.run([mkimage, "--version"], capture_output=True, text=True,
                         check=True).stdout.strip()
    version = out.split()[-1]
    version = version.split(":", 1)[-1]
    return version.split("-", 1)[0]


@dataclass
class ModuleSource:
    """Where grub-mkimage takes the kernel and modules from."""
    mkimage: str
    moddir: str
    extra_mods: dict = field(default_factory=dict)   # name -> (path, moddep line)

    def missing(self, modules):
        return [m for m in modules
                if m not in self.extra_mods
                and not os.path.exists(os.path.join(self.moddir, m + ".mod"))]


def build_image(img, config, modules, source, out_path):
    """Writes a new core image to OUT_PATH with the same prefix, memdisk,
    SBAT and flags as IMG, but CONFIG and MODULES."""
    missing = source.missing(modules)
    if missing:
        raise ImageError("modules not available: " + ", ".join(missing))

    with tempfile.TemporaryDirectory(prefix="unlock-theme-") as tmp:
        moddir = source.moddir
        if source.extra_mods:
            # grub-mkimage resolves dependencies through moddep.lst, so give
            # it a module directory that also contains our modules.
            moddir = os.path.join(tmp, "mods")
            os.mkdir(moddir)
            for name in os.listdir(source.moddir):
                os.symlink(os.path.join(source.moddir, name), os.path.join(moddir, name))
            with open(os.path.join(source.moddir, "moddep.lst")) as f:
                moddep = f.read()
            for name, (path, dep_line) in source.extra_mods.items():
                dst = os.path.join(moddir, name + ".mod")
                if os.path.lexists(dst):
                    os.unlink(dst)
                shutil.copy(path, dst)
                moddep = "".join(l for l in moddep.splitlines(True)
                                 if not l.startswith(name + ":"))
                moddep += dep_line.rstrip("\n") + "\n"
            os.unlink(os.path.join(moddir, "moddep.lst"))
            with open(os.path.join(moddir, "moddep.lst"), "w") as f:
                f.write(moddep)

        cfg = os.path.join(tmp, "early.cfg")
        with open(cfg, "w") as f:
            f.write(config if config.endswith("\n") else config + "\n")
        cmd = [source.mkimage, "-d", moddir, "-O", img.target, "-p", img.prefix,
               "-c", cfg, "-o", out_path]
        if img.memdisk is not None:
            path = os.path.join(tmp, "memdisk")
            with open(path, "wb") as f:
                f.write(img.memdisk)
            cmd += ["-m", path]
        if img.sbat:
            path = os.path.join(tmp, "sbat.csv")
            with open(path, "wb") as f:
                f.write(img.sbat)
            cmd += ["--sbat", path]
        if img.disable_shim_lock:
            cmd.append("--disable-shim-lock")
        if img.disable_cli:
            cmd.append("--disable-cli")
        cmd += list(dict.fromkeys(modules))
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise ImageError("grub-mkimage failed: " + (res.stderr or res.stdout).strip())
