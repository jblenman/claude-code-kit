# Verify: do metrics and events arrive?

Order matters: collector up, then a local console test on one machine, then the network path,
then the real export, then the stores. Each step says what "good" looks like. Client-side
reference: the [monitoring docs](https://code.claude.com/docs/en/monitoring-usage), "Quick start".
`COLLECTOR_HOST` is the collector's address; the kit is cloned at `~/claude-code-kit` on the
collector host.

## 1. Collector running

On the collector host:

```sh
systemctl status otelcol-contrib --no-pager          # active (running)
journalctl -u otelcol-contrib -n 20 --no-pager       # "Everything is ready. Begin running and processing data."
ss -ltnp | grep -E ':4317|:4318|:8889'              # three listeners, process otelcol-contrib
curl -s http://127.0.0.1:8889/metrics | head -n 5   # 200 with target_info lines; no claude_code_ series yet is normal
sudo otelcol-contrib validate --config=/etc/otelcol-contrib/config.yaml && echo config-ok
```

If the service is not active, `journalctl -u otelcol-contrib -n 50` shows the config error; fix
`otelcol-config.yaml` and re-run `install-collector.sh`.

## 2. Console exporter on one machine (no network involved)

Run Claude Code once with telemetry printed to the terminal, before touching `settings.json`.

macOS / Linux shell:

```sh
CLAUDE_CODE_ENABLE_TELEMETRY=1 OTEL_METRICS_EXPORTER=console OTEL_LOGS_EXPORTER=console \
OTEL_METRIC_EXPORT_INTERVAL=1000 OTEL_LOGS_EXPORT_INTERVAL=1000 claude
```

Windows PowerShell:

```powershell
$env:CLAUDE_CODE_ENABLE_TELEMETRY='1'; $env:OTEL_METRICS_EXPORTER='console'; $env:OTEL_LOGS_EXPORTER='console'
$env:OTEL_METRIC_EXPORT_INTERVAL='1000'; $env:OTEL_LOGS_EXPORT_INTERVAL='1000'; claude
```

Good: within a few seconds the terminal shows a metric record named `claude_code.session.count`
with `session.id`, `user.id`, `user.email` and the standard attributes. Type a short prompt and
`claude_code.user_prompt` appears with `prompt: <REDACTED>` and a `prompt_length`. Quit (`/exit`).
The console output goes nowhere else; close the shell afterwards so the variables are gone.

## 3. Network path from the machine to the collector

OTLP/HTTP receiver (an empty POST is a valid, empty export; expect HTTP 200):

```sh
curl -s -o /dev/null -w '%{http_code}\n' -X POST -H 'Content-Type: application/json' -d '{}' http://COLLECTOR_HOST:4318/v1/metrics
```

gRPC port open (what the recommended env block uses):

```sh
nc -vz COLLECTOR_HOST 4317          # macOS/Linux: "succeeded"
```

```powershell
Test-NetConnection COLLECTOR_HOST -Port 4317   # Windows: TcpTestSucceeded : True
```

Not 200 / not open: the collector is down (step 1), the machine is on another network (a VPN
without routes to your LAN, a phone hotspot), or a firewall on the collector host
(`sudo ufw status`, `sudo nft list ruleset`).

## 4. Real export from one machine

Add the `env` block (`settings-env.json`, with `COLLECTOR_HOST` and the machine's name filled in)
to `~/.claude/settings.json`, start `claude`, wait up to 60 s (`OTEL_METRIC_EXPORT_INTERVAL`),
then on the collector host:

```sh
curl -s http://127.0.0.1:8889/metrics | grep '^claude_code_session_count'
```

Good: one line per session with `machine="<name>"`, `session_id="..."`, `start_type="fresh"`.
Then submit one prompt in the session and within about 10 s:

```sh
sudo tail -n 1 /var/lib/otelcol-contrib/events/claude-code-events.jsonl | jq -c -f ~/claude-code-kit/telemetry/flatten.jq | jq -c '{machine, e: .["event.name"], s: .["session.id"][0:8], t: .["event.timestamp"]}'
```

Good: `user_prompt`, then `api_request`, `tool_decision`, `tool_result` lines for that machine.

Metric names check (the collector should strip unit and `_total` suffixes):

```sh
curl -s http://127.0.0.1:8889/metrics | grep -o '^claude_code[a-z_]*' | sort -u
```

Expected after a session with an edit: `claude_code_active_time_total`,
`claude_code_code_edit_tool_decision`, `claude_code_cost_usage`, `claude_code_session_count`,
`claude_code_token_usage` (plus `claude_code_lines_of_code_count` and `claude_code_commit_count`
when those happened). If the names carry `_tokens_total` or `_USD_total` suffixes, the
`translation_strategy` key was not applied: check the collector version
(`otelcol-contrib --version`) and the journal for an unknown-key warning, or switch to
`add_metric_suffixes: false` on an older collector, and adjust `queries.md` to match.

## 5. Privacy checks

```sh
sudo cat /var/lib/otelcol-contrib/events/claude-code-events*.jsonl | jq -c -f ~/claude-code-kit/telemetry/flatten.jq \
| jq -s '{events: length, with_email: (map(has("user.email")) | map(select(.)) | length), unredacted_prompts: (map(select(.["event.name"]=="user_prompt" and .prompt != "<REDACTED>")) | length)}'
curl -s http://127.0.0.1:8889/metrics | grep -c 'user_email='
```

Good: `with_email: 0`, `unredacted_prompts: 0`, and `0` matches on the metrics page. If not, the
`attributes/drop-identity` processor is missing from a pipeline, or a machine has
`OTEL_LOG_USER_PROMPTS=1` set somewhere (shell profile, settings).

## 6. Prometheus (when installed with `--with-prometheus`)

```sh
curl -s 'http://127.0.0.1:9090/api/v1/targets' | jq -r '.data.activeTargets[] | [.labels.job, .health, .lastError] | @tsv'
curl -s 'http://127.0.0.1:9090/api/v1/query' --data-urlencode 'query=count by (machine) (claude_code_session_count)' | jq -r '.data.result[] | [.metric.machine, .value[1]] | @tsv'
```

Good: job `otelcol-claude` is `up`; the second query lists each machine that has exported.
Retention: `curl -s http://127.0.0.1:9090/api/v1/status/flags | jq -r '.data["storage.tsdb.retention.time"]'`
should be `2y` (or your `PROM_RETENTION`; the package default is 15 days).

## 7. When nothing arrives

- On the machine: `claude --debug-file ~/claude-otel-debug.log`, use the session for a minute,
  then `grep '3P telemetry' ~/claude-otel-debug.log`. Lines prefixed `[3P telemetry]` are the
  exporters configured here; `[Anthropic telemetry]` lines are Anthropic's own and not a sign of
  trouble.
- `OTEL_EXPORTER_OTLP_PROTOCOL` must be set: Claude Code has no default protocol, and an `otlp`
  exporter without one exports nothing.
- `OTEL_*` variables in a repository's `.claude/settings.json` or `.claude/settings.local.json`
  are ignored; they must be in `~/.claude/settings.json` (user scope), managed settings or the
  shell environment.
- A repository can still set `OTEL_LOGS_EXPORTER` or `OTEL_METRICS_EXPORTER` to `none` and switch
  a signal off for sessions in that repository; run `grep -r OTEL_ .claude/` in the project if one
  machine's sessions in one repository are missing.
- `/status` inside the session shows exporter problems.
- On the collector host, add `debug` to a pipeline's `exporters` list in
  `/etc/otelcol-contrib/config.yaml`, `sudo systemctl restart otelcol-contrib`, then
  `journalctl -u otelcol-contrib -f`: every received record is printed. Remove it again afterwards
  (it is verbose).
- Collector memory: `systemctl show otelcol-contrib -p MemoryCurrent`; the unit caps it at 400 MB
  and the `memory_limiter` processor refuses data before that.

## 8. Back to normal settings

Make sure no shell profile still exports the console variables or the 1 s intervals from step 2;
the `env` block in `settings.json` should be the only place they are set.
