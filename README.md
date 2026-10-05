# claude-code-kit

A Claude Code plugin marketplace: guard hooks, tool plugins with their skills inside, research
subagents, an inbox monitor, an MCP server template, an OpenTelemetry setup, and documentation of
the Claude Code mechanics they rely on. Everything is stdlib Python 3.8+ and shell; nothing needs a
package install to run.

Each plugin README says what the plugin does, how to install it (marketplace or by hand), how to
configure it, how to prove it works with one copy-paste command, what it cannot see or do, and how
to roll it back. The pages in [`docs/`](docs/) say which Claude Code version and operating system
each behaviour was checked on.

## Plugins

| Plugin | What it does | Components |
|---|---|---|
| [`request-guard`](plugins/request-guard/README.md) | Counts and spaces every request a session or subagent sends to a public website; refuses loops, bursts and unreadable targets; backs off when a site answers 403, 429 or 503. Includes a paced fetcher for batches. | hooks, skill |
| [`session-guard`](plugins/session-guard/README.md) | Keeps a session-notes file current: a tool-using turn cannot end once while the notes are stale or missing; records compactions. | hooks, skill |
| [`action-guard`](plugins/action-guard/README.md) | Refuses or asks about actions against one web application you name: writes, protected paths, form input and JavaScript in its browser tabs, listed MCP tools. Policy in a JSON file. | hook |
| [`video-digest`](plugins/video-digest/README.md) | Reads an online video without watching it: yt-dlp fetches audio and captions, whisper.cpp transcribes locally, the two transcripts are compared and numeric disagreements flagged, keyframes come with timestamps. | skill, scripts |
| [`docx-pages`](plugins/docx-pages/README.md) | Page counts for Word documents from a real LibreOffice render, with a page-limit check that never passes on an unknown count. Never edits the document. | skill, script |
| [`screenshot`](plugins/screenshot/README.md) | Screenshots and timed bursts of the screen, a monitor or the front window (macOS, Windows, Linux), longest edge capped at 1800 px; the images are read in a subagent so they stay out of the main context. | skill, scripts |
| [`call-recorder`](plugins/call-recorder/README.md) | Records a call on a Mac as one stereo file (your microphone left, the other party right) and transcribes each channel with whisper.cpp, so the speaker labels come from the wiring. macOS only. | skill, scripts |
| [`photos-find`](plugins/photos-find/README.md) | Finds a photo in the macOS Photos library by the words Live Text found in it, optionally within dates, from Photos' own search index, read-only. | skill, script |
| [`session-forensics`](plugins/session-forensics/README.md) | Finds which session worked on something, shows where a session's context and tokens went (tool output, screenshots, thinking), and checks whether sessions keep a notes file and memory current unasked. Read-only, from the local transcripts. | skill, scripts |
| [`research-agents`](plugins/research-agents/README.md) | Subagents for paced web research, claim-by-claim verification of a written document, read-only browser reading and image/PDF reading. | agents |
| [`inbox-monitor`](plugins/inbox-monitor/README.md) | A plugin monitor that streams outside events into an interactive session, one notification per event (messages from a push-topic server, optionally signature-checked, or new lines of a local file); the model acts on them even while idle. | monitor |

Also in the repository:

| Folder | Contents |
|---|---|
| [`scripts/`](scripts/) | `link-assets.py` (link skills, agents and rules from a repository into `~/.claude`), `setup_check.py` (check a machine against a file of expectations), `cloud_launch.py` (start a cloud session from a script) |
| [`templates/`](templates/README.md) | A stdlib MCP stdio server skeleton with a self-test |
| [`telemetry/`](telemetry/README.md) | An OpenTelemetry collector for Claude Code's metrics and events on a Debian or Ubuntu host, client settings, PromQL and `jq` recipes |
| [`docs/`](docs/) | The mechanics below |

## Quick start

```sh
git clone https://github.com/jblenman/claude-code-kit ~/claude-code-kit
claude plugin marketplace add ~/claude-code-kit
claude plugin install request-guard@claude-code-kit
```

Then run the proof in the plugin's README. On Windows, set `KIT_PYTHON` in `~/.claude/settings.json`
before the install ([docs/windows.md](docs/windows.md)). The full order, including checks and
rollback, is [docs/setup.md](docs/setup.md).

Added from your clone, the plugins load in place: `git pull` and `/reload-plugins` (or a new
session) is the whole update. Added from GitHub (`claude plugin marketplace add
https://github.com/jblenman/claude-code-kit.git`), they are copied into Claude Code's plugin cache
and updated with `claude plugin marketplace update` and `claude plugin update`.

## Requirements

- Claude Code, kept current (`claude update`). Behaviour in these docs was checked on 2.1.283 to
  2.1.289.
- Python 3.8 or newer. Tests run on 3.9 and 3.13.
- macOS or Linux; Windows 11 with Git for Windows (hooks and monitors run under Git Bash).
- Some tool plugins need outside programs (for example ffmpeg, yt-dlp, whisper.cpp, LibreOffice);
  each README lists its own.

## Documentation

| Page | Covers |
|---|---|
| [setup.md](docs/setup.md) | Whole-kit setup order, the machine check, verification, rollback |
| [hooks.md](docs/hooks.md) | The hook contract: JSON in and out, deny vs ask, exit codes, timeouts, fail-open design, `|| exit 1` |
| [plugins.md](docs/plugins.md) | In-place vs copied plugins, `hooks.json`, `${CLAUDE_PLUGIN_ROOT}` quoting, the interpreter chain, hooks running twice, cutover, `plugin details` and `plugin validate`, reload vs restart |
| [mcp.md](docs/mcp.md) | stdio servers, the handshake on current CLIs, shutdown signals, tool-name prefixes, allow rules for headless calls, `--strict-mcp-config`, variables in `.mcp.json` |
| [monitors.md](docs/monitors.md) | Plugin monitors: per-line notifications, turns while idle, no restart, interactive only, environment |
| [subagents.md](docs/subagents.md) | Definition fields (memory, skills, omitClaudeMd, tools and MCP patterns), linked agent folders, what `plugin validate` misses |
| [windows.md](docs/windows.md) | Store/PyManager Python aliases that make hooks fail open, `KIT_PYTHON`, Git Bash, paths, backups |
| [cloud.md](docs/cloud.md) | Starting cloud sessions from scripts, follow-ups, teleport, AGENTS.md as the brief, ultrareview's size limit |

## Testing

```sh
claude plugin validate ~/claude-code-kit --strict          # the marketplace and its manifests
python3 plugins/request-guard/tests/test_request_guard.py  # each plugin with code has a tests/ folder
python3 telemetry/tests/test_telemetry.py
```

## Related repositories

- [`kb-mcp`](https://github.com/jblenman/kb-mcp): a local knowledge-base MCP server over folders
  of Markdown and text notes. Keyword search with optional local embeddings, results that point at
  a heading and line range, five tools (`kb_search`, `kb_get`, `kb_recent`, `kb_reindex`,
  `kb_status`). Stdlib Python.
- [`hivemind`](https://github.com/jblenman/hivemind): a router and coding assistant for local
  models served by Ollama; `hivemind_mcp.py` offers the same models to Claude Code as MCP tools.
  Local models suit drafts and checks whose result you verify.

## License

MIT, see [LICENSE](LICENSE).
