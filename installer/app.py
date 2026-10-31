"""Shared code of install.py and uninstall.py.

Two kinds of setups are handled:

- embedded: the image's built-in config runs cryptomount (grub-install with
  GRUB_ENABLE_CRYPTODISK). The image is rebuilt with the unlock_theme module
  and "cryptomount -u UUID" becomes "unlock_theme -t ... -u UUID". The
  original image is kept as a backup.
- menu: menu entries in a grub.cfg next to the image run cryptomount. The
  module and a small block that loads it are added to that grub.cfg (and to
  the grub.d templates it was generated from, if any); the module then themes
  every plain cryptomount. Neither the image nor the menu entries change.

The theme is copied next to the image in both cases.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass

import config as grubconfig
import esp
import grubimage
import theme as themes

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

THEMES_DIR = os.path.join(REPO, "themes")
DEFAULT_BUILD = os.path.join(REPO, "build", "grub-efi")
BUILD_SCRIPT = os.path.join(REPO, "scripts", "build-grub.sh")
SYSTEM_MODULE_ROOT = "/usr/lib/grub"
BACKUP_SUFFIX = ".pre-unlock-theme"
MANIFEST = "unlock-theme.json"
# Modules the themed screen needs besides what the image already has.
EXTRA_MODULES = ["unlock_theme", "efi_gop", "png", "jpeg", "tga"]


class InstallError(Exception):
    pass


TEMPLATE_NAME = "05_unlock_theme"


@dataclass
class Candidate:
    esp: str
    rel: str
    image: grubimage.GrubImage
    labels: list
    menu_cfg: str = None        # grub.cfg next to the image that unlocks the disk
    menu_text: str = ""

    @property
    def path(self):
        return os.path.join(self.esp, self.rel)

    @property
    def dir(self):
        return os.path.dirname(self.path)

    @property
    def kind(self):
        if grubconfig.unlock_lines(self.image.config):
            return "embedded"
        if self.menu_cfg:
            return "menu"
        return None

    @property
    def patched(self):
        if self.kind == "menu":
            return grubconfig.has_menu_block(self.menu_text)
        return grubconfig.is_patched(self.image.config)


# Output helpers --------------------------------------------------------------

def say(msg=""):
    print(msg, flush=True)


def ask(prompt, default=None):
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        raise InstallError("aborted") from None
    return answer or (default or "")


def confirm(prompt, args):
    if args.yes:
        return True
    return ask(prompt + " (y/N)").lower() in ("y", "yes")


def choose(prompt, options):
    """OPTIONS: [(key, label)]. Returns the chosen key."""
    for key, label in options:
        say(f"  [{key}] {label}")
    keys = {str(k).lower(): k for k, _ in options}
    while True:
        answer = ask(prompt).lower()
        if answer in keys:
            return keys[answer]
        say("  Please enter one of: " + ", ".join(str(k) for k, _ in options))


# Discovery -------------------------------------------------------------------

def discover(esp_dirs):
    mounts = [os.path.realpath(m) for m in esp.esp_mounts()]
    if not esp_dirs:
        esp_dirs = mounts
        if not esp_dirs:
            raise InstallError("no mounted EFI system partition found; mount it "
                               "(e.g. /boot/efi) or pass --esp DIR")
    entries = esp.boot_entries()
    found = []
    for root in esp_dirs:
        # Boot entries only describe the real ESPs, not test directories.
        root_entries = entries if os.path.realpath(root) in mounts else {}
        try:
            files = esp.efi_files(root)
        except PermissionError:
            raise InstallError(f"cannot read {root}; run with sudo") from None
        for rel in files:
            path = os.path.join(root, rel)
            try:
                img = grubimage.read_image(path)
            except PermissionError:
                raise InstallError(f"cannot read {path}; run with sudo") from None
            if img is None:
                continue
            labels = root_entries.get(rel.replace(os.sep, "/").lower(), [])
            cand = Candidate(esp=root, rel=rel, image=img, labels=labels)
            cfg = os.path.join(cand.dir, "grub.cfg")
            if os.path.isfile(cfg):
                with open(cfg, encoding="utf-8", errors="replace") as f:
                    text = f.read()
                if grubconfig.unlock_lines(text) or grubconfig.has_menu_block(text):
                    cand.menu_cfg, cand.menu_text = cfg, text
            found.append(cand)
    return found


def describe(cand):
    lines = [cand.path]
    if cand.labels:
        lines.append("boot entry: " + ", ".join(cand.labels))
    if cand.kind == "menu":
        unlock = sorted(set(grubconfig.unlock_lines(cand.menu_text)))
        where = f"from its menu ({cand.menu_cfg})"
    else:
        unlock = grubconfig.unlock_lines(cand.image.config)
        where = "itself"
    if cand.patched:
        theme = read_manifest(cand).get("theme", "?")
        lines.append(f"PATCHED, theme: {theme}")
    elif unlock:
        lines.append(f"unlocks {where}: " + "; ".join(unlock))
    else:
        lines.append("does not unlock a disk itself")
    if cand.image.unsupported:
        lines.append("contains: " + ", ".join(cand.image.unsupported) + " (not supported)")
    return lines


def print_candidates(cands):
    for i, cand in enumerate(cands, 1):
        first, *rest = describe(cand)
        say(f"  [{i}] {first}")
        for line in rest:
            say(f"      {line}")


def pick_image(cands, args, purpose):
    if args.grub_location:
        wanted = args.grub_location
        if args.esp and not os.path.isabs(wanted):
            wanted = os.path.join(args.esp[0], wanted)     # relative to the given ESP
        wanted = os.path.realpath(wanted)
        for cand in cands:
            if os.path.realpath(cand.path) == wanted:
                return cand
        raise InstallError(f"{args.grub_location} is not a GRUB EFI image found on the ESP")

    usable = [c for c in cands if c.kind]
    if not usable:
        raise InstallError("no GRUB image that unlocks an encrypted disk was found "
                           "(no cryptomount in its embedded config or its grub.cfg)")
    if args.yes and len(usable) == 1:
        return usable[0]
    if args.yes:
        raise InstallError("several images unlock a disk; choose one with --grub-location")
    say(f"\nGRUB images that unlock a disk ({purpose}):")
    options = [(i, "\n      ".join(describe(c))) for i, c in enumerate(usable, 1)]
    return usable[choose("Image", options) - 1]


# Themes ----------------------------------------------------------------------

def resolve_theme(spec, workdir):
    """SPEC: a bundled theme name, a directory or an archive."""
    if os.path.isdir(os.path.join(THEMES_DIR, spec)) and os.sep not in spec:
        return themes.load(os.path.join(THEMES_DIR, spec), spec)
    if os.path.isdir(spec):
        return themes.load(spec)
    if os.path.isfile(spec) and themes.is_archive(spec):
        d = themes.extract(spec, os.path.join(workdir, "archive"))
        name = os.path.basename(spec)
        for suffix in themes.ARCHIVE_SUFFIXES:
            if name.lower().endswith(suffix):
                name = name[:-len(suffix)]
                break
        return themes.load(d, name)
    raise InstallError(f"theme `{spec}' not found: give a bundled theme name, "
                       "a directory or an archive")


def pick_theme(args, workdir):
    if args.theme:
        return resolve_theme(args.theme, workdir)
    if args.yes:
        raise InstallError("choose a theme with --theme")
    say("\nThemes:")
    options = []
    bundled = themes.bundled(THEMES_DIR)
    for i, (theme, name, error) in enumerate(bundled, 1):
        label = f"{name}: {theme.description}" if theme else f"{name} (broken: {error})"
        options.append((i, label))
    options += [("c", "Custom: a theme directory of your own"),
                ("a", "Archive: a downloaded theme (.tar.gz, .tar.xz, .zip, ...)")]
    while True:
        key = choose("Theme", options)
        try:
            if key == "c":
                return themes.load(os.path.expanduser(ask("Theme directory")))
            if key == "a":
                return resolve_theme(os.path.expanduser(ask("Archive file")), workdir)
            theme, name, error = bundled[key - 1]
            if theme is None:
                raise themes.ThemeError(error)
            return theme
        except (themes.ThemeError, InstallError) as e:
            say(f"  {e}")


def install_theme(theme, image_dir):
    dest = os.path.join(image_dir, grubconfig.THEME_DIR_NAME)
    tmp, old = dest + ".new", dest + ".old"
    for d in (tmp, old):
        shutil.rmtree(d, ignore_errors=True)
    for rel in theme.files:
        target = os.path.join(tmp, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        # FAT has no permissions to copy, so copy the data only.
        shutil.copyfile(os.path.join(theme.dir, rel), target)
    if os.path.exists(dest):
        os.rename(dest, old)
    os.rename(tmp, dest)
    shutil.rmtree(old, ignore_errors=True)


# Building --------------------------------------------------------------------

def ensure_build(build):
    """Runs scripts/build-grub.sh if BUILD has no unlock_theme module yet."""
    def built():
        return (os.path.isfile(os.path.join(build, "grub-core", "unlock_theme.mod"))
                and os.access(os.path.join(build, "grub-mkimage"), os.X_OK))

    if built():
        return
    if build != os.path.abspath(DEFAULT_BUILD):
        raise InstallError(f"the unlock_theme module is not built in {build}")
    say("\nThe unlock_theme module is not built yet; building it now "
        "(a few minutes, the first time it downloads gnulib).")
    cmd = [BUILD_SCRIPT]
    user = os.environ.get("SUDO_USER")
    if os.geteuid() == 0 and user and user != "root":
        # Build as the real user, so the project files don't end up owned by root.
        cmd = ["sudo", "-u", user, "--"] + cmd
    if subprocess.run(cmd).returncode != 0 or not built():
        raise InstallError("building GRUB failed (see above)")
    say("")


def module_source(args, target):
    build = os.path.abspath(args.grub_build)
    ensure_build(build)
    mod = os.path.join(build, "grub-core", "unlock_theme.mod")
    mkimage = os.path.join(build, "grub-mkimage")
    if target != "x86_64-efi":
        raise InstallError(f"only x86_64-efi is supported for now, not {target}")

    if args.modules == "project":
        return grubimage.ModuleSource(mkimage=mkimage, moddir=os.path.join(build, "grub-core"))

    system_dir = os.path.join(SYSTEM_MODULE_ROOT, target)
    system_mkimage = shutil.which("grub-mkimage")
    if not system_mkimage or not os.path.isdir(system_dir):
        raise InstallError(f"system GRUB not found ({system_dir}); install it or use "
                           "--modules project")
    ours, theirs = grubimage.grub_version(mkimage), grubimage.grub_version(system_mkimage)
    if ours != theirs and not args.force:
        raise InstallError(f"the system GRUB is {theirs} but unlock_theme was built from "
                           f"{ours}; rebuild it from GRUB {theirs} (or use --force)")
    with open(os.path.join(build, "grub-core", "moddep.lst")) as f:
        dep = next((l for l in f if l.startswith("unlock_theme:")), "unlock_theme:")
    return grubimage.ModuleSource(mkimage=system_mkimage, moddir=system_dir,
                                  extra_mods={"unlock_theme": (mod, dep)})


def rebuild(cand, new_config, modules, source, args):
    """Builds the new image next to the old one and swaps it in."""
    if args.dry_run:
        out = os.path.join(tempfile.mkdtemp(prefix="unlock-theme-"), "image.efi")
    else:
        out = cand.path + ".new"
    try:
        grubimage.build_image(cand.image, new_config, modules, source, out)
        check = grubimage.read_image(out)
        if check is None or check.config.strip() != new_config.strip():
            raise InstallError("the rebuilt image does not contain the new config")
        if args.dry_run:
            say("(dry run) the new image was built and checked, but not installed")
            return
        os.replace(out, cand.path)
    finally:
        if os.path.exists(out):
            os.unlink(out)
        if args.dry_run:
            shutil.rmtree(os.path.dirname(out), ignore_errors=True)


def secure_boot_plan(cand, args):
    """Returns True if the new image must be signed with sbctl."""
    if os.path.realpath(cand.esp) not in map(os.path.realpath, esp.esp_mounts()):
        return False        # not a live ESP (e.g. a test directory)
    if not esp.secure_boot_enabled():
        return False
    if shutil.which("sbctl"):
        say("\nSecure Boot is enabled: the rebuilt image must be signed to boot.")
        if confirm("Sign it with sbctl after installing?", args):
            return True
    if not args.force:
        raise InstallError("Secure Boot is enabled and the rebuilt image would not be "
                           "signed, so it would not boot. Install and set up sbctl, or "
                           "use --force if you sign it yourself.")
    say("WARNING: the image is not signed; sign it before rebooting.")
    return False


def sign(path):
    res = subprocess.run(["sbctl", "sign", "-s", path], capture_output=True, text=True)
    if res.returncode != 0:
        raise InstallError("sbctl sign failed: " + (res.stderr or res.stdout).strip())
    say("Signed with sbctl (it will be re-signed on updates).")


# Manifest --------------------------------------------------------------------

def manifest_path(cand):
    return os.path.join(os.path.dirname(cand.path), MANIFEST)


def read_manifest(cand):
    try:
        with open(manifest_path(cand)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_manifest(cand, data):
    with open(manifest_path(cand), "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


# Commands --------------------------------------------------------------------

def check_writable(cand, args):
    if args.dry_run:
        return
    d = os.path.dirname(cand.path)
    if not os.access(d, os.W_OK):
        raise InstallError(f"cannot write to {d}; run with sudo")


def cmd_list(args):
    cands = discover(args.esp)
    if not cands:
        say("No GRUB EFI images found.")
        return
    say("GRUB EFI images:")
    print_candidates(cands)


def menu_template_path(cand):
    """grub.d/05_unlock_theme if the menu was generated from templates."""
    d = grubconfig.templates_dir(cand.menu_text)
    return os.path.join(d, TEMPLATE_NAME) if d and os.path.isdir(d) else None


def install_menu(cand, theme, args):
    template = menu_template_path(cand)
    if not args.dry_run and template and not os.access(os.path.dirname(template), os.W_OK):
        raise InstallError(f"cannot write to {os.path.dirname(template)}; run with sudo")
    source = module_source(args, cand.image.target)     # builds and checks versions
    module = source.extra_mods.get("unlock_theme", (None,))[0] or \
        os.path.join(source.moddir, grubconfig.MODULE_FILE)

    say(f"\nImage:  {cand.path} (unchanged)")
    say(f"Menu:   {cand.menu_cfg}")
    say(f"Theme:  {theme.name} ({theme.dir})")
    say(f"Module: {os.path.join(cand.dir, grubconfig.MODULE_FILE)}")
    if template:
        say(f"Template: {template} (keeps the block when the menu is regenerated)")
    if cand.patched:
        say("The menu already loads the unlock screen; the theme and module are replaced.")
    else:
        say("These menu lines will show the themed screen (they stay as they are):")
        for line in sorted(set(grubconfig.unlock_lines(cand.menu_text))):
            say(f"  {line}")
        say("Added near the top of the menu:")
        for line in grubconfig.MENU_BLOCK.splitlines():
            say(f"  + {line}")
    if not confirm("\nProceed?", args):
        raise InstallError("aborted")
    if args.dry_run:
        say("(dry run) nothing written")
        return

    shutil.copyfile(module, os.path.join(cand.dir, grubconfig.MODULE_FILE))
    install_theme(theme, cand.dir)
    if not cand.patched:
        backup = cand.menu_cfg + BACKUP_SUFFIX
        if not os.path.exists(backup):
            shutil.copyfile(cand.menu_cfg, backup)
        tmp = cand.menu_cfg + ".new"
        with open(tmp, "w") as f:
            f.write(grubconfig.add_menu_block(cand.menu_text))
        os.replace(tmp, cand.menu_cfg)
    if template:
        with open(template, "w") as f:
            f.write(grubconfig.menu_template())
        os.chmod(template, 0o755)

    manifest = read_manifest(cand)
    manifest.update({"kind": "menu", "config": os.path.basename(cand.menu_cfg),
                     "theme": theme.name, "template": template})
    write_manifest(cand, manifest)


def uninstall_menu(cand, args):
    template = read_manifest(cand).get("template") or menu_template_path(cand)
    say(f"Remove the unlock screen block from {cand.menu_cfg}")
    if template and os.path.exists(template):
        say(f"Remove {template}")
    if not confirm("Proceed?", args):
        raise InstallError("aborted")
    if args.dry_run:
        say("(dry run) nothing changed")
        return
    tmp = cand.menu_cfg + ".new"
    with open(tmp, "w") as f:
        f.write(grubconfig.remove_menu_block(cand.menu_text))
    os.replace(tmp, cand.menu_cfg)
    for path in (template, cand.menu_cfg + BACKUP_SUFFIX,
                 os.path.join(cand.dir, grubconfig.MODULE_FILE), manifest_path(cand)):
        if path and os.path.exists(path):
            os.unlink(path)
    shutil.rmtree(os.path.join(cand.dir, grubconfig.THEME_DIR_NAME), ignore_errors=True)
    say("Uninstalled.")


def cmd_install(args, cand=None):
    if cand is None:
        cand = pick_image(discover(args.esp), args, "the one that asks for the passphrase")
    img = cand.image
    if img.unsupported and cand.kind == "embedded":
        raise InstallError("this image contains " + ", ".join(img.unsupported) +
                           ", which the installer cannot carry over")
    check_writable(cand, args)

    with tempfile.TemporaryDirectory(prefix="unlock-theme-") as work:
        theme = pick_theme(args, work)
        for w in theme.warnings:
            say(f"Warning: {w}")

        if cand.kind == "menu":
            install_menu(cand, theme, args)
            if not args.dry_run:
                say(f"\nInstalled. The theme is in "
                    f"{os.path.join(cand.dir, grubconfig.THEME_DIR_NAME)}.")
                say("If the theme ever fails to load, GRUB falls back to the normal "
                    "passphrase prompt.")
            return

        if cand.patched:
            new_config, changes, skipped = img.config, [], []
        else:
            new_config, changes, skipped = grubconfig.patch(img.config)
            if not changes:
                raise InstallError("no cryptomount line in this image can be patched: " +
                                   "; ".join(skipped))
        source = module_source(args, img.target) if changes else None
        if source:
            missing = source.missing(img.modules + EXTRA_MODULES)
            if missing:
                raise InstallError("modules of this image are missing from the GRUB "
                                   "build: " + ", ".join(missing) +
                                   " (try --modules project or --modules system)")

        say(f"\nImage:  {cand.path}")
        say(f"Theme:  {theme.name} ({theme.dir})")
        if changes:
            mode = "system GRUB modules + unlock_theme" if args.modules == "system" \
                else "this project's GRUB build"
            say(f"Build:  {mode}")
            say(f"Backup: {cand.path + BACKUP_SUFFIX}")
            say("Config changes:")
            for old, new in changes:
                say(f"  - {old.strip()}")
                say(f"  + {new.strip()}")
            for line in skipped:
                say(f"  (left unchanged: {line.strip()})")
        else:
            say("The image is already patched; only the theme is replaced.")
        need_sign = secure_boot_plan(cand, args) if changes else False
        if not confirm("\nProceed?", args):
            raise InstallError("aborted")

        if changes:
            backup = cand.path + BACKUP_SUFFIX
            if not args.dry_run and not os.path.exists(backup):
                shutil.copyfile(cand.path, backup)
            rebuild(cand, new_config, img.modules + EXTRA_MODULES, source, args)
        if args.dry_run:
            say("(dry run) theme and manifest not written")
            return
        install_theme(theme, os.path.dirname(cand.path))
        if need_sign:
            sign(cand.path)

        manifest = read_manifest(cand)
        manifest.update({
            "image": os.path.basename(cand.path),
            "backup": os.path.basename(cand.path) + BACKUP_SUFFIX,
            "theme": theme.name,
            "patched_sha256": grubimage.sha256(cand.path),
        })
        if changes:
            manifest["modules"] = args.modules
        write_manifest(cand, manifest)

    say(f"\nInstalled. The theme is in "
        f"{os.path.join(os.path.dirname(cand.path), grubconfig.THEME_DIR_NAME)}.")
    say("If the theme ever fails to load, GRUB falls back to the normal passphrase prompt.")
    say("Note: reinstalling GRUB (grub-install) replaces the patched image; run this "
        "installer again afterwards.")


def cmd_uninstall(args, cand=None):
    if cand is None:
        cands = [c for c in discover(args.esp) if c.patched
                 or os.path.exists(manifest_path(c))]
        if not cands:
            raise InstallError("no patched GRUB image found")
        if args.grub_location:
            cand = pick_image(cands, args, "to restore")
        elif len(cands) == 1 or args.yes:
            cand = cands[0]
        else:
            say("\nPatched images:")
            cand = cands[choose("Image", [(i, c.path) for i, c in enumerate(cands, 1)]) - 1]
    check_writable(cand, args)
    if cand.kind == "menu":
        uninstall_menu(cand, args)
        return

    manifest = read_manifest(cand)
    backup = cand.path + BACKUP_SUFFIX
    if cand.patched:
        current = grubimage.sha256(cand.path)
        if os.path.exists(backup) and manifest.get("patched_sha256") in (None, current):
            say(f"Restore {backup} -> {cand.path}")
            action = "restore"
        else:
            say("No matching backup; the image will be rebuilt with cryptomount instead "
                "of unlock_theme.")
            action = "rebuild"
    else:
        say(f"{cand.path} is not patched (GRUB was probably reinstalled); only the "
            "theme files are removed.")
        action = None
    if not confirm("Proceed?", args):
        raise InstallError("aborted")
    if args.dry_run:
        say("(dry run) nothing changed")
        return

    if action == "restore":
        tmp = cand.path + ".new"
        shutil.copyfile(backup, tmp)
        os.replace(tmp, cand.path)
        os.unlink(backup)
    elif action == "rebuild":
        new_config, _ = grubconfig.unpatch(cand.image.config)
        source = module_source(args, cand.image.target)
        modules = [m for m in cand.image.modules if m != "unlock_theme"]
        rebuild(cand, new_config, modules, source, args)

    d = os.path.dirname(cand.path)
    shutil.rmtree(os.path.join(d, grubconfig.THEME_DIR_NAME), ignore_errors=True)
    if os.path.exists(manifest_path(cand)):
        os.unlink(manifest_path(cand))
    say("Uninstalled.")
    if action == "restore" and esp.secure_boot_enabled():
        say("Secure Boot is enabled: make sure the restored image is signed "
            "(e.g. sbctl sign -s).")


def common_args(parser):
    parser.add_argument("--grub-location", metavar="PATH",
                        help="the GRUB EFI image, e.g. /boot/efi/EFI/cachyos-menu/grubx64.efi "
                             "(default: ask, or the only candidate with --yes)")
    parser.add_argument("--esp", action="append", metavar="DIR",
                        help="ESP directory to search (default: all mounted FAT file systems)")
    parser.add_argument("--modules", choices=["system", "project"], default="system",
                        help="for images that are rebuilt: use the system GRUB modules plus "
                             "unlock_theme (default) or only this project's GRUB build")
    parser.add_argument("--grub-build", default=DEFAULT_BUILD, metavar="DIR",
                        help="GRUB build directory with unlock_theme.mod")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="don't ask for confirmation (for scripts)")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="check everything, but change nothing")
    parser.add_argument("--force", action="store_true",
                        help="ignore GRUB version and Secure Boot checks")


def run(func, args):
    try:
        func(args)
    except (InstallError, themes.ThemeError, grubimage.ImageError) as e:
        sys.exit(f"error: {e}")
    except KeyboardInterrupt:
        sys.exit("\naborted")


def main_install():
    parser = argparse.ArgumentParser(
        description="Install the themed unlock screen, or change its theme. "
                    "Options that are not given are asked for.")
    parser.add_argument("--theme", metavar="NAME|DIR|ARCHIVE",
                        help="a bundled theme (" + ", ".join(
                            name for _, name, _ in themes.bundled(THEMES_DIR)) +
                        "), a theme directory, or a theme archive")
    parser.add_argument("--list", action="store_true",
                        help="only list the GRUB images found and exit")
    common_args(parser)
    args = parser.parse_args()

    def install(args):
        if args.list:
            cmd_list(args)
            return
        cand = pick_image(discover(args.esp), args, "the one that asks for the passphrase")
        if cand.patched:
            say(f"\n{cand.path} already has the unlock screen; installing the chosen theme.")
        cmd_install(args, cand)

    run(install, args)


def main_uninstall():
    parser = argparse.ArgumentParser(
        description="Remove the themed unlock screen and restore the original GRUB setup. "
                    "Options that are not given are asked for.")
    common_args(parser)
    run(cmd_uninstall, parser.parse_args())
