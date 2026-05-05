# Running under OpenClaw

[OpenClaw](https://openclaw.ai) is a self-hosted always-on personal AI agent. The aggregator is just a long-running Python process — OpenClaw can supervise it the same way `systemd` would.

There are two integration patterns. Pick whichever fits your setup.

## Pattern A: OpenClaw supervises the process

This is the simplest setup: OpenClaw runs `start.sh` as a managed long-running task. It survives restarts, captures stdout/stderr in OpenClaw's log surface, and you can ask OpenClaw via chat to start/stop/restart it.

Add an entry to your OpenClaw process manifest pointing at:

```
/path/to/tg-reddit-aggregator/start.sh
```

`start.sh` calls `uv run aggregator run`, which respects the same `config.yaml` / `filters.md` / `.env` files as a manual run.

## Pattern B: Systemd, with OpenClaw alongside

If you already use systemd for service supervision, install the included unit and let OpenClaw co-exist:

```bash
sudo cp systemd/aggregator.service /etc/systemd/system/aggregator.service
# Edit /etc/systemd/system/aggregator.service — replace User=, WorkingDirectory=
sudo systemctl daemon-reload
sudo systemctl enable --now aggregator
sudo systemctl status aggregator
```

The unit runs `start.sh doctor` as `ExecStartPre`, so a misconfigured deploy fails loudly at startup instead of silently sending nothing.

## Recommended: gate startup on `aggregator doctor`

Either pattern benefits from running `aggregator doctor` before `aggregator run` so a misconfiguration (expired Reddit secret, bad model name, missing session) is caught at boot, not three days later when nothing is being delivered. The systemd unit does this with `ExecStartPre`. For OpenClaw, you can wrap `start.sh` to run `doctor` first or chain two managed tasks.

## Logs

`config.yaml` controls the log path (default: `data/aggregator.log`). When supervised, both OpenClaw's stdout capture AND the file sink will receive log lines. Useful queries while tuning `filters.md`:

```bash
tail -f data/aggregator.log | grep -E '(Decision for item|Pruner|Dispatcher running)'
```

## Stopping

`SIGTERM` triggers a graceful shutdown — the dispatcher finishes the item it's processing, the Telegram client disconnects cleanly, the Reddit poller closes its session, SQLite commits and closes. Both supervisors send `SIGTERM` by default.
