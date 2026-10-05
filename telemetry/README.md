# telemetry: Claude Code's OpenTelemetry export to a collector you run

Claude Code can export metrics and events over OpenTelemetry (OTLP). This folder sets up a small
receiver for them on any Debian or Ubuntu host on your network, plus the client settings, queries
and checks. The collector serves the metrics to Prometheus and writes the events to rotated JSONL
files. That answers "how many tokens, how much money, per machine, session, model and day" with
PromQL, and "what happened in session X" with `jq`, without reading transcripts.

Everything here follows the [monitoring docs](https://code.claude.com/docs/en/monitoring-usage)
(variable names, metric names, event names, attributes, privacy statements). See the bottom of
this page for what was run where.

## Design

```
each machine running Claude Code (macOS, Linux, Windows)
  claude (CLI, Desktop Code tab)                  OTLP gRPC :4317  (or HTTP :4318)
    env: CLAUDE_CODE_ENABLE_TELEMETRY=1 ───────────────────────────────┐
                                                                       v
collector host = COLLECTOR_HOST (Debian/Ubuntu, arm64 or amd64; a Raspberry Pi is enough)
  otelcol-contrib (native binary, systemd unit, user otelcol-contrib, 400 MB memory cap)
    metrics -> deltatocumulative -> drop identity attributes -> Prometheus exporter :8889/metrics
    events  -> drop identity attributes -> file exporter -> /var/lib/otelcol-contrib/events/*.jsonl
  prometheus (apt package, optional) scrapes :8889 every 60 s, keeps 2 years, UI/API on :9090
```

Why a native binary and not a container: a small always-on host often runs other services
directly on the OS; one more systemd unit fits there, a container runtime may not. Why Prometheus
and a file instead of a full observability stack: the questions are counts per machine, session,
model and day (PromQL) and "what happened in session X" (`jq` over a file). Grafana can be added
later and pointed at the same Prometheus.

`COLLECTOR_HOST` stands for the collector's address (an IP such as `10.0.0.20` or a LAN name such
as `collector.lan`) everywhere in these files. Replace it with yours.

## Files

| File | Purpose |
|---|---|
| `otelcol-config.yaml` | Collector config: OTLP receivers on 4317 (gRPC) and 4318 (HTTP), memory limiter, identity-attribute drop, delta to cumulative, Prometheus exporter on 8889, JSONL file exporter with rotation (50 MB files, 100 backups, 400 days). |
| `install-collector.sh` | Idempotent installer for a Debian/Ubuntu host: detects the CPU (arm64 or amd64), downloads the matching `otelcol-contrib` release, verifies its checksum, installs binary, config, systemd unit and service user, restarts only on change. `--dry-run`, `--with-prometheus`, `--config PATH`, `OTELCOL_VERSION=x.y.z`, `PROM_RETENTION=2y`. Root required except for `--dry-run`. |
| `prometheus-scrape.yaml` | The scrape job `install-collector.sh --with-prometheus` appends to `/etc/prometheus/prometheus.yml`. |
| `settings-env.json` | The `env` block for `~/.claude/settings.json` on each machine (replace `COLLECTOR_HOST` and `<name>`). |
| `flatten.jq` | Turns the collector's OTLP-JSON event lines into one JSON object per event. |
| `tool-decisions.jq`, `cost-per-day.jq`, `peak-context.jq` | Recipes over flattened events: per-session tool decisions; exact cost and tokens per machine per day; per-session peak context. |
| `report.py` | Tokens and cost per machine and model from Prometheus, today and the last 7 days by default (`--since 12h,1d,2w,today,week`; `--json`; `--url`, `PROMETHEUS_URL` or `COLLECTOR_HOST`; `--self-test`). Totals each session series exactly, which `increase()` cannot do for short sessions. |
| `queries.md` | PromQL for tokens and cost per machine, session, model and day; `jq` recipes; what telemetry can and cannot replace. |
| `verify.md` | Step list to confirm metrics and events arrive, privacy checks, troubleshooting. |
| `tests/test_telemetry.py` | Offline tests: config structure, variable, metric and event names against the docs, the installer's dry run (Debian/Ubuntu detection, arm64/amd64 asset names), the `jq` recipes against a synthetic fixture, `report.py`'s arithmetic. |
| `tests/events-fixture.jsonl` | **Synthetic** events in the file exporter's format (built from the OTLP/JSON encoding and the docs' attribute lists), used by the tests. |

## Install the collector

On the collector host (Debian, Ubuntu or a derivative such as Raspberry Pi OS; 64-bit; systemd):

```sh
git clone https://github.com/jblenman/claude-code-kit ~/claude-code-kit   # or git pull in an existing clone
cd ~/claude-code-kit/telemetry
./install-collector.sh --dry-run                        # what would change; nothing written, no root needed
sudo ./install-collector.sh --with-prometheus           # collector + Prometheus server
systemctl status otelcol-contrib --no-pager
```

The script resolves the latest `opentelemetry-collector-releases` tag from GitHub's redirect,
downloads `otelcol-contrib_<version>_linux_<arm64|amd64>.tar.gz`, checks it against the release's
checksums file when that file lists it, installs to `/usr/local/bin/otelcol-contrib`, validates
the config with `otelcol-contrib validate` before putting it in place, and writes a hardened unit
(`ProtectSystem=strict`, `ReadWritePaths=/var/lib/otelcol-contrib`, `MemoryMax=400M`). Re-running
it upgrades the collector when a newer release exists and otherwise prints `OK` for every step.
Pin a release with `OTELCOL_VERSION=0.162.0` if an upgrade misbehaves. To change the config,
edit `otelcol-config.yaml` and re-run the script. The last line it prints is the endpoint to use
as `COLLECTOR_HOST`.

`--with-prometheus` installs the distribution's `prometheus` package, appends the `otelcol-claude`
scrape job to `/etc/prometheus/prometheus.yml` (a timestamped backup is kept; if `promtool check
config` rejects the result, the backup is put back) and sets `--storage.tsdb.retention.time=2y`
(or `PROM_RETENTION`) in `/etc/default/prometheus`. Disk: the events files are capped at about
5 GB by rotation; Prometheus for a handful of sessions a day stays well under 1 GB a year.

Memory: the collector's `memory_limiter` (256 MiB) and the unit's `MemoryMax=400M` suit a small
host that runs other things too. Raise both on a bigger host.

Ports on the collector host: 4317, 4318 (OTLP, all interfaces), 8889 (metrics scrape), 9090
(Prometheus). **There is no authentication**: keep these ports on your LAN or VPN and do not
forward them. With `ufw` active, allow your LAN only, for example
`sudo ufw allow from 10.0.0.0/24 to any port 4317,4318 proto tcp`.

## Configure each machine

Merge this into the `env` object of `~/.claude/settings.json` (user scope) on every machine. The
file is `settings-env.json`; replace `COLLECTOR_HOST` with the collector's address and `<name>`
with a short name for the machine (letters, digits, `_`, `.`, `-`; no spaces or quotes), such as
`laptop` or `build-box`.

```json
{
  "env": {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "OTEL_METRICS_EXPORTER": "otlp",
    "OTEL_LOGS_EXPORTER": "otlp",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://COLLECTOR_HOST:4317",
    "OTEL_RESOURCE_ATTRIBUTES": "machine=<name>",
    "OTEL_METRIC_EXPORT_INTERVAL": "60000",
    "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE": "cumulative"
  }
}
```

Export starts with the next session (running sessions keep their environment).

| Variable | Why this value |
|---|---|
| `CLAUDE_CODE_ENABLE_TELEMETRY=1` | Required switch; without it the other variables do nothing. |
| `OTEL_METRICS_EXPORTER=otlp`, `OTEL_LOGS_EXPORTER=otlp` | Both signals go to the collector. (`prometheus` would open a scrape port on each machine; `console` prints to the terminal, see below.) |
| `OTEL_EXPORTER_OTLP_PROTOCOL=grpc` | Claude Code has **no default protocol**: an `otlp` exporter without this exports nothing. `http/protobuf` with `http://COLLECTOR_HOST:4318` is the alternative where gRPC is blocked. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | The collector. Plain HTTP on your LAN, no headers or tokens. |
| `OTEL_RESOURCE_ATTRIBUTES=machine=<name>` | Becomes the `machine` label on every datapoint and event (custom keys are attached to datapoints; `OTEL_METRICS_INCLUDE_RESOURCE_ATTRIBUTES` defaults to true). The format is strict: comma-separated `key=value`, no spaces, no quotes. |
| `OTEL_METRIC_EXPORT_INTERVAL=60000` | The default (60 s), written out so it is visible. Lower it only while testing. Events export every 5 s by default (`OTEL_LOGS_EXPORT_INTERVAL`). |
| `OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=cumulative` | The default is `delta`. The Prometheus exporter needs cumulative series, and cumulative means every export carries the full count, so a collector restart loses nothing. The collector also runs `deltatocumulative` for a machine that lacks the variable. |

Left unset on purpose:

- `OTEL_LOG_USER_PROMPTS` **stays off** (the default): prompts are exported as `<REDACTED>` with
  only `prompt_length`. `OTEL_LOG_ASSISTANT_RESPONSES` stays off with it. Prompts and replies
  carry whatever you work on, and the events file is plain text on disk.
- `OTEL_LOG_TOOL_DETAILS` **off by default**. On, it adds tool parameters (Bash command lines,
  file paths, MCP server and tool names, skill names) to `tool_result` and `tool_decision`, real
  skill names on `skill_activated` (otherwise user-defined skills show as `custom_skill`), and real
  MCP names on the cost and token counters. Turn it on once you are content with who can read the
  events file (root and the `otelcol-contrib` group).
- `OTEL_METRICS_INCLUDE_VERSION=true` would add `app.version` to every series; the collector's
  `target_info` already carries `service.version`.
- Traces (`CLAUDE_CODE_ENHANCED_TELEMETRY_BETA`, `OTEL_TRACES_EXPORTER`): beta, needs a trace
  backend; nothing here stores spans.

Where the variables can live (from the docs):

- `OTEL_*` variables in a **repository's** `.claude/settings.json` or `.claude/settings.local.json`
  are ignored for turning telemetry on or routing it; a repository can only switch a signal off
  (`none`). User-scope `~/.claude/settings.json`, managed settings or the shell environment are the
  places.
- Claude Code removes the `OTEL_*` exporter variables from every subprocess it starts (Bash tool,
  hooks, MCP servers), so your hooks and tools are unaffected.
- `service.name` is `claude-code` for terminal sessions and `claude-code-desktop` for Code tab
  sessions in the Desktop app; both land in the same pipelines here.
- Windows: the `env` block works the same; nothing else to set.
- Cloud sessions (`claude --cloud`, claude.ai/code) do not read your `~/.claude/settings.json`
  and cannot reach a LAN collector. They are not covered here.

### Try it locally first with the console exporter

Before editing `settings.json`, run one session with the exporters pointed at the terminal, so you
see the output format and the attributes once with no network in the way:

```sh
CLAUDE_CODE_ENABLE_TELEMETRY=1 OTEL_METRICS_EXPORTER=console OTEL_LOGS_EXPORTER=console \
OTEL_METRIC_EXPORT_INTERVAL=1000 OTEL_LOGS_EXPORT_INTERVAL=1000 claude
```

(PowerShell: `$env:CLAUDE_CODE_ENABLE_TELEMETRY='1'` and so on, then `claude`.) Expect a
`claude_code.session.count` record within a second or two and a `claude_code.user_prompt` event
after the first prompt. The full step list, including the network and privacy checks, is
[`verify.md`](verify.md).

## What Claude Code exports

### Metrics (`OTEL_METRICS_EXPORTER`), meter `com.anthropic.claude_code`

| Metric | Unit | Incremented | Extra attributes |
|---|---|---|---|
| `claude_code.session.count` | none | at session start | `start_type`: fresh, resume, continue, agents_view (the `claude agents` dashboard; filter it out) |
| `claude_code.token.usage` | tokens | after each API request | `type` (input, output, cacheRead, cacheCreation; `input` excludes cached tokens), `model`, `query_source` (main, subagent, auxiliary), `speed`, `effort`, `agent.name`, `skill.name`, `plugin.name`, `marketplace.name`, `mcp_server.name`, `mcp_tool.name` |
| `claude_code.cost.usage` | USD (an estimate) | after each API request | same as token usage |
| `claude_code.code_edit_tool.decision` | none | on accept/reject of Edit, Write, NotebookEdit | `tool_name`, `decision`, `source`, `language` |
| `claude_code.active_time.total` | s | during interaction and CLI work | `type`: user, cli |
| `claude_code.lines_of_code.count` | none | on code added or removed | `type`: added, removed; `model` |
| `claude_code.commit.count`, `claude_code.pull_request.count` | none | on commits and pull requests made through Claude Code | |

### Events (`OTEL_LOGS_EXPORTER`), event name `claude_code.<name>`

Used by the recipes here: `user_prompt` (prompt_length, redacted prompt, `message.uuid`,
`command_name`), `api_request` (model, cost_usd, duration_ms, input/output/cache_read/cache_creation
tokens, request_id, query_source, speed, effort), `tool_decision` (tool_name, tool_use_id, decision,
source, tool_source), `tool_result` (tool_name, tool_use_id, success, duration_ms, error_type,
decision_source, tool_input_size_bytes, tool_result_size_bytes), `skill_activated` (skill.name,
invocation_trigger, skill.source), `hook_registered` (hook_event, hook_type, hook_source),
`compaction` (trigger, success, pre_tokens, post_tokens).

Also exported and kept in the file: `assistant_response` (length only, text redacted), `api_error`,
`api_refusal`, `api_retries_exhausted`, `permission_mode_changed`, `auth`, `mcp_server_connection`,
`internal_error`, `plugin_installed`, `plugin_loaded`, `at_mention`, `hook_execution_start`,
`hook_execution_complete`, `hook_plugin_metrics`, `subagent_completed`, `feedback_survey`,
`retention_sweep`, `managed_settings_resolved`. `api_request_body` and `api_response_body` exist
but only with `OTEL_LOG_RAW_API_BODIES`, which is not set here.

### Attributes on everything

`session.id`, `user.id` (random per install, no personal information), `user.email`,
`user.account_uuid`, `user.account_id`, `organization.id`, `terminal.type`, plus the
`OTEL_RESOURCE_ATTRIBUTES` keys (`machine`). Optional, off by default: `app.version`,
`app.entrypoint`, and the repository's identity (`vcs.*`, `OTEL_METRICS_INCLUDE_REPOSITORY`).
Events also carry `prompt.id` (all events of one prompt; hooks receive the same value as
`prompt_id`), `event.timestamp`, `event.sequence` (per process, restarts on resume), and
`tool_use_id` on tool events, which matches the id hooks receive. Resource block: `service.name`,
`service.version`, `os.type`, `os.version`, `host.arch`.

### Privacy

- **`user.email` and the account ids are attached to every metric datapoint and event** when you
  are signed in with a Claude account. The docs say they go only to the collector you configure,
  never to Anthropic, and suggest filtering at the backend. This collector **deletes**
  `user.email`, `user.account_uuid` and `user.account_id` in both pipelines
  (`attributes/drop-identity`): the `machine` label says where data came from, and the events file
  is plain text on disk. Remove the processor from the pipelines if you need to tell several
  accounts apart. `organization.id` stays. On the client side,
  `OTEL_METRICS_INCLUDE_ACCOUNT_UUID=false` drops the two account ids from metrics only;
  `user.email` has no client-side switch.
- `OTEL_LOG_USER_PROMPTS` stays off: no prompt text, no assistant text, only lengths.
- No file contents or code are in metrics or events (docs, "Security and privacy").
- The events directory is readable by root and the `otelcol-contrib` group only (`0750`).
- This export is separate from Anthropic's own operational telemetry, which none of this changes.

## How the collector stores it

**Metrics** are served on `http://COLLECTOR_HOST:8889/metrics` in Prometheus text format with
`translation_strategy: UnderscoreEscapingWithoutSuffixes`, so names stay recognisable
(`claude_code_token_usage`, `claude_code_cost_usage`, ...) instead of gaining unit and `_total`
suffixes (`claude_code_token_usage_tokens_total`). Attribute names become labels with dots turned
into underscores (`session_id`, `query_source`, `skill_name`). A session's series stay on the page
for `metric_expiration: 12h` after its last export, so a finished session keeps its final value
visible (a flat line) for half a day. Prometheus scrapes every 60 s and keeps 2 years.

**Events** go to `/var/lib/otelcol-contrib/events/claude-code-events.jsonl`, one OTLP
`ExportLogsServiceRequest` in JSON per line (the file exporter's `json` format: a batch per line,
not an event per line). At 50 MB the file is renamed with a timestamp
(`claude-code-events-2026-10-04T14-00-00.000.jsonl`); at most 100 old files are kept, nothing older
than 400 days, so about 5 GB at the ceiling. `flatten.jq` produces one object per event with
resource and record attributes merged; the recipes in [`queries.md`](queries.md) build on that. The
collector flushes every 5 s.

**Not stored**: traces (not exported), prompt or response text, tool arguments (unless you turn on
`OTEL_LOG_TOOL_DETAILS`), anything from cloud sessions.

## Usage report: `report.py`

Tokens and cost per machine and per model, today and over the last 7 days, from the collector's
Prometheus (stdlib Python 3.8+, any machine that reaches the collector host; Windows: `py`):

```sh
COLLECTOR_HOST=10.0.0.20 python3 telemetry/report.py       # today (since 00:00 local time) + last 7 days
python3 telemetry/report.py --url http://10.0.0.20:9090 --since 1d   # or 12h, 7d, 2w, today, week
python3 telemetry/report.py --json                        # rows, by_machine, by_model, total per window
python3 telemetry/report.py --machines laptop,desktop     # name machines that should have data
python3 telemetry/report.py --self-test                   # offline tests against an in-process fake Prometheus
```

Columns: sessions (distinct `session_id` with usage in the window), input / output / cache read /
cache write tokens (`type` = input, output, cacheRead, cacheCreation), total tokens, cost (Claude
Code's estimate). Each window also lists session starts per machine
(`claude_code_session_count` without `agents_view`) and, with `--machines` or
`TELEMETRY_MACHINES`, the machines that sent nothing. Exit codes: 1 Prometheus unreachable, 2 usage,
3 query refused, 4 request-guard refusal.

How it totals: per session series, the highest value in the window minus the value at the window's
start, `((max_over_time(m[W]) - (m offset W)) >= 0) or max_over_time(m[W])`, summed per machine and
model; a series whose counter restarts inside the window (a resumed session) is recomputed from its
raw samples. Not `increase()`: a session's first export already carries everything it used before
the first scrape, and a short headless run exports once and then stays flat, so `increase()` sees no
growth. In a live check `sum by (machine) (increase(claude_code_cost_usage[1d]))` returned 0 for
three machines whose sessions had each spent about $0.05; the `increase()` recipes in `queries.md`
are for charts, not totals.

When this kit's `request-guard` plugin is in the same clone, the report sends its queries through
the guard's `wait_turn()`: a private or LAN address passes at once, a public `--url` is paced.

## Verify

[`verify.md`](verify.md) is the full list. The short version, once a machine has the `env` block
and has run one session for a minute:

```sh
curl -s http://COLLECTOR_HOST:8889/metrics | grep '^claude_code_session_count'
```

Good: one line per session with `machine="<name>"`, `session_id="..."`, `start_type="fresh"`.

## Rollback

- A machine: remove the `env` keys from `~/.claude/settings.json` (or set
  `CLAUDE_CODE_ENABLE_TELEMETRY` to `0`); the next session exports nothing.
- The collector: `sudo systemctl disable --now otelcol-contrib`, then remove
  `/usr/local/bin/otelcol-contrib`, `/etc/otelcol-contrib/`,
  `/etc/systemd/system/otelcol-contrib.service` and, when you no longer want the data,
  `/var/lib/otelcol-contrib/`. Prometheus: `sudo apt-get remove prometheus`, or delete the
  `otelcol-claude` job from `/etc/prometheus/prometheus.yml` (a `.bak-<timestamp>` copy from before
  the install is next to it).

## Tests

```sh
python3 telemetry/tests/test_telemetry.py        # Windows: py telemetry\tests\test_telemetry.py
```

Stdlib only. `jq` is needed for the recipe tests (skipped without it); `bash` for the installer
tests (skipped without it). PyYAML is used when installed, otherwise a small built-in YAML reader
parses the collector config (both are compared when PyYAML is present; the reader's output for both
YAML files was also checked against Ruby's YAML parser on 2026-10-04). What the tests check: the
collector config parses and every pipeline references defined components; the identity fields are
dropped in both pipelines; every `OTEL_*` and `CLAUDE_CODE_*` name in these files is in the docs'
variable list (a snapshot inside the test); metric and event names map to documented ones;
`settings-env.json` matches the README block; no email address and no IPv4 address other than the
loopback, the any-address and the `10.0.0.x` examples is in any file; `install-collector.sh` passes `bash -n`, a dry run writes nothing, picks the arm64 or
amd64 asset from the CPU, recognises Debian and Ubuntu and warns on other systems; the three `jq`
recipes produce the expected numbers from the fixture; `report.py`'s windows, restart-aware totals,
aggregation and default URL (in memory, no sockets). `python3 telemetry/report.py --self-test` adds
the HTTP path against an in-process fake Prometheus on 127.0.0.1.

What they cannot check: that your collector accepts the config (`otelcol-contrib validate` on the
host), the exact Prometheus names your exporter version produces (verify.md step 4), and that
Claude Code's export reaches the host.

## What was run where

- The collector config, `prometheus-scrape.yaml` and the arm64 install path were run on
  2026-10-04 on a Raspberry Pi with Debian 13 (aarch64): otelcol-contrib 0.162.0 as a systemd
  service and Prometheus 2.53 from the Debian package with this scrape job and 2-year retention,
  after a clean dry run. A headless session on a macOS laptop (Claude Code 2.1.286,
  `OTEL_METRIC_EXPORT_INTERVAL=5000` for the test) produced `claude_code_session_count`,
  `claude_code_cost_usage`, `claude_code_token_usage` and `claude_code_active_time_total` series
  labelled with its `machine` value at `:8889/metrics` within seconds, and the events file filled.
  `organization_id` stays on the series, as designed: the collector drops only the email and the
  account ids.
- The Debian/Ubuntu detection and the amd64 asset selection in `install-collector.sh` are covered
  by the dry-run tests only.
- Names were checked against the monitoring docs on 2026-10-04 (Claude Code 2.1.289).
