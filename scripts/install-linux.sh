#!/usr/bin/env bash
# awa installer for macOS and Linux: installs awa from the local awa folder it sits in, for
# your user account (no administrator rights), or updates an existing installation, puts an
# "awa" start shortcut on the desktop and in the applications menu, and opens awa.
#
#   macOS: double-click install-macos.command (or run: bash install-macos.command)
#   Linux: bash install-linux.sh
#
# install-macos.command and install-linux.sh are the same script. Run it with --help for
# the options.

set -euo pipefail

PYTHON_VERSION="3.13"
UV_VERSION="0.11.3"  # the uv release that is downloaded when none is installed (--uv-version)
case "$(uname -s)" in
    Darwin)
        SYSTEM=macos
        AWA_HOME="$HOME/Library/Application Support/awa"
        PROJECT="$HOME/Documents/awa"
        ;;
    *)
        SYSTEM=linux
        AWA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}/awa"
        PROJECT="$(xdg-user-dir DOCUMENTS 2>/dev/null || printf '%s' "$HOME")/awa"
        ;;
esac
SOURCE=""
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
SHORTCUT_DIR=""
UV_PATH="${AWA_UV:-}"
UV_SHA256=""
LATEST=0
YES=0
SHORTCUTS=1
LAUNCH=1
UNINSTALL=0
MARKER=".awa-installer"  # in the home folder of every installation this script made

usage() {
    cat <<'EOF'
awa installer for macOS and Linux (installs or updates awa for your user, then opens it)

awa itself comes from this local copy: the awa folder this script sits in (the
folder above scripts/), or else an awa_agents-*.whl file next to the script; awa is
never downloaded. awa's open-source dependencies are installed exactly as locked in
uv.lock of the awa folder, each checked against its SHA-256 hash (from a wheel there is no
lock: the newest versions awa's ranges allow, unverified). uv, Python and the dependencies
are downloaded from their usual sources, or from your company mirror when UV_INDEX_URL is set.

uv: an uv that is installed already is used (or the one named with --uv). Otherwise uv
UV_VERSION_HERE is downloaded as the install script https://astral.sh/uv/UV_VERSION_HERE/install.sh
from astral.sh over TLS; that script checks the uv archive it fetches against a checksum it
contains. To verify the script too, take its SHA-256 on a trusted machine
(curl -LsSf <that URL> | sha256sum) and pass it with --uv-sha256: nothing runs on a mismatch.
Python is fetched by uv, which verifies it against hashes it knows.

  --yes                 do not ask before installing or uninstalling
  --source PATH         install this awa wheel file or awa folder instead
  --uv PATH             use this uv program (also AWA_UV); nothing of uv is downloaded
  --uv-version V        the uv release to download when none is installed (default UV_VERSION_HERE)
  --uv-sha256 HASH      the SHA-256 the downloaded uv install script must have
  --latest              install the newest dependency versions awa's ranges allow, not the locked ones
  --home DIR            where awa is installed, a folder of its own
                        (macOS: ~/Library/Application Support/awa, Linux: ~/.local/share/awa)
  --project DIR         the folder awa opens (default: Documents/awa)
  --shortcut-dir DIR    put the start shortcut here (default: desktop and applications menu)
  --no-shortcuts        do not create start shortcuts
  --no-launch           do not open awa at the end
  --uninstall           remove awa and its shortcuts (your project folder stays); only
                        a home folder this installer made is touched, and only what it put there
EOF
}

step() { printf '\n==> %s\n' "$1"; }
fail() { printf '\nThe installation did not finish: %s\n' "$1" >&2; exit 1; }
confirm() {
    if [ "$YES" = 1 ] || [ ! -t 0 ]; then return 0; fi
    printf '%s [Y/n] ' "$1"
    read -r answer || answer=""
    case "$answer" in n|N|no|NO|No) return 1 ;; esac
}
sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
    elif command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | cut -d' ' -f1
    else fail "neither sha256sum nor shasum is available to check the uv install script"; fi
}
absolute() { (cd "$(dirname "$1")" && dir="$(pwd)" && printf '%s/%s' "${dir%/}" "$(basename "$1")"); }
from_here() { case "$1" in /* | [A-Za-z]:/*) printf '%s' "$1" ;; *) printf '%s/%s' "${PWD%/}" "$1" ;; esac; }
# A word for a .desktop Exec line: in double quotes, with " ` $ \ escaped and % doubled.
desktop_quote() {
    local text="$1" quoted="" char
    while [ -n "$text" ]; do
        char="${text:0:1}"
        text="${text:1}"
        case "$char" in
            '\') quoted+='\\\\' ;;
            '"' | '`' | '$') quoted+="\\$char" ;;
            '%') quoted+='%%' ;;
            *) quoted+="$char" ;;
        esac
    done
    printf '"%s"' "$quoted"
}
# The start shortcut: a script on macOS, a desktop entry on Linux (paths of any spelling).
write_shortcut() {
    if [ "$SYSTEM" = macos ]; then
        printf '#!/bin/bash\nexec %q ui --project %q\n' "$AWA" "$PROJECT" > "$1"
    else
        printf '[Desktop Entry]\nType=Application\nName=awa\nComment=%s\nExec=%s ui --project %s\nTerminal=true\nCategories=Development;Office;\n' \
            "Turn a process description into a tested system" "$(desktop_quote "$AWA")" "$(desktop_quote "$PROJECT")" > "$1"
    fi
}

while [ $# -gt 0 ]; do
    case "$1" in
        --source|--home|--project|--shortcut-dir|--uv|--uv-version|--uv-sha256)
            [ $# -ge 2 ] || fail "$1 needs a value"
            case "$1" in
                --uv) UV_PATH="$2" ;;
                --uv-version) UV_VERSION="$2" ;;
                --uv-sha256) UV_SHA256="$(printf '%s' "$2" | tr 'A-F' 'a-f')" ;;
                --source) SOURCE="$2" ;;
                --home) AWA_HOME="$2" ;;
                --project) PROJECT="$2" ;;
                --shortcut-dir) SHORTCUT_DIR="$2" ;;
            esac
            shift ;;
        --yes) YES=1 ;;
        --latest) LATEST=1 ;;
        --no-shortcuts) SHORTCUTS=0 ;;
        --no-launch) LAUNCH=0 ;;
        --uninstall) UNINSTALL=1 ;;
        -h|--help) usage | sed "s/UV_VERSION_HERE/$UV_VERSION/g"; exit 0 ;;
        *) fail "unknown option '$1' (see --help)" ;;
    esac
    shift
done

AWA_HOME="${AWA_HOME%/}"
[ -n "$AWA_HOME" ] || fail "--home must not be the root folder"
AWA_HOME="$(from_here "$AWA_HOME")"
PROJECT="$(from_here "$PROJECT")"
[ -z "$SHORTCUT_DIR" ] || SHORTCUT_DIR="$(from_here "$SHORTCUT_DIR")"
resolved="$(cd "$AWA_HOME" 2>/dev/null && pwd -P || printf '%s' "$AWA_HOME")"
if [ "$resolved" = "$(cd "$HOME" 2>/dev/null && pwd -P || printf '%s' "$HOME")" ]; then
    fail "--home must be a folder of its own, not your home folder"
fi
case "${PROJECT%/}/" in
    "$AWA_HOME"/*) fail "--home must not contain your project folder $PROJECT" ;;
esac
ENV_DIR="$AWA_HOME/env"
AWA="$ENV_DIR/bin/awa"
if [ -n "$SHORTCUT_DIR" ]; then
    SHORTCUT_DIRS=("$SHORTCUT_DIR")
elif [ "$SYSTEM" = macos ]; then
    SHORTCUT_DIRS=("$HOME/Desktop" "$HOME/Applications")
else
    SHORTCUT_DIRS=("${XDG_DATA_HOME:-$HOME/.local/share}/applications")
    [ -d "$HOME/Desktop" ] && SHORTCUT_DIRS+=("$HOME/Desktop")
fi
if [ "$SYSTEM" = macos ]; then SHORTCUT_NAME="awa.command"; else SHORTCUT_NAME="awa.desktop"; fi

# ------------------------------------------------------------------ uninstall

if [ "$UNINSTALL" = 1 ]; then
    [ -e "$AWA_HOME" ] || { echo "awa is not installed in $AWA_HOME; nothing to remove."; exit 0; }
    [ -f "$AWA_HOME/$MARKER" ] || fail "$AWA_HOME was not made by this installer (it has no $MARKER file), so nothing was removed. Check --home; to remove the folder anyway, delete it yourself."
    [ "$YES" = 1 ] || [ -t 0 ] || fail "uninstalling asks first, and no terminal is there to ask: pass --yes to confirm."
    confirm "Remove awa from $AWA_HOME and its shortcuts? Your project folder stays." || exit 0
    for dir in "${SHORTCUT_DIRS[@]}"; do rm -f "$dir/$SHORTCUT_NAME"; done
    for part in env python uv "$MARKER"; do rm -rf "${AWA_HOME:?}/$part"; done
    rmdir "$AWA_HOME" 2>/dev/null || true  # other files in it stay
    echo "awa is removed. Your projects in $PROJECT are still there."
    exit 0
fi

# awa comes from this local copy: --source, else the awa folder around scripts/, else a wheel
# next to this script.
if [ -z "$SOURCE" ]; then
    root="$(dirname "$SCRIPT_DIR")"
    wheel="$(ls -t "$SCRIPT_DIR"/awa_agents-*.whl 2>/dev/null | head -n 1 || true)"
    if [ -f "$root/pyproject.toml" ] && grep -q '^name = "awa-agents"' "$root/pyproject.toml"; then
        SOURCE="$root"
    elif [ -n "$wheel" ]; then
        SOURCE="$wheel"
    else
        fail "no local awa found. Start the installer from the scripts folder of your awa folder, put an awa_agents-*.whl file next to it, or pass --source PATH."
    fi
fi
[ -e "$SOURCE" ] || fail "--source '$SOURCE' does not exist"
SOURCE="$(absolute "$SOURCE")"

echo "awa installer"
echo "  awa comes from:    $SOURCE"
echo "  awa goes to:       $AWA_HOME   (only for your user; no administrator rights)"
echo "  your projects:     $PROJECT"
confirm "Install (or update) awa now?" || exit 0
if [ -d "$AWA_HOME" ] && [ ! -f "$AWA_HOME/$MARKER" ] && [ -n "$(ls -A "$AWA_HOME")" ]; then
    fail "$AWA_HOME already holds files this installer did not make (it has no $MARKER file), so nothing was changed. Choose a folder of its own with --home."
fi
mkdir -p "$AWA_HOME"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
printf 'made by the awa installer; --uninstall removes only env, python and uv here\n' > "$AWA_HOME/$MARKER"

# ------------------------------------------------------------------ 1. uv

step "Looking for uv (the tool that installs Python and awa)"
UV=""
if [ -n "$UV_PATH" ]; then
    [ -x "$UV_PATH" ] || fail "--uv '$UV_PATH' is not an executable file"
    UV="$UV_PATH"
else
    for candidate in "$(command -v uv 2>/dev/null || true)" "$AWA_HOME/uv/uv" "$HOME/.local/bin/uv"; do
        if [ -n "$candidate" ] && [ -x "$candidate" ]; then UV="$candidate"; break; fi
    done
fi
if [ -z "$UV" ]; then
    url="https://astral.sh/uv/$UV_VERSION/install.sh"
    echo "    Downloading uv $UV_VERSION: $url"
    if command -v curl >/dev/null 2>&1; then
        download=(curl -LsSf -o "$WORK/uv-install.sh" "$url")
    elif command -v wget >/dev/null 2>&1; then
        download=(wget -qO "$WORK/uv-install.sh" "$url")
    else
        fail "neither curl nor wget is available to download uv; install one of them and run the installer again"
    fi
    "${download[@]}" \
        || fail "uv could not be downloaded. Check the internet connection (behind a company proxy, set HTTPS_PROXY) and run the installer again."
    if [ -n "$UV_SHA256" ]; then
        actual="$(sha256_of "$WORK/uv-install.sh")"
        [ "$actual" = "$UV_SHA256" ] || fail "the downloaded uv install script has the SHA-256 $actual, not the $UV_SHA256 given with --uv-sha256. Nothing was run."
        echo "    The uv install script matches --uv-sha256."
    else
        echo "    The uv install script is not verified (pass --uv-sha256 to check it; see --help)."
    fi
    env UV_INSTALL_DIR="$AWA_HOME/uv" UV_NO_MODIFY_PATH=1 INSTALLER_NO_MODIFY_PATH=1 sh "$WORK/uv-install.sh" \
        || fail "uv could not be installed (see above). Check the internet connection and run the installer again."
    UV="$AWA_HOME/uv/uv"
    [ -x "$UV" ] || fail "uv was downloaded but is missing from $AWA_HOME/uv"
fi
echo "    uv: $UV"

# ------------------------------------------------------------------ 2. environment

step "Preparing a private Python environment"
export UV_PYTHON_INSTALL_DIR="$AWA_HOME/python"
export UV_PYTHON_INSTALL_BIN=0
if [ -e "$ENV_DIR" ] && ! "$ENV_DIR/bin/python" -c "import sys" >/dev/null 2>&1; then
    echo "    The existing environment does not start; creating it again."
    rm -rf "$ENV_DIR"
fi
if [ ! -x "$ENV_DIR/bin/python" ]; then
    "$UV" venv "$ENV_DIR" --python "$PYTHON_VERSION" --python-preference only-managed \
        || fail "Python $PYTHON_VERSION could not be set up (see above). Check the internet connection and try again."
fi

# ------------------------------------------------------------------ 3. awa

# The reinstall makes an update pick up changed files that keep the same version number.
# From the awa folder the dependencies are the ones locked in its uv.lock, each checked by hash.
if [ "$LATEST" = 0 ] && [ -f "$SOURCE/uv.lock" ]; then
    step "Installing the locked dependencies of awa (checked by hash)"
    "$UV" export --frozen --no-dev --no-emit-project --no-header --extra all --project "$SOURCE" \
        --format requirements-txt -o "$WORK/locked.txt" \
        || fail "the dependency lock of $SOURCE could not be read (see above)."
    "$UV" pip install --python "$ENV_DIR/bin/python" --require-hashes -r "$WORK/locked.txt" \
        || fail "installing the dependencies failed (see above). Check the internet connection and run the installer again."
    step "Installing $SOURCE"
    "$UV" pip install --python "$ENV_DIR/bin/python" --no-deps --reinstall-package awa-agents "$SOURCE" \
        || fail "installing awa failed (see above)."
else
    [ "$LATEST" = 1 ] || echo "    Note: $SOURCE has no uv.lock, so the dependencies are the newest versions awa's ranges allow, not checked by hash. Install from the awa folder for locked ones."
    step "Installing $SOURCE[all]"
    "$UV" pip install --python "$ENV_DIR/bin/python" --upgrade --reinstall-package awa-agents "$SOURCE[all]" \
        || fail "installing awa failed (see above). Check the internet connection and run the installer again."
fi
version="$("$AWA" --version)" || fail "awa was installed but does not start ($AWA)"
echo "    $version"

# ------------------------------------------------------------------ 4. shortcuts

mkdir -p "$PROJECT"
if [ "$SHORTCUTS" = 1 ]; then
    step "Creating the 'awa' shortcut"
    for dir in "${SHORTCUT_DIRS[@]}"; do
        mkdir -p "$dir"
        shortcut="$dir/$SHORTCUT_NAME"
        write_shortcut "$shortcut"
        if [ "$SYSTEM" = linux ]; then gio set "$shortcut" metadata::trusted true >/dev/null 2>&1 || true; fi
        chmod +x "$shortcut"
        echo "    $shortcut"
    done
fi

echo
echo "awa is installed. Start it any time with the 'awa' shortcut."
echo "To update later, run this installer again; to remove awa, run it with --uninstall."
echo "To use the awa command in a terminal, run this line in it first:"
printf '    export PATH="%s:$PATH"\n' "$ENV_DIR/bin"
[ "$LAUNCH" = 1 ] || exit 0

rm -rf "$WORK"  # exec below replaces this shell, so the exit trap would not run
step "Starting awa (your browser opens in a moment)"
echo "Keep this window open while you use awa; close it to stop awa."
exec "$AWA" ui --project "$PROJECT"
