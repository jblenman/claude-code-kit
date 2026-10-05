# Queries over Claude Code telemetry

Two stores on the collector host, see [`README.md`](README.md):

- **Prometheus** (`http://COLLECTOR_HOST:9090`) scrapes the collector's `:8889/metrics`. Metric names
  are the Claude Code names with dots replaced by underscores (the collector runs with
  `translation_strategy: UnderscoreEscapingWithoutSuffixes`); attribute names likewise
  (`session.id` -> `session_id`, `query_source`, `model`, `type`, `machine`).
- **Events file** `/var/lib/otelcol-contrib/events/claude-code-events.jsonl` (+ rotated
  `claude-code-events-<timestamp>.jsonl`). Each line is one OTLP batch; `flatten.jq` turns it
  into one JSON object per event. All `jq` recipes below start with that step.

Metric and attribute names were checked against the
[monitoring docs](https://code.claude.com/docs/en/monitoring-usage) on 2026-10-04. The Prometheus
names assume the collector config in this folder; confirm them once with
`curl -s http://COLLECTOR_HOST:8889/metrics | grep -o '^claude_code[a-z_]*' | sort -u` (verify.md).

## Metrics available (Prometheus names)

| Prometheus name | Claude Code metric | Labels beyond the standard set |
|---|---|---|
| `claude_code_token_usage` | `claude_code.token.usage` | `type` (input, output, cacheRead, cacheCreation), `model`, `query_source` (main, subagent, auxiliary), `speed`, `effort`, `skill_name`, `agent_name`, `plugin_name`, `mcp_server_name`, `mcp_tool_name` |
| `claude_code_cost_usage` | `claude_code.cost.usage` (USD, an estimate) | same as token usage |
| `claude_code_session_count` | `claude_code.session.count` | `start_type` (fresh, resume, continue, agents_view) |
| `claude_code_code_edit_tool_decision` | `claude_code.code_edit_tool.decision` | `tool_name`, `decision`, `source`, `language` |
| `claude_code_active_time_total` | `claude_code.active_time.total` (seconds) | `type` (user, cli) |
| `claude_code_lines_of_code_count` | `claude_code.lines_of_code.count` | `type` (added, removed), `model` |
| `claude_code_commit_count`, `claude_code_pull_request_count` | `claude_code.commit.count`, `claude_code.pull_request.count` | |

Standard labels on every series: `machine` (from `OTEL_RESOURCE_ATTRIBUTES`), `session_id`,
`user_id` (random per install), `terminal_type`, `organization_id`. `user_email`,
`user_account_uuid` and `user_account_id` are deleted by the collector (README, privacy).

All metrics are counters. With `OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=cumulative`
each session's series counts up from 0 at session start and is re-sent every
`OTEL_METRIC_EXPORT_INTERVAL` (60 s) while the session runs.

### Two ways to total a counter, and when each is right

- `increase(metric[range])` sums growth inside the window across sessions. It extrapolates at the
  edges of the window (a series that starts or ends inside the range is scaled to the window
  boundaries), so a day total can be a few percent off. Right for charts and "roughly how much".
  **It undercounts badly for short sessions**: a `claude -p` run's whole usage arrives in its first
  export and the series then stays flat, so `increase()` sees no growth. In a live check
  `sum by (machine) (increase(claude_code_cost_usage[1d]))` returned 0 for three machines while
  `max_over_time` showed real cost. For totals use `report.py`, or its form summed per machine and
  model: `((max_over_time(m[W]) - (m offset W)) >= 0) or max_over_time(m[W])`. The `increase()`
  recipes below are kept for charts.
- `max_over_time(metric[range])` per `session_id` is the exact per-session total, because the
  series is cumulative from the session's start. Right for "what did session X cost". Caveat: a
  resumed session (`claude --resume`) keeps its `session.id` but its counters restart at 0 in the
  new process, so `max_over_time` covers only the largest run; `/clear` gives a new `session.id`.
- For exact daily figures use the events file (`cost-per-day.jq`): each `api_request` event carries
  that request's `cost_usd` and token counts, nothing is extrapolated.

## PromQL

Tokens per machine, last 24 hours, by token type:

```promql
sum by (machine, type) (increase(claude_code_token_usage[24h]))
```

Tokens per machine without the cache split:

```promql
sum by (machine) (increase(claude_code_token_usage[24h]))
```

Cost per machine, last 7 days (exact, the form `report.py` uses):

```promql
sum by (machine) (((max_over_time(claude_code_cost_usage[7d]) - (claude_code_cost_usage offset 7d)) >= 0) or max_over_time(claude_code_cost_usage[7d]))
```

The same with `increase()`, fine for a chart but low for short sessions:

```promql
sum by (machine) (increase(claude_code_cost_usage[7d]))
```

Cost per session (exact per-session totals, sessions seen in the last 7 days):

```promql
sort_desc(sum by (machine, session_id) (max_over_time(claude_code_cost_usage[7d])))
```

Tokens per session, main thread only (subagents and auxiliary requests excluded):

```promql
sum by (machine, session_id) (max_over_time(claude_code_token_usage{query_source="main"}[7d]))
```

Cost per model, last 30 days:

```promql
sum by (model) (increase(claude_code_cost_usage[30d]))
```

Cost per day per machine: evaluate a 1-day increase with a 1-day step. In Grafana set the query to
`sum by (machine) (increase(claude_code_cost_usage[1d]))` with *Min step* `1d`; from a shell on the
collector host:

```sh
curl -s 'http://127.0.0.1:9090/api/v1/query_range' \
  --data-urlencode 'query=sum by (machine) (increase(claude_code_cost_usage[1d]))' \
  --data-urlencode "start=$(date -u -d '14 days ago' +%Y-%m-%dT00:00:00Z)" \
  --data-urlencode "end=$(date -u +%Y-%m-%dT00:00:00Z)" \
  --data-urlencode 'step=86400' \
| jq -r '.data.result[] | .metric.machine as $m | .values[] | [$m, (.[0] | todate | .[0:10]), (.[1] | tonumber | . * 100 | round / 100)] | @tsv'
```

(macOS `date` has no `-d`; use `-v-14d`, or run it on the collector host.) Each value is the
increase over the day that *ends* at the timestamp shown.

Yesterday's cost, all machines:

```promql
sum(increase(claude_code_cost_usage[1d] offset 1d))
```

Sessions started per machine per day, excluding the `claude agents` dashboard process:

```promql
sum by (machine) (increase(claude_code_session_count{start_type!="agents_view"}[1d]))
```

Share of tokens spent by subagents, last 7 days:

```promql
sum(increase(claude_code_token_usage{query_source="subagent"}[7d]))
  / sum(increase(claude_code_token_usage[7d]))
```

Cost by skill (user-defined skill names appear verbatim on the cost counter; third-party plugin
skills show as `third-party`):

```promql
sum by (skill_name) (increase(claude_code_cost_usage[30d]))
```

Edit/Write/NotebookEdit permission decisions by source, last 7 days (the metric form; the events
give the same per tool for every tool, not only the three code editors):

```promql
sum by (machine, decision, source) (increase(claude_code_code_edit_tool_decision[7d]))
```

Active hours per machine per day, split into user typing and CLI work:

```promql
sum by (machine, type) (increase(claude_code_active_time_total[1d])) / 3600
```

Which Claude Code versions are running (from the collector's `target_info`, resource attributes):

```promql
count by (service_version) (target_info{service_name=~"claude-code.*"})
```

## jq recipes over the events file

Flatten first. `EVENTS` below stands for the current file plus its rotations; reading them needs
root or membership in the `otelcol-contrib` group:

```sh
cd ~/claude-code-kit/telemetry
EVENTS=/var/lib/otelcol-contrib/events/claude-code-events*.jsonl
sudo cat $EVENTS | jq -c -f flatten.jq > /tmp/events-flat.jsonl
```

Per-session tool-decision counts (accepted, rejected, by decision source, by tool), from
`tool_decision` events, which fire for every tool, built-in and MCP:

```sh
jq -s -r -f tool-decisions.jq /tmp/events-flat.jsonl | column -t -s $'\t'
jq -s -r --arg since 2026-10-01 -f tool-decisions.jq /tmp/events-flat.jsonl   # from a date (UTC)
```

`source` values (docs, "Tool decision event"): `config` (allow/deny rules, permission mode, session
grants, inherently safe tools), `hook` (a PreToolUse or PermissionRequest hook decided; this is how
refusals from guard hooks such as this kit's request-guard and action-guard show up),
`user_permanent`, `user_temporary` (a prompt answered yes), `user_abort`, `user_reject` (a prompt
answered no). Without `OTEL_LOG_TOOL_DETAILS=1`, MCP tools appear as the literal `mcp_tool`.

Exact cost and tokens per machine per day:

```sh
jq -s -r -f cost-per-day.jq /tmp/events-flat.jsonl | column -t -s $'\t'
```

Peak context per session and request count:

```sh
jq -s -r -f peak-context.jq /tmp/events-flat.jsonl | sort -k3 | column -t -s $'\t'
```

Compactions per session with pre/post sizes:

```sh
jq -r 'select(.["event.name"]=="compaction") | [.machine, .["session.id"][0:8], .["event.timestamp"], .trigger, .success, .pre_tokens, .post_tokens] | @tsv' /tmp/events-flat.jsonl
```

Tool result sizes by tool for one session (bytes; divide by about 3.7 for a rough token estimate):

```sh
jq -r --arg s SESSION_ID 'select(.["event.name"]=="tool_result" and .["session.id"]==$s)' /tmp/events-flat.jsonl \
| jq -s -r 'group_by(.tool_name) | .[] | [.[0].tool_name, length, (map(.tool_input_size_bytes // 0) | add), (map(.tool_result_size_bytes // 0) | add)] | @tsv'
```

Skill activations (user-defined skills show as `custom_skill` unless `OTEL_LOG_TOOL_DETAILS=1`):

```sh
jq -r 'select(.["event.name"]=="skill_activated") | [.machine, .["event.timestamp"], .["skill.name"], .invocation_trigger, .["skill.source"]] | @tsv' /tmp/events-flat.jsonl
```

Hooks registered per machine at session start (an inventory: is every guard hook you expect present
on every machine?):

```sh
jq -r 'select(.["event.name"]=="hook_registered") | [.machine, .["session.id"][0:8], .hook_event, .hook_type, .hook_source] | @tsv' /tmp/events-flat.jsonl | sort -u
```

API errors and exhausted retries:

```sh
jq -r 'select(.["event.name"]=="api_error" or .["event.name"]=="api_retries_exhausted") | [.machine, .["event.timestamp"], .["event.name"], .model, .status_code, .attempt // .total_attempts, .error] | @tsv' /tmp/events-flat.jsonl
```

All events of one prompt in order (the docs' `prompt.id` correlation; a hook's input carries the
same value as `prompt_id`):

```sh
jq -r --arg p PROMPT_ID 'select(.["prompt.id"]==$p) | [.["event.timestamp"], .["event.name"], .tool_name // .model // "", .decision // .success // ""] | @tsv' /tmp/events-flat.jsonl | sort
```

## What telemetry can replace, and what it cannot

Without telemetry, the usual way to answer "where did the tokens go" is to read the session
transcripts (`~/.claude/projects/<project>/<session>.jsonl`) on each machine. Telemetry gives most
of those numbers from one place, for all machines, within a minute, and adds things the transcripts
never had (cost in USD, the cache read/creation split, request durations, permission decision
sources, hook executions, API errors). It carries no text, so anything that needs the words of a
conversation stays with the transcripts.

| Question | Transcripts | Telemetry | Notes |
|---|---|---|---|
| Model, turns per session | yes | **yes** | `api_request.model`; turns = `user_prompt` events per `session.id` |
| Peak context per session (exact) | `usage` blocks | **yes** | `peak-context.jq`: max of input + cache read + cache creation over `api_request`; the same source numbers as the transcript's `usage` block |
| Output tokens per session | `usage` blocks | **yes** | `token_usage{type="output"}` or `api_request.output_tokens` |
| Cost per session, day, model, machine | estimate only | **yes** | `cost_usage` metric, `api_request.cost_usd`; an estimate per the docs |
| Compactions, with sizes | count only | **yes** | `compaction` events carry `pre_tokens` and `post_tokens` |
| Tool argument and result size by tool | estimated tokens | **approximately** | `tool_result.tool_input_size_bytes` and `tool_result_size_bytes` (bytes, not tokens) |
| Screenshot count and image tokens | image dimensions | **no** | no content; images are not distinguished in `tool_result` |
| Find sessions by a word | text search | **no** | nothing textual is exported |
| Thinking kept in context | visible content against `usage` | **no** | needs the size of visible content per turn |
| What the prompts asked for | prompt text | **no** by default | prompts are `<REDACTED>` unless `OTEL_LOG_USER_PROMPTS=1`, which this setup keeps off |
| Which files a session wrote | Edit/Write paths, shell command text | **only with `OTEL_LOG_TOOL_DETAILS=1`** | `tool_result.tool_input` then carries file paths and `bash_command` |
| When the last turn before idle happened | timestamps | **yes** | `event.timestamp` on `user_prompt` and `tool_result` |
| What the final reply said | assistant text | **no** | redacted unless `OTEL_LOG_ASSISTANT_RESPONSES=1` |
| Per-tool permission decisions and their sources | no | **yes** | `tool-decisions.jq`; the `hook` source shows guard-hook refusals |
| Which hooks, plugins and skills are live on each machine | manual check | **yes** | `hook_registered`, `plugin_loaded`, `skill_activated` |
| Which transcript holds this session | the file itself | **partly** | `session.id` + `message.uuid` by time and machine; the text is still in the transcript on that machine |
