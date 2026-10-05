# templates

## `mcp-stdio-server.py`: an MCP server with nothing but the standard library

A skeleton for a Model Context Protocol server that Claude Code starts as a subprocess and talks to over stdin/stdout. Copy the file, change `SERVER_NAME`, replace the `echo` tool with yours, keep the protocol layer. Python 3.8+, no dependencies, about 300 lines.

What the protocol layer does:

- **Transport:** newline-delimited JSON-RPC 2.0 over stdin/stdout (the specification's "stdio" transport). It also tolerates `Content-Length` framing from other clients and answers in the same framing. Nothing but JSON-RPC is written to stdout; a stray `print()` corrupts the stream and the client drops the server, so logging goes elsewhere (below).
- **Methods:** `initialize`, `notifications/initialized`, `ping`, `tools/list`, `tools/call`. Anything else, including `resources/*`, `prompts/*` and the `server/discover` probe that newer clients send before `initialize`, is answered with JSON-RPC error `-32601` (method not found), which the client reads as "not supported" and carries on.
- **Protocol version negotiation:** the server echoes the version the client asks for when it is one of `2025-11-25`, `2025-06-18`, `2025-03-26`, `2024-11-05`, and offers `2025-03-26` to anything else. Claude Code 2.1.286 and later ask for `2025-11-25`; the negotiated version appears in Claude Code's connection log line.
- **Tool results:** a tool returns a value, which becomes one `text` content block (JSON-encoded unless it is already a string). A tool's own failure (`raise ToolError("...")`) comes back as a result with `isError: true`, which the model reads and can recover from; a bug in a tool is caught, logged with its traceback and reported the same way, so the server never dies on a tool. Unknown tool names and malformed `arguments` are protocol errors (`-32602`).
- **Logging:** stderr receives WARNING and above only, because Claude Code records every stderr line from an MCP server as `[ERROR] ... Server stderr` whatever its level (verified on CLI 2.1.289). `MCP_SERVER_LOG=<file>` adds a file log at INFO (rotated at 2 MB; `MCP_SERVER_DEBUG=1` for DEBUG).
- **Shutdown:** handlers for SIGINT, SIGTERM and SIGHUP end the loop at once.

### How Claude Code ends a stdio server

Measured on CLI 2.1.289 (macOS): when the session ends, Claude Code sends **SIGINT**, then **SIGTERM about 0.1 s later**, then **SIGKILL about 0.5 s after the first signal**, and it **never closes the server's stdin**. Consequences for a server:

- A server that only reads stdin to EOF never sees a shutdown and is killed. The template exits on the first signal (4 ms in the test below); the `stdin closed` path is kept for other clients.
- Nothing long-running belongs in the signal path: half a second is the whole budget, including flushing logs. Write state as you go.
- Python's default SIGINT behaviour (`KeyboardInterrupt` in a blocking `readline`) is enough on macOS and Linux; the template installs explicit handlers so SIGTERM and SIGHUP behave the same way. On Windows the handlers are installed where the platform allows and the process ends on the kill otherwise.

### Verify it

Offline, no client:

```
python3 templates/mcp-stdio-server.py --self-test
# self-test ok: example-server 0.1.0; protocol 2025-11-25, 2025-06-18, 2025-03-26, 2024-11-05; tools: echo
```

The self-test drives `handle()` through a handshake with the current and an unknown protocol version, the `initialized` notification, `ping`, `tools/list`, a good and a bad `tools/call`, an unknown tool, `server/discover`, a parse error, a batch, and a wrong `jsonrpc` field. One WARNING line on stderr (the deliberate bad call) is expected.

In a session, without touching your MCP configuration. Write a one-server config:

```json
{"mcpServers": {"example-server": {"command": "python3",
                                   "args": ["/absolute/path/to/templates/mcp-stdio-server.py"],
                                   "env": {"MCP_SERVER_LOG": "/tmp/example-server.log"}}}}
```

then run a headless turn that may call the tool:

```
claude -p 'Call the MCP tool echo (server example-server) with text "kit template works" and uppercase true, then reply with exactly the text the tool returned.' \
  --mcp-config mcp.json --strict-mcp-config --allowedTools mcp__example-server__echo --model haiku
# KIT TEMPLATE WORKS
```

Three flags matter:

- `--mcp-config <file>` adds the server for this session only.
- `--strict-mcp-config` makes that file the **only** MCP configuration for the session: servers from `~/.claude.json`, project `.mcp.json` files **and enabled plugins** are left out (verified on CLI 2.1.289 by counting a plugin server's start-up log lines: none with the flag, one without). Good for isolating a test; remember that a plugin-provided server is therefore absent in such a run.
- `--allowedTools mcp__example-server__echo`: a headless (`-p`) turn cannot answer a permission prompt, so an MCP tool must be allowed explicitly or the model reports "requires approval". The tool name is `mcp__<server key>__<tool>` for a server registered by config, and `mcp__plugin_<plugin>_<server>__<tool>` for a server that a plugin provides; permission rules and hook matchers must use the prefix of the path actually used. In interactive sessions the same rule belongs in `permissions.allow` of `settings.json`.

Expected server log (`MCP_SERVER_LOG`), from a real run on CLI 2.1.289:

```
INFO  example-server 0.1.0 starting (pid ..., python 3.13.7)
INFO  initialize from {"name": "claude-code", "title": "Claude Code", "version": "2.1.289", ...} requested 2025-11-25 -> 2025-11-25
INFO  client initialized
INFO  tools/call echo ok in 0.00s
INFO  signal 2; exiting
```

### Putting it in a plugin

A plugin ships the server inside its own directory (a plugin installed from a git URL is copied; files outside the plugin directory are not) and registers it in `.mcp.json` at the plugin root:

```json
{"mcpServers": {"example-server": {"command": "${KIT_PYTHON:-python3}",
                                   "args": ["${CLAUDE_PLUGIN_ROOT}/server/mcp-stdio-server.py"]}}}
```

`${CLAUDE_PLUGIN_ROOT}` is substituted by Claude Code; `${KIT_PYTHON:-python3}` lets the `env` block of `settings.json` choose the interpreter per machine (settings `env` reaches MCP server commands; on Windows set `KIT_PYTHON` to the path of a real `python.exe`, never a Store or PyManager alias). `claude plugin validate <plugin> --strict` checks the entry (CLI 2.1.281+).

### What the template does not do

No resources, prompts, sampling, progress notifications, server-to-client requests, or HTTP transport. Add capabilities to `handle_initialize` and branches to `handle_one` when you need them; the specification is at modelcontextprotocol.io.
