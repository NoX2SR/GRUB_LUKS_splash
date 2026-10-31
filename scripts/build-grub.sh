#!/usr/bin/env bash
# Build GRUB (x86_64-efi) with the unlock_theme module into build/grub-efi.
# Run as your normal user, not root. install.py runs it when needed.
#
# grub/ is a checkout of upstream GRUB (downloaded here if missing); the module
# source lives in module/ and is linked into it.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/grub"
BUILD="$ROOT/build/grub-efi"
JOBS="$(nproc)"
GRUB_GIT=https://gitlab.freedesktop.org/gnu-grub/grub.git
GRUB_TAG=grub-2.16

# step NAME LOG COMMAND...: run quietly, show the end of the log on failure.
step() {
    local name="$1" log="$2"
    shift 2
    echo "== $name"
    if ! "$@" > "$log" 2>&1; then
        tail -n 25 "$log" >&2
        echo "error: $name failed, full log: $log" >&2
        exit 1
    fi
}

# retry COMMAND...: automake 1.19 with perl 5.44 crashes at random (signal 11)
# while generating GRUB's makefiles; another run usually succeeds.
retry() {
    local i
    for i in 1 2 3 4 5; do
        "$@" && return 0
        echo "(attempt $i failed, retrying)"
    done
    return 1
}

mkdir -p "$BUILD"
if [[ ! -d "$SRC" ]]; then
    step "download GRUB ($GRUB_TAG)" "$BUILD/clone.log" \
        git clone --depth 1 --branch "$GRUB_TAG" "$GRUB_GIT" "$SRC"
fi

cd "$SRC"
# Bootstrap the upstream tree as is, then add the module.
if [[ ! -x configure ]]; then
    step "bootstrap (downloads gnulib)" "$BUILD/bootstrap.log" retry ./bootstrap
fi

# Link the module in and register it with GRUB's build.
ln -sfn ../../../module/unlock_theme.c grub-core/commands/unlock_theme.c
if ! grep -q "name = unlock_theme;" grub-core/Makefile.core.def; then
    cat >> grub-core/Makefile.core.def <<'EOF'

module = {
  name = unlock_theme;
  common = commands/unlock_theme.c;
};
EOF
fi
if [[ grub-core/Makefile.core.def -nt grub-core/Makefile.core.am ]]; then
    step "autogen (registers unlock_theme)" "$BUILD/autogen.log" retry ./autogen.sh
fi

cd "$BUILD"
if [[ ! -f Makefile || "$SRC/configure" -nt Makefile ]]; then
    step configure configure.log "$SRC/configure" --with-platform=efi --target=x86_64 \
        --enable-grub-mkfont --disable-werror
fi
step "build gnulib" make-gnulib.log /usr/bin/make -C grub-core/lib/gnulib -j"$JOBS"
step "build modules" make-core.log /usr/bin/make -C grub-core -j"$JOBS"
step "build tools" make-tools.log /usr/bin/make -j"$JOBS" grub-mkimage grub-mkfont
echo "Built $BUILD/grub-core/unlock_theme.mod ($("$BUILD/grub-mkimage" --version))"
