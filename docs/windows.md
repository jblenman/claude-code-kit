# Windows: interpreters, Git Bash, paths and backups

Everything in this kit runs on Windows, but three things differ enough to break a guard silently:
which Python a hook starts, which shell runs the hook, and how paths are written. This page is the
checklist.

**Verified on:** Windows 11 on two machines, 2026-10-04, Claude Code 2.1.286 to 2.1.289, Git for
Windows installed: one with the python.org installer (Python 3.14, the classic `py` launcher in
`C:\Windows`), one with the Store/PyManager Python (3.14, `py` and `python3` as app-execution
aliases). Both ran the same hook command chain and stdlib MCP servers as this kit under Git Bash,
once the interpreter variable was set. The monitor rules below come from the docs; a monitor was
not run on Windows.

## The interpreter pitfall: Store and PyManager aliases

A Store or PyManager Python puts 0-byte **app-execution aliases** for `py`, `python` and `python3`
in `%LOCALAPPDATA%\Microsoft\WindowsApps`. They work in a normal desktop shell, and fail elsewhere:

| Where | What happened |
|---|---|
| Another logon session (an SSH logon while a desktop session was using the same Python package) | `py`, `python` and `python3` answered `Permission denied` / "Access is denied" from the minute the desktop session started two MCP servers through `py`, although they had worked over SSH for half an hour before. |
| An elevated shell | `python` printed "Python was not found; run without arguments to install from the Microsoft Store...", while `py` from the same package worked. |
| Task Scheduler | The task failed with last result `0x80070002` (file not found): the service cannot start the 0-byte alias. |

**Consequence for hooks:** a hook command that starts with a refused alias exits non-zero, which
Claude Code treats as a non-blocking error, so **the guard fails open without a visible
refusal**. A request went out unguarded during a test this way. After any headless test, read
the debug log for `hook_non_blocking_error` or `Permission denied` before trusting that a guard
"allowed" something.

### Tell an alias from a real launcher

```powershell
where.exe py python python3
(Get-Item (where.exe py | Select-Object -First 1)).Attributes
```

- `ReparsePoint` and a path under `...\AppData\Local\Microsoft\WindowsApps\` = an alias. Do not put
  it in a hook or MCP command.
- `C:\Windows\py.exe` with `Archive` = the classic launcher from the python.org installer, a real
  file. Safe, and it follows Python upgrades.

### The fix: `KIT_PYTHON`

Every hook, monitor and MCP command in the kit starts with `"${KIT_PYTHON:-python3}"`. Set the
variable in the `env` block of `~/.claude/settings.json` (Claude Code passes that block to hooks,
monitors and MCP servers):

```json
{
  "env": {
    "KIT_PYTHON": "C:/Users/you/AppData/Local/Python/bin/python.exe"
  }
}
```

| Your Python | `KIT_PYTHON` |
|---|---|
| Store / PyManager (`py` is an alias) | the PyManager launcher `C:/Users/<you>/AppData/Local/Python/bin/python.exe`: a regular file that follows the default runtime across upgrades |
| python.org installer (`py` is `C:\Windows\py.exe`) | `C:/Windows/py.exe` (or `py`) |

Never a versioned path such as `...\Python314\python.exe` (it breaks at the next upgrade), and
forward slashes. `scripts/setup_check.py` with `setup-check.example.json` reports a missing
`KIT_PYTHON` on Windows. Without the variable the chain still falls back to `python3` and then
`py`, which works on a machine where those are real files and fails open where they are refused
aliases.

## Git Bash runs the hooks and monitors

- Shell-form hook commands run under **Git Bash** when Git for Windows is installed, and under
  PowerShell otherwise. The kit's commands (`"${KIT_PYTHON:-python3}" ... || ... || exit 1`) are
  `sh` syntax: under PowerShell they do not parse, no guard runs, and the session continues
  unguarded. **Install Git for Windows.** If it is installed somewhere unusual, point
  `CLAUDE_CODE_GIT_BASH_PATH` at its `bash.exe`.
- Monitors need Git Bash too: without it the Monitor tool is not available and plugin monitors do
  not start.
- Under Git Bash, `python3` is usually absent, so `sh` prints `command not found` (to the debug log
  only) and the chain moves on; the Store alias for `python3` exits 9009 instead. Either way one
  branch runs the script once.
- Git Bash's `$HOME` follows `HOMEDRIVE`/`HOMEPATH`, which on managed machines can be a network
  drive, while Claude Code's `~` follows `%USERPROFILE%`. In anything that runs under Git Bash,
  write full paths instead of `$HOME/...`.

## Paths

- Use **forward slashes** in settings, hook commands and arguments: `C:/Users/you/...` works in Git
  Bash, PowerShell and Python alike.
- `${CLAUDE_PLUGIN_ROOT}` arrives with forward slashes (`C:/Users/you/claude-code-kit/plugins/...`).
- Component paths inside a plugin manifest must use forward slashes; a path with a backslash is
  rejected on macOS and Linux, so a plugin written that way works on Windows only.
- `claude plugin marketplace add C:\Users\you\claude-code-kit` is accepted (`.\`, `..\` and drive
  forms); Claude Code stores the path as given.
- Symlinks: Windows git writes them as plain text files unless `core.symlinks` is on, which is one
  reason the plugins contain none. For skills and agents linked into `~/.claude`, a directory
  junction works without admin rights (`New-Item -ItemType Junction`) and is picked up by running
  sessions. Remove a junction with `cmd /c rmdir <path>`, never `Remove-Item -Recurse`, which
  deletes the target's contents. A refused *file* symlink becomes a copy, which does not follow
  `git pull`: re-run `scripts/link-assets.py` after pulling.

## Backups and settings files

- Back up `~/.claude/settings.json` before any edit: `Copy-Item $HOME\.claude\settings.json
  "$HOME\.claude\settings.json.bak-$(Get-Date -Format yyyyMMdd-HHmmss)"`. The kit's scripts that
  write settings make a timestamped backup first and leave every other key alone.
- Check the JSON after editing: `py -c "import json; json.load(open(r'C:\Users\you\.claude\settings.json', encoding='utf-8'))"`.
  `claude -p` **silently ignores a settings file that fails validation** (its `--help` says so), so a
  broken file means headless runs without your hooks, permissions or `env`.
- Write settings as UTF-8 **without** a byte-order mark. Windows PowerShell 5.1's
  `Set-Content -Encoding UTF8` adds one; use PowerShell 7, an editor, or Python.

## Other Windows behaviour that bites scripts

- `os.kill(pid, 0)` does not test a process on Windows: it terminates it. Use `OpenProcess` and
  `GetExitCodeProcess` through `ctypes`.
- Windows reuses PIDs quickly: a lock file that records a PID should also record when it was
  taken, and treat a process younger than the lock as a different process.
- The console code page is usually CP1252. Open files with `encoding="utf-8"`, pass `encoding=` to
  `subprocess.run`, and set `PYTHONIOENCODING=utf-8` (and `PYTHONUTF8=1`) for MCP servers that
  print non-ASCII.
- An `npx`-based MCP server needs `cmd /c npx ...` in a manual registration.
- Windows PowerShell 5.1 started in a folder whose name contains `[` or `]` reports its own program
  folder as the current location; scripts that write "into the current folder" should use
  `$PSScriptRoot` and refuse targets under `$env:windir`.

## Keeping the login alive on a machine nobody uses

A machine that runs only background or headless work can find its Claude Code login expired the
next time it is needed (an idle machine does not refresh it). A daily headless call keeps it
fresh; register it once (PowerShell, elevated or not):

```powershell
$log = "$env:LOCALAPPDATA\claude-keepalive.log"
$taskArgs = "/c claude -p ok --model haiku --max-turns 1 >> `"$log`" 2>&1"
$action = New-ScheduledTaskAction -Execute cmd.exe -Argument $taskArgs
$trigger = New-ScheduledTaskTrigger -Daily -At 10:07
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName ClaudeLoginKeepalive -Action $action -Trigger $trigger -Settings $settings -Force
```

- If a Stop hook should skip unattended runs, add its off switch to the command; for this kit's
  `session-guard`: `/c set CLAUDE_SESSION_GUARD=off&& claude -p ok ...`.
- Do not call the variable `$args`: it is PowerShell's automatic argument array, and assigning a
  string to it fails with "Cannot convert value to type System.String".
- Test with `Start-ScheduledTask -TaskName ClaudeLoginKeepalive`, then `Get-ScheduledTaskInfo`
  (`LastTaskResult` 0) and the last line of the log.
- Task Scheduler's defaults stop a task after 72 hours and refuse to start or keep it on battery;
  the settings line above sets both explicitly. Use the same settings for any long-running task.
