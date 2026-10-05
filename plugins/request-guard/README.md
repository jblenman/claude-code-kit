# request-guard

A Claude Code plugin that **counts and spaces every request a session or subagent sends to a public website**, through one ledger per machine. A `PreToolUse` hook on `Bash`, `PowerShell`, `WebFetch` and browser navigation books each request and waits for the site's turn; it refuses loops, bursts, unreadable targets and invented identifiers with a reason the model can act on. A `PostToolUse` hook on `WebFetch` starts a back-off when a site answers 403, 429 or 503. A paced fetcher handles batches, and the `fetch-paced` skill tells the model how to use it.

Why: an agent that researches on the web sends requests the way it reads files, in bursts and loops. One such run put about 60 requests into a small company's site within minutes (the site then blocked the address), and other requests carried an invented `User-Agent` with a project name in it. This plugin makes the pace mechanical instead of instruction-only, for the main session and every subagent it spawns.

| File | Purpose |
|---|---|
| `hooks/hooks.json` | Three hook entries: `PreToolUse` (hook), `PostToolUse` and `PostToolUseFailure` on `WebFetch` (result). |
| `scripts/request_guard.py` | The hook (`hook`, `result`), the paced fetcher (`fetch`, `wait`), and `check`, `status`, `grant`, `clear-backoff`, `help`. Importable: `request_guard.wait_turn(url)`, `request_guard.report(url, status)`. Stdlib, Python 3.8+. |
| `scripts/defaults.json` | Shipped limits and the list of known public services. |
| `skills/fetch-paced/SKILL.md` | The skill the model reads when more than one URL is needed. |
| `tests/test_request_guard.py` | 40 tests, about 150 command cases. No request leaves the machine (the fetcher is tested against a server on 127.0.0.1). |

Machine-local, never in git: `~/.claude/request-guard/config.json` (your additions to the defaults), `ledger.json` (bookings of the last 24 hours), `guard.log`.

## Limits (defaults; per site, per machine)

| Profile | Applies to | Apart | Per hour | Per day | Per call | Back-off |
|---|---|---|---|---|---|---|
| `default` | any public site not listed | 10 s | 20 | 50 | 1 | 60 min after 403, 429 or 503; 30 min after 3 missing pages in a row |
| `careful` | search engines, social networks and other account-bound sites | 20 s | 10 | 30 | 1 | 120 min, same triggers |
| `bulk` | public data services and large documentation sites (`hosts` in `defaults.json`) | 3 s | 300 | 1,500 | 3 | 15 min after 429 |
| `archive` | the Internet Archive | 6 s | 150 | 600 | 1 | 30 min after 403, 429 or 503 |
| `infra` | code hosts, package registries, the model provider | none | 600 | 5,000 | 20 | none |

- A **site** is the registrable domain (`www.example.com` and `api.example.com` are one site), or the listed suffix for listed hosts.
- **All public sites together:** 1,500 an hour (`infra` not counted). A backstop against a runaway, not a working limit.
- **Private and LAN addresses are never counted** (loopback, 10/8, 172.16/12, 192.168/16, 100.64/10, link-local, names without a dot, `.local`, `.internal`, `.test` and similar) unless the config lists the host.
- The `bulk` figures are conservative and **not taken from each service's terms**. Check a service's own limit before raising its figure.
- **A refusal is evidence.** When a listed service answers 429 at the listed pace, slow its profile down (tightening needs no one's approval; raising does). The `archive` profile exists because the Internet Archive answered 429 after about 27 requests sent 2 to 3 seconds apart.

## What the hook does

Matcher: `Bash|PowerShell|WebFetch|mcp__claude-in-chrome__navigate|mcp__claude-in-chrome__browser_batch`.

| Situation | Result |
|---|---|
| No web request in the call | Silent. About 40 ms. |
| A request whose site is free | Booked, passes silently. |
| Too soon after the last request to that site | The hook itself waits for the turn (up to `max_hook_wait_s`, 25 s), then passes. |
| The queue for the site is longer than that | Refused ("not yet"), nothing booked. |
| Hour, day or machine-wide limit reached | Refused, with the time the next request can go. |
| Site in back-off, or on the `deny` list | Refused. |
| Count cannot be read: a request inside a loop, `xargs`, `parallel`, `watch`, `find -exec`, recursive or mirror `wget`, a URL range, a URL list in a file, code or a script that sends requests in a loop | Refused, with the three ways to proceed (below). |
| Target cannot be read: `curl "$URL"` with the variable set elsewhere, `$(cat url.txt)`, a shell function's `$1` | Refused: write the URL out. |
| More requests to one site in one call than the profile allows | Refused. |
| An explicit `User-Agent` that is not a stock client or browser string; a `never_send` term in a header, a URL or a request body; a `sensitive_terms` term in a header | Refused. |
| A scanning tool (`nmap`, `nikto`, `gobuster` ...) aimed at a public address | Refused. |
| `request_guard.py grant` / `clear-backoff`, or a shell write to the guard's own files | Permission prompt (`ask`): only the user raises a limit. |

It refuses with JSON on stdout and exit 0 (`hookSpecificOutput.permissionDecision: "deny"`); the reason goes to the model, a one-line `systemMessage` to the user. With no objection it prints nothing, so the normal permission flow applies. It never prints `allow`. Hooks fire for subagent calls too (the input carries `agent_type`; the log shows it as `who=`), so one plugin covers a whole fan-out.

The `result` hook (`PostToolUse` and `PostToolUseFailure`, matcher `WebFetch`) reads the status code of the answer and starts the back-off.

Reading a shell command means: here-documents, `$(...)` and backticks (read where they stand, so `code=$(curl ...)` inside a loop is seen as inside the loop), variables set in the same command, `ssh host "..."`, `bash -c`, `pwsh -Command`, inline `python -c` / `node -e`, and script files run by an interpreter or directly. A loop that does not contain the request does not count against it. Scripts inside this plugin are trusted and not read (the fetcher paces itself); other scripts can be exempted with `trusted_scripts`.

## Three ways to send requests

1. **One request per tool call**, the URL written out. The guard books it and waits if needed.
2. **The paced fetcher**, for several (`RG` = `<plugin>/scripts/request_guard.py`; inside a session the skill gives the path as `${CLAUDE_PLUGIN_ROOT}/scripts/request_guard.py`):
   ```
   python3 "$RG" fetch --out-dir DIR URL1 URL2 ...
   python3 "$RG" fetch --list urls.txt --out-dir DIR
   ```
   It waits for each site's turn for as long as it takes, stops asking a site at its limit or when the site refuses, and prints one line per URL (status, bytes, file) on stderr. Exit 3 = some URLs not fetched. Exit 4 = every URL was tried but some answered an error status (outside 200-399) or failed outright; 3 wins when both apply. Run long lists in the background or with a raised tool timeout.
3. **In a script**, one call before each request:
   ```python
   import sys; sys.path.insert(0, "<plugin>/scripts")
   import request_guard
   request_guard.wait_turn(url)              # raises request_guard.Refusal at a limit
   r = requests.get(url, timeout=60)
   request_guard.report(url, r.status_code)  # lets the guard back off
   ```
   Shell: `python3 "$RG" wait "$url" && curl -sS "$url" -o out.html`.

At a limit: stop and tell the user. `request_guard.py status` shows what is booked; `check URL` shows what would happen, without booking.

## Install

From the marketplace (the plugin loads in place from the clone; `git pull` is the whole update):

```
git clone https://github.com/jblenman/claude-code-kit ~/claude-code-kit
claude plugin marketplace add ~/claude-code-kit
claude plugin install request-guard@claude-code-kit
claude plugin list                                   # request-guard@claude-code-kit  Status: enabled
```

Session-only, nothing installed: `claude --plugin-dir ~/claude-code-kit/plugins/request-guard`.

Manual, without the marketplace: copy the three hook entries from `hooks/hooks.json` into the `hooks` key of `~/.claude/settings.json`, replacing `${CLAUDE_PLUGIN_ROOT}` with the absolute plugin path (forward slashes on Windows too). Keep the `|| exit 1` tail. **Never have both:** a hook that is in `settings.json` and in an enabled plugin runs twice per event, which here means every request booked twice (half the real limit, double the spacing). `scripts/setup_check.py` in this repository checks for that.

**Interpreter.** The command is `"${KIT_PYTHON:-python3}" "<script>" hook || python3 "<script>" hook || py "<script>" hook || exit 1`: a shell-form command, run with `sh -c` on macOS and Linux and with Git Bash on Windows (Git for Windows must be installed there). Set `KIT_PYTHON` in the `env` block of `~/.claude/settings.json` when `python3` is not the interpreter you want; settings `env` reaches plugin hook commands (verified on CLI 2.1.289). On Windows set it to the path of a real `python.exe` with forward slashes, never a Store or PyManager alias (`py`, `python3` under `WindowsApps`): those aliases can be denied to other logon sessions such as SSH, and a hook that cannot start fails open. Hook subcommands exit 0 once an interpreter started, so the guard runs exactly once per event even with the fallbacks.

## Configuration (`~/.claude/request-guard/config.json`)

Objects merge, lists add, values replace; `CLAUDE_REQUEST_GUARD_CONFIG=<file>` points elsewhere. The file belongs to the user; a session changes it only when asked.

```json
{
  "deny": ["site-never-to-contact.example"],
  "never_send": ["own-mail-name"],
  "sensitive_terms": ["project-name"],
  "hosts": {"api.own-service.example": "infra", "staging.example": "private"},
  "profiles": {"default": {"max_per_hour": 30}},
  "trusted_scripts": ["~/Projects/some-project/scripts/*.py"],
  "max_hook_wait_s": 25,
  "enabled": true
}
```

| Key | Meaning |
|---|---|
| `deny` | Host suffixes never contacted, by any route. Names of research subjects and their sites belong here, never in the shipped `defaults.json`. |
| `never_send` | Terms that must never appear in a header, a URL or a request body (an own e-mail name, an account id). |
| `sensitive_terms` | Terms kept out of headers (a project name; a search query may still carry it). |
| `hosts` | Suffix to profile name, or `"private"` for a host that is never counted. Longest matching suffix wins. |
| `profiles` | Per-profile `min_interval_s`, `max_per_hour`, `max_per_day`, `max_per_command`, `backoff_codes`, `backoff_min`, `miss_limit`. |
| `trusted_scripts` | Path patterns (`fnmatch`, `~` expanded) of scripts the guard does not read because they pace themselves through `request_guard`. |
| `allowed_user_agents` | Patterns an explicit `User-Agent` may match (stock clients and browsers). |

Environment: `CLAUDE_REQUEST_GUARD=off` disables the guard for a `claude` process (set it in the shell that starts `claude`; a tool call cannot set it for the hook); `CLAUDE_REQUEST_GUARD_STATE=<dir>` moves the ledger and log (default `~/.claude/request-guard`).

## Verify it works

1. Tests, from the repository root: `python3 plugins/request-guard/tests/test_request_guard.py` (Windows: `py ...`). Expect `Ran 40 tests ... OK`. Nothing under `~/.claude` is touched; the `claude plugin validate` and interpreter-chain checks skip when their prerequisites are missing.
2. A scratch-state session proof (the live ledger stays untouched):
   ```
   export CLAUDE_REQUEST_GUARD_STATE=/tmp/rg-proof
   claude --plugin-dir ~/claude-code-kit/plugins/request-guard -p \
     'Run exactly this with the Bash tool and quote the tool result verbatim: for i in 1 2 3; do curl -sS https://example.com/?i=$i; done' \
     --allowedTools "Bash(curl *)" --allowedTools "Bash(for *)" --model haiku
   grep DENY /tmp/rg-proof/guard.log
   ```
   The call is refused before it runs; the model quotes `request-guard: refused. This call would send web requests whose number the guard cannot read (a loop) ...`, and the log shows `DENY(count) why=a loop sites=example.com`. One plain `curl -sS https://example.com/` instead gives `ALLOW example.com(default)x1` in the log and no output from the hook. Put the prompt **before** `--allowedTools`: that flag is variadic and swallows a trailing prompt. In `-p`, `curl` without an allow rule is refused by the permission system ("requires approval"), which a small model may report as a guard block; read `guard.log`, not the prose.
3. `python3 "$RG" status` shows the limits in force, the local config path, the sites booked in the last 24 hours and the log tail.

Verified on CLI 2.1.289 (macOS, Python 3.13 and 3.9): the loop was refused with the reason quoted by the model; a single `curl` was booked once (`ALLOW`), a second one 7 s later waited for the 10 s spacing; a 403 started a 60-minute back-off and the next call to that site was refused; a browser `navigate` was booked like any other request. In one real research run the guard let 100 requests to 29 sites through in 23 minutes (93 of them via the paced fetcher), kept ordinary sites 9 to 10 s apart and started four back-offs by itself; a replay of 2,744 recorded tool calls found it would have refused 306 of 1,800 shell calls (257 loops or looping scripts, 19 bursts, 20 invented identifiers, 10 unreadable targets) and let the rest pass.

## What it cannot see

- **Requests it cannot read before the call:** compiled programs, package managers, `git`, redirects, the files a browser page loads, clicks and form submissions in the browser (only `navigate` is counted).
- **A script that mentions `request_guard`** is taken at its word that it paces itself. The guard protects against haste, not against intent.
- **One ledger per machine.** Several machines behind one public address do not share a ledger.
- **WebSearch** runs at the provider, not from this machine, and is not counted.
- **It is not a security boundary.** A determined operator can route around a hook; this keeps an agent's behaviour sensible and its reasons visible.

## Rollback

`claude plugin disable request-guard@claude-code-kit --scope user` (or `uninstall`); run it from a directory other than `$HOME`, where `~/.claude/settings.json` doubles as the project settings and the scope reads as "project". For one process: `CLAUDE_REQUEST_GUARD=off`. For one site for a while: `python3 "$RG" grant example.com --hour 50 --for-hours 2` (a permission prompt when a session runs it). State and log: `rm -r ~/.claude/request-guard` removes the ledger, the log and your local config together, so move `config.json` out first if you want to keep it.
