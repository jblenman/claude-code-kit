# Plugin monitors: a background command whose output starts a turn

A monitor is a command a plugin runs in the background for the whole session. Each line it prints
reaches the model as a notification, and the model acts on it right away, even when nobody has
typed anything. This kit's `inbox-monitor` plugin is one. The docs section is
[plugins/components, "Monitors"](https://code.claude.com/docs/en/plugins/components#monitors).

**Verified on:** Claude Code 2.1.286, macOS (Apple silicon), 2026-10-04: an interactive session
driven through a pseudo-terminal with a monitor plugin loaded by `--plugin-dir`, a message
published to the source it listened to, and the session's logs and output files read afterwards.
Rules quoted from the docs were checked on 2026-10-04 (Claude Code 2.1.289).

## The file

`monitors/monitors.json` at the plugin root (or the `experimental.monitors` manifest key):

```json
[
  {
    "name": "inbox",
    "command": "\"${KIT_PYTHON:-python3}\" \"${CLAUDE_PLUGIN_ROOT}/scripts/listen.py\" || python3 \"${CLAUDE_PLUGIN_ROOT}/scripts/listen.py\" || py \"${CLAUDE_PLUGIN_ROOT}/scripts/listen.py\"",
    "description": "Inbox"
  }
]
```

## What Claude Code does with it

| Behaviour | Observed |
|---|---|
| Start | About one second after the session starts, through the same login-shell wrapper the Bash tool uses (`/bin/zsh -c source <shell snapshot> ... && eval '<command>' < /dev/null` on macOS). So `||` works, your `PATH` applies, stdin is `/dev/null`, stdout is a pipe, the working directory is the session's. |
| Each stdout line | Becomes one notification, shown as `⏺ Monitor event: <line> · <description>` (cut to the terminal width in the UI, complete for the model). |
| While idle | **The model reacts at once**: a turn starts with no user prompt. Measured 4.5 to 7 s from publishing a message to the model's reply beginning. |
| Batching | Two lines written in one chunk arrived as one event; a line without its newline waits for it. A 3,000-character line arrived as one event. |
| Full output | Also kept in `<tmp>/<project>/<session>/tasks/<task-id>.output`. |
| Visibility | The status line shows `1 monitor` (`1 monitor still running` after a turn); `/tasks` lists it as `monitor · <description>`. |
| Exit | **Not restarted.** The model gets one `⏺ Monitor "<description>" stream ended` notification and the monitor is gone for the rest of the session. |
| `/reload-plugins` | Did not start a second instance (same process before and after). The docs allow that a reload may start monitors again, so guard against duplicates anyway. |
| `/exit` | Asks once: `Background work is running — The following will stop when you exit: monitor · <description> — 1. Exit and stop tasks / 2. Stay`. Enter confirms; the process was gone within 2 s. |
| Disabled mid-session | Already running monitors keep running until the session ends (docs). |

## Where monitors do not run

- **Never in `claude -p`** (docs: "plugin monitors start in an interactive session and never in
  non-interactive mode with the -p flag"). Headless automation needs its own way to read the source.
- Not where the Monitor tool is unavailable: some API providers (Bedrock, Agent Platform, Foundry)
  and telemetry settings that turn it off (`DISABLE_TELEMETRY`,
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`).
- Not on Windows without Git Bash (the Monitor tool needs it).
- A monitor command cannot use `${user_config.*}` (it does not start) and gets no
  `CLAUDE_PLUGIN_OPTION_<KEY>` variables.
- Configuration changes need a **new session**; a monitor reads its environment once.

## The environment a monitor gets

- **Present:** the `env` block of `settings.json` (a `--settings '{"env": {...}}'` value reached the
  script), `CLAUDE_CODE_SESSION_ID`, `CLAUDE_PID` (the `claude` process), `CLAUDECODE=1`.
- **Not exported:** `CLAUDE_PLUGIN_ROOT`. It is substituted into the command text, but the variable
  is not in the process environment (unlike hooks and MCP servers, which get it). A monitor script
  finds its own files from `__file__`.

## Writing a monitor script

1. **Print only event lines to stdout**, one per event, newline-terminated and flushed. Every line
   is a notification and a model turn. Diagnostics go to a log file.
2. **Never exit while the session lives.** Reconnect inside the script with a back-off (for
   example 5 s doubling to 60 s). An exit ends the monitor for good.
3. **One instance per session.** Keep a PID file keyed by `CLAUDE_CODE_SESSION_ID`; a second
   instance that finds a live first one idles silently.
4. **Exit when the session is gone.** Claude Code stops the monitor on `/exit`, but add a watchdog
   anyway: leave within a few seconds of `CLAUDE_PID` disappearing or of stdout closing.
5. **Keep lines short and single.** Join multi-line bodies (for example with ` | `) and cut very
   long ones; say in the line where the full text can be read.
6. **Make it switchable.** An environment variable that makes the script idle with no connection
   lets one session opt out without disabling the plugin.
7. Windows: never use `os.kill(pid, 0)` to test whether a process lives, because on Windows it
   terminates the process. Use `OpenProcess` plus `GetExitCodeProcess` through `ctypes`. Write
   output as UTF-8 bytes regardless of the console code page.

## Cost

`claude plugin details` does not list monitors, and a monitor adds nothing to the prompt (always-on
cost ~0). Each delivered line costs one model turn on whatever model the session runs, so a monitor
that prints chatter is expensive: filter at the source.

## Checking that a monitor runs

- Status line `· 1 monitor` right after start, or `/tasks`.
- The script's own log: a start line with the session id, one line per delivered event.
- A `stream ended` notification right after start means the script exited: read the last line of
  its log for the reason.
- Run the script alone in a terminal first; the lines it prints are exactly what the model will
  get.
