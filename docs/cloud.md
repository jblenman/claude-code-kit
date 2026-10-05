# Cloud sessions and ultrareview from the command line

Cloud sessions run Claude Code in an Anthropic-managed VM against a clone of your GitHub
repository, so long jobs keep going while your machine sleeps. This page covers starting and
steering them from a terminal or a script, bringing the result back, the brief a cloud session
reads, and the size limit of `claude ultrareview`. The official page is
[code.claude.com/docs/en/claude-code-on-the-web](https://code.claude.com/docs/en/claude-code-on-the-web).

**Verified on:** Claude Code 2.1.286 to 2.1.289, macOS, 2026-10-04: three cloud sessions started
from scripts through a pseudo-terminal, steered with follow-up messages, results returned as
pushed branches; one `claude ultrareview` refused for size and then run in two halves. Rules quoted
from the docs were checked on 2026-10-04.

## What a cloud session sees

- **Your repository's remote, not your working copy.** The VM clones the GitHub remote at your
  current branch. Push first. (Without a GitHub remote the CLI uploads a bundle of the local
  repository instead.)
- **Not your machine.** It does not read your `~/.claude/settings.json` or `~/.claude/CLAUDE.md`,
  your local hooks and plugins do not come along, and it cannot reach your LAN (no local model
  server, no collector, no other machines). Telemetry from cloud sessions needs the cloud
  environment's own variables.
- **The repository's own instructions.** `CLAUDE.md` in the repository, or `AGENTS.md` when there is
  no `CLAUDE.md` (below), and the repository's `.claude/settings.json`.
- Cloud sessions count against the same plan limits as everything else; the docs list no separate
  compute charge for the VM.

## The brief: AGENTS.md

Claude Code reads `AGENTS.md` natively (since 2.1.277) **when there is no `CLAUDE.md` or
`CLAUDE.local.md` in the working directory or above it**; your `~/.claude/CLAUDE.md` does not count.
A repository that wants one brief for cloud sessions and other agents keeps a short `AGENTS.md` at
the root (or a `CLAUDE.md` that imports it with `@AGENTS.md`). What belonged in it in practice:

- the rules: no secrets or personal data in files; which files not to edit (put proposed changes in
  the report instead);
- **work on a branch named `cloud/<short-task>` and push it**; small commits with plain messages;
  never force-push or rewrite the default branch;
- how to test: the exact test commands, and "stdlib Python 3.8+" or whatever the repository's rule
  is;
- what the session cannot reach (the LAN, other machines, your drive), so it reports instead of
  trying;
- the report format: what changed (paths), how it was tested (commands and results), open points.

With that brief, every session came back as a pushed `cloud/<task>` branch that could be reviewed
and merged locally.

## Starting a session from a script: it needs a terminal

```sh
claude --cloud "Execute the plan in docs/plan.md"      # interactive terminal: fine
```

`claude --cloud "<task>"` creates the session and shows a live checklist while the VM is set up;
it queues the task and sends it once the session is ready, so the client has to stay up until
then. It **needs a terminal (TTY)**: started from an agent's shell tool, which has none, it did not
work. `scripts/cloud_launch.py` runs it inside a pseudo-terminal, which also makes it usable from CI
or any other script:

```sh
python3 scripts/cloud_launch.py --cwd ~/src/myrepo --log /tmp/cloud1.log "Execute the plan in docs/plan.md"
# prints the session URL: https://claude.ai/code/session_...
```

It answers the folder-trust dialog if one appears (its default choice is "No, exit", so the script
selects "Yes, I trust this folder"; `--no-trust` leaves it unanswered), waits until a
`claude.ai/code` session URL is printed plus `--settle` seconds (default 90) so the queued task gets
sent, then stops the local client and prints the URL. The cloud session keeps running. `--max`
(default 420 s) bounds the wait; exit code 2 means no URL appeared, and the log keeps the raw
terminal output. `--env KEY=VALUE` adds environment for the local client, for example
`--env CLAUDE_SESSION_GUARD=off` so the `session-guard` Stop hook does not hold up an unattended
launch. It uses Python's `pty` module, so it runs on macOS and Linux (on Windows, use WSL).

## Steering a running session

```sh
claude -p "your message" --cloud <session-id-or-url>
echo "your message" | claude -p --cloud <session-id-or-url>
```

The message is queued into the session and the command exits without waiting for a reply. It works
from any machine signed in to the same account; the id is `session_...` or `cse_...`, or the
`claude.ai/code/...` URL. `--output-format json` returns `{ok, session_id, url}`. A slash command
sent this way, such as `/model opus`, was used to switch a session's model after launch.

## Bringing the work back

- **Merge the branch** the session pushed, after reviewing it like any other contribution.
- **Teleport** to continue in your terminal: `claude --teleport` (a picker) or
  `claude --teleport <session-id>`, `/teleport` (or `/tp`) inside a running session, or `t` in
  `/tasks`. Requirements: a clean working tree (it offers to stash), a checkout of the same
  repository (not a fork), the session's branch pushed, the same account. The terminal gets its own
  copy of the conversation: new work there does not appear in the cloud session.
- `--resume` is different: it reopens a conversation from this machine's local history and does not
  list cloud sessions.

## Ultrareview: a cloud review of a branch, and its size limit

`claude ultrareview [target]` (or `/code-review ultra` in a session) runs a multi-agent review in
the cloud of the current branch against a base branch or a pull request, and prints the findings
(`--json` for the raw payload, `--timeout` minutes, default 45; `--post` to comment on a PR target).

**Size cap.** A large diff is refused before anything runs:

```
Diff is too large for ultrareview: 84 files, 11,702 lines changed (limits: 500 files, 8,000 lines)
```

The line count is insertions plus deletions. Measure candidates first:

```sh
git diff --shortstat <base> <commit>      # e.g. "40 files changed, 3,100 insertions(+), 1,110 deletions(-)"
```

**Split by base branch.** Pick a commit in the middle where the first half stays under the limits,
then review the two halves separately:

```sh
git branch review/first-half <middle-commit>
git worktree add ../myrepo-review review/first-half
(cd ../myrepo-review && claude ultrareview <original-base>)   # first half: base .. middle
claude ultrareview review/first-half                          # second half, from the main clone: middle .. HEAD
```

Seen on 2.1.289: two reviews of 4,210 and 7,760 lines this way. The worktree keeps your main clone
on its branch (useful when plugins load in place from it).

**Who starts it.** In practice an agent session could not launch it for you: prepare the base
branch and run the command yourself. The launch showed `Free ultrareview 1 of 3`, which reads as
an allowance of its own, separate from the plan's usage limits; check your usage page after a run
if it matters.
