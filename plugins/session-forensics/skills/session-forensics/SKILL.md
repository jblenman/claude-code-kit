---
name: session-forensics
description: Find which Claude Code session worked on something, and audit sessions — where the context or tokens went (screenshots, tool output, thinking), what a session wrote, whether sessions kept a notes file, knowledge base and memory current without being asked. Use for "which session did X", "find the transcript where we…", "resume the session that…", "why did the context fill up", screenshot or token cost questions, and record-keeping (proactivity) measurements. Runs the session-forensics plugin's scripts over ~/.claude/projects.
---

# session-forensics — finding and auditing Claude Code transcripts

Transcripts are kept **per machine and per working directory**: `~/.claude/projects/<cwd-slug>/<session-id>.jsonl` (the slug is the starting directory with its separators turned into `-`). A session's subagents sit in `<session-id>/subagents/agent-<id>.jsonl`, large tool outputs in `<session-id>/tool-results/`. `/resume` lists only this machine and this directory. Claude Code deletes transcripts older than `cleanupPeriodDays` (default 30) at startup.

| Question | Script |
|---|---|
| which session worked on X; resume it | `find_session.py` |
| where did the context go; token, image, screenshot cost | `token_audit.py` |
| does thinking stay in context | `thinking_retention.py` |
| did sessions keep the record current without being asked | `proactivity.py` and its companions |
| which CLI build added a system-prompt sentence | `needles.py`, `prose.py` |

All are stdlib Python 3.8+, read-only, and take a few seconds over a few hundred transcripts. Windows: `py` instead of `python3`.

## Check the environment first

```
ls ~/.claude/projects/ | head; ls ~/.claude/projects/*/*.jsonl | wc -l
```

## Which session? — find_session.py

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/find_session.py" <keyword>
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/find_session.py" <keyword> <projects-root>    # a copied projects folder
```

Why not grep: CLAUDE.md files and the memory index are injected into every transcript's first turn, so a raw grep for anything named there matches every file. find_session strips `<system-reminder>` blocks and counts hits in real user and assistant text only. The keyword is literal and case-insensitive (no regex).

Output, one line per transcript, oldest to newest by modification time:

```
  <== 67461976    16573KB  2026-09-28T01:03 -> 2026-10-04T22:21  user_turns=61  hits(user/asst)=9/8  title='<the /rename name>'  first='<first real prompt>'
```

- `<==` marks more than two hits. The id is the first 8 characters of the file name; timestamps are UTC.
- Subagent transcripts are not scanned, and a word that appears only in tool calls or tool output does not count: try a file name the session wrote, its `/rename` title, or another word.
- Resume: find the full id with `ls ~/.claude/projects/*/<prefix>*.jsonl`, start `claude` from that session's original working directory, and run `claude --resume <full-id>`.
- Another machine, without copying anything: `ssh <host> 'python3 - <keyword>' < "${CLAUDE_PLUGIN_ROOT}/scripts/find_session.py"` (Windows hosts: `py -`, or the interpreter's full path where `py` is a Store alias).

## Where did the context go? — token_audit.py

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/token_audit.py" --list                  # one row per session with at least 20 assistant calls
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/token_audit.py" --list --min-turns 1    # include short and headless sessions
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/token_audit.py" --session <id-prefix>   # one session, broken down by tool
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/token_audit.py" --root <dir> --list     # transcripts copied from elsewhere
```

- `--list` columns: id, first, last (UTC dates), model (most used), turns (assistant API calls, not user turns), maxctx(k) (peak context, exact from usage), out(k) (output tokens), imgs, imgtok(k) (≈ width × height / 750 per image), tooltxt(k) (tool-result text), think(k), cmp (compactions), title or cwd. `think(k)` counts only thinking text stored in the transcript, which current models leave mostly empty: it is not the thinking cost (use thinking_retention.py).
- `--session` prints the models, assistant turns, output tokens, peak context and compactions, estimated tokens of assistant text, thinking and user text, a per-tool table (calls, argument tokens, result tokens, images, image tokens; costliest first) and the context at 25/50/75 % of the session. The row `(image in user msg: …)` is PDF pages read with `Read … pages=` or pasted screenshots. `--session` only sees sessions that pass `--min-turns`.
- `--grep WORD` is a raw substring search with the same every-file problem as grep: find the session with find_session.py, then audit it with `--session`.
- Text tokens are estimated at 3.7 characters per token (JSON and code tokenize heavier).

## Does thinking stay in context? — thinking_retention.py

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/thinking_retention.py" [--root <dir>] <id-prefix> [<id-prefix> …]
```

Per session it compares context growth inside a user turn with the visible content added; the unexplained rest against the thinking produced gives a "retained ratio" (≈ 1 = thinking kept within a turn; about 0.4 was seen for two different models on CLI 2.1.289). The second line shows the change at user-turn boundaries (a drop = earlier thinking discarded).

## Did sessions keep the record unasked? — proactivity.py and companions

"The record" is what you ask sessions to keep current. Tell the scripts what that is:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/proactivity.py" --notes-file session-notes.md --kb '/my-notes/' ~/.claude/projects
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/proact2.py" --bands 2.1.250,2.1.260 ~/.claude/projects   # wrap-ups by compactions, session age, version band, nested CLAUDE.md
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/announce.py" --trailer Context ~/.claude/projects         # did the reply SAY it updated the record; trailer-line rate
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/qshare.py" ~/.claude/projects                             # question vs directive turns
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/turns.py" <transcript.jsonl> <YYYY-MM-DD>                 # one session turn by turn: prompt, every record write, last text
```

- Categories: **notes** = a write to a file named by `--notes-file` (repeatable; defaults `session-notes.md` — session-guard's file — and `session-context.md`, matched case-insensitively), **kb** = a path matching `--kb REGEX` (none by default), **mem** = Claude Code's auto-memory (the per-project `memory/` folder and `autoMemoryDirectory` from settings; `--memory REGEX` replaces them). The same settings go in a JSON file with `--config` (keys `notes_files`, `kb_patterns`, `memory_patterns`, `ask_regex`, `wrap_gap_hours`, `wrap_window`). Every script prints a `RECORD:` line with what it used.
- "Asked" = the prompt matched `--ask` (default: the notes-file names plus knowledge base, KB, memory, wrap up, hand off, durable, insight, up to date). A **wrap-up** is the last turn before at least 2 h idle (`--wrap-gap-hours`), including a session's last turn once it has been idle that long; it is compliant when the notes file was written unasked in it or the two turns before (`--wrap-window 3`).
- Left out: subagent transcripts and headless (`claude -p`) runs. Writes are read from tool inputs, including the body of a script the session wrote with Write/Edit and then ran (Windows sessions run much of their shell work that way).
- Caveats: "asked" is a keyword match — **read every matched ask by hand** (in one measurement 24 of 40 matches were prompts *about* a knowledge-base or memory topic, not requests to update the record). Transcripts older than 30 days are gone, so compare a new measurement with a recorded baseline, not with a fresh run over old weeks. Weekly counts are small; read trends.
- `needles.py [~/.local/share/claude/versions]` counts system-prompt sentences in each installed CLI build (`--needle LABEL=TEXT` adds one); `prose.py <binary>` lists a build's prose strings for diffing two versions.

## Discretion

- Transcripts hold everything a session saw: prompts, file contents, mail read through connectors, work material. Quote the minimum, keep tool output in the session or a scratch folder, and never copy transcripts off the machine unless the user asks.
- Excerpts from sessions that touched confidential material never go into a public repository or a shared document.

## When it fails

| Symptom | Cause / fix |
|---|---|
| every transcript matches a grep | expected (instructions and memory are in every first turn): use find_session.py |
| find_session shows 0 hits for a session you know | the word sits only in tool calls/output or a subagent, or the session ran on another machine or from another directory: try other words, then the SSH form |
| `token_audit.py --session` prints nothing | fewer than 20 assistant calls (add `--min-turns 1`) or a wrong prefix |
| `claude --resume <id>` finds nothing | wrong machine or wrong starting directory: start from the session's original directory |
| `kb` columns all 0 | no `--kb` pattern given (the `RECORD:` line says "not configured") |
| `skip <path> …` from proactivity.py | a path that is not a transcript: pass directories |

## Not this skill

- The current session's live usage → `/context`, `/cost` or the status line.
- Messaging another session → Claude Code's own session tools.
