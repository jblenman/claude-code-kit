# Hooks: the contract a guard relies on

A hook is a command Claude Code runs at a point in a session: before a tool call (`PreToolUse`),
after it (`PostToolUse`), when the model finishes a turn (`Stop`), before compaction
(`PreCompact`), and so on. The guard plugins in this kit (`request-guard`, `session-guard`,
`action-guard`) are hooks. This page is the part of the contract they depend on, written down
with what was checked and where. The full reference is
[code.claude.com/docs/en/hooks](https://code.claude.com/docs/en/hooks).

**Verified on:** Claude Code 2.1.289, macOS 26 (Apple silicon), 2026-10-04: a headless session with
a probe plugin (deny reason reaching the model, the same hook in settings and in a plugin running
twice, settings `env` reaching the hook, the environment a plugin hook gets). Claude Code 2.1.283,
macOS, 2026-09-29: PreToolUse guards live, hooks firing for subagents. Windows 11 with Git for
Windows, Claude Code 2.1.286 to 2.1.289: shell-form hooks under Git Bash. Rules quoted from the
docs were checked against the hooks reference on 2026-10-04.

## The contract in one table

| Part | What happens |
|---|---|
| Input | One JSON object on **stdin**: `session_id`, `prompt_id`, `transcript_path`, `cwd`, `permission_mode`, `hook_event_name`, plus per-event fields. `PreToolUse` adds `tool_name`, `tool_input` (Bash: `{"command": ...}`, WebFetch: `{"url": ...}`, MCP tools: their arguments) and `tool_use_id`. Inside a subagent: `agent_id` and `agent_type`. |
| No opinion | Print nothing, exit 0. The normal permission flow decides. |
| Decision | Print **one JSON object** on stdout and exit 0. `PreToolUse` uses `hookSpecificOutput.permissionDecision`; `Stop`, `PreCompact`, `UserPromptSubmit`, `PostToolUse` use top-level `decision: "block"` with `reason`. |
| Notice to the user | Top-level `systemMessage` in the same JSON. Hooks have no terminal (`/dev/tty` is not available). A successful hook's stderr goes to the debug log only, and so does its stdout, except on `SessionStart` and `UserPromptSubmit` (and a few others), where plain stdout is added to the model's context. |
| Exit 2 | Blocks on events that can block, whatever the JSON says (even an `allow`). The reason is the JSON's blocking reason when there is one, otherwise stderr. |
| Any other non-zero exit | A non-blocking error: the action proceeds and the transcript shows `<hook> hook error` with the first line of stderr. A hook that cannot start (missing script, missing interpreter) lands here too. |
| Timeout | `timeout` in seconds; default 600 for command hooks (30 on `UserPromptSubmit`). A timed-out `PreToolUse` command hook does not block: the call continues through the permission flow. |
| Parallelism | All matching hooks run in parallel. For `PreToolUse` the most restrictive decision wins: deny, then defer, then ask, then allow. |

## PreToolUse: refusing, asking, staying silent

Refuse a call, exit 0:

```json
{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "request-guard: refused. ... Tell the user what you needed instead."}}
```

- **`deny`**: the call is cancelled and the reason goes to the model as the tool result. Seen on
  2.1.289: `PreToolUse:Bash hook error: <your reason>`, marked as an error, and the model quoted it.
  Write the reason for the model: say what was refused and what to do instead. A bare "denied"
  produces retries and workarounds.
- **`ask`**: the user gets the permission prompt, with your reason and a label saying where the
  hook came from (`[settings]`, `[plugin:<name>]`). In auto mode an `ask` still forces the prompt.
  In a `-p` run nobody can answer, so the call is denied and the model reads the reason.
- **`allow`**: skips the prompt. Deny and ask rules from settings still apply. **A guard that only
  objects should never print `allow`**; silence leaves the call to the user's own rules.
- **`defer`**: only for programs that drive `claude -p` and resume it later; ignored (with a
  warning) in interactive sessions.
- `updatedInput` next to the decision replaces the tool's arguments (for example, adding
  `--dry-run`). The guards in this kit do not use it.

**Matchers.** A matcher made only of letters, digits, `_`, `-`, spaces, `,` and `|` is an exact
name or a list of exact names: `Bash|PowerShell|WebFetch`. Anything else is an unanchored
JavaScript regular expression: `mcp__.*`. So `mcp__github` matches no tool (it is compared as an
exact name); write `mcp__github__.*`. MCP tools are named `mcp__<server>__<tool>`; tools of a
server bundled in a plugin are `mcp__plugin_<plugin>_<server>__<tool>` (see [mcp.md](mcp.md)). On
Windows the PowerShell tool is named `PowerShell`, so a shell guard matches both.

**Subagents.** Hooks from settings and plugins fire for subagent tool calls too, background agents
included; the input then carries `agent_id` and `agent_type`. One hook covers a whole fan-out.

**The `if` field** (one permission rule such as `Bash(git *)`) narrows when a handler runs, but the
docs call it best-effort: when Claude Code cannot tell which commands a Bash line runs, the hook
runs anyway. Use it to save process starts, not as the gate.

**A hook may wait.** A hook that sleeps holds the tool call back for that long (up to its
`timeout`). A request-spacing guard uses this: it books the next slot for a site and sleeps until
then, within a bound, or refuses with the time of the next free slot.

## Stop, PreCompact and the other blocking events

```json
{"decision": "block", "reason": "Update the notes file before you finish: ..."}
```

- On `Stop`, `reason` is what the model reads; the turn continues. The input carries
  `stop_hook_active` (true when the turn is already continuing because of a stop hook: return
  success then, or the hook loops) and `last_assistant_message`. Read the final text from that
  field, not from `transcript_path`, which is written asynchronously and can lag.
- Claude Code caps a run of continuations: after stop hooks have continued the turn eight times in
  a row it ends the turn anyway (the count resets whenever the model calls a tool;
  `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP` raises it). Design for one block per turn, not eight.
- `hookSpecificOutput.additionalContext` on `Stop` continues the turn as feedback, without the
  error styling of a block.
- `PreCompact` can block both manual and automatic compaction. **Never block automatic
  compaction**: when it was triggered to recover from a context-limit error, blocking it makes the
  request fail. Bounce a manual `/compact` at most.

## Exit codes, and why every command ends in `|| exit 1`

Exit code 2 blocks. That sounds like the right way to refuse, but **Python itself exits with 2
when it cannot open the script** (`can't open file ... [Errno 2]`), and `argparse` exits with 2 on a
usage error. A hook whose script was moved, renamed, or is unreadable to the interpreter would
then block every matching call; on `UserPromptSubmit` it blocks every prompt, which locks the
session. So every command in this kit ends in `|| exit 1`: any non-zero exit becomes 1, a
non-blocking error. The consequence is that **decisions must travel as JSON on exit 0**, never as
exit 2.

With an interpreter fallback chain (the form the kit's plugins use, explained in
[plugins.md](plugins.md)):

```
"${KIT_PYTHON:-python3}" "${CLAUDE_PLUGIN_ROOT}/scripts/guard.py" hook || python3 "${CLAUDE_PLUGIN_ROOT}/scripts/guard.py" hook || py "${CLAUDE_PLUGIN_ROOT}/scripts/guard.py" hook || exit 1
```

the script must **exit 0 whenever it started**, including after an internal error (log it, print
nothing). Otherwise a crash in the first branch runs the guard a second time in the next one.

## Fail-open, on purpose

The guards in this kit **fail open**: an internal error, a timeout or an unreadable input lets the
call through and writes a log line. A guard that can wedge a session is worse than no guard, and
the hook runs on the critical path of every matching call. The price is that a broken guard is
silent, so make its state visible:

- Log every decision and every internal error to a file the guard owns, and give it a `status`
  command.
- Watch the first run after installing. A hook that cannot start shows a `<hook> hook error` notice
  with `Failed with non-blocking status code: ...` in the transcript, easy to miss in a busy session
  and invisible in a headless run. The debug log has the full stderr
  (`claude --debug-file <path>`, look for `hook_non_blocking_error` or `Permission denied`); read it
  after a headless test before trusting that the guard allowed something.
- `/hooks` in a session lists every configured hook and where it came from. With telemetry on,
  `hook_registered` events list them per machine ([../telemetry/queries.md](../telemetry/queries.md)).
- A guard is a guardrail against an agent's mistakes, not a security boundary. Anything that must
  be impossible belongs in the system's own permissions.

## Shell form, exec form, and which shell

- **Shell form** (no `args`): `command` goes to `sh -c` on macOS and Linux, **Git Bash on
  Windows**, PowerShell when Git Bash is not installed. The `shell` field (`"bash"` or
  `"powershell"`) forces one. Shell form is what makes `||` and `${VAR:-default}` work.
- **Exec form** (`args` present): `command` is spawned directly with `args`, no shell, no quoting.
  The kit does not use it for Python hooks: one `command` would have to be an interpreter name that
  exists on every machine, and `python3` is usually missing on Windows while `py` is missing on
  macOS.
- In shell form, double-quote every path placeholder: `"${CLAUDE_PLUGIN_ROOT}/scripts/guard.py"`.
- On Windows without Git Bash, the chain above does not parse in PowerShell, so no guard runs (a
  non-blocking error, so the session goes on unguarded). Install Git for Windows. See
  [windows.md](windows.md).

## The environment a hook runs in

- The hook inherits Claude Code's environment, **including the `env` block of `settings.json`**.
  Seen on 2.1.289: `KIT_PYTHON` set only in a `--settings` file selected the interpreter in the
  plugin's hook command and was visible inside the script.
- Plugin hooks also get `CLAUDE_PLUGIN_ROOT` and `CLAUDE_PLUGIN_DATA` as environment variables
  (the docs list both; `CLAUDE_PLUGIN_ROOT` was seen in the hook's environment on 2.1.289), and
  every hook gets `CLAUDE_PROJECT_DIR`. `$CLAUDE_EFFORT` holds the effort level.
- Claude Code removes the `OTEL_*` exporter variables from every subprocess it starts.
- The working directory is the session's current directory (`cwd` in the input follows `cd` and
  worktrees; `CLAUDE_PROJECT_DIR` does not).

## Where hooks live, and how often they run

| Source | Scope |
|---|---|
| `~/.claude/settings.json` | you, every project |
| `.claude/settings.json` / `.claude/settings.local.json` | one project (shared / not shared) |
| managed settings | organization-wide |
| a plugin's `hooks/hooks.json` | wherever the plugin is enabled |
| skill or subagent front matter | while that skill or subagent is active |

- Hook lists from different sources **add up**; nothing replaces anything.
- The same handler in more than one *settings file* runs once. **A plugin's copy stays separate:
  a hook that is both in `settings.json` and in a plugin runs twice.** Seen on 2.1.289 even with
  byte-identical commands after substitution: two runs, same millisecond, one with
  `CLAUDE_PLUGIN_ROOT` set and one without. For a rate limiter that halves the real limit; for a
  Stop hook it means two evaluations. Install a guard one way, never both
  ([plugins.md](plugins.md), "Moving a hook from settings to a plugin").
- Edits to hooks in settings files are picked up by the file watcher, no restart. Plugin hooks
  change at `/reload-plugins` or the next session.
- `"disableAllHooks": true` turns every non-managed hook off; managed `allowManagedHooksOnly`
  blocks user, project and plugin hooks. If a guard never fires on a managed machine, check these.

## Testing a hook

- Pipe a sample input through the script: `echo '{"hook_event_name": "PreToolUse", "tool_name":
  "Bash", "tool_input": {"command": "curl https://example.com"}}' | python3 guard.py hook`.
- Prove it in a real session with nothing installed: `claude --plugin-dir ./plugins/<name>`
  loads one plugin for that session only. Headless, give the guard a scratch state directory
  through an environment variable so the proof does not touch your real ledger.
- In `-p` runs, put the prompt **before** `--allowedTools`: that flag takes several values and
  swallows a prompt that follows it. A tool without an allow rule is refused by the permission
  system with "requires approval" or "you haven't granted it yet", which a small model may report
  as a hook refusal. Check the guard's own log, not the model's prose.
- `claude -p` waits up to 3 seconds for data on stdin when stdin is not a terminal; add
  `< /dev/null` in scripts.
- **Replay your own history.** Every tool call of past sessions is in the transcripts
  (`~/.claude/projects/<project>/<session>.jsonl` and `<session>/subagents/*.jsonl`, `tool_use`
  blocks). Running them through the hook's decision function shows false alarms and misses on real
  commands before anyone relies on the hook. One such replay of 2,744 calls found one miss and one
  class of false alarm.
- Cost of a stdlib Python hook on every Bash call: about 40 ms on an M1 laptop with heavy imports
  deferred.

## Facts a request guard needs

- **WebFetch goes out from your machine**, not from the model provider (tested with a listener on
  127.0.0.1 and on a LAN address, both received the TLS handshake). A guard on `WebFetch` sees real
  traffic.
- WebFetch's answer reaches `PostToolUse` as `tool_response: {bytes, code, codeText, result,
  durationMs, url}`. An HTTP 403 is an ordinary result (`code: 403`), not a `PostToolUseFailure`,
  so a back-off on 403/429/503 must read `code`.
- WebFetch upgrades `http` to `https` and refuses host names without a dot.
- What a hook cannot see: requests made by code (`python -c`, a test suite, a compiled program),
  redirects, clicks inside a browser, other machines. Say so in the guard's README.
