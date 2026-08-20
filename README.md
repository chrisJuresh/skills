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
integration branch and the provenance of the copy it just made, and links the skill into
`~/.claude/skills/` so `/worktree-per-change` resolves everywhere. Commit all three, and
check `.gitignore` is not swallowing them — a worktree only gets a file if git puts it
there.

Re-running it is how a repo resyncs. The config file is merged rather than replaced, so
the branch, the provenance and anything else that repo keeps beside them all survive.

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
| `--settings-file NAME` | Register in a different settings file, e.g. `settings.local.json` |
| `--guard-root DIR` | Keep the guard and `land.py` in `DIR` and reference them absolutely |
| `--worktrees-root PATH` | Where this repo's worktrees go, quoted in the guard's remedy text |

Omit `--repo` to install at user scope for every repository on the machine; it applies one
integration branch to repos that may not share it, so prefer per-repo.

Some repositories cannot take the commit at all — a shared checkout where this would change
what a *colleague's* session is allowed to do in their own working directory. There,
`--settings-file settings.local.json --guard-root <dir>` gives a real install with nothing
added to the repo. The catch is worth knowing before you choose it: a worktree is a checkout
of tracked files, so an untracked settings file is absent from every worktree, and whatever
creates worktrees has to write one into each of them. The installer says so, and `--status`
marks it.

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
normal change. Neither writes nor `git` calls that target **another** repository are ever its
business: every rule is judged against the tree the operation names, so `cd ~/other-repo &&
git add -A` passes, and a `git -C` back into this repository's main checkout does not.

`CLAUDE_WORKTREE_GATE` selects `on` (default), `warn` (report without denying, for watching
what a repo would block before committing to it), or `off`.
`CLAUDE_INTEGRATION_BRANCH` overrides the configured branch. Both are read from the hook's
environment, so they are the **operator's** switches: a per-command prefix is set after the
hook has already run, and a change applies to sessions started afterwards.

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
    test_guard.py                              104 checks on what the hook decides
    test_install.py                            27 checks on what the installer leaves on disk
  evals/
    evals.json                                 skill evals
    fixture/                                   synthetic repo the evals run against
```

### Tests

Both run against real git repositories in a temp dir, and need nothing but git.

```bash
python worktree-per-change/scripts/test_guard.py
```

```bash
python worktree-per-change/scripts/test_install.py
```
