---
name: parallel-agents
description: >-
  Run several Claude sessions, teammates or writing subagents in one repository without
  them silently corrupting each other's work — and install a guard hook that enforces it
  in every repo automatically. Use this skill whenever more than one agent might be
  working in the same checkout: the user mentions parallel agents, multiple sessions,
  agent teams, worktrees, "another agent is editing this", two tickets at once, or asks
  you to fan work out across subagents that write. Use it when a tool call is denied for
  a concurrent writer, when you are about to switch branches, stash, or `git add -A` in a
  repo you may be sharing, and before starting a second `/implement` in one folder. Also
  use it when the user wants this protection installed once and working everywhere.
---

# Parallel agents in one checkout

Two agents in one working directory is the worst failure available in this space, and it
does not announce itself. `git checkout` is a property of the *directory*, so one session
switching branches rewrites the files another is mid-edit on. The index is a single lock,
so one session's `git add -A` sweeps up another's half-finished files. Two sessions
editing one file means the later write discards the earlier one — git never sees two
versions, so there is no conflict marker. None of these produce an error. A conflict you
resolve is cheap; a branch that goes red for something it never did is not.

Claude Code already enforces isolation for a session that is **inside** a worktree: file
edits, command working directories and `git -C`/`GIT_DIR`/`cd`-then-git redirects that
reach back into the main checkout are all blocked, for the session and every subagent
under it. What nothing covers is the step before that — sessions sharing the main
checkout, none of them isolated yet. That gap is what this skill fills.

## Install it once, everywhere

```bash
python "${CLAUDE_SKILL_DIR}/scripts/install.py" --dry-run
```

Show the user that output — it writes to their global `~/.claude/settings.json` — then run
it without `--dry-run`. It copies the guard to `~/.claude/hooks/`, registers three hooks,
and links this skill into `~/.claude/skills/` so `/parallel-agents` resolves everywhere.
Every repository on the machine is then covered without a per-repo step and without anyone
invoking a skill first.

- `--repo .` installs into a repository's committed `.claude/settings.json` instead, for
  when the rule has to hold for teammates and not just for you.
- `--status` reports what is installed and who else has written in this checkout.
- `--uninstall` removes it. Add `--repo` to remove a repo install.
- `--no-skill` skips the skill link. Only use it if the skill is already installed some
  other way — the guard's denials name `/parallel-agents`, and a machine with the hook and
  without the skill hands an agent a dead reference exactly when it needs the protocol.

New hooks apply to sessions started afterwards, so say so rather than letting the user
assume the current session is protected.

Read [references/guard-internals.md](references/guard-internals.md) when the guard fires
unexpectedly, when a repo needs a different strictness, or before changing it.

## Do you actually need a worktree?

Isolate on evidence, not on principle. The failure above needs **two writers** — one
writer cannot race itself, and an agent that only reads cannot cause it. A worktree taken
when nothing was contending costs a dependency install and puts the diff in a directory
the operator is not looking at, which is how work ends up invisible to the person who
asked for it.

| Signal | Verdict |
|---|---|
| The guard denied you, or `install.py --status` lists another live session | worktree |
| `list_sessions` shows another session with `isRunning: true` whose `cwd` is this checkout | worktree |
| `git status --porcelain` shows work you did not make | worktree |
| None of those | **work in place** |

The last two rows catch what the guard cannot see: a person in an editor and a plain
`claude` CLI session write without ever running a hook. A dev server on :3000 is **not** a
contention signal — it is the operator's own preview, and working in place is precisely
what makes it show them your branch.

Also stay in place when:

- **You only read.** `Explore`, `Plan`, reviewers, anything limited to `Read`/`Grep`/`Glob`.
  A read-only subagent gets nothing from a worktree and pays an install for it.
- **Several agents hand one branch down a chain.** The unit is the branch, not the agent.
- **You are already inside a worktree.** Subagents you spawn inherit its enforcement.

## Taking one properly

Call **`EnterWorktree`**. It creates the worktree under `.claude/worktrees/`, moves the
session into it, and from that moment Claude Code enforces the boundary instead of you
remembering it. `cd` into a worktree does none of that — every reach back into the shared
tree still lands, and the session goes on reporting the main checkout as its `cwd`, which
quietly breaks the `list_sessions` signal for everybody else.

When the branch needs a base that is not the repository's default, create it with git
first and then enter that path:

```bash
git worktree add .claude/worktrees/<name> -b <branch> <base>
```

This is not a style preference. `worktree.baseRef` accepts only `"fresh"` (the default
branch on the remote) or `"head"` — never a branch name — so `claude --worktree` and a
bare `EnterWorktree` cut from the default branch. In a repo whose work merges through
`development` rather than `master`, or on a ticket that builds on an unmerged blocker,
that base is wrong and carries the divergence into the diff without complaining.

For **writing subagents** running at the same time as each other, put `isolation: worktree`
in the subagent definition, or ask for "worktrees for your agents", rather than taking one
yourself. For **agent-team teammates**, Claude Code does not isolate them at all: partition
the work so each teammate owns a different set of files, which is what the guard's
same-file rule then holds you to.

## What a worktree does not isolate

- **`refs/stash` is one stack for the whole repository.** A `git stash` in any worktree
  pushes onto every other worktree's entries and renumbers them, so a later `pop` or
  `drop` in *either* takes the wrong one. This is the one place a worktree looks like
  isolation and is not. **Commit instead** — a commit belongs to your branch and no
  stranger can pop it — and leave someone else's `stash@{0}` alone even when it looks
  redundant.
- **A fixed output path in the project's own tooling.** Scripts written when there was one
  checkout name their output after the *project* — `%TEMP%\<project>-tests`,
  `~/.cache/<project>` — and clear it at the start of every run. Every worktree then shares
  one directory, so you wait for a marker file and read a result some other tree produced.
  One repo's test runner did exactly that: three consecutive full-suite runs on a tree
  whose only change was a comment reported 966, 959 and 966 passed. **A number that moves
  between runs on a tree you did not change is shared state, not a flaky test** — find out
  where the runner writes before you chase the flake. Fix it at the default rather than by
  passing a flag every time: derive the path from the checkout — its leaf name, so a reader
  can tell whose it is, plus a few bytes of hash over the absolute path, so two worktrees
  with the same leaf still differ — and keep the explicit override working.
- **Ports, dev servers, databases, and any single machine resource.** Two trees cannot both
  bind the same port, and a timing measurement cannot be trusted while another agent is
  saturating the same disk.
- **The work item.** Two agents can happily take the same ticket. Claim it before you
  build — see [references/ticketing.md](references/ticketing.md).
- **Shared insert points in docs.** An append-ordered changelog or a hand-maintained
  index is a guaranteed conflict on every branch. Prefer one file per entry with a
  generated index, and keep doc edits to the narrowest diff, in one commit, last.

## When the guard denies you

Each denial has exactly one next move. Take it and carry on — do not go looking for a way
around, and do not re-run the same command hoping it lands.

| Denied | Do this |
|---|---|
| `git switch` / `checkout` / `reset` / `rebase` / `merge` / `restore` / `clean` | Call `EnterWorktree` and do it there. You wanted your own branch; now you have your own tree too. |
| `git add -A` / `git add .` / `git commit -a` | `git status --short`, then `git add <path> ...` for the paths you changed, then commit. No worktree needed. |
| `git stash` | Commit instead: `git add <paths> && git commit -m "wip"`. |
| An `Edit`/`Write` to a file another session wrote | Work on files it is not touching, ask it what it is doing, or `EnterWorktree`. |

If you genuinely believe the other session is gone, verify with `list_sessions` before
setting `CLAUDE_PARALLEL_GUARD=off`, and say in your reply that you did it and why.
Turning the guard off because it is inconvenient is how the incident it prevents happens.

## Leave the work where the operator can see it

A branch that exists only on the remote is not finished work.

- **Worked in the main checkout** — nothing to do, which is most of why it is the default.
- **Worked in a worktree** — *leave it there* and name the path in your reply. Removing it
  when the prompt ends is what makes the work vanish: the operator is left with a PR link
  and no files.
- **Main checkout clean, no other session in it** — put it on the branch there too, so the
  diff is where the editor is already open. Never `git switch` a checkout out from under a
  live session, and never over uncommitted work.

Sweep merged worktrees at the **start** of a session, when nothing is in flight:

```bash
git worktree list --porcelain | awk '/^worktree /{print $2}' | tail -n +2
```

Remove the ones that are yours whose branch has merged. Another session's worktree is its
business even after its branch merges — leave it and say it is there. Claude Code's own
periodic sweep already removes subagent and background-session worktrees that hold no
work, and never touches one you made with `--worktree`.

## Keeping N agents cheap

The intuition points at the worktrees, and the worktrees are not the expensive part. One
costs a dependency install and no context at all — a path prefix is the only trace of it
that ever reaches a prompt. What multiplies with N agents is **context**: every agent
loads `CLAUDE.md` on every turn, re-reads the same orienting docs, rediscovers code the
session that spawned it had already read, and runs the full gate into its own window.

So the levers are on the briefing side, not the checkout side:

- **Hand each agent a scoped brief** — the files to read, the one doc it may write —
  rather than "read the docs and work it out". The exploration was already paid for once;
  paying again in a fresh context is the real cost of a second agent.
- **Delegate reads, keep writes.** A read-only subagent returns a summary instead of forty
  file reads. That is the cheap kind of parallelism, and the reason read-only agents are
  carved out above.
- **Run the full gate once, at the end** — and check it reports on *your* tree, not on
  whichever one finished last. Mid-work iterations want a typecheck and a lint.
- **Parallelise by area, not by layer.** Two agents building the same feature conflict in
  the source, and no checkout discipline helps.
- **Don't parallelise sequential work.** Coordination overhead and token cost scale with
  the number of agents; three focused ones beat five scattered ones.

The guard itself costs nothing when you are alone: no output, no tokens, and it never
speaks unless a second session has actually written here.

## Reference

- [references/guard-internals.md](references/guard-internals.md) — what the guard checks,
  its modes, what it deliberately does not cover, and how to debug it.
- [references/ticketing.md](references/ticketing.md) — working a ticket queue with several
  agents, including Matt Pocock's `to-tickets` → `implement` → `code-review` chain.
- [references/migrating-repo-guards.md](references/migrating-repo-guards.md) — for repos
  that already ship a concurrent-writer hook of their own.
