# scripts

Stand-alone helpers; each has its usage in its docstring (`python3 scripts/<name> --help`). Stdlib Python 3.8+.

| Script | What it does |
|---|---|
| `link-assets.py` | Link a repository's `skills/`, `agents/` and `rules/` into `~/.claude` (symlinks; a junction or a copy where Windows refuses a symlink), with a manifest so `--uninstall` removes exactly what it made and two linked repositories never prune each other. `--dry-run`, `--only skills,agents,rules`, `--name` for the agents folder, `--machine-file PATH` to link one machine's notes as `~/.claude/rules/machine.md`. Honours `CLAUDE_CONFIG_DIR` and `CLAUDE_SKILLS_DIR`. |
| `cloud_launch.py` | Start `claude --cloud "<task>"` from a context without a terminal (a tool call, cron, another agent): gives it a pseudo-terminal, answers the folder-trust dialog, waits for the session URL plus a settle time, ends the local client and prints the URL. macOS and Linux (uses `pty`). `--env KEY=VALUE` passes environment to the client, e.g. to switch a Stop hook off for the headless turn. |
| `setup_check.py` | Check `~/.claude/settings.json` against a JSON file of expectations: values, `permissions.allow` entries present or absent, enabled plugins, known marketplaces, settings hooks that must be absent because a plugin provides them (a hook in both runs twice per event), `env` keys per platform, files that must exist. Read-only; exit 1 on a failed check. `setup-check.example.json` is the starting point. |

Verified behaviour behind `link-assets.py`, from CLI 2.1.283-2.1.289: a symlinked skill folder, a symlinked agents folder and a symlinked rule file all load; a new skill appears in a running session within a minute, a `~/.claude/skills` folder that did not exist at start needs `/reload-skills`, a new `~/.claude/agents` folder needs a restart, rules load at session start. `claude plugin validate` does not read a linked folder: validate the real path.
