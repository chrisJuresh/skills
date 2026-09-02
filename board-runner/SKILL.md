---
name: board-runner
description: >-
  Work a GitHub board unattended — one ticket, one brand-new session, one worktree, one
  landed change — with readiness derived from the board itself and a live tree to watch it
  by. Use this skill when several tickets should be implemented without supervision, when
  work must stop at a wall-clock time, when tickets unblock each other as they land, when a
  ticket's dependency owns a GPU or a CPU that a second job must not contend for, when a
  run has to survive a closed laptop or exhausted credits and be picked up later, when a
  board needs watching while agents work it, and when the user wants this installed against
  a repository.
---

# The queue is a query, and the query is the board

A list of tickets to work through is wrong the moment it is written. Something lands, and
three tickets that were blocked are now runnable; something is picked up by hand in another
chat, and one of them must not be started twice. A list cannot know either. So there is no
list: every cycle, the runner asks the board which tickets are open, carry the agent label,
and have **no open blocker**, and works whatever comes back.

That single decision is what makes the rest of it small. Cascades need no orchestration —
closing a ticket *is* the release of everything behind it. A ticket claimed by a person is
skipped because the board says so. A run resumed after a crash re-derives its position from
the board rather than from anything it wrote down.

The unit of work is **one ticket, one brand-new session**, because that is what
`/implement` is for. Several tickets in one chat share a context window that fills, an
`ExitPlanMode` that means different things at different times, and a cwd that only one of
them can own. Several tickets in *several processes* share nothing. The runner is therefore
a process pool, not an agent: `claude -p "/implement <url>"` per ticket, and a slot freed
when that process exits.

Everything here assumes [worktree-per-change](../worktree-per-change/SKILL.md). One ticket
is one session is one worktree is one landed change, and the guard hook still runs — see
[permission mode is not hook mode](#permission-mode-is-not-hook-mode).

## The three things the board cannot tell you

Readiness comes from `GET /repos/{owner}/{repo}/issues/{n}/dependencies/blocked_by`, which
is a fact rather than a reading of the prose. Three things are not in it, and each one has
burned a run:

**Epics look exactly like leaf work.** A parent ticket with no blockers and an agent label
is indistinguishable, through the API, from a small independent job. Implementing one means
implementing all its children at once, in one session, which is the failure this skill
exists to avoid. There is no signal to detect it by — a repository may or may not use
sub-issues, and a "## Problem Statement" heading is a convention, not a contract. So epics
are **named in the config** and never launched. Getting this list right is the one piece of
human judgement the setup requires; `install.py --suggest-epics` prints the candidates and
refuses to guess.

**Work already claimed by a person.** A chat someone started by hand holds a ticket the
board still shows as ready. The runner claims what it takes with a label
(`agent-running` by default) so that every other chat and its own next poll can see it, but
it cannot retroactively claim what it did not start. Those go in `claimed` at setup, once.

**A dependency whose *job* is still running.** `blocked_by` clears when a ticket **lands**.
It says nothing about the training run that ticket started, which may hold the GPU for
another five hours. Two jobs that are logically independent still contend for one card, and
a second one started beside a live one does not queue — it OOMs. That is what
[gates](references/gates.md) are for.

## The loop

```bash
node scheduler.mjs --dry-run     # print the board, launch nothing
node scheduler.mjs               # work it
```

Per repository, every `pollSeconds`:

1. ask GitHub for open issues carrying the agent label;
2. drop the epics, the claimed, and anything with an open blocker;
3. if any remain and a slot is free, run the repo's **gate** — once per tick, not once per
   launch;
4. launch `claude -p "/implement <issue url>"` in the repository's main checkout, with
   `--permission-mode bypassPermissions`, its own `--session-id`, and the stream written to
   `logs/<repo>-<issue>.jsonl`;
5. label the issue claimed; on a non-zero exit, unlabel it so a retry can see it.

**The deadline is checked before each launch and never during one.** Past it, nothing new
starts and everything already running is left alone — killing a session mid-land is how a
branch gets pushed with no merge behind it. A run that must stop at 10:00 stops *starting*
at 10:00; the last ticket may well finish at 11.

The prompt tells the worker three things a fresh session cannot discover: that nobody will
answer a question, so an open choice is made small and written down rather than asked about;
which of the repository's two delivery commands this repo uses; and that **closing the issue
is what releases the tickets blocked on it**, which is the one step that makes the next
cycle work. A landed change with an open issue stalls everything behind it.

## Permission mode is not hook mode

Workers run with `--permission-mode bypassPermissions`, and it is worth being exact about
what that does and does not switch off. It suppresses the permission *prompt* — which in a
`-p` session is not a prompt at all but an automatic denial, so without it every write dies
silently. It does **not** disable hooks. The `worktree-per-change` `PreToolUse` guard still
fires on every `Edit`, `Write` and `Bash`, and still refuses a write in the main checkout.
The protection that matters is intact; only the interactive question is gone.

This is why the runner launches workers in the **main checkout** rather than pre-cutting
worktrees for them. The guard is what teaches the worker to cut its own, from the fetched
integration branch, under a name it chose — and a worktree made for it by something else is
one more thing that can be cut from the wrong base.

## Watching it

```bash
node tree.mjs                    # http://localhost:7717
node tree.mjs --watch            # the same tree, in a terminal
```

A ticket hangs under **the blocker still gating it**, never under one that has closed, so
the tree reads downward as *when this lands, these are released*. Each running chat shows
elapsed time, current tool, context-window occupancy and the last thing it said — that last
line is the fastest way to tell real progress from a loop.

It reads five sources, because none of them knows the whole story:

| Source | What only it knows |
|---|---|
| GitHub | the graph — `blocked_by`, open/closed, labels |
| `state*.json` | the runner's own jobs: session id, start time, cost |
| `logs/*.log` | what each worker is doing right now |
| `git worktree list` | work standing on disk |
| `~/.claude/projects/<cwd>/<sid>.jsonl` | **chats the runner never started** |

That last one is the reason the board can be trusted. Without it a hand-started session is
invisible and the board quietly claims nothing is happening in a repository where somebody
is working.

**Context occupancy is measured, not estimated**, and two things make it easy to get wrong.
Subagent turns carry `parent_tool_use_id` and are billed against their own window — reading
whichever assistant message came last reports a 141k subagent as the 454k chat that spawned
it. And the window is per model: a finished run states its own in the `result` event, so
those are learned and remembered rather than assumed. Assuming 200k against a 1M-window
model renders a healthy chat as `ctx 225%`.

## When it stops

Credits run out, a laptop closes, a machine reboots. Recovery is **per ticket, not per
run**: `state.json` records a session id and cwd for every job, and

```bash
claude --resume <sessionId>
```

from that repository's directory brings that worker back with its context intact, inside
the worktree it had already cut. Restarting the runner itself needs nothing — it re-derives
the board, and the claim label keeps it off tickets still held. See
[references/recovery.md](references/recovery.md), which also covers clearing a claim left
by a worker that died.

## Installing

```bash
python board-runner/scripts/install.py --dir ~/afk --add-repo /path/to/repo --dry-run
```

Read what it prints, then run it without `--dry-run`. It copies the four scripts, writes a
`config.json`, and links the skill into `~/.claude/skills/`. `--add-repo` reads the
repository's `worktree-per-change.json` for the integration branch and the delivery
command, so the two skills agree by construction rather than by being told the same thing
twice.

It **will not guess the epics**. `--suggest-epics` prints open agent-labelled tickets that
nothing blocks and that have sub-issues or a problem-statement shape, and you put the real
ones in `config.json` yourself. A wrong epic list is the one setup error that does damage:
name a leaf as an epic and it never runs; miss a real epic and an agent tries to implement
six tickets at once.

| Flag | Effect |
|---|---|
| `--dir PATH` | Where the runner lives. Default `~/afk` |
| `--add-repo PATH` | Detect a repository and append it to `config.json` |
| `--suggest-epics PATH` | Print likely epics for a repository, and change nothing |
| `--deadline HH:MM` | The wall-clock stop. Default `10:00` |
| `--status` | What is installed, which repos are configured, what is running |
| `--dry-run` | Print every change and make none |
| `--uninstall` | Remove it |
| `--no-skill` | Skip the `~/.claude/skills` link |

## What this does not do

It does not review the work. Every ticket lands through whatever gate the repository
already has — the Checks, a PR, `land.py` — and this skill adds no judgement of its own to
that. It does not decide what is worth building; the board does. And it does not supervise:
a worker that hits a decision only the author can make is told to stop and leave its
worktree standing, so a standing worktree with commits in it afterwards is a **question**,
not a failure. Check `git worktree list` when a run ends.
