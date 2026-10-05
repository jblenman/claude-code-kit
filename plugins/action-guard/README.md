# action-guard

A Claude Code plugin whose `PreToolUse` hook **keeps an agent from changing a web application it works with.** Before every `Bash`, `PowerShell`, `WebFetch` and MCP tool call, the hook works out what the call would do to the application named in the policy and either lets it through, refuses it with a reason the model can read, or turns it into a permission prompt for you. The policy file says which application to protect and what counts as "changing" it. The shell reader, the hook contract and the fail-open plumbing are the same as request-guard's; this plugin decides on **method and path** instead of counting requests.

| The call would... | Decision |
|---|---|
| Read from the application (GET, HEAD, OPTIONS) | passes, no prompt |
| Write to it (POST, PUT, PATCH, DELETE) | **refused**, unless the path is in `write_ok_paths` |
| Touch a path in `denied_paths` (e.g. `/admin`) with any method | **refused** |
| Touch a path in `ask_paths` | permission prompt |
| Send a request from inline code or a script (the method cannot be read) | **refused** (`on_unreadable_method`) |
| Send a request whose target cannot be read (`curl "$URL"`, `xargs curl`) | permission prompt (`on_unknown_target`) |
| Send an unreadable number of requests to the application (a shell loop) | **refused** |
| Fill a form, run JavaScript or upload a file in a browser tab that is on the application | **refused** |
| Click or type in such a tab | permission prompt |
| Take a screenshot, read the page, find an element | passes |
| Call a tool of the application's own MCP server that is in `deny_tools` / `ask_tools` | refused / prompt |
| Anything else (other hosts, other tools) | no opinion: the normal permission flow applies |

A refusal cancels the call and hands the model a sentence saying what was refused and what to do instead (tell the user what change it wanted). It does not end the session. Deny beats ask beats silence when one call raises several.

| File | Purpose |
|---|---|
| `hooks/hooks.json` | One `PreToolUse` entry, matcher `Bash\|PowerShell\|WebFetch\|mcp__.*`, 20 s timeout. |
| `scripts/action_guard.py` | The hook (`hook`), a dry-run reader (`check "<command>"`), `status`. Stdlib, Python 3.8+. |
| `scripts/action_guard.json` | The example policy: protects `app.example.com`. Edit it, or override it from `~/.claude/action-guard/config.json`. |
| `tests/test_action_guard.py` | 25 tests: host and path matching, config merge, the shell reader (curl, wget, httpie, PowerShell cmdlets, inline code, loops, wrappers), the policy, the hook as a subprocess, tab memory, the plugin shape, `claude plugin validate`. |

State and log: `~/.claude/action-guard/` (`guard.log`, rotated at 512 KB; `tabs.json`, the tab-to-host memory).

## How Claude Code hooks work (the contract this relies on)

Checked against the hooks reference (`code.claude.com/docs/en/hooks`) on CLI 2.1.283-2.1.289.

- A `PreToolUse` entry names a **matcher** (a regular expression over tool names) and a shell **command**. Before any matching call, Claude Code runs the command with one JSON object on **stdin**: `tool_name`, `tool_input` (Bash: `{"command": ...}`; WebFetch: `{"url": ...}`; MCP tools: their arguments), `cwd`, `session_id`, `tool_use_id`, `permission_mode`, `hook_event_name`, and inside a subagent `agent_id` and `agent_type`.
- **Print nothing and exit 0** = no opinion; the normal permission flow follows.
- **Print** `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "..."}}` **and exit 0** = the call is cancelled and the reason is shown to the model. `"ask"` shows you the permission prompt with that reason. `"allow"` skips the prompt; never print it from a guard. A top-level `"systemMessage"` puts a line in the UI for the human.
- Exit code 2 also blocks, but the command is wrapped as `... || exit 1`, which would turn a 2 into a non-blocking 1; so decisions always travel as JSON.
- On a **timeout** or a non-zero exit other than 2, the call proceeds. The guard is written to **fail open**: any internal error is logged and the call goes through. A guard that can wedge a session is worse than none; the tests are what make this trade acceptable.
- Hooks fire for **subagents** too (their calls carry `agent_type`), and several matching hooks run in parallel with the **most restrictive decision winning** (deny > ask > allow), so this hook sits next to request-guard without coordination.
- Windows: hooks run under Git Bash when it is installed; the PowerShell tool is named `PowerShell`, so the matcher includes it.

## The three layers inside the script

1. **Reader** turns a tool call into `Request(url, method, via)` objects plus notes about what it could not read. For Bash/PowerShell it splits the command at unquoted `; | & newline ( )`, strips `sudo`/`env`/`time`/assignments, follows `bash -c`, `ssh host '...'`, `$(...)` and here-documents, substitutes variables assigned in the same command, and reads the options of curl, wget, httpie, `Invoke-WebRequest`/`Invoke-RestMethod`. For `python -c`, `node -e` and script files it can find the URLs but not the method, so those come back as `UNKNOWN`. For the browser tools it reads `navigate` targets and remembers which host each tab is on, so later `form_input`/`computer` calls on that tab can be judged. For any other MCP tool it records `(server, tool)`.
2. **Findings**: the requests, plus `uncounted` (loops, `xargs`, `wget -r`), `unknown_targets`, `browser` events and the `mcp` tool.
3. **Policy**: `policy()` in the script, about seventy lines, reads top to bottom, and is the only place that knows what "changing the application" means. This is the part you change.

Two design choices worth keeping: **the hook never sleeps or waits** (it is on the critical path of every matching call; a run takes a few hundredths of a second), and **every refusal says what to do instead**, because the model reads it and a bare "denied" produces retries and workarounds.

## Install

```
git clone https://github.com/jblenman/claude-code-kit ~/claude-code-kit
# edit ~/claude-code-kit/plugins/action-guard/scripts/action_guard.json, or write ~/.claude/action-guard/config.json
claude plugin marketplace add ~/claude-code-kit
claude plugin install action-guard@claude-code-kit
```

Session-only: `claude --plugin-dir ~/claude-code-kit/plugins/action-guard`. Manual, without the marketplace: copy the entry from `hooks/hooks.json` into the `hooks.PreToolUse` list of `~/.claude/settings.json` (or a project's `.claude/settings.json` to guard one project only), with `${CLAUDE_PLUGIN_ROOT}` replaced by the absolute plugin path. Never both: a hook in `settings.json` and in an enabled plugin runs twice per call.

Interpreter: `"${KIT_PYTHON:-python3}" ... || python3 ... || py ... || exit 1`; set `KIT_PYTHON` in the `env` block of `settings.json` when `python3` is not the right interpreter (Windows: the path of a real `python.exe`, forward slashes, never a Store or PyManager alias).

## Configuration

`scripts/action_guard.json` ships the example; `~/.claude/action-guard/config.json` (or `CLAUDE_ACTION_GUARD_CONFIG=<file>`) is read after it: `protected_hosts`, `denied_paths`, `ask_paths`, `write_ok_paths` and `mcp_rules` add up; every other value replaces. Put the whole policy there and leave the shipped file alone if you prefer.

| Key | Meaning |
|---|---|
| `protected_hosts` | Hosts the policy applies to, matched by suffix (`app.example.com` also covers `api.app.example.com`). Everything else is ignored by this guard. Empty = the guard does nothing. |
| `allowed_methods` | Methods that pass without a prompt (default `GET`, `HEAD`, `OPTIONS`). |
| `denied_paths` | Path prefixes refused with any method (`/admin` covers `/admin` and `/admin/...`; `/adm*` covers anything starting `/adm`). |
| `ask_paths` | Path prefixes that always prompt. |
| `write_ok_paths` | Path prefixes where any method is allowed (writes the agent is supposed to make). |
| `deny_browser_tools_on_protected_hosts` | Browser tools refused on a tab that is on the application (default `form_input`, `javascript_tool`, `file_upload`, `shortcuts_execute`). |
| `ask_browser_actions_on_protected_hosts` | `computer` actions that prompt on such a tab (clicks, typing, keys). Screenshots and reading always pass. |
| `mcp_rules` | Per MCP server (`mcp__<server>__<tool>`): `deny_tools` and `ask_tools`. |
| `on_unreadable_method` | `deny` (default), `ask` or `ignore` for a request to the application made from code. |
| `on_unknown_target` | `ask` (default), `deny` or `ignore` for a request whose host cannot be read. |

Environment: `CLAUDE_ACTION_GUARD=off` disables the guard for one `claude` process; `CLAUDE_ACTION_GUARD_STATE=<dir>` moves state and log.

## Verify it works

1. Tests: `python3 plugins/action-guard/tests/test_action_guard.py`. Expect `Ran 25 tests ... OK`. They use a scratch directory and their own policy; nothing under `~/.claude` is touched.
2. Dry-run the reader against commands you expect the agent to type:
   ```
   python3 plugins/action-guard/scripts/action_guard.py check "curl -X POST https://app.example.com/api/items -d '{}'" "curl https://app.example.com/api/items"
   ```
   Each line shows the requests found (method, URL, client) and the decision. Do this for anything you are unsure the reader understands (a CLI of your own, an unusual client): a client it does not know is invisible to it.
3. In a session (the example policy, nothing installed):
   ```
   export CLAUDE_ACTION_GUARD_STATE=/tmp/ag-proof
   claude --plugin-dir ~/claude-code-kit/plugins/action-guard -p \
     'Run exactly this shell command with the Bash tool and quote the tool result verbatim: curl -s -X POST https://app.example.com/api/items -d "{}"' \
     --allowedTools "Bash(curl *)" --model haiku
   grep DENY /tmp/ag-proof/guard.log
   ```
   The call is refused before it runs; the model reports `PreToolUse:Bash hook error: action-guard: refused. a POST request to app.example.com. Only GET, HEAD, OPTIONS requests are allowed ...`, and the log carries `DENY method POST https://app.example.com/api/items`. Verified on CLI 2.1.289 (macOS; tests on Python 3.13 and 3.9, earlier also on Windows 11 with Python 3.14).

## Adapting the policy

Everything below is a change to `policy()` or to the JSON; the reader and the plumbing stay.

- **Only in one project:** install the hook entry into the project's `.claude/settings.json` instead of enabling the plugin at user scope.
- **Stricter for subagents:** the hook input carries `agent_type` when a subagent made the call (`do_hook` already logs it as `who=`); pass it into `policy()` and turn every `ask` into a `deny` when `who != "main"`, since a subagent cannot answer a prompt usefully.
- **Different rules for different parts of the app:** add `"rules": [{"path": "/api/v1/orders", "methods": ["GET"], "decision": "ask"}, ...]` to the JSON and loop over it at the top of `policy()`; `path_matches()` and `Request.method` are all you need.
- **Rewrite a call instead of refusing it:** a PreToolUse hook may return `"updatedInput"` inside `hookSpecificOutput` with a modified `tool_input` (append `--dry-run`, add a header). Not used here.
- **The application's own MCP server:** if its tools take a target inside their arguments (an id, a path), read `tool_input` in `findings_for()` and give the policy something to judge.
- **Your own CLI that talks to the app** (`mycli deploy ...`): in `scan()`, add `elif name == "mycli": read_mycli(args, f, variables)` that appends `Request(...)` objects, or a plain `f.uncounted.append("mycli")` if it should always take the strict path.

## What it cannot see

- **The method inside code.** `python -c`, `node -e`, scripts, test suites, `npm run`, `make`, containers: the reader finds URLs written in them but not what is done with them, and finds nothing when the URL is built at run time. That is why requests from code to the application are refused by default and unreadable targets prompt. If the agent must run a test suite against the app, point the suite at a staging host that is not in `protected_hosts`.
- **What the server does with a request.** A GET that changes state on the server (`/api/delete?id=3` as a link) looks like a read. The guard sees the request as written; the application has to keep reads safe.
- **Redirects and second-order effects:** a request to another host that then calls your application, a webhook, a queued job.
- **Tabs the human navigated.** The tab-to-host memory is filled by the agent's own `navigate` calls; a tab you opened by hand is unknown until the agent navigates it, and a page that redirects to another host after loading is not seen.
- **Other clients and other machines.** Hooks run inside Claude Code only. Anything the agent can reach through a different tool (another MCP server, an SSH session to a host without the hook) is outside it, which is why the refusal text says not to route around it, and why the permission prompt remains the second line.
- **A determined adversary.** This is a guardrail against an agent's mistakes and drift, not a security boundary. Anything that must be impossible belongs in the application's own authorization (a read-only API token for the agent, a role without delete rights), with this hook in front of it to keep the session's behaviour sensible and its reasons visible.

## Rollback

`claude plugin disable action-guard@claude-code-kit --scope user` (or `uninstall`), from a directory other than `$HOME`. For one process: `CLAUDE_ACTION_GUARD=off`. `rm -r ~/.claude/action-guard` removes the log, the tab memory and your local policy together.
