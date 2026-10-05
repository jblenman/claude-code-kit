# session-forensics

Find and audit Claude Code transcripts. Claude Code keeps every session as a JSON-lines file under `~/.claude/projects/<working-directory-slug>/`; these read-only scripts answer the questions those files can answer and `/resume` cannot:

| Question | Script |
|---|---|
| Which session worked on X? (grep matches every file, because your instructions are copied into each one) | `find_session.py` |
| Where did a session's context go: tool output, screenshots, PDF pages, thinking? | `token_audit.py` |
| Does earlier thinking stay in context? | `thinking_retention.py` |
| Do sessions keep my notes file, knowledge base and memory current without being asked, and do they say so? | `proactivity.py`, `proact2.py`, `announce.py`, `qshare.py`, `turns.py` (shared core: `record_audit.py`) |
| Which Claude Code build added a given system-prompt sentence? | `needles.py`, `prose.py` |

A skill tells Claude when to use which script and how to read the output. Everything is stdlib Python 3.8+ and reads transcripts only.

## Install

```
claude plugin marketplace add https://github.com/jblenman/claude-code-kit    # or the path of a local clone
claude plugin install session-forensics@claude-code-kit
```

For one session without installing: `claude --plugin-dir <clone>/plugins/session-forensics`. The skill is `session-forensics:session-forensics`. The scripts also run by hand from any terminal (Windows: `py` instead of `python3`).

## Use

```
python3 "<plugin>/scripts/find_session.py" "parser refactor"                     # which transcript; resume it with claude --resume <id>
python3 "<plugin>/scripts/token_audit.py" --list                                 # one row per session: peak context, images, tool output, compactions
python3 "<plugin>/scripts/token_audit.py" --session 6746                         # one session by tool
python3 "<plugin>/scripts/thinking_retention.py" 6746                            # retained-thinking ratio
python3 "<plugin>/scripts/proactivity.py" --notes-file NOTES.md --kb '/my-kb/'   # record upkeep, asked vs unasked
python3 "<plugin>/scripts/announce.py" --trailer Context                         # did replies say so; how many end with "Context: …"
python3 "<plugin>/scripts/turns.py" ~/.claude/projects/<slug>/<id>.jsonl 2026-10-01
```

All scripts default to `~/.claude/projects`; give a folder (or `--root` for token_audit and thinking_retention) to read transcripts copied from another machine. `find_session.py` also runs on another machine without copying: `ssh host 'python3 - KEYWORD' < find_session.py`.

## Configuring "the record" (proactivity and companions)

You decide what sessions should keep current; the scripts count writes to it:

| Category | Default | Change with |
|---|---|---|
| **notes** — a notes file you ask sessions to update | `session-notes.md` (session-guard's default) and `session-context.md`, matched case-insensitively anywhere in a write's path or command | `--notes-file NAME` (repeatable) |
| **kb** — a knowledge base | none | `--kb REGEX` (repeatable) |
| **mem** — memory | Claude Code's auto-memory: `~/.claude/projects/<project>/memory/` and the folder in `autoMemoryDirectory` (settings.json) | `--memory REGEX` (repeatable) |
| what counts as *asking* | the notes-file names, knowledge base, KB, memory, wrap up, hand off, durable, insight, up to date | `--ask REGEX` |
| a wrap-up | the last turn before ≥ 2 h idle (or a session's last turn once idle that long); compliant when the notes file was written unasked in it or the two turns before | `--wrap-gap-hours`, `--wrap-window` |

The same keys go in a JSON file passed with `--config` (`notes_files`, `kb_patterns`, `memory_patterns`, `ask_regex`, `wrap_gap_hours`, `wrap_window`); flags win. Every script prints a `RECORD:` line showing what it used. Subagent transcripts and headless (`claude -p`) runs are left out of the record audit: they do not keep a record. Writes are read from tool inputs: Write/Edit paths, shell commands with a writing verb, and the body of a script the session wrote and then ran (Windows sessions on CLI 2.1.266 and later run much of their shell work that way; the body is in the transcript as the Write input).

`proact2.py --bands 2.1.250,2.1.260` groups CLI versions; `--nested REGEX` limits the "nested CLAUDE.md live in context" split to matching instruction files. `announce.py --trailer LABEL` counts replies that end with a `LABEL: …` line, for setups that ask sessions to close with one.

What it measured in practice: in one setup, adding a Stop hook that blocks a tool-using turn once while the notes file is stale (and asking replies to end with a one-line record note) moved unasked wrap-up upkeep from about 55 % to 92 % and replies that said so from 23 % to 92 % over three weeks; the remaining misses were turns that used no tools, which that hook does not check.

## Verify it works

```
python3 "<plugin>/tests/test_session_forensics.py"     # 29 checks on synthetic transcripts, about 5 s
```

It builds a small projects folder (a three-turn session with a notes write, a knowledge-base write, a script that writes the notes file, an asked memory update, a screenshot, a nested CLAUDE.md and a compaction; a headless run; a subagent transcript) and checks every script's output against it, including the exclusions and `--config`. The refactored scripts were also run against the original versions over 189 real transcripts with matching settings and produced identical output.

On your machine: `python3 "<plugin>/scripts/token_audit.py" --list --min-turns 1` lists your sessions.

## What it cannot do

- **Transcripts older than `cleanupPeriodDays`** (default 30) are deleted by Claude Code at startup; the scripts see what is left. Compare a new measurement with a recorded baseline.
- **Estimates:** text tokens at 3.7 characters per token (JSON and code tokenize heavier), images at width × height / 750. Peak context per call is exact (from usage records).
- **"Asked" is a keyword match.** Read the listed asks by hand: in one measurement 24 of 40 matches were prompts *about* a knowledge-base or memory topic, not requests to update it.
- **Writes it cannot see:** files changed by a program the session started without naming it, or by a script whose body never appeared in the transcript and is gone from disk.
- **find_session** counts real user and assistant text only: a word that appears only in tool calls, tool output or subagents is not found.
- **needles.py's** sentence list is a snapshot of one harness version's wording; add your own with `--needle`.
- Transcripts contain everything a session saw. The scripts print prompts and titles to your terminal; keep their output out of public places.

## Rollback

`claude plugin uninstall session-forensics@claude-code-kit`. The scripts write nothing.
