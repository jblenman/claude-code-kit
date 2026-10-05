# Plugins and the marketplace: how they load, update and run

This repository is a Claude Code plugin marketplace: `.claude-plugin/marketplace.json` at the root
lists the plugins under `plugins/<name>/`. This page covers what you need to know to install,
update and debug them, and to write your own the same way. Official references:
[plugins/components](https://code.claude.com/docs/en/plugins/components) and
[plugins/loading](https://code.claude.com/docs/en/plugins/loading).

**Verified on:** Claude Code 2.1.289, macOS 26 (Apple silicon), 2026-10-04: in-place loading from
a local-path marketplace (session init event, isolated config directory), the hook environment and
the duplicate-hook behaviour (headless probe), `claude plugin validate` and `claude plugin details`
output. Claude Code 2.1.286, macOS and Windows 11 with Git Bash, 2026-10-04: the hook command chain
below, the full marketplace lifecycle, the settings-to-plugin cutover on three machines. Rules
quoted from the docs were checked on 2026-10-04.

## Layout

```
<repo>/
├── .claude-plugin/marketplace.json     # lists plugins: {"name": ..., "source": "./plugins/<name>", ...}
└── plugins/<name>/
    ├── .claude-plugin/plugin.json      # name, version, description, author (only the manifest lives here)
    ├── hooks/hooks.json                # hooks, same shape as the "hooks" key of settings.json
    ├── .mcp.json                       # MCP servers
    ├── monitors/monitors.json          # session-long background commands
    ├── skills/<skill>/SKILL.md
    ├── agents/<agent>.md
    └── scripts/ ...                    # anything the components call, referenced via ${CLAUDE_PLUGIN_ROOT}
```

- `name` in `plugin.json` = the directory name = the marketplace entry name.
- Components sit at the plugin root, never inside `.claude-plugin/`. Component paths in a manifest
  start with `./` and use forward slashes (a path with a backslash is rejected on macOS and Linux).
- A `CLAUDE.md` at the plugin root is **not** loaded (`claude plugin validate` warns). Ship context
  as a skill.
- List only plugins whose directory exists: an entry whose `source` is missing passes
  `claude plugin validate` but fails at install with `Source path does not exist`.

## In place or copied: it depends on how the marketplace was added

| Marketplace added from | Plugin loads from | An update is |
|---|---|---|
| a local path (`claude plugin marketplace add ~/claude-code-kit`) | **the clone itself** | `git pull`, then `/reload-plugins` or a new session. No version bump, no `claude plugin update`. |
| GitHub or another git URL (`claude plugin marketplace add https://github.com/jblenman/claude-code-kit.git`) | a **copy** under `~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/` | `claude plugin marketplace update claude-code-kit`, then `claude plugin update <plugin>@claude-code-kit`. A plugin that pins `version` in its manifest keeps everyone on the cached copy until the string changes. |
| `--plugin-dir <path>` (one session only) | the directory itself | nothing to update; it also silently replaces an installed plugin of the same name for that session |

Seen on 2.1.289: after `claude plugin install` from a local-path marketplace,
`~/.claude/plugins/installed_plugins.json` records an `installPath` under the cache (with the
repository's commit SHA), and a byte copy is written there; but the session loads the plugin from
the marketplace folder. That cache copy is an install-time artifact the loader does not use, and
the docs (plugins/loading, "In-place and copied plugins") say the same: edits take effect at the
next session start or `/reload-plugins`, with no version change.

**Files outside the plugin directory are not copied** for a git-URL install ("so when a script
inside a copied plugin reads a path above the plugin root, such as `../shared`, it doesn't find
them"). Every plugin in this kit is therefore self-contained: its scripts live inside it and are
reached through `${CLAUDE_PLUGIN_ROOT}`; no `../`, no symlinks (git on Windows writes them as text
files unless `core.symlinks` is on, and a symlink that leads outside the plugin is rejected as a
component path).

**How to see where a plugin loads from**, without a model call: start a headless session with a
model name that does not exist. Claude Code prints its init event (plugins, MCP servers, tools,
skills, agents) and then fails before any request is made:

```sh
claude -p ok --model no-such-model --output-format stream-json --verbose < /dev/null | head -n 1 \
  | python3 -c "import json,sys; [print(p['name'], p.get('path')) for p in json.load(sys.stdin)['plugins']]"
```

For an in-place plugin the path is your clone. The debug log says the same
(`claude --debug-file /tmp/cc.log`, then look for `Read hooks.json for plugin <name>`).

## hooks/hooks.json

The same shape as the `hooks` object in `settings.json`, plus an optional top-level
`description`, so a settings hook can be copied in unchanged:

```json
{
  "description": "what these hooks do",
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash|PowerShell|WebFetch",
        "hooks": [
          {
            "type": "command",
            "command": "\"${KIT_PYTHON:-python3}\" \"${CLAUDE_PLUGIN_ROOT}/scripts/guard.py\" hook || python3 \"${CLAUDE_PLUGIN_ROOT}/scripts/guard.py\" hook || py \"${CLAUDE_PLUGIN_ROOT}/scripts/guard.py\" hook || exit 1",
            "timeout": 60,
            "statusMessage": "guard: checking the call..."
          }
        ]
      }
    ]
  }
}
```

Plugin hooks are registered when the session loads the plugin and fire on their events from then
on; they do not wait for one of the plugin's skills to be used. They cost no context
(`claude plugin details` lists them as "harness-only").

## `${CLAUDE_PLUGIN_ROOT}`: substitution and quoting

- Claude Code **substitutes** `${CLAUDE_PLUGIN_ROOT}` (and `${CLAUDE_PLUGIN_DATA}`) into hook
  commands, MCP `command`/`args`/`env` and monitor commands before anything runs. On Windows the
  value arrives with forward slashes (`C:/Users/you/claude-code-kit/plugins/request-guard`).
- In a shell-form command (no `args`), **wrap it in double quotes** so a path with spaces stays one
  word. In exec form (`args`) and in `.mcp.json` `args`, no quotes are needed.
- It is also **exported** as an environment variable to hook processes and MCP servers (seen on
  2.1.289), but **not** to monitor processes (seen on 2.1.286): a monitor script finds its own
  location from its file path.
- For an in-place plugin it points at your clone; for a copied plugin at the version directory in
  the cache, which changes with every update. Keep state in `${CLAUDE_PLUGIN_DATA}` or your own
  directory under `~/.claude/`, never inside the plugin.

## The interpreter chain

Every Python hook in this kit is one shell-form command:

```
"${KIT_PYTHON:-python3}" "${CLAUDE_PLUGIN_ROOT}/scripts/x.py" ARGS || python3 "${CLAUDE_PLUGIN_ROOT}/scripts/x.py" ARGS || py "${CLAUDE_PLUGIN_ROOT}/scripts/x.py" ARGS || exit 1
```

| Piece | Why |
|---|---|
| `"${KIT_PYTHON:-python3}"` | An interpreter you choose in the `env` block of `~/.claude/settings.json`; plain `python3` when unset. Settings `env` reaches plugin hook commands (seen on 2.1.289: `KIT_PYTHON` from a settings file selected `/usr/bin/python3`; unset, `python3` from `PATH` ran). Needed on Windows, see [windows.md](windows.md). |
| `|| python3 ...` | Runs only when the first interpreter could not start (missing file, unset variable pointing nowhere). |
| `|| py ...` | Windows without `python3`: the `py` launcher. |
| `|| exit 1` | No interpreter at all: a visible, non-blocking hook error instead of a lockout ([hooks.md](hooks.md), "Exit codes"). |

This gives exactly one run per event only if **each script exits 0 whenever it started** (it turns
its own exceptions into a log line and exit 0). Measured on macOS: with `KIT_PYTHON` set, unset, or
pointing at a missing file, the hook ran once each time. On Windows the `python3` branch fails with
`command not found` (or the Store alias exits 9009) and `py` runs it once.

MCP servers use the same variable in `.mcp.json`: `"command": "${KIT_PYTHON:-python3}"`.

## A hook in settings and in a plugin runs twice

The docs: "If you define the same handler in more than one settings file, it runs once. A
plugin's or skill's copy of the same handler stays separate." Seen on 2.1.289: one Bash call, the
same hook defined in a settings file and in a plugin, byte-identical after substitution: **two
runs** in the same millisecond. A guard that books requests books each one twice; a Stop hook
evaluates twice.

### Moving a hook from settings to a plugin

1. Run the plugin's tests; install the plugin. The settings hook still runs alongside it, so for a
   moment both run.
2. Prove the plugin's copy runs (its README has a copy-paste proof; a scratch state directory keeps
   the proof out of your real logs). Two log lines for one event means both copies ran, which is
   expected at this step.
3. Remove the settings hook (the old installer's `--uninstall`, or delete the entry by hand after a
   backup).
4. **Run `/reload-plugins` in every open session**, or do the cutover with no session open. The
   settings watcher removes the settings hook from running sessions at once, but a running session
   only gets the plugin's hooks at its next start or `/reload-plugins`, so it runs unguarded in
   between.
5. Check one more event: a single log line.

Rollback: put the settings hook back, then `claude plugin disable <plugin>@claude-code-kit --scope
user` (or `uninstall`). Run that from a directory other than your home folder or pass
`--scope user` explicitly: from `$HOME` itself, `claude plugin disable` reported scope "project",
because `~/.claude/settings.json` doubles as the home folder's project settings. Never leave both on.

## What a plugin costs in context

`claude plugin details <name>` (add `--plugin-dir ./plugins/<name>` for one that is not installed)
prints the inventory and a projected token cost. On 2.1.289, for a small example plugin with one
skill, one agent, two hooks and one MCP server:

```
Component inventory
  Skills (1)  hello
  Agents (1)  reader
  Hooks (2)  PreToolUse, Stop  (harness-only — no model context cost)
  MCP servers (1)  probe  (tool schemas resolved at runtime; not counted)
  LSP servers (0)

Projected token cost
  Always-on:   ~67 tok   added to every session
```

Hooks and monitors add nothing to the prompt (monitors are not listed at all; each line a monitor
prints costs one model turn when it arrives). Skill and agent descriptions are always-on; their
bodies are paid on invocation.

## What `claude plugin validate` catches, and what it misses

`claude plugin validate <path>` checks a plugin or a marketplace; `--strict` turns warnings into
errors (exit 1) and is what this repository's tests use. Seen on 2.1.289:

| Mistake | Result |
|---|---|
| `"hooks": "../elsewhere/hooks.json"` in `plugin.json` | **error** (must start with `./`; `..` is a path traversal) |
| unknown field in `plugin.json` | warning: "Unknown field 'foo'. Claude Code ignores it at load time." |
| misspelled hook event (`PreToolUSE`) | warning: "unknown hook event; entry ignored at runtime" |
| `CLAUDE.md` at the plugin root | warning: not loaded as project context |
| marketplace without a `description` | warning (so `--strict` fails) |
| misspelled handler field (`"timeot": 5`) | not reported |
| a symlink under `scripts/` that points outside the plugin | not reported |
| a hook command whose script path does not exist | not reported (the first run shows a non-blocking hook error) |

Agent files have their own gaps; see [subagents.md](subagents.md). `claude plugin validate` takes a
directory or a manifest: pointed at a single `.md` file it tries to parse it as JSON.

## Running sessions: `/reload-plugins` or a restart?

| Change | Picked up by |
|---|---|
| hooks (plugin `hooks.json`, or a plugin enabled/disabled) | `/reload-plugins` or a new session |
| MCP servers | `/reload-plugins` (a server whose configuration did not change keeps its connection; in a session without an interactive terminal, plugin MCP changes wait for the next session) |
| skills (`SKILL.md` text) | live, within the session |
| monitors | **a new session**. A disabled plugin's running monitors keep running until the session ends; `/reload-plugins` did not start a second instance (2.1.286), though the docs allow that it may, so guard against duplicates ([monitors.md](monitors.md)) |
| agent definitions | live for edits in an existing agents folder; a folder created after the session started needs a restart |

## What an install writes

- `~/.claude/settings.json`: `extraKnownMarketplaces.<marketplace>` (for a local path:
  `{"source": {"source": "directory", "path": "<clone>"}}`) and
  `enabledPlugins["<plugin>@<marketplace>"] = true`.
- `~/.claude/plugins/known_marketplaces.json`: the clone as `installLocation` (local path), or the
  clone under `~/.claude/plugins/marketplaces/<name>/` (git URL).
- `~/.claude/plugins/installed_plugins.json`: the install record with `installPath`, `version` and
  `gitCommitSha`.

Nothing machine-specific beyond that, which is why the kit's hooks carry no absolute paths.

**Trying an install without touching your setup:** `CLAUDE_CONFIG_DIR=<scratch dir>` gives Claude
Code a separate profile (settings, plugins, records). The whole lifecycle (marketplace add, install,
details, update, uninstall, marketplace remove) runs there and leaves your real files byte-identical.
A session under that profile is not signed in (credentials follow the config directory), so use the
init-event check above, which needs no sign-in, or `--plugin-dir` in your normal profile.
