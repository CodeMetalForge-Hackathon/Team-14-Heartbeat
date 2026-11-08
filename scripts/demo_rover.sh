#!/usr/bin/env bash
# Deterministic rover demo: drive the ~1 m lane (profiles/route-1m.yaml), stop when
# the stick hears a knock, then challenge the survivor with spoken knock counts.
# No camera, OpenAI API or CLAP service needed.
# Usage: bash scripts/demo_rover.sh [--yes] [port]
#   Dashboard E-STOP, or e + Enter in this terminal, stops the motors immediately.
#   After a stop the rover holds for review: press Resume (or r + Enter) to continue.
set -euo pipefail

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd -- "$repo_dir"
confirm=1
if [[ "${1:-}" == --yes ]]; then confirm=0; shift; fi
port="${1:-8000}"
export PATH="$HOME/.local/bin:$PATH"

rover=/dev/serial/by-path/platform-xhci-hcd.1-usbv2-0:1:1.0-port0
stick=/dev/serial/by-id/usb-Hades2001_M5stack_81520161EE-if00-port0
for dev in "$rover" "$stick"; do
    if [[ ! -e "$dev" ]]; then
        echo "Missing $dev. Plug the rover and M5Stick into the same USB ports as before; see ls -l /dev/serial/by-path/" >&2
        exit 1
    fi
done

# The challenge knocks must be loud enough for the survivor to copy.
wpctl set-volume @DEFAULT_AUDIO_SINK@ 0.8 2>/dev/null || true

ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
printf '\nHEARTBEAT rover demo (profile: rover-demo, route: 1 m lane)\n'
printf 'Dashboard on this Pi : http://127.0.0.1:%s\n' "$port"
[[ -n "$ip" ]] && printf 'Dashboard for phones : http://%s:%s\n' "$ip" "$port"
printf 'E-STOP: dashboard button, or e + Enter here.\n\n'
if (( confirm )); then
    read -r -p "The rover drives ~1 m FORWARD as soon as it starts. Clear the path, then press Enter (Ctrl+C cancels)... "
fi

(
    for _ in $(seq 1 30); do
        if curl -s --max-time 1 "http://127.0.0.1:$port/api/state" >/dev/null; then
            xdg-open "http://127.0.0.1:$port" >/dev/null 2>&1 || true
            break
        fi
        sleep 1
    done
) &

exec uv run --offline --no-sync hb patrol --rover "$rover" --device "stick:$stick" \
    --route profiles/route-1m.yaml --profile rover-demo --knocker speaker \
    --dashboard --host 0.0.0.0 --port "$port"
