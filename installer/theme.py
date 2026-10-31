"""Load and check unlock themes before they are installed.

The checks mirror what the GRUB module would reject at boot (where a broken
theme means falling back to the plain prompt), so problems show up here
instead.
"""

import os
import re
import struct
import tarfile
import zipfile
from dataclasses import dataclass, field

THEME_FILE = "theme.txt"

GLOBAL_KEYS = {"background-image", "background-mode", "background-color",
               "gfxmode", "max-tries", "font-file"}
ELEMENTS = {"box", "image", "label", "passphrase"}
ELEMENT_KEYS = {"id", "left", "top", "width", "height", "show", "text", "file",
                "font", "mask", "placeholder", "color", "background-color",
                "border-color", "placeholder-color", "cursor-color",
                "border-width", "padding", "fixed-length", "align", "mask-mode"}

ARCHIVE_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2",
                    ".tar.zst", ".zip")

# GRUB decodes images at 4 bytes per pixel.
IMAGE_MEMORY_WARN = 64 << 20


class ThemeError(Exception):
    pass


@dataclass
class Theme:
    dir: str
    name: str
    description: str = ""
    files: list = field(default_factory=list)     # relative to dir, incl. theme.txt
    warnings: list = field(default_factory=list)


def _parse(text):
    """Yields (element or None, key, value, line) like the GRUB module."""
    i, n, line = 0, len(text), 1

    def error(msg):
        raise ThemeError(f"{THEME_FILE}:{line}: {msg}")

    def skip():
        nonlocal i, line
        while i < n:
            if text[i] == "#":
                while i < n and text[i] != "\n":
                    i += 1
            elif text[i].isspace():
                line += text[i] == "\n"
                i += 1
            else:
                break

    def name():
        nonlocal i
        start = i
        while i < n and (text[i].isalnum() or text[i] in "-_"):
            i += 1
        if i == start:
            error("name expected")
        return text[start:i].replace("_", "-")

    def value():
        nonlocal i, line
        if i < n and text[i] == '"':
            i += 1
            out = []
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    i += 1
                line += text[i] == "\n"
                out.append(text[i])
                i += 1
            if i >= n:
                error("unterminated string")
            i += 1
            return "".join(out)
        start = i
        while i < n and not text[i].isspace() and text[i] != "}":
            i += 1
        if i == start:
            error("value expected")
        return text[start:i]

    def expect(c):
        nonlocal i
        skip()
        if i >= n or text[i] != c:
            error(f"`{c}' expected")
        i += 1

    while True:
        skip()
        if i >= n:
            return
        if text[i] != "+":
            key = name()
            expect(":")
            skip()
            yield None, key, value(), line
            continue
        i += 1
        skip()
        kind = name()
        if kind not in ELEMENTS:
            error(f"unknown element `{kind}'")
        expect("{")
        while True:
            skip()
            if i >= n:
                error("`}' expected")
            if text[i] == "}":
                i += 1
                break
            key = name()
            expect("=")
            skip()
            yield kind, key, value(), line


def pf2_info(path):
    """(font name, set of code points) of a .pf2 file."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"FILE" or data[8:12] != b"PFF2":
        raise ThemeError(f"{os.path.basename(path)}: not a GRUB .pf2 font")
    name, chars = None, set()
    pos = 12
    while pos + 8 <= len(data):
        tag = data[pos:pos + 4]
        size = struct.unpack_from(">I", data, pos + 4)[0]
        if tag == b"NAME":
            name = data[pos + 8:pos + 8 + size].split(b"\0")[0].decode()
        elif tag == b"CHIX":
            # Character index: 9 bytes per glyph, code point first.
            for off in range(pos + 8, pos + 8 + size, 9):
                chars.add(struct.unpack_from(">I", data, off)[0])
        elif tag == b"DATA":
            break
        pos += 8 + size
    if name is None:
        raise ThemeError(f"{os.path.basename(path)}: font has no name")
    return name, chars


def _check_image(path):
    """Returns the decoded size in bytes; raises on formats GRUB can't read."""
    rel = os.path.basename(path)
    with open(path, "rb") as f:
        data = f.read()
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        width, height, depth, color, _, _, interlace = struct.unpack_from(">IIBBBBB", data, 16)
        if color in (0, 4):
            raise ThemeError(f"{rel}: grayscale PNG is not supported by GRUB, save it as RGB")
        if interlace:
            raise ThemeError(f"{rel}: interlaced PNG is not supported by GRUB")
        if depth not in (8, 16) and not (color == 3 and depth == 4):
            raise ThemeError(f"{rel}: PNG bit depth {depth} is not supported by GRUB")
        return width * height * 4
    if data[:2] == b"\xff\xd8":
        pos = 2
        while pos + 4 <= len(data):
            if data[pos] != 0xFF:
                break
            marker = data[pos + 1]
            if marker == 0xC0:
                height, width = struct.unpack_from(">HH", data, pos + 5)
                return width * height * 4
            if 0xC1 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                raise ThemeError(f"{rel}: only baseline JPEG is supported by GRUB "
                                 "(this one is progressive or extended)")
            pos += 2 + struct.unpack_from(">H", data, pos + 2)[0]
        raise ThemeError(f"{rel}: unreadable JPEG")
    if path.lower().endswith(".tga"):
        width, height = struct.unpack_from("<HH", data, 12)
        return width * height * 4
    raise ThemeError(f"{rel}: unsupported image format (use PNG, JPEG or TGA)")


def _relative(theme_dir, value, line):
    if value.startswith(("/", "(")):
        raise ThemeError(f"{THEME_FILE}:{line}: `{value}': use a path relative "
                         "to the theme directory")
    rel = os.path.normpath(value)
    if rel.startswith(".."):
        raise ThemeError(f"{THEME_FILE}:{line}: `{value}' is outside the theme directory")
    if not os.path.isfile(os.path.join(theme_dir, rel)):
        raise ThemeError(f"{THEME_FILE}:{line}: file `{value}' not found")
    return rel


def load(theme_dir, name=None):
    """Parses and checks THEME_DIR/theme.txt. Raises ThemeError."""
    path = os.path.join(theme_dir, THEME_FILE)
    if not os.path.isfile(path):
        raise ThemeError(f"no {THEME_FILE} in {theme_dir}")
    with open(path, encoding="utf-8") as f:
        text = f.read()

    theme = Theme(dir=theme_dir, name=name or os.path.basename(os.path.normpath(theme_dir)))
    first = text.lstrip().splitlines()[0] if text.strip() else ""
    if first.startswith("#"):
        theme.description = first.lstrip("#").strip()

    files = {THEME_FILE}
    fonts, used_fonts, image_bytes = set(), [], 0
    glyphs, texts = set(), []
    for element, key, value, line in _parse(text):
        if element is None and key not in GLOBAL_KEYS:
            raise ThemeError(f"{THEME_FILE}:{line}: unknown setting `{key}'")
        if element is not None and key not in ELEMENT_KEYS:
            raise ThemeError(f"{THEME_FILE}:{line}: unknown property `{key}' in {element}")
        if key in ("background-image", "file", "font-file"):
            rel = _relative(theme_dir, value, line)
            files.add(rel)
            full = os.path.join(theme_dir, rel)
            if key == "font-file":
                name, chars = pf2_info(full)
                fonts.add(name)
                glyphs |= chars
            else:
                image_bytes += _check_image(full)
        elif key == "font":
            used_fonts.append((value, line))
        elif key in ("text", "mask", "placeholder"):
            texts.append((value, line))

    for value, line in used_fonts:
        if value not in fonts:
            known = ", ".join(sorted(fonts)) or "none"
            raise ThemeError(f"{THEME_FILE}:{line}: font `{value}' is not loaded by any "
                             f"font-file (loaded: {known})")
    # GRUB falls back to any loaded font for a missing glyph, and draws a box
    # if none has it.
    for value, line in texts:
        plain = re.sub(r"\{(device|attempt|max_tries|attempts_left)\}", "", value)
        missing = sorted({c for c in plain if ord(c) not in glyphs})
        if fonts and missing:
            raise ThemeError(f"{THEME_FILE}:{line}: no loaded font has the character(s) "
                             + " ".join(f"`{c}' (U+{ord(c):04X})" for c in missing))
    if image_bytes > IMAGE_MEMORY_WARN:
        theme.warnings.append(f"images need {image_bytes >> 20} MB of memory in GRUB; "
                              "smaller images load faster")
    theme.files = sorted(files)
    return theme


def bundled(themes_root):
    """Themes shipped in THEMES_ROOT: [(Theme or None, name, error)]."""
    out = []
    for name in sorted(os.listdir(themes_root)):
        d = os.path.join(themes_root, name)
        if not os.path.isfile(os.path.join(d, THEME_FILE)):
            continue
        try:
            out.append((load(d), name, None))
        except ThemeError as e:
            out.append((None, name, str(e)))
    return out


def is_archive(path):
    return path.lower().endswith(ARCHIVE_SUFFIXES)


def extract(archive, dest):
    """Extracts ARCHIVE into DEST and returns the directory holding theme.txt."""
    if archive.lower().endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            for member in z.namelist():
                norm = os.path.normpath(member)
                if norm.startswith(("..", "/")) or os.path.isabs(norm):
                    raise ThemeError(f"unsafe path in archive: {member}")
            z.extractall(dest)
    else:
        try:
            with tarfile.open(archive) as t:
                t.extractall(dest, filter="data")
        except (tarfile.TarError, OSError) as e:
            raise ThemeError(f"cannot extract {archive}: {e}") from e

    candidates = []
    for root, _dirs, names in os.walk(dest):
        if THEME_FILE in names:
            candidates.append(root)
    if not candidates:
        raise ThemeError(f"no {THEME_FILE} in {archive}")
    if len(candidates) > 1:
        raise ThemeError(f"{archive} contains more than one theme")
    return candidates[0]
