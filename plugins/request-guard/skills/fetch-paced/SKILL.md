---
name: fetch-paced
description: Fetch several web pages, files or API endpoints -- a list of URLs, a batch of documents, several pages from one site -- at the pace request-guard enforces. Use whenever more than one URL needs fetching, and before writing any shell loop, xargs, wget -r or script around curl, wget or requests, because the request-guard hook refuses those. Runs the plugin's paced fetcher (request_guard.py fetch: paced, counted, stops at a site's limit) or request_guard.wait_turn() inside your own script. A refusal stands.
---

# fetch-paced: several URLs at the guard's pace

Every request this session or one of its subagents sends to a **public** website or endpoint is counted and spaced in time by the request-guard plugin: a PreToolUse hook on Bash, PowerShell, WebFetch and browser navigation, with one ledger per machine shared by every session and subagent. One URL is simply sent, with the URL written out. This skill is for more than one.

The script: `${CLAUDE_PLUGIN_ROOT}/scripts/request_guard.py` (below as `$RG`). On Windows run it with `py` or the interpreter in `KIT_PYTHON`.

## The rules

- **Every request to a public site is counted and spaced**, per site, per machine. Private and LAN addresses are not counted.
- **One URL per tool call, the URL written out.** For several: `$RG fetch --out-dir DIR URL...` or `--list urls.txt`; in your own script, `request_guard.wait_turn(url)` before each request. A request inside a loop, `xargs`, a recursive `wget`, a URL list or a script that fetches is refused, because its count cannot be read.
- **A refusal stands.** Never route around it: no other tool, other machine, proxy, browser or reworded command. At a limit, stop and tell the user how many requests the work needs and why.
- **Only the user raises a limit**, lifts a back-off or changes `~/.claude/request-guard/config.json`. `request_guard.py grant` and `clear-backoff` raise a permission prompt; never run them on your own initiative.
- **A site that answers 403, 429 or 503 is left alone** (automatic back-off). Do not guess at addresses either: three missing pages in a row pause the site.
- **Identifiers:** leave the `User-Agent` at the client's default or use a stock browser string. Never put a project, person, folder or session name, or the user's name or e-mail address, into a header, a URL or a request body -- also when a service's own rules ask for a contact address (ask the user first).
- **Subagent prompts** for work that fetches carry one line: "Web requests are paced by request-guard: one URL per call or `request_guard.py fetch` for several; a refusal stands."

Limits per site (shipped defaults, `scripts/defaults.json`; the machine-local config may differ -- `$RG status` shows what is in force):

| Profile | Applies to | Apart | Per hour | Per day | Per call |
|---|---|---|---|---|---|
| `default` | any public site not listed | 10 s | 20 | 50 | 1 |
| `careful` | search engines, social networks and other account-bound sites | 20 s | 10 | 30 | 1 |
| `bulk` | listed public data services and large documentation sites | 3 s | 300 | 1,500 | 3 |
| `archive` | the Internet Archive | 6 s | 150 | 600 | 1 |
| `infra` | code hosts, package registries, the model provider | none | 600 | 5,000 | 20 |

All public sites together: 1,500 an hour. A **site** is the registrable domain (`www.` and `api.` of one domain count together).

## Check before a batch

```
python3 "$RG" check <URL> [<URL> ...]    # site, profile, "can go now / in N s" or why not; books nothing
python3 "$RG" status                     # profiles, local config, sites booked in the last 24 h, log tail
```

Work out the time a list needs before sending it: 30 URLs on one ordinary site take at least 5 minutes at 10 s apart, and only 20 go in an hour; the rest come back as skipped.

## Fetch a list

```
RG="${CLAUDE_PLUGIN_ROOT}/scripts/request_guard.py"
python3 "$RG" fetch --out-dir "<scratch>/pages" <URL1> <URL2> <URL3>
python3 "$RG" fetch --list "<scratch>/urls.txt" --out-dir "<scratch>/pages"     # one URL per line; # starts a comment
python3 "$RG" fetch -o "<scratch>/page.html" <URL>                              # one URL to a file
python3 "$RG" fetch <URL>                                                       # one URL to stdout
python3 "$RG" fetch --head <URL>                                                # headers only
```

- Options: `-H "Name: value"` (repeatable; the identifier rules apply), `-A <agent>` (leave it out), `--max-time N` seconds per request (default 60). Several URLs need `--out-dir`.
- It waits for each site's turn as long as that takes, stops asking a site at its limit or when the site refuses, and follows up to 5 redirects.
- A long list outlives the Bash tool's default 2-minute timeout (about a dozen requests to one ordinary site): run it in the background or raise the timeout.

Output goes to **stderr**, one tab-separated line per URL:

```
200	48213	<scratch>/pages/001_www.example.org_report.html	https://www.example.org/report
skip	-	-	https://www.example.org/other	(<reason>)
request-guard: 1 of 2 not fetched. Do not retry early and do not route around this ...
```

- Files are named `NNN_<host>_<last path part>` (NNN = position in the list) with an extension from the content type (`.html`, `.pdf`, `.json`, `.txt`, `.xml`, `.csv`). `ERR` in the status column means no answer at all; a curl error follows in brackets.
- Exit 0 = everything fetched with a 2xx/3xx answer; **3 = some URLs not fetched** (limit, back-off, refusal, rejected identifier); **4 = all were tried but some answered an error status**.
- Read the bodies with Read (or a subagent for long ones).

## In your own script

```python
import os, sys
sys.path.insert(0, "${CLAUDE_PLUGIN_ROOT}/scripts")
import request_guard
for url in urls:
    request_guard.wait_turn(url)              # waits for the site's turn; raises request_guard.Refusal at a limit: stop there
    r = requests.get(url, timeout=60)         # default User-Agent
    request_guard.report(url, r.status_code)  # lets the guard back off on 403/429/503
```

In a shell, one request: `python3 "$RG" wait "<URL>" && curl -sS "<URL>" -o out.html` (`wait` exits 3 at a limit). The guard trusts a script or command that paces itself through request_guard ("it protects against haste, not against intent"), so wait for exactly the URL you then request, once per request.

## When a call is refused

- Read the reason. "Not yet" means the queue for that site is longer than the hook waits (25 s): send it again after the time it names, not earlier. A count, target or identifier refusal means rewriting the call as one of the three ways above (or dropping the header).
- At a limit or back-off: stop, and tell the user which site, how many more requests the work needs and why.
- Never switch to WebFetch, the browser, another machine, a proxy or a mirror to get past a site's limit, and never split a batch to dodge the count.

## What the guard cannot see (the rule still covers it)

Compiled programs, package managers, `git`, redirects, the files a browser page loads, clicks inside the browser (only `navigate` is counted). WebSearch runs at the provider and is not counted. Each machine has its own ledger.

## Not this skill

- One page: WebFetch or one `curl` with the URL written out (still counted).
- A search: WebSearch.
- A logged-in site: the browser (every `navigate` is counted; account-bound sites are `careful`).
- Services on the LAN or on this machine: not counted; plain requests are fine.

Design, what it reads, and the measurements behind the defaults: the plugin's README; `python3 "$RG" help`.
