# research-agents

Four Claude Code subagents for research work. Each runs in its own context window, starts from the task prompt only (not your conversation), and returns a fixed-form report, so the main session keeps the conclusions and not the page dumps, images or raw sources.

| Agent | Use it for | Tools | Notable settings |
|---|---|---|---|
| `research-agents:researcher` | questions that need current or outside information; comparisons; background on a public topic or organization. Every claim comes back with its URL, "confirmed" (matched in raw text) kept apart from "probable" | WebSearch, WebFetch, Bash, Read, Write, Grep, Glob | `effort: high`; preloads `request-guard:fetch-paced` when that plugin is installed |
| `research-agents:verifier` | checking a finished agent-written document claim by claim against its original sources, before anyone relies on it; edits the document in place with a logged edit list | Read, Grep, Glob, Edit, Write, Bash, WebFetch | `effort: xhigh`; `memory: user` (keeps its own notes between runs) |
| `research-agents:browser-reader` | pages that need a real browser (script-rendered pages, sites that refuse automated fetches, "show more" and transcript panels); read-only, through your Chrome | the Claude in Chrome read and navigation tools, Read, Write | form filling, page scripts, uploads, shortcuts and batch actions are disallowed |
| `research-agents:frame-reader` | screenshots, images, PDF pages and video keyframes: pictures in, text out, so images never enter your main context | Read, Bash, Glob | `model: sonnet`; `omitClaudeMd: true` (your CLAUDE.md is not loaded into it) |

Claude delegates to them on its own when a task matches the description (three of them say "use proactively"), or you name one: "use the verifier agent on report.md", or `@agent-research-agents:verifier`.

## Install

```
claude plugin marketplace add https://github.com/jblenman/claude-code-kit    # or the path of a local clone
claude plugin install research-agents@claude-code-kit
```

For one session without installing: `claude --plugin-dir <clone>/plugins/research-agents`. Start a new session after installing: agents are read at session start. `/agents` lists them.

Optional companions from the same marketplace:

- **request-guard** — counts and spaces web requests. The researcher's front matter preloads its `fetch-paced` skill (`skills: [request-guard:fetch-paced]`), which carries the paced fetcher's command. Verified on CLI 2.1.289: with request-guard installed, the researcher starts with that skill's full text in its context; without it, the researcher loads and runs normally with nothing preloaded (no error) and falls back to one fetch per call, spaced out.
- **screenshot**, **video-digest** — produce the images and keyframes frame-reader reads.

browser-reader needs the **Claude in Chrome** extension connected to the session; without it the agent reports that it has no browser.

## The verifier's memory (`memory: user`)

The verifier keeps notes between runs in a folder of its own under your profile:

- **Where:** `~/.claude/agent-memory/research-agents-verifier/` — the agent's scoped name with `:` turned into `-` (checked on CLI 2.1.289; the folder is created the first time the agent runs).
- **What happens:** Claude Code tells the agent where the folder is and loads its `MEMORY.md` into the agent's context at the start of each run; the agent may read and write files there. The verifier's instructions say to add, after each round, one generic line per lesson (error classes, sites that refuse automated reading, raw-text methods that worked) and never document contents, subject names or personal data.
- **Scope:** `user` means one memory for that agent across all your projects on this machine. It is not shared with other agents, other machines or your main sessions, and it is not part of your own auto-memory.
- **Reset or inspect:** open or delete the folder. Delete it before sharing a machine profile if you are unsure what it holds.

## Verify it works

```
python3 "<plugin>/tests/test_research_agents.py"      # 24 offline checks: front matter, tool limits, the skill reference, validate --strict
```

Listed at session start (one tiny model call for the "ok"):

```
claude -p "ok" --plugin-dir "<plugin>" --output-format stream-json --verbose < /dev/null | grep -m1 '"subtype":"init"'
```

The `init` event lists `research-agents:browser-reader`, `research-agents:frame-reader`, `research-agents:researcher` and `research-agents:verifier` under `agents` (with SessionStart hooks configured, hook events come first; the grep skips them).

A real run, cheap: ask a session "Use the frame-reader agent to read <some screenshot.png> and tell me the window title" — the answer comes back as text and no image appears in your session.

## What they cannot do

- **Plugin agents ignore `permissionMode`, `hooks`, `mcpServers` and `initialPrompt`** in their front matter (documented); none of these agents uses them. Permission prompts and your session's hooks still govern what their tools may do.
- **The browser-reader's read-only rule is partly behavioral.** The dangerous Chrome tools are disallowed, but it keeps the `computer` tool for expanding panels and dismissing dialogs, and it acts in your real browser profile with your sign-ins. Give it the URLs and say what to extract.
- **The verifier edits documents in place** by default (every edit logged in the findings file). Say "do not edit" in the prompt to get findings only.
- **Web searches** may come from an allowance shared by every agent in the session; a large fan-out of researchers can exhaust it.
- **Summaries are not sources.** WebFetch returns a model's summary; both researcher and verifier are told to confirm quotes and figures against raw text, which takes extra requests.
- They return what their sources say. A "confirmed" finding means "matched in a primary source", not "true".

## Rollback

`claude plugin uninstall research-agents@claude-code-kit`, then delete `~/.claude/agent-memory/research-agents-verifier/` if you want the verifier's notes gone.
