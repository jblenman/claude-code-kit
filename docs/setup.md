# Setting up the kit on a machine

The order below matters in two places: on Windows the interpreter variable comes before any
plugin with Python hooks, and a guard that already runs from `settings.json` must be removed there
once its plugin is proven (a hook in both places runs twice). Everything else is one command per
step.

**Verified on:** Claude Code 2.1.289, macOS 26 (Apple silicon), 2026-10-04: marketplace add and
install from a local path in an isolated profile, plugin loading in place (session init event),
`claude plugin validate`. Windows 11 with Git for Windows, Claude Code 2.1.286 to 2.1.289,
2026-10-04: the same plugin layout, interpreter chain and cutover.

## Before you start

- A current Claude Code (`claude update`; `claude --version`). The pages in `docs/` say which
  version each behaviour was seen on.
- Python 3.8 or newer, stdlib only: `python3` on macOS and Linux, a real `python.exe` on Windows
  ([windows.md](windows.md)).
- Git. On Windows, Git for Windows: hooks and monitors run under its Git Bash.

## 1. Clone

```sh
git clone https://github.com/jblenman/claude-code-kit ~/claude-code-kit
```

(Windows PowerShell: `git clone https://github.com/jblenman/claude-code-kit "$HOME\claude-code-kit"`.)

## 2. Windows only: choose the interpreter

Add `KIT_PYTHON` to the `env` block of `~/.claude/settings.json` **before** installing plugins with
Python hooks; without it a hook that starts a refused Store/PyManager alias fails open. Back the
file up first.

```json
{ "env": { "KIT_PYTHON": "C:/Users/you/AppData/Local/Python/bin/python.exe" } }
```

Which path to use: [windows.md](windows.md), "The fix: `KIT_PYTHON`". On macOS and Linux leave it
unset (`python3` is used) or point it at a specific interpreter.

## 3. Add the marketplace

```sh
claude plugin marketplace add ~/claude-code-kit          # from your clone (recommended)
# or: claude plugin marketplace add https://github.com/jblenman/claude-code-kit.git
```

| Added from | Plugins load from | Update with |
|---|---|---|
| your clone | the clone, in place | `git pull`, then `/reload-plugins` or a new session |
| GitHub | a copy in `~/.claude/plugins/cache/` | `claude plugin marketplace update claude-code-kit`, then `claude plugin update <plugin>@claude-code-kit` |

Details: [plugins.md](plugins.md).

## 4. Install the plugins you want

User scope is the default, which is what you want for guards (they then cover every project):

```sh
claude plugin install request-guard@claude-code-kit
claude plugin install session-guard@claude-code-kit
claude plugin list                                        # each: Status: enabled
```

The table in the [README](../README.md) lists every plugin. Each plugin's README covers its
configuration and defaults, what it cannot see or do, rollback, and **a copy-paste proof** that it
works.

**Already running one of these guards from `settings.json`?** Install the plugin, prove it, then
remove the settings copy and run `/reload-plugins` in every open session
([plugins.md](plugins.md), "Moving a hook from settings to a plugin").

## 5. Optional: skills, agents and rules that are not in a plugin

Plugins carry their own skills and agents. For a folder of your own (or a team's) with
`skills/<name>/SKILL.md`, `agents/*.md` and `rules/*.md`, link it into `~/.claude` so `git pull`
updates every machine:

```sh
python3 ~/claude-code-kit/scripts/link-assets.py ~/my-claude-assets --dry-run
python3 ~/claude-code-kit/scripts/link-assets.py ~/my-claude-assets
```

It never replaces an existing file or a link that points elsewhere, records what it made, and
`--uninstall` removes exactly that. On Windows a refused symlink becomes a junction (folders) or a
copy (files; re-run after pulling).

## 6. Check the machine

`scripts/setup_check.py` compares `settings.json` (and the plugin records) with a file of
expectations and changes nothing:

```sh
cp ~/claude-code-kit/scripts/setup-check.example.json ~/claude-setup-check.json   # keep the keys you care about
python3 ~/claude-code-kit/scripts/setup_check.py ~/claude-setup-check.json
```

It can require settings values, allow rules (and the absence of broad ones such as `Bash(*)`),
enabled plugins, known marketplaces, `KIT_PYTHON` on Windows, and that no hook a plugin now provides
is still in `settings.json`. Exit 0 = all checks pass.

## 7. Verify each plugin

- Run the proof in each installed plugin's README.
- `/hooks` in a session lists every hook and where it came from; `/mcp` lists MCP servers; the
  status line and `/tasks` show a running monitor.
- See what a session loads, from where, without a model call:

  ```sh
  claude -p ok --model no-such-model --output-format stream-json --verbose < /dev/null | head -n 1 > /tmp/init.json
  python3 -c "import json; d=json.load(open('/tmp/init.json')); print([(p['name'], p.get('path')) for p in d['plugins']]); print(d['mcp_servers'])"
  ```

  The first line of output is the session's init event (plugins with their paths, MCP servers,
  tools, skills, agents); the run then fails on the unknown model name before any request is made.
- Tests: plugins with code have a `tests/` folder (`python3 plugins/<name>/tests/test_*.py`), and
  `claude plugin validate ~/claude-code-kit --strict` checks the marketplace.

## 8. Sessions that were already open

A running session gets new or changed plugin hooks and MCP servers at `/reload-plugins`; new
skills appear by themselves; monitors and a newly created agents folder need a new session. Rules
and `CLAUDE.md` load at session start.

## Optional: telemetry

To see tokens and cost per machine, session and day without reading transcripts, set up the
collector in [`telemetry/`](../telemetry/README.md) and add its `env` block to each machine.

## Rollback

| To undo | Do |
|---|---|
| one plugin | `claude plugin disable <plugin>@claude-code-kit --scope user` (or `uninstall`). Run it from a directory other than your home folder, or keep `--scope user`: from `$HOME` itself the command took the scope as "project". |
| the marketplace | uninstall its plugins, then `claude plugin marketplace remove claude-code-kit` |
| linked assets | `python3 ~/claude-code-kit/scripts/link-assets.py ~/my-claude-assets --uninstall` |
| the interpreter variable | remove `KIT_PYTHON` from the `env` block |
| a guard you moved from settings | put the settings hook back first, then disable the plugin; never leave both on |
| telemetry | remove the `env` keys; [telemetry/README.md](../telemetry/README.md), "Rollback", for the collector |

Back up `~/.claude/settings.json` before editing it by hand; `claude -p` silently ignores a settings
file that fails validation, so a broken file means headless runs without your hooks.
