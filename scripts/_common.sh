# Shared by the install/uninstall scripts. Sourced, not executed.
set -euo pipefail

SRC=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
PREFIX=${KEYLIGHT_PREFIX:-$HOME/.local}
LIB=$PREFIX/share/keylight
BIN=$PREFIX/bin
CONF_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/keylight
MODEL_NAME=face_detection_yunet_2023mar.onnx
MODEL_URL=https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/$MODEL_NAME
MODEL_SHA256=8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4

WITH_AUTO=0
for arg in "$@"; do
    case $arg in
        --with-auto) WITH_AUTO=1 ;;
        -h|--help) echo "usage: $0 [--with-auto]   (--with-auto: numpy/opencv venv + face model for closed-loop auto-brightness)"; exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done
# A previous --with-auto install stays a --with-auto install.
[ -x "$LIB/venv/bin/python" ] && WITH_AUTO=1

sha256_of() {
    if command -v sha256sum >/dev/null; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

install_auto_deps() {
    local py=$1
    [ -x "$LIB/venv/bin/python" ] || "$py" -m venv "$LIB/venv"
    "$LIB/venv/bin/python" -m pip install -q --upgrade pip numpy opencv-python-headless
    mkdir -p "$LIB/models"
    local model=$LIB/models/$MODEL_NAME
    if [ ! -f "$model" ] || [ "$(sha256_of "$model")" != "$MODEL_SHA256" ]; then
        if [ -f "$SRC/models/$MODEL_NAME" ]; then cp "$SRC/models/$MODEL_NAME" "$model.tmp"
        else curl -fsSL -o "$model.tmp" "$MODEL_URL"; fi
        if [ "$(sha256_of "$model.tmp")" != "$MODEL_SHA256" ]; then
            rm -f "$model.tmp"; echo "error: face model checksum mismatch" >&2; return 1
        fi
        mv "$model.tmp" "$model"
    fi
    echo "auto-brightness deps ready ($("$LIB/venv/bin/python" -c 'import cv2; print("opencv", cv2.__version__)'))"
}

link_config() {
    mkdir -p "$CONF_DIR"
    local target=$CONF_DIR/config.toml
    if [ -L "$target" ] && [ "$(readlink "$target")" = "$SRC/config.toml" ]; then
        echo "config: $target -> $SRC/config.toml"
        return
    fi
    if [ -e "$target" ] || [ -L "$target" ]; then
        local backup="$target.bak-$(date +%Y%m%d-%H%M%S)"
        mv "$target" "$backup"
        echo "config: moved existing $target to $backup"
    fi
    ln -s "$SRC/config.toml" "$target"
    echo "config: $target -> $SRC/config.toml (edit it in the repo and commit)"
}

seed_local_config() {
    local local_cfg=$CONF_DIR/local.toml
    [ -e "$local_cfg" ] && { echo "local: kept $local_cfg"; return; }
    cat > "$local_cfg" <<'LOCAL'
# Per-machine keylight overrides. Not in git: merged over the shared config.toml.

# [location]          # for the Sun Schedule; city-level precision is plenty
# latitude = 40.7
# longitude = -74.0
LOCAL
    echo "local: created $local_cfg; set [location] there for the Sun Schedule"
}

find_python() {
    local candidate
    for candidate in ${KEYLIGHT_PYTHON:-} python3.14 python3.13 python3.12 python3.11 python3 \
                     /opt/homebrew/bin/python3 /usr/local/bin/python3; do
        if command -v "$candidate" >/dev/null 2>&1 &&
           "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
            command -v "$candidate"
            return 0
        fi
    done
    echo "error: need Python 3.11+ (set KEYLIGHT_PYTHON=/path/to/python3)" >&2
    return 1
}

install_package() {
    local py=$1
    mkdir -p "$LIB" "$BIN"
    if [ "$WITH_AUTO" = 1 ]; then
        install_auto_deps "$py"
        py=$LIB/venv/bin/python
    fi
    rm -rf "$LIB/keylight.new"
    cp -R "$SRC/keylight" "$LIB/keylight.new"
    find "$LIB/keylight.new" -name __pycache__ -prune -exec rm -rf {} +
    rm -rf "$LIB/keylight"
    mv "$LIB/keylight.new" "$LIB/keylight"

    local name module
    for name in keylight keylight-watch; do
        module=keylight.cli
        [ "$name" = keylight-watch ] && module=keylight.watch
        cat > "$BIN/$name" <<WRAPPER
#!/bin/sh
# Installed by keylight/scripts. Re-run the installer to update.
PYTHONPATH="$LIB\${PYTHONPATH:+:\$PYTHONPATH}" exec "$py" -m $module "\$@"
WRAPPER
        chmod 755 "$BIN/$name"
    done
    echo "installed $BIN/keylight and $BIN/keylight-watch (python: $py)"

    link_config
    seed_local_config
    case ":$PATH:" in *":$BIN:"*) ;; *) echo "note: $BIN is not on your PATH" ;; esac
}

remove_package() {
    rm -rf "$LIB"
    rm -f "$BIN/keylight" "$BIN/keylight-watch"
    if [ -L "$CONF_DIR/config.toml" ] && [ "$(readlink "$CONF_DIR/config.toml")" = "$SRC/config.toml" ]; then
        rm "$CONF_DIR/config.toml"
        echo "removed config symlink (the config itself stays in the repo)"
    fi
    echo "removed $LIB and wrappers in $BIN"
}
