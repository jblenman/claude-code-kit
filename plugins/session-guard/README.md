# session-guard

A Claude Code plugin with **Stop and PreCompact hooks that keep a session-notes file fresh.** A turn that used tools cannot end while `~/.claude/session-notes.md` is more than 30 minutes stale, has not been written since the session's first turn, or is missing; the hook blocks the turn end once with a reason that tells the model exactly what to record. A compaction that ran while the file was stale forces an update at the next turn end, because the compaction summary may by then be the only record of what came before it. The `session-notes` skill tells the model what belongs in the file and when a project should get a notes file of its own.

Why: an instruction to "keep the notes current" is followed about half the time; measured over several weeks of sessions, the record was updated unprompted in roughly 50-60 % of wrap-ups, and replies mentioned the update in about 30 %. A hook that holds the turn end until the file is fresh closes the gap without costing model context (hooks are harness-only: `claude plugin details` reports `Always-on: ~0 tok`).

| File | Purpose |
|---|---|
| `hooks/hooks.json` | `Stop` (20 s timeout) and `PreCompact` (10 s). |
| `scripts/session_guard.py` | The hook: `stop` and `precompact` read Claude Code's hook JSON on stdin; `status` prints file age, thresholds, per-session state and the log tail. Stdlib, Python 3.8+. |
| `skills/session-notes/SKILL.md` | What to record, when, the file layout, and when to split a project out. |
| `tests/test_session_guard.py` | 37 checks: every scenario below as a subprocess against a fake transcript, the plugin shape, the interpreter chain, `claude plugin validate`. Pass real transcript paths as arguments to time the tail parser. |

State and log, never in git: `~/.claude/session-guard/<session>.json` (per-session latch, pruned after 30 days) and `guard.log`.

## Rules (defaults; environment overrides below)

- **Stop** blocks the turn end **once** (JSON `{"decision": "block", "reason": ...}` on stdout, exit 0) when the turn used tools and the file is (a) more than **30 min** stale, (b) not written since the session's first turn (a write within 5 minutes before the first turn counts as this session's), or (c) missing. The reason names the file, its age, the turn's tool calls, and what to write: tasks and status, decisions with their reasons, files touched, background work, open threads. A turn without tool calls is never blocked by (a)-(c).
- **After a compaction**, `precompact` records the event in the session state. If the file was more than **10 min** stale at that moment, the next Stop is blocked (tools or not) until the file is newer than the compaction. Auto-compaction itself is **never blocked**: a blocked compaction at the context ceiling can wedge a session.
- **Manual `/compact`** while the file is more than 30 min stale is bounced once with a message; a second `/compact` within 30 minutes proceeds (and is then recorded like any compaction).
- **Never loops.** `stop_hook_active` in the hook input plus a per-session latch keyed by `prompt_id` give at most one block per turn; the CLI's own consecutive-block cap is the backstop.
- **Fail-open.** Any internal error, a missing transcript or unreadable input gives no output and exit 0. It never blocks via exit code 2, because the hook command ends in `|| exit 1`, which would turn a 2 into a non-blocking 1.
- **Subagents never trigger it** (they fire `SubagentStop`, which is not configured).

How it decides "this turn used tools": it reads the **tail** of `transcript_path` (512 KB, growing fourfold until the current turn is found; about 0.03 s on a 50 MB transcript), finds the last real user prompt (not a tool result, not meta, not a compaction summary) and counts `tool_use` blocks after it. The transcript is written asynchronously, so the very last tool call of a turn can be missing; that only ever under-counts.

## Install

```
git clone https://github.com/jblenman/claude-code-kit ~/claude-code-kit
claude plugin marketplace add ~/claude-code-kit
claude plugin install session-guard@claude-code-kit
```

Session-only, nothing installed: `claude --plugin-dir ~/claude-code-kit/plugins/session-guard`. The plugin loads in place from the clone, so `git pull` plus the next session start (or `/reload-plugins`) is the whole update.

Manual, without the marketplace: copy the two entries from `hooks/hooks.json` into the `hooks` key of `~/.claude/settings.json`, replacing `${CLAUDE_PLUGIN_ROOT}` with the absolute plugin path (forward slashes on Windows too). **Never both:** a hook present in `settings.json` and in an enabled plugin runs twice per event (two Stop evaluations per turn).

**Interpreter.** `"${KIT_PYTHON:-python3}" "<script>" stop || python3 "<script>" stop || py "<script>" stop || exit 1`, run with `sh -c` on macOS and Linux and with Git Bash on Windows. Set `KIT_PYTHON` in the `env` block of `~/.claude/settings.json` when `python3` is not the interpreter you want; on Windows use the path of a real `python.exe` with forward slashes, never a Store or PyManager alias, which other logon sessions can be denied. The script exits 0 whenever it started, so the chain runs it exactly once.

## Configuration

Everything is environment, read by the hook process; set the variables in the `env` block of `~/.claude/settings.json` (reaches plugin hooks; verified on CLI 2.1.289) or in the shell that starts `claude`.

| Variable | Default | Meaning |
|---|---|---|
| `CLAUDE_SESSION_NOTES` | `~/.claude/session-notes.md` | The file to keep fresh. |
| `CLAUDE_SESSION_NOTES_STALE_MIN` | `30` | Staleness that blocks a tool-using turn and bounces a manual `/compact`. |
| `CLAUDE_SESSION_NOTES_COMPACT_FRESH_MIN` | `10` | Staleness at compaction time that forces an update at the next turn end. |
| `CLAUDE_SESSION_GUARD_STATE` | `~/.claude/session-guard` | State and log directory. |
| `CLAUDE_SESSION_GUARD` | unset | `off` disables the guard for that `claude` process: for headless or scripted runs that keep their own record. |

## Verify it works

1. Tests: `python3 plugins/session-guard/tests/test_session_guard.py` (Windows: `py ...`). Expect `37/37 passed`. Nothing under `~/.claude` is touched.
2. A scratch session proof (a stale scratch file, a headless turn that uses one tool):
   ```
   mkdir -p /tmp/sg-proof && echo "# notes" > /tmp/sg-proof/notes.md && touch -t 202601010000 /tmp/sg-proof/notes.md
   export CLAUDE_SESSION_NOTES=/tmp/sg-proof/notes.md CLAUDE_SESSION_GUARD_STATE=/tmp/sg-proof/state
   claude --plugin-dir ~/claude-code-kit/plugins/session-guard -p \
     'Run `ls /tmp/sg-proof` with the Bash tool, then answer in one line.' --allowedTools "Bash(ls *)" --model haiku
   cat /tmp/sg-proof/state/guard.log
   ```
   The log shows `stop ... BLOCK(stale)` for the first Stop, then the model's update of `/tmp/sg-proof/notes.md` (its mtime is now), then `stop ... allow: stop_hook_active` or `allow: already blocked this turn` for the second. Every tool the model needs for the update must be allowed in `-p` (add `--allowedTools Write Edit`), or auto mode; without that, the second Stop still passes (one block per turn) and the file stays stale, which the log also shows.
3. `python3 <plugin>/scripts/session_guard.py status` shows the file, its age against the thresholds, recent sessions (`blocks=`, `last=`) and the log tail.

Verified on CLI 2.1.289 (macOS, Python 3.13 and 3.9) with the proof above: `BLOCK(stale)`, the model rewrote the file and said so in its reply, the second Stop logged `allow: stop_hook_active`. One bounce costs about one extra turn of model work, which is why the skill says to update *before* the hook has to.

## What it cannot see

- **Content.** It checks the file's modification time, not what was written. A session that touches the file without recording anything passes; the skill and the reason text carry the content rule.
- **Several machines.** One file and one state directory per machine; nothing is shared across machines unless you point `CLAUDE_SESSION_NOTES` at a synced location yourself.
- **Subagents** (they do not fire `Stop`) and **headless runs started with `CLAUDE_SESSION_GUARD=off`**.
- **The last tool call of a turn** may be missing from the transcript when the hook runs (asynchronous write); a turn is then judged to have used fewer tools, never more.

## Rollback

`claude plugin disable session-guard@claude-code-kit --scope user` (or `uninstall`); run it from a directory other than `$HOME`. For one process: `CLAUDE_SESSION_GUARD=off`. `rm -r ~/.claude/session-guard` removes the state and log; the notes file is yours and stays.
