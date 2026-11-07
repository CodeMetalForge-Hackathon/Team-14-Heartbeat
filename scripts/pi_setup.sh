#!/usr/bin/env bash
# Run as the account that will own the sentinel, never with sudo bash.
set -euo pipefail

if [[ "$(id -u)" -eq 0 ]]; then
    printf '%s\n' 'Run bash scripts/pi_setup.sh as your normal user, without sudo.' >&2
    exit 1
fi
if [[ "$(uname -s)" != Linux || "$(uname -m)" != aarch64 ]]; then
    printf '%s\n' 'This setup script requires 64-bit Raspberry Pi OS (Linux aarch64).' >&2
    exit 1
fi

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd -- "$repo_dir"
user_name="$(id -un)"

sudo apt-get update
sudo apt-get install -y git libportaudio2 libsndfile1 ffmpeg alsa-utils espeak-ng avahi-daemon

# Reuse an existing user installation even when a fresh SSH shell lacks its PATH.
uv_bin="$(command -v uv || true)"
if [[ -z "$uv_bin" && -x "$HOME/.local/bin/uv" ]]; then
    uv_bin="$HOME/.local/bin/uv"
fi
if [[ -z "$uv_bin" ]]; then
    installer="$(mktemp)"
    trap 'rm -f -- "$installer"' EXIT
    if command -v curl >/dev/null 2>&1; then
        curl --proto '=https' -LsSf https://astral.sh/uv/install.sh -o "$installer"
    elif command -v wget >/dev/null 2>&1; then
        wget -q https://astral.sh/uv/install.sh -O "$installer"
    else
        printf '%s\n' 'The official uv installer needs curl or wget; install one, then rerun setup.' >&2
        exit 1
    fi
    UV_INSTALL_DIR="$HOME/.local/bin" UV_NO_MODIFY_PATH=1 sh "$installer"
    uv_bin="$HOME/.local/bin/uv"
fi

# uv supplies Python 3.11 when the OS Python is a different version.
# --locked preserves the submitted dependency versions instead of rewriting them.
"$uv_bin" sync --locked --python 3.11 --extra ml

account_groups=" $(id -nG "$user_name") "
if [[ "$account_groups" != *' dialout '* || "$account_groups" != *' audio '* ]]; then
    sudo usermod -aG dialout,audio "$user_name"
fi

model_dir="$("$uv_bin" run --no-sync --extra ml python -c 'from heartbeat.config import load_profile; print(load_profile().classifier.model_dir)')"
if [[ ! -f "$model_dir/yamnet.tflite" || ! -f "$model_dir/yamnet_class_map.csv" ]]; then
    printf '%s\n' 'Fetching missing public YAMNet artifacts and verifying CPU inference.'
    "$uv_bin" run --no-sync --extra ml python tools/fetch_models.py --allow-network
else
    printf '%s\n' 'YAMNet artifacts already exist; leaving them unchanged.'
fi

printf '\nSetup complete in %s\n' "$repo_dir"
printf 'Using uv: %s\n' "$uv_bin"
printf '%s\n' \
    'Log out and reconnect so dialout/audio group membership takes effect.' \
    'Then run from this repository:' \
    '  export PATH="$HOME/.local/bin:$PATH"' \
    '  uv run --no-sync --extra ml hb doctor --device stick' \
    '  uv run --no-sync --extra ml hb bench' \
    '  uv run --no-sync --extra ml hb run --device stick --knocker auto --dashboard --auto' \
    'See docs/PI.md for speaker selection, the phone dashboard and optional boot service.'
