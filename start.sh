#!/usr/bin/env bash
# Run the aggregator. Use this as the OpenClaw / systemd ExecStart.
set -euo pipefail
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:$PATH"
exec uv run aggregator run "$@"
