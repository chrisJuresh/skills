# skills

Claude Code skills.

## `worktree-per-change`

One change, one worktree, one branch, one merged PR — plus a guard hook that enforces it.
Nothing is ever written in the main checkout: not a one-line fix, not a typo, not "just
this once".

The absoluteness is the design. A protocol that asks you to judge whether a change is small
enough to do in place fails immediately, because every change is small enough while you are
making it. The rule is also free to check: `.git` is a *file* in a worktree and a
*directory* in the main checkout, so "may I write here" is one stat call and never a
judgement.

What it buys, in the order the failures actually happen. Two agents in one working
directory is a failure that does not announce itself — `git checkout` is a property of the
*directory*, so one session switching branches rewrites the files another is mid-edit on;
the index is a single lock, so one `git add -A` sweeps up another's half-finished files; two
sessions editing one file means the later write discards the earlier one, with no conflict
marker because git never sees two versions. Beyond that, the main checkout stays clean and
on the integration branch, so the operator's editor and dev server show what actually
landed; and every change arrives as a diff someone can read while it is still cheap to
change.

### Install

```bash
git clone https://github.com/chrisJuresh/skills.git
```

```bash
python skills/worktree-per-change/scripts/install.py --repo . --branch development --dry-run
```

Read the diff it prints, then run it without `--dry-run`. Per repository is the usual
install, because the rule depends on what that repository's branches mean. It copies the
guard to `.claude/hooks/`, registers three hooks (`PreToolUse`, `SessionStart`, `Stop`) in
the committed `.claude/settings.json`, writes `.claude/worktree-per-change.json` with the
integration branch, and links the skill into `~/.claude/skills/` so `/worktree-per-change`
resolves everywhere. Commit all three, and check `.gitignore` is not swallowing them — a
worktree only gets a file if git puts it there.

New hooks apply to sessions started afterwards, not the one you are in.

| Flag | Effect |
|---|---|
| `--repo .` | Install into a repository's committed `.claude/settings.json` (the usual case) |
| `--branch NAME` | The branch changes merge into. Default `development` |
| `--status` | What is installed, which branch this repo integrates through, and what each worktree is still holding |
| `--uninstall` | Remove it |
| `--python EXE` | Interpreter to run the guard with, where `python` is not on `PATH` |
| `--keep-legacy` | Leave a predecessor concurrent-writer guard registered instead of replacing it |
| `--no-skill` | Skip the skill link — only if the skill is already installed another way |

Omit `--repo` to install at user scope for every repository on the machine; it applies one
integration branch to repos that may not share it, so prefer per-repo.

Requires Python 3 and git. No third-party packages.

### What the guard denies

- `Edit`/`Write`/`NotebookEdit` anywhere in the **main checkout**
- `git add`, `commit`, `checkout`, `switch`, `restore`, `reset`, `rebase`, `merge`,
  `cherry-pick`, `revert`, `am`, `apply`, `clean`, `rm`, `mv` in the **main checkout**
- Edits in a worktree sitting on the **integration branch**
- Edits in a worktree whose **PR has already merged** — which is what makes "a new worktree
  every time" a rule rather than a habit
- `git stash`, **everywhere** — `refs/stash` is one stack shared by every worktree, so it is
  the one hazard a worktree looks like it isolates and does not

A `Stop` hook refuses to end a session holding uncommitted or unpushed work, twice at most.

Everything else proceeds: every read, `push`, `fetch`, `log`, `diff`, `status`, `branch`,
`git worktree`, `stash list`, every `gh` call, and every edit in a live worktree on its own
branch — which is where all the work happens, so the guard is silent for the whole of a
normal change. Writes outside the repository are never its business.

`CLAUDE_WORKTREE_GATE` selects `on` (default), `warn` (report without denying, for watching
what a repo would block before committing to it), or `off`.
`CLAUDE_INTEGRATION_BRANCH` overrides the configured branch.

### Layout

```
worktree-per-change/
  SKILL.md                                     the skill itself
  references/
    guard-internals.md                         what the guard checks, its modes, how to debug it
    ticketing.md                               working a ticket queue with several agents
    replacing-a-concurrent-writer-guard.md     migrating a repo that ships its own hook
  scripts/
    install.py                                 install / --status / --uninstall
    worktree_guard.py                          the hook
    test_guard.py                              48 checks against real git repos in a temp dir
  evals/
    evals.json                                 skill evals
    fixture/                                   synthetic repo the evals run against
```

### Tests

```bash
python worktree-per-change/scripts/test_guard.py
```
