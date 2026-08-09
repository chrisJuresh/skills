# The guard: what it checks, and what it deliberately does not

`scripts/parallel_guard.py` runs as a `PreToolUse`, `SessionStart` and `SessionEnd` hook.
Read this when it fires somewhere it should not, when a repo needs different strictness,
or before changing it.

## Contents

- [The rule set](#the-rule-set)
- [How it decides who is live](#how-it-decides-who-is-live)
- [Modes](#modes)
- [Where it stands down](#where-it-stands-down)
- [What it does not protect](#what-it-does-not-protect)
- [Cost](#cost)
- [Debugging](#debugging)
- [Changing it](#changing-it)

## The rule set

A session enters the registry the first time it actually **writes**, and never before. So
an idle session and a read-only agent claim nothing, and a lone writer is never blocked —
which is the case worth protecting hardest, because a guard that strands the only person
in the tree is a guard someone deletes.

Once a second session is live in the same working tree, four things are denied:

| Denied | Why it has to be a denial rather than a warning |
|---|---|
| `checkout`, `switch`, `restore`, `reset`, `rebase`, `merge`, `cherry-pick`, `revert`, `am`, `apply`, `clean` | They rewrite the working tree and move `HEAD` under whoever else is in it. Neither session sees an error. |
| `git add -A`/`.`/`-u`, `git commit -a`/`-am` | They stage the whole tree, so your commit carries another session's half-finished files into history under your message. |
| `git stash` (checked repository-wide, not per tree) | `refs/stash` is one stack shared by every worktree. A push renumbers another session's entries, so a later `pop` takes the wrong one. |
| `Edit`/`Write`/`NotebookEdit` on a file another live session has written | The later write discards the earlier one. Git never sees two versions, so there is no conflict marker. |

Everything else proceeds: targeted `git add <path>`, an ordinary `commit`, `push`, `fetch`,
`log`, `diff`, `status`, `branch`, `tag`, `stash list`, `git worktree` (blocking the remedy
would be a trap), and any edit to a file nobody else is in.

That restraint is the design, not an omission. Claude Code's own agent teams share one
working directory on purpose and partition files between teammates, so a guard that denied
every write to a shared checkout would deny a supported workflow — and a rule people route
around protects nothing. Denying only the silent-wrongness leaves the supported shapes
working and still catches every incident that motivated this: a branch switch landing in a
directory another session was mid-edit on, and a commit sweeping up work its author never
saw.

## How it decides who is live

Each session owns one file at `<git-common-dir>/claude-parallel-sessions/<session-id>.json`,
holding its working-tree root, its transcript path, the time of its last write, and the
paths it has written recently (capped, and pruned to the liveness window).

One file per session, so two sessions writing at once cannot lose each other's update —
which was the whole failure mode being guarded against, and would have been embarrassing.
Inside the shared git directory, so every worktree of the repository reads the same
registry and nothing ever shows up in `git status`.

A claim counts as live while **either** its last write **or** its transcript file's mtime
is within 20 minutes. The transcript is the better of the two: it moves on every turn, so a
session that has spent twenty minutes reading still looks alive, where a
last-write-only signal would hand its checkout away mid-task. Twenty minutes is longer than
any gap inside a working session and short enough that a session killed rather than closed
frees the tree without anyone tidying up.

`SessionEnd` deletes the claim outright, so a clean exit frees the tree immediately. A
claim that has gone stale and then comes back restarts its clock rather than reclaiming
seniority, so a session returning from a long idle cannot displace whoever took the tree
while it was away.

## Modes

`CLAUDE_PARALLEL_GUARD` controls it:

- **`balanced`** (default) — the rule set above.
- **`strict`** — denies *any* write landing in a checkout another live session holds, so a
  second writer isolates before it writes anything rather than after it collides. This is
  the rule most repos that ship their own guard settle on. It is the right setting for a
  repo where the operator is often in an editor at the same time, and the wrong one
  anywhere agent teams are used.
- **`off`** — the escape hatch. Use it when the guard is provably wrong, say so in your
  reply, and check `list_sessions` first.

Per repository, set it where it lives with the repository:

```json
{
  "env": { "CLAUDE_PARALLEL_GUARD": "strict" }
}
```

in that repo's `.claude/settings.json`. Per session, export it in the environment.

## Where it stands down

Before doing anything else the guard exits silently when:

- the mode is `off`;
- `cwd` is not inside a git repository, or the git metadata will not resolve;
- the repository already registers a concurrent-writer hook of its own in
  `.claude/settings.json` or `.claude/settings.local.json` — matched on
  `concurrent-writer`, `writer-guard` or `parallel-guard` in the command, with the separator
  optional so `concurrent_writer_guard.py` matches too. Two guards means two denials with two
  different remedies for one action, which is the flail this exists to prevent.
  `install.py --status` applies the same test, so the two never disagree about whether a repo
  is covered. See [migrating-repo-guards.md](migrating-repo-guards.md).

It also fails open on every question it cannot answer: an unparseable payload, a missing
session id, an unreadable registry, a claim file that is not valid JSON. Blocking the only
writer in a tree over state it merely failed to read is the worse error.

## What it does not protect

Honest limits, so nobody assumes cover that is not there.

- **A human in an editor, or a plain `claude` CLI session**, writes without ever running
  this hook. `git status --porcelain` showing work you did not make remains the only signal
  that catches them, which is why it stays in the table in `SKILL.md`.
- **A shell redirect into a file** — `echo x > file` — is not parsed. A parser that guessed
  at shell semantics would be a worse hole than the one it closed.
- **`git -C "$W" commit`** reports no target, because the `-C` read is textual and never
  expands a variable; the call is judged against the session's own directory instead. Spell
  the path out literally when you mean another tree. This is the guard being conservative
  rather than clever.
- **Sibling writing subagents** may share their parent's session id, in which case they are
  one writer as far as the registry is concerned. `isolation: worktree` is the mechanism
  for those, not this.
- **Everything a worktree does not isolate** — ports, databases, a runner's fixed output
  path, the work item itself. Those are in `SKILL.md` and in [ticketing.md](ticketing.md).

## Cost

Zero tokens when uncontended: the guard produces no output at all unless it denies
something or another session has already written here. The uncontended path is one
directory listing and one small file write — no subprocess, no `git` call; the repository
is located by walking up for `.git` rather than shelling out, precisely because a
subprocess per write-tool call is the one cost that cannot be amortised.

Wall-clock is interpreter startup, roughly 75 ms per guarded tool call on Windows (the
install registers `-S` to skip site initialisation, which is about 13% of that). Against a
tool call that itself takes hundreds of milliseconds, and against a commit that quietly
contains someone else's work, that is the right trade.

## Debugging

```bash
python scripts/install.py --status        # what is installed, and who is in this checkout
python scripts/test_guard.py              # 47 checks against real git repos in a temp dir
```

`--status` lists every claim on the current repository with its age and whether it counts
as live, and flags a repo that ships its own guard. To see a decision directly, feed the
guard a payload on stdin:

```bash
echo '{"session_id":"x","hook_event_name":"PreToolUse","cwd":".","tool_name":"Bash","tool_input":{"command":"git switch main"}}' | python scripts/parallel_guard.py
```

Empty output means allowed. Claim files are inert once the guard is uninstalled; delete
`<repo>/.git/claude-parallel-sessions/` if you want them gone.

## Changing it

Run `test_guard.py` after any change — every case in it is either a failure someone
actually hit or a false positive that would make someone delete the hook, and the second
category is the one that matters. Adding a subcommand to `FLOOR_MOVERS` is cheap; every
false positive spends trust the guard has to keep, so breadth is the wrong instinct.
`Edit`/`Write` already cover file writes, and the shell rules only need to cover the
failures a file edit cannot cause.
