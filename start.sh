#!/usr/bin/env bash
# Run an aggregator subcommand. Defaults to `run` so `./start.sh` is the daemon entry.
# Used by both the systemd unit (which calls `start.sh doctor` as ExecStartPre and
# `start.sh` as ExecStart) and OpenClaw process management.
set -euo pipefail
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:$PATH"
# Forward every argument to `aggregator`. If no argument is given, default to `run`.
if [ "$#" -eq 0 ]; then
    set -- run
fi
exec uv run aggregator "$@"
