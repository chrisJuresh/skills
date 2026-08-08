# skills

Claude Code skills.

## `parallel-agents`

Run several Claude sessions, teammates or writing subagents in one repository without them
silently corrupting each other's work — plus a guard hook that enforces it in every repo
automatically.

Two agents in one working directory is a failure that does not announce itself. `git
checkout` is a property of the *directory*, so one session switching branches rewrites the
files another is mid-edit on. The index is a single lock, so one session's `git add -A`
sweeps up another's half-finished files. Two sessions editing one file means the later write
discards the earlier one — git never sees two versions, so there is no conflict marker.
None of these produce an error.

Claude Code already enforces isolation for a session that is *inside* a worktree. What
nothing covers is the step before that: sessions sharing the main checkout, none of them
isolated yet. That gap is what this skill fills.

### Install

```bash
git clone git@github.com:chrisJuresh/skills.git
```

```bash
python skills/parallel-agents/scripts/install.py --dry-run
```

Read the diff it prints — it writes to your global `~/.claude/settings.json` — then run it
without `--dry-run`. It copies the guard to `~/.claude/hooks/`, registers three hooks
(`PreToolUse`, `SessionStart`, `SessionEnd`), and links the skill into `~/.claude/skills/`
so `/parallel-agents` resolves everywhere. Keep the clone where it is; the skill is linked,
not copied.

New hooks apply to sessions started afterwards, not the one you are in.

| Flag | Effect |
|---|---|
| `--repo .` | Install into a repository's committed `.claude/settings.json` instead, so the rule holds for teammates too |
| `--status` | What is installed, and who else has written in this checkout |
| `--uninstall` | Remove it (add `--repo` to remove a repo install) |
| `--no-skill` | Skip the skill link — only if the skill is already installed another way |

Requires Python 3 and git. No third-party packages.

### What the guard denies

Only once a **second** session is actually writing in the same working tree. A lone writer
is never blocked, a read-only agent claims nothing, and the uncontended path produces no
output and no tokens.

- Working-tree movers — `checkout`, `switch`, `restore`, `reset`, `rebase`, `merge`,
  `cherry-pick`, `revert`, `am`, `apply`, `clean`
- Blind staging — `git add -A`/`.`/`-u`, `git commit -a`/`-am`
- `git stash` — `refs/stash` is one stack shared by every worktree, so it is the one failure
  a worktree does not isolate
- `Edit`/`Write`/`NotebookEdit` on a file another live session has written

Everything else proceeds: targeted `git add <path>`, ordinary commits, `push`, `fetch`,
`log`, `diff`, `status`, `git worktree`, and any edit to a file nobody else is in.

`CLAUDE_PARALLEL_GUARD` selects `balanced` (default), `strict` (deny any write landing in a
shared checkout), or `off`.

### Layout

```
parallel-agents/
  SKILL.md                              the skill itself
  references/
    guard-internals.md                  what the guard checks, its modes, how to debug it
    ticketing.md                        working a ticket queue with several agents
    migrating-repo-guards.md            for repos that already ship their own guard
  scripts/
    install.py                          install / --status / --uninstall
    parallel_guard.py                   the hook
    test_guard.py                       47 checks against real git repos in a temp dir
  evals/
    evals.json                          skill evals
    fixture/                            synthetic repo the evals run against
```

### Tests

```bash
python parallel-agents/scripts/test_guard.py
```
