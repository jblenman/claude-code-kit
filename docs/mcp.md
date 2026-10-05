# MCP servers: stdio, naming, permissions, isolation

An MCP server gives Claude tools from another program. The servers in this kit's ecosystem
(`kb-mcp`, the `hivemind` server, the stdio template in `templates/`) are local **stdio**
servers written in stdlib Python. This page is what was learned writing and testing them. The
official guide is [code.claude.com/docs/en/mcp](https://code.claude.com/docs/en/mcp).

**Verified on:** Claude Code 2.1.289, macOS 26 (Apple silicon), 2026-10-04, with a probe server
that logs every message and signal it receives (headless sessions, user settings excluded):
handshake, shutdown signals, tool names for both registration paths, the allow-rule requirement,
`--strict-mcp-config`, settings `env` in `.mcp.json`. Claude Code 2.1.286, macOS and Windows 11,
2026-10-04: protocol version, the stderr logging rule. Rules quoted from the docs were checked on
2026-10-04.

## A stdio server in brief

- Claude Code starts the server as a child process and talks **newline-delimited JSON-RPC 2.0**
  over its stdin and stdout: one JSON object per line, each way.
- **Nothing but JSON-RPC may go to stdout.** A stray `print` breaks the connection. Log to a file.
- **stderr is logged as an error.** Claude Code records every stderr line as
  `[ERROR] MCP server "<name>" Server stderr: ...` whatever its level, so send only warnings and
  errors there; a cheerful "ready" line on stderr looks like a failure in the logs.
- Methods a tools-only server needs: `initialize`, `notifications/initialized` (no reply),
  `ping`, `tools/list`, `tools/call`. Answer any other request with error `-32601` ("Method not
  found"); never answer a notification.
- The server's environment gets `CLAUDE_PROJECT_DIR` (the project root; seen on 2.1.289) and, for a
  plugin's server, `CLAUDE_PLUGIN_ROOT`.

## The handshake on current CLIs

Logged by the probe server on 2.1.289, in order:

```
server/discover            id "server-discover-probe-1", params {_meta}   -> answered -32601
initialize                 protocolVersion "2025-11-25"
notifications/initialized
tools/list
tools/call                 (when the model uses a tool)
```

- `server/discover` came first. The docs say Claude Code 2.1.285 and later ask stdio servers
  whether they support a newer protocol revision; this request fits that. A server that does not
  know the method answers `-32601` and the client continues with `initialize` (seen).
- `initialize` asked for **protocol 2025-11-25** (also on 2.1.286). Echo the requested version when
  you support it; otherwise answer with a version you do support (the kit's template accepts
  2025-11-25, 2025-06-18, 2025-03-26 and 2024-11-05). The debug log shows the result as
  `negotiatedProtocolVersion`.
- The docs describe two client runtimes and a newer revision (2026-07-28) that Claude Code
  negotiates with servers that support it; `MCP_PROTOCOL_NEGOTIATION` (`auto`/`legacy`) and
  `MCP_SDK_GENERATION` (`v1`/`v2`) choose. A server that implements only the subset above keeps
  working either way.

## Shutdown

Seen on 2.1.289 with two different servers: at the end of a session Claude Code sends the server
**SIGINT, then SIGTERM about 0.1 s later, then SIGKILL about 0.5 s after the first signal**. It does
**not** close stdin first.

So: treat the first SIGINT or SIGTERM as "exit now", ignore the later ones, and finish within a few
hundred milliseconds. Anything that must survive a kill (a lock file, an index being written)
needs a recovery path: write the owner's PID into a lock and let the next run take over a lock
whose PID is gone, and keep writes transactional.

## Registration paths and tool names

The path you register a server by decides its tool names, and permission rules, hook matchers and
subagent `tools` lists must use the name that is actually in effect.

| Registered by | Server shows as | Tool names | Seen on 2.1.289 |
|---|---|---|---|
| a plugin's `.mcp.json` (key `probe` in plugin `kitprobe`) | `plugin:kitprobe:probe` | `mcp__plugin_<plugin>_<server>__<tool>` | `mcp__plugin_kitprobe_probe__ping` |
| `claude mcp add --scope user <name> -- <command>` or `--mcp-config` | `<name>` | `mcp__<name>__<tool>` | `mcp__kitprobe__ping` |

Characters outside `A-Z a-z 0-9 _ -` in the plugin or server name become `_`. A hook matcher or an
allow rule written for one path never matches the other.

## Headless calls need an allow rule

In `claude -p` nobody can answer a permission prompt. Seen on 2.1.289 (permission mode `default`,
no rule): the model found the tool, called it, and got
`Claude requested permissions to use mcp__plugin_kitprobe_probe__ping, but you haven't granted it yet.`
With `--allowedTools mcp__plugin_kitprobe_probe` (a **server-level** rule: every tool of that
server) the same call returned the tool's text.

```sh
claude -p "Use the search tool to find ..." --allowedTools mcp__plugin_<plugin>_<server>
claude -p "..." --allowedTools mcp__<name>                      # manual registration
```

Put the prompt before `--allowedTools` (it takes several values and swallows what follows). For
interactive sessions, put the same rule in `permissions.allow` of `~/.claude/settings.json` if you
do not want a prompt for a read-only server.

## Testing in isolation

- `--strict-mcp-config` uses **only** the servers passed with `--mcp-config`. Seen on 2.1.289: with
  a plugin loaded through `--plugin-dir` and `--mcp-config` pointing at `{"mcpServers": {}}`, the
  session had **no** MCP servers: the plugin's server and the claude.ai connectors were both left
  out. Without the flag, a headless session also connects the claude.ai connectors of your account.
- One server, nothing else: `--strict-mcp-config --mcp-config server.json` with
  `{"mcpServers": {"<name>": {"command": "python3", "args": ["/abs/path/server.py"]}}}`.
- A plugin's server with nothing installed: `claude --plugin-dir ./plugins/<name>`.
- What a configuration loads, without a model call: run `claude -p ok --model no-such-model
  --output-format stream-json --verbose < /dev/null`. The first line (the init event) lists
  `mcp_servers` with their status and source, and `tools`; then the run fails on the model name
  before any request is made.
- A stdio server can be tested with no Claude Code at all: write `initialize`, `tools/list` and a
  `tools/call` line to its stdin and read the replies. The kit's template does this with
  `python3 templates/mcp-stdio-server.py --self-test` ([../templates/README.md](../templates/README.md)).

## Variables in `.mcp.json`

- `${VAR}` and `${VAR:-default}` expand in `command`, `args`, `env`, `url` and `headers`. An unset
  variable without a default stays as the literal text `${VAR}` (with a warning in
  `claude mcp list`).
- **The `env` block of `settings.json` feeds this expansion.** Seen on 2.1.289: `"command":
  "${KIT_PYTHON:-python3}"` started `/usr/bin/python3` when `KIT_PYTHON` was set only in a
  settings file, and `python3` from `PATH` when it was not. This is how the kit picks the
  interpreter per machine without editing the plugin ([windows.md](windows.md)).
- In a plugin, `${CLAUDE_PLUGIN_ROOT}`, `${CLAUDE_PLUGIN_DATA}` and `${CLAUDE_PROJECT_DIR}` are
  substituted directly; `args` elements need no quoting.
- In a remote server's `url` and `headers`, Claude Code reads credential variables
  (`ANTHROPIC_API_KEY` and similar) as empty on purpose. Copy a credential into a variable with your
  own name if a server needs it.

## Other behaviour worth knowing

- **Deferred tool schemas.** Seen on 2.1.289: before calling the probe's tool, the model first called
  `ToolSearch` to load the tool's schema, then the tool. Expect that extra step in transcripts; the
  tool schemas are not part of the always-on context (`claude plugin details` reports MCP tools as
  "resolved at runtime; not counted").
- `/reload-plugins` keeps a server connected when its configuration did not change, reconnects a
  changed one and disconnects a removed one.
- Windows: an `npx`-based server needs `cmd /c npx ...` in a manual registration; set
  `PYTHONIOENCODING=utf-8` and `PYTHONUTF8=1` in the server's `env` if it prints non-ASCII; pick
  the interpreter with `KIT_PYTHON` (a Store/PyManager `python3` alias can be refused, see
  [windows.md](windows.md)).
