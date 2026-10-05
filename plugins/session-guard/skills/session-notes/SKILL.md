---
name: session-notes
description: How to keep the session-notes file (default ~/.claude/session-notes.md) that session-guard enforces -- what to record (tasks and status, decisions with their reasons, files touched, background work, open threads), when to update it, and when a project outgrows the shared file and gets a notes file of its own with an index row in the machine file. Use when the session-guard Stop hook blocks a turn, when updating the record after a decision or a significant step, or at the start of a session to pick up where the last one stopped.
---

# session-notes: the record a new session boots from

A Claude Code session loses its memory when it ends, when its context is compacted, or when the terminal is closed. The session-notes file is the record that survives: short enough to read in a minute, complete enough that a new session can resume the work. The session-guard plugin makes keeping it current mechanical: a turn that used tools cannot end while the file is stale (default 30 minutes), untouched since the session started, or missing, and a compaction that ran while the file was stale forces an update at the next turn end.

The file: `~/.claude/session-notes.md` unless `CLAUDE_SESSION_NOTES` names another path. One file per machine, shared by every session on it.

## What to record

- **What is being worked on** and its status: done, in progress, remaining.
- **Decisions and their reasons**, including alternatives rejected and why. An outcome without its reasoning gets re-litigated by the next session.
- **Files created or changed** (paths), identifiers that matter (commands, branch names, ids, error texts verbatim).
- **Background work still running**: what it does, where its log is, how to check it.
- **Open threads, blockers, questions for the user.** Half-finished thinking matters more than finished work: finished work is in the files, open threads exist only in the conversation.
- **Corrections and preferences the user stated** ("don't do X", "yes, that is right"): durable, and cheap to carry.

Keep it brief and scannable: headings, bullets, one line per fact. A dated line at the top (`Updated: 2026-10-04 14:32`) tells the reader and the guard's `status` command how fresh it is.

## When to update

- At the start of a session: read the file, note what the previous state was, add or refresh a section for this session.
- After completing a significant step; before and after a long-running operation; when switching tasks.
- **After any decision reached in discussion**, even when no file changed. A question-and-answer turn that settles what will be done is the most often lost record.
- Before ending a turn that used tools while the file is stale: update first, then reply. The hook blocks once per turn and tells you why; updating *before* it has to saves the extra turn.
- Right after a compaction: the compaction summary may now be the only record of what came before it. Write the durable parts into the file first.
- A manual `/compact` while the file is stale is bounced once: update, then compact.

End a reply that follows tool use or a decision with one line saying what was recorded (for example `Notes: session-notes updated (decision on the parser rewrite, files touched).`), so the user sees the update without opening the file.

## Layout of the file

```markdown
# Session notes -- <machine name>
Updated: 2026-10-04 14:32

## <Task or project A>
- Status: ...
- Decisions: ... (why ...)
- Files: ...
- Open: ...

## <Task or project B>
...

## Background
- <task>: <what it does>, log <path>, check with <command>
```

Ad-hoc work (a question answered, a small fix, a one-off task) records its state inline here. Do not create a project file for it, and do not create a catch-all "general" project: that is what this file is.

## When a project outgrows the shared file

Split a project into a notes file of its own **only when both hold**:

- **Contention:** another session on this machine is updating the shared file in the same timeframe (concurrently, or within the same day), and
- **Substance:** the work is an ongoing multi-session project with real state (a mission, decisions, open threads), not an afternoon's task.

One without the other: stay in the shared file. When in doubt, stay; splitting later is cheap. A shared file of a hundred lines or more is a nudge to split out whichever *substantial* project is bloating it, never the small items.

When splitting:

1. Create a notes file for the project where its working files live (for example `<project>/SESSION-NOTES.md`) and move the project's state into it. Creating the file is the claim: a second session that finds it already there adopts it instead of making another.
2. The shared file keeps an **index row** per split-out project: name, path of the project file, one-line status, date of the last session. Whenever a session updates its project file, it bumps that row in the same turn. The shared file is what the user reads and what the guard checks; a fresh project file behind a stale shared file reads as "nothing happened".
3. A session moves only its own project's content out of the shared file, never another session's; leave a "pending migration" row instead.
4. Two sessions working the *same* project is a coordination problem, not a file-layout problem: say so to the user rather than racing over the file.

## When the hook blocks

The reason names the file, how stale it is, what this turn did (tool count and names) and what to record. Update the file, then end the turn normally; the hook allows the second attempt within the same turn. If the file named is wrong for this machine, say so to the user: the path comes from `CLAUDE_SESSION_NOTES` in the environment `claude` was started from, and the thresholds from `CLAUDE_SESSION_NOTES_STALE_MIN` and `CLAUDE_SESSION_NOTES_COMPACT_FRESH_MIN`.

`python3 ${CLAUDE_PLUGIN_ROOT}/scripts/session_guard.py status` shows the file's age, the thresholds, recent session state and the log tail.
