#!/usr/bin/env bash
# Run the existing CLAP service using an ARM64 CPU-only PyTorch wheel.
set -euo pipefail
cd -- "$(dirname -- "$0")/.."
exec uv run --python 3.12 \
  --with 'torch==2.9.1+cpu' \
  --index https://download.pytorch.org/whl/cpu \
  --index-strategy unsafe-best-match \
  --script tools/clap_service.py \
  --profile profiles/clap-distress.yaml "$@"
