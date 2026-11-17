#!/bin/sh
set -eu

REPO_DIR=""

if [ -f "$0" ]; then
    REPO_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
fi

QUIP_DIR="$HOME/.quip"
BIN_DIR="$QUIP_DIR/bin"
LOCAL_BINARY="${QUIPC_INSTALL_BINARY:-$REPO_DIR/target/release/quipc}"

TEMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/quip-install.XXXXXX")
INSTALL_LOCK=""
trap 'rm -rf "$TEMP_DIR"; if [ -n "$INSTALL_LOCK" ]; then rm -rf "$INSTALL_LOCK"; fi' EXIT
trap 'exit 1' HUP INT TERM

if ! command -v clang >/dev/null 2>&1 ||
    ! printf 'int main(void) { return 0; }\n' |
        clang -x c - -o "$TEMP_DIR/clang-check"; then
    echo "Error: a working clang is required to link Quip programs." >&2
    echo "Install clang with your package manager, or install Xcode Command Line Tools on macOS (xcode-select --install), then rerun this script." >&2
    exit 1
fi

if [ -n "$REPO_DIR" ] && [ -f "$LOCAL_BINARY" ] && [ -f "$REPO_DIR/library/runtime/pkg.quip" ] && [ -d "$REPO_DIR/library/std" ]; then
    cp "$LOCAL_BINARY" "$TEMP_DIR/quipc"
    LIBRARY_DIR="$REPO_DIR/library"

else
    OS=$(uname -s | tr '[:upper:]' '[:lower:]')
    case "$OS" in linux|darwin) ;; *) echo "Unsupported OS: $OS" >&2; exit 1 ;; esac
    ARCH=$(uname -m)

    case "$ARCH" in
        x86_64) ARCH=amd64 ;;
        aarch64|arm64) ARCH=arm64 ;;
        *) echo "Unsupported architecture: $ARCH" >&2; exit 1 ;;
    esac

    command -v curl >/dev/null 2>&1 || { echo "Error: curl is required to download the compiler." >&2; exit 1; }
    RELEASE_URL=$(curl -fsSL -o /dev/null -w '%{url_effective}' https://github.com/quiplang/quipc/releases/latest)

    case "$RELEASE_URL" in
        https://github.com/quiplang/quipc/releases/tag/*) TAG=${RELEASE_URL##*/} ;;
        *) echo "Error: could not determine the latest release tag." >&2; exit 1 ;;
    esac

    case "$TAG" in ''|*[!a-zA-Z0-9._-]*) echo "Error: invalid release tag: $TAG" >&2; exit 1 ;; esac

    echo "Downloading compiler and library for $TAG ($OS/$ARCH)..."
    curl -fsSL "https://github.com/quiplang/quipc/releases/download/$TAG/quipc-${OS}-${ARCH}.tar.gz" -o "$TEMP_DIR/quipc.tar.gz"
    curl -fsSL "https://github.com/quiplang/quipc/archive/refs/tags/$TAG.tar.gz" -o "$TEMP_DIR/source.tar.gz"

    mkdir "$TEMP_DIR/archive" "$TEMP_DIR/source"
    tar -xzf "$TEMP_DIR/quipc.tar.gz" -C "$TEMP_DIR/archive" --strip-components=1
    tar -xzf "$TEMP_DIR/source.tar.gz" -C "$TEMP_DIR/source" --strip-components=1

    if [ ! -f "$TEMP_DIR/archive/quipc" ] || [ ! -f "$TEMP_DIR/source/library/runtime/pkg.quip" ] || [ ! -d "$TEMP_DIR/source/library/std" ]; then
        echo "Error: compiler or library not found in release archives." >&2
        exit 1
    fi

    cp "$TEMP_DIR/archive/quipc" "$TEMP_DIR/quipc"
    LIBRARY_DIR="$TEMP_DIR/source/library"
fi

chmod +x "$TEMP_DIR/quipc"

VERSION=$("$TEMP_DIR/quipc" --version | sed -n 's/^Quip //p')
case "$VERSION" in ''|[!0-9]*|*[!a-zA-Z0-9._-]*) echo "Error: could not determine a safe compiler version." >&2; exit 1 ;; esac
mkdir -p "$BIN_DIR"
if ! mkdir "$QUIP_DIR/.install-lock" 2>/dev/null; then
    echo "Error: could not acquire the install lock at $QUIP_DIR/.install-lock." >&2
    exit 1
fi
INSTALL_LOCK="$QUIP_DIR/.install-lock"
DEST="$QUIP_DIR/$VERSION"
STAGE="$INSTALL_LOCK/version"
if [ -e "$QUIP_DIR/current" ] && [ ! -L "$QUIP_DIR/current" ]; then
    echo "Error: $QUIP_DIR/current already exists and is not a symbolic link." >&2
    exit 1
fi
mkdir -p "$STAGE/bin" "$STAGE/lib"
cp "$TEMP_DIR/quipc" "$STAGE/bin/quipc"
cp -R "$LIBRARY_DIR/." "$STAGE/lib/"
if [ -e "$DEST" ] || [ -L "$DEST" ]; then
    if [ -L "$DEST" ] || ! cmp -s "$STAGE/bin/quipc" "$DEST/bin/quipc" || ! diff -qr "$STAGE/lib" "$DEST/lib" >/dev/null 2>&1; then
        echo "Error: $DEST already contains a different build; it was not overwritten." >&2
        exit 1
    fi
else
    mv "$STAGE" "$DEST"
fi

# Preserve a pre-versioned installation before replacing its entry points.
if [ ! -L "$BIN_DIR/quipc" ] && [ -f "$BIN_DIR/quipc" ] && ! grep -q '^# Quip version launcher$' "$BIN_DIR/quipc" 2>/dev/null; then
    LEGACY=$(mktemp -d "$QUIP_DIR/legacy.XXXXXX")
    mkdir -p "$LEGACY/bin" "$LEGACY/lib"
    cp -p "$BIN_DIR/quipc" "$LEGACY/bin/quipc"
    if [ -d "$QUIP_DIR/lib" ]; then cp -R "$QUIP_DIR/lib/." "$LEGACY/lib/"; fi
    echo "Preserved the previous installation in $LEGACY."
fi
if [ -e "$QUIP_DIR/lib" ] && [ ! -L "$QUIP_DIR/lib" ]; then
    mv "$QUIP_DIR/lib" "$QUIP_DIR/legacy-lib.$(date +%Y%m%d%H%M%S).$$"
fi
OS_NAME=$(uname -s)
if [ "$OS_NAME" = Darwin ]; then
    xattr -d com.apple.quarantine "$DEST/bin/quipc" 2>/dev/null || true
fi

ln -s "$VERSION" "$INSTALL_LOCK/current"
if [ "$OS_NAME" = Darwin ]; then
    mv -fh "$INSTALL_LOCK/current" "$QUIP_DIR/current"
else
    mv -fT "$INSTALL_LOCK/current" "$QUIP_DIR/current"
fi
ln -sfn ../current/bin/quipc "$BIN_DIR/quipc"
ln -sfn current/lib "$QUIP_DIR/lib"
echo "Installed Quip $VERSION at $DEST."

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo "Add Quip to your PATH (for example, in ~/.profile or ~/.zshrc):"
       echo '    export PATH="$HOME/.quip/bin:$PATH"' ;;
esac
