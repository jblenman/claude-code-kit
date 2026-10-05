# Subagents: definition files that work

A subagent is a separate assistant with its own instructions and context window that the main
session delegates to. Definitions are Markdown files with YAML front matter. This kit's
`research-agents` plugin ships several; this page covers the fields they use, how definition
folders load, and what `claude plugin validate` does and does not catch. The official reference is
[code.claude.com/docs/en/sub-agents](https://code.claude.com/docs/en/sub-agents).

**Verified on:** Claude Code 2.1.289, macOS 26 (Apple silicon), 2026-10-04: the
`claude plugin validate` results below (an agents folder with deliberate mistakes, standalone and
inside a plugin). Claude Code 2.1.286, macOS, 2026-10-04: a symlinked agents folder and the fields
in the examples (agents listed in the session's init event). Field rules quoted from the docs were
checked on 2026-10-04.

## Where definitions live

| Location | Scope | Priority on a name clash |
|---|---|---|
| managed settings | organization | 1 (highest) |
| `--agents '<json>'` | this session | 2 |
| `.claude/agents/` (walked up from the working directory) | project | 3 |
| `~/.claude/agents/` | you, every project | 4 |
| a plugin's `agents/` | where the plugin is enabled | 5 |

- `.claude/agents/` and `~/.claude/agents/` are scanned **recursively**; identity comes only from
  the `name` field, not the folder. Keep names unique across the tree.
- In a plugin, subfolders become part of the name: `agents/review/security.md` in plugin
  `my-plugin` is `my-plugin:review:security`.
- **Symlinked folders load.** `~/.claude/agents/<name>` as a link to a folder elsewhere (a
  clone of a repository) is read like a real folder; the agents appeared in the session's init
  event on 2.1.286. On Windows a directory junction does the same where symlinks are refused. The
  kit's `scripts/link-assets.py` makes these links.
- **Timing.** Edits to an existing agents folder are picked up within seconds. An agents folder
  created after the session started needs a restart (the watcher only covers folders that existed
  at start).

## The fields that matter

Only `name` and `description` are required. Field names are exact and camelCase; **an unknown or
misspelled field is ignored without any message**.

| Field | What it does |
|---|---|
| `name` | Identifier; hooks receive it as `agent_type`. No `:` (reserved for plugin names). |
| `description` | When the main session should delegate. Write the trigger first. |
| `tools` | Allowlist, comma-separated or a YAML list. Omitted: inherits the session's tools. Accepts MCP server patterns: `mcp__<server>` or `mcp__<server>__*` grants every tool of that server. If nothing in the list resolves to a tool, the agent refuses to launch with an error naming the entries. |
| `disallowedTools` | Denylist, applied first. `mcp__<server>` removes a server's tools; `mcp__*` removes every MCP tool. An entry with a specifier (`Bash(git push *)`) still removes the **whole** tool. |
| `model` | `sonnet`, `opus`, `haiku`, `fable`, a full model id, or `inherit`. |
| `effort` | `low` to `max`; overrides the session's effort for this agent. |
| `memory` | `user` (`~/.claude/agent-memory/<name>/`), `project` (`.claude/agent-memory/<name>/`) or `local` (`.claude/agent-memory-local/<name>/`). The agent's prompt then includes the first 200 lines or 25 KB of `MEMORY.md` there, and Read/Write/Edit are enabled for that folder. Off when auto memory is off. |
| `skills` | Skills to **preload**: their full content is injected at start, not just the description. A skill with `disable-model-invocation: true` cannot be preloaded. Without the field the agent can still invoke skills through the Skill tool. |
| `omitClaudeMd` | `true` starts the agent without the user, project and local CLAUDE.md files (Claude Code 2.1.271 or later). For agents that get everything from the delegation prompt, it saves the whole instruction load. |
| `background` | `true` always runs it in the background. Background subagents get a reduced set of built-in tools (Read, Grep, Glob, Bash, Edit, Write, WebFetch, WebSearch, Skill, ... and every MCP tool). |
| `isolation` | `worktree` runs it in a temporary git worktree, removed if it changed nothing. |
| `maxTurns`, `color` | Turn limit; display color. |
| `permissionMode`, `hooks`, `mcpServers`, `initialPrompt` | Honoured in your own and project agent files, **ignored in plugin agents** (a plugin ships hooks and MCP servers at the plugin level instead). |

Tools never available to a subagent, whatever the list says: `AskUserQuestion`, `EnterPlanMode`,
`ScheduleWakeup`, `Workflow`, and `Agent` once the nesting limit is reached.

## Patterns the kit's research agents use

| Pattern | Front matter | Why |
|---|---|---|
| A checker that learns across runs | `memory: user`, `effort: xhigh` | Recurring mistakes and verdict counts survive between sessions. |
| A web researcher that must pace its requests | `skills: [request-guard:fetch-paced]`, `effort: high`, an explicit `tools` list | The pacing rules are in its context from the first step instead of being discovered late. A skill from a plugin is named `<plugin>:<skill>`; if it is missing (request-guard not installed), Claude Code skips it with a warning in the debug log and the agent still starts. |
| An image/PDF reader that returns text only | `model: sonnet`, `omitClaudeMd: true` | Pictures stay out of the caller's context; the reader needs no project instructions. |
| A browser reader that must not act | an explicit `tools` list plus `disallowedTools` naming the browser tools that type, upload, run JavaScript or submit | Read-only by construction, not by instruction. |

Names and exact lists: the plugin's README.

## What `claude plugin validate` catches, and what it misses

Run it on a folder (`claude plugin validate ~/.claude/agents`, or a plugin). Seen on 2.1.289 with
one deliberate mistake per file:

| Mistake | Reported? |
|---|---|
| YAML that does not parse (unterminated quote, broken indentation) | **error**: "YAML frontmatter failed to parse ... At runtime this agent loads with ..." |
| no `description` | warning |
| front matter not on the first line (blank line before `---`) | warning inside a plugin ("No frontmatter block found"); not reported for a standalone folder |
| no `name` | not reported (the docs say so too; such a file is skipped in user and project folders) |
| misspelled field (`omitClaudeMD`, `disallowed-tools`) | not reported |
| invalid values (`model: gpt-5`, `memory: everywhere`, `effort: extreme`, `isolation: container`, `color: magenta`) | not reported |
| misspelled tool names (`tools: Raed, Grpe`) | not reported (the agent refuses to launch later) |
| `name` containing `:` | not reported (the file is skipped at load) |
| a duplicated key | not reported |

Pointed at a single `.md` file instead of a folder, `validate` tries to parse it as JSON. So a clean
`validate` means "parses", not "does what you meant": check field names against the table above
and launch each agent once. In user and project folders a file with broken front matter is
**skipped silently** (only the debug log says why); in a plugin it still loads, under its file name,
with every field ignored.

## Headless and hooks

- Hooks from settings and plugins fire for a subagent's tool calls, with `agent_id` and
  `agent_type` in the input: a guard covers delegated work too ([hooks.md](hooks.md)).
- In `claude -p`, a subagent's Bash calls need the same `--allowedTools` rules as the main
  session's; a refused call comes back to the subagent as a permission error.
- `--agents '<json>'` (or a JSON file with `-p`) defines session-only agents for tests: each key is
  a name, `prompt` is the body, and the other fields are the front matter fields (`color` and
  `experimental` are ignored there).
