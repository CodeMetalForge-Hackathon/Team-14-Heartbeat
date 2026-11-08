#!/usr/bin/env bash
# Live stationary sentinel for the demo: stick mic listens, the speaker plays the
# knock challenge, the heuristic classifier scores knocks, the dashboard shows it.
# No CLAP service, rover, API key or servo knocker needed.
# Usage: bash scripts/demo_live.sh [port]     (Ctrl+C stops; the run is saved under runs/)
set -euo pipefail

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd -- "$repo_dir"
port="${1:-8000}"
export PATH="$HOME/.local/bin:$PATH"

stick_id=/dev/serial/by-id/usb-Hades2001_M5stack_81520161EE-if00-port0
if [[ -e "$stick_id" ]]; then
    device="stick:$stick_id"
else
    echo "WARNING: $stick_id not found; falling back to stick auto-detection." >&2
    device=stick
fi

# The challenge knocks must be loud enough for the survivor to copy.
wpctl set-volume @DEFAULT_AUDIO_SINK@ 0.8 2>/dev/null || true

ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
printf '\nHEARTBEAT live sentinel (profile: demo)\n'
printf 'Dashboard on this Pi : http://127.0.0.1:%s\n' "$port"
[[ -n "$ip" ]] && printf 'Dashboard for phones : http://%s:%s\n' "$ip" "$port"
printf 'Flow: quiet room -> "All quiet -> Challenge" (or survivor knocks twice) -> copy the knocks.\n\n'

# Open the dashboard once the server answers; gives up quietly after 30 s.
(
    for _ in $(seq 1 30); do
        if curl -s --max-time 1 "http://127.0.0.1:$port/api/state" >/dev/null; then
            xdg-open "http://127.0.0.1:$port" >/dev/null 2>&1 || true
            break
        fi
        sleep 1
    done
) &

exec uv run --offline --no-sync hb run --device "$device" --knocker speaker --auto \
    --profile demo --dashboard --host 0.0.0.0 --port "$port"
