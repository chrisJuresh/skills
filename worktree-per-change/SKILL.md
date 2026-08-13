---
name: worktree-per-change
description: >-
  One change, one worktree, one branch, one merged PR — the protocol for repositories
  where nothing is ever written in the main checkout, and the guard hook that enforces
  it. Use this skill before the first Edit or Write in any repository that has the guard
  installed, when a write, `git switch`, `git add` or `git stash` is denied, when
  EnterWorktree cuts from the wrong base, when a change is finished and has to be pushed
  and merged, when a second change starts in a session that already merged one, and when
  the user wants this rule installed in a repository or on a machine.
---

# One change, one worktree, one branch, one merged PR

Every change is made in its own git worktree, on its own branch, and reaches the
integration branch as a merged pull request. Nothing is written in the main checkout —
not a one-line fix, not a typo, not "just this once".

The absoluteness is the whole design. A protocol that asks you to judge whether a change
is small enough to do in place fails immediately, because every change is small enough
while you are making it. The rule is cheap to follow and free to check: `.git` is a
*file* in a worktree and a *directory* in the main checkout, so "am I allowed to write
here" is one stat call and never a judgement.

What it buys, in the order the failures actually happen:

- **Two writers never share a directory.** `git checkout` is a property of the
  directory, so a session switching branches rewrites files another is mid-edit on. The
  index is a single lock, so one `git add -A` sweeps up another's half-finished work.
  Two sessions editing one file means the later write silently discards the earlier —
  git never sees two versions, so there is no conflict marker. None of these produce an
  error.
- **The main checkout stays trustworthy.** It is on the integration branch, clean, and
  pullable, so the operator's editor and dev server always show what actually landed
  rather than somebody's work in progress.
- **Every change is reviewable before it lands.** A PR per change is a diff someone can
  read while it is still cheap to change, and a history where each entry is one thing.

## The loop

```bash
# 1. before the first edit — a worktree cut from the integration branch
git worktree add .claude/worktrees/<name> -b <short-topic-name> origin/<integration>
```

Then call **`EnterWorktree`** with that path. Entering is what matters: Claude Code
enforces the boundary from that moment — edits, command working directories and
`git -C`/`GIT_DIR`/`cd`-then-git redirects that reach back into the main checkout are all
refused, for the session and every subagent under it. A `cd` into the worktree does none
of that, and the session goes on reporting the main checkout as its `cwd`, which quietly
breaks the signal every other session reads.

A bare `EnterWorktree` (no `git worktree add` first) is only correct when the
repository's **default branch is also its integration branch**. `worktree.baseRef`
accepts `"fresh"` or `"head"` and never a branch name, so in a repo that merges through
`development` a bare call cuts from `main` and carries the divergence into your diff
without complaining. Check `.claude/worktree-per-change.json` for which branch this repo
integrates through.

```bash
# 2. work, then commit — name the paths, never `git add -A`
git add <path> ...
git commit -m "<what changed>"

# 3. deliver — all three steps, unasked
git push -u origin HEAD
gh pr create --base <integration> --fill
gh pr merge --squash
```

Pushing and merging are part of finishing, not a separate errand to be asked about. A
branch that exists only on this disk is not a delivered change: the operator is left
with a directory nobody will look in, and the next worktree is cut from an integration
branch that is missing your work. The `Stop` hook refuses to end a session that is
walking away from uncommitted or unpushed work, and says which.

```bash
# 4. the next change starts over
```

A second change in the same session gets a **new** worktree and a **new** branch, cut
from the integration branch you just merged into. The guard marks a worktree spent once
`gh pr merge` has run in it and denies further edits there — a merged branch that grows
a new commit reaches nobody, because the PR that would have carried it is already
closed.

On Windows, write a multi-line PR body to a file and pass `--body-file`, and write that
file **without a BOM** — PowerShell's `Set-Content -Encoding utf8` emits one, it lands at
the top of the body, and it stops a leading markdown heading from rendering. Use
`[System.IO.File]::WriteAllText($path, $text, (New-Object System.Text.UTF8Encoding $false))`.

## What a fresh worktree does not have

This is the cost that changed. Under a rule where worktrees were occasional, setting one
up was a rare tax; under this one it is paid on **every change**, so it is worth making
cheap rather than rediscovering it each time.

A worktree is a fresh checkout of tracked files and nothing else. Dependencies, build
output, local config and anything else `.gitignore` covers are simply absent:

- **Dependencies.** `node_modules/`, a virtualenv, a `pnpm install`. Some repos dodge
  most of this by committing build output — check before assuming a full install is
  needed; often only a change that touches source requires one.
- **Ignored-but-required config.** A `.claude/launch.json` that tells the preview how to
  start the dev server, an `.env`, an editor config. If it is ignored, no worktree has it,
  and the failure looks like the tool being broken rather than the file being missing.
- **Untracked scratch state** the last session left in the main checkout.

Two ways to fix it, and the second is better for anything a *human* also needs:

- **`.worktreeinclude`** lists untracked paths Claude Code copies into each new worktree.
  Right for machine-local secrets and caches that must not be committed.
- **Un-ignore the file.** If every worktree needs it and it holds nothing private, the
  honest answer is to commit it — a worktree only gets a file if git puts it there. This
  applies to the guard itself: `.claude/settings.json`, `.claude/hooks/worktree-guard.py`
  and `.claude/worktree-per-change.json` must be tracked, or the rule stops applying
  inside the very worktrees it sends you to. A repo that ignores `.claude/` wholesale
  needs its ignore narrowed to name them, keeping `settings.local.json` and
  `.claude/worktrees/` out.

A repo that commits the hook should also test it, in its own test suite and idiom — the
committed copy is what actually runs, and a hook that silently stopped denying looks
exactly like a hook that had nothing to deny.

## Installing it

Per repository is the usual install, because the rule depends on what that repository's
branches mean:

```bash
python "${CLAUDE_SKILL_DIR}/scripts/install.py" --repo . --branch development --dry-run
```

Show the user that output, then run it without `--dry-run`. It copies the guard to
`.claude/hooks/`, registers three hooks in the committed `.claude/settings.json`, writes
`.claude/worktree-per-change.json` with the integration branch, and links this skill into
`~/.claude/skills/` so `/worktree-per-change` resolves everywhere. Commit all three, and
check `.gitignore` is not swallowing them.

A repo install registers the hook as `python` against
`${CLAUDE_PROJECT_DIR}/.claude/hooks/worktree-guard.py`, deliberately: the file is
committed, so it must not carry the installing machine's interpreter path or this
checkout's absolute location, and `${CLAUDE_PROJECT_DIR}` resolves to whichever worktree
the session is actually in. `--python` overrides the interpreter where `python` is not on
`PATH`.

- Omit `--repo` to install at user scope for every repository on the machine. It applies
  one integration branch to repos that may not share it, so prefer per-repo.
- `--status` reports what is installed, which branch this repo integrates through,
  whether the cwd may write, and every worktree with what it is still holding.
- `--uninstall` removes it. `--keep-legacy` leaves a predecessor concurrent-writer guard
  registered instead of replacing it.

New hooks apply to sessions started afterwards, so say so rather than letting the user
assume the current session is covered.

The integration branch has to exist on the remote before the first PR. If the repo
integrates through a branch it does not have yet, create it from the default branch and
push it once — and say you did, because it changes what everyone else's PRs target.

## When the guard denies you

Each denial has exactly one next move. Take it and carry on — do not go looking for a
way around, and do not re-run the same command hoping it lands.

| Denied | Do this |
|---|---|
| `Edit`/`Write`/`NotebookEdit` in the main checkout | `git worktree add` off the integration branch, then `EnterWorktree` that path. |
| `git switch` / `add` / `commit` / `reset` / `rebase` / `merge` / `clean` in the main checkout | The same. The main checkout is for reading and pulling; nothing else runs there. |
| An edit in a worktree that is on the integration branch | `git switch -c <short-topic-name>` first. |
| An edit in a worktree whose PR has merged | That change is finished. Take a new worktree for the next one. |
| `git stash`, anywhere | Commit instead: `git add <paths> && git commit -m "wip"`. |

`git stash` is denied in worktrees too, and that is not an oversight: `refs/stash` is a
single stack for the whole repository, so a push in one worktree renumbers every other
worktree's entries and a later `pop` or `drop` in *either* takes the wrong one. It is the
one hazard a worktree looks like it isolates and does not.

If the guard is provably wrong, set `CLAUDE_WORKTREE_GATE=off` for the session and say in
your reply that you did it and why. `CLAUDE_WORKTREE_GATE=warn` reports without denying,
which is the setting for watching what a repo would have blocked before committing to it.

## What a worktree still does not isolate

- **Ports, dev servers, databases, and any single machine resource.** A build writing a
  shared output directory still kills a dev server serving it, and a timing measurement
  cannot be trusted while another agent saturates the same disk.
- **The work item.** Two agents can happily take the same ticket. Claim it before you
  build — see [references/ticketing.md](references/ticketing.md).
- **Shared insert points in docs.** An append-ordered changelog or a hand-maintained
  index conflicts on every branch. Prefer one file per entry with a generated index, and
  keep doc edits to the narrowest diff, in one commit, last.

## Housekeeping

Leave your worktree standing until its PR has merged; the path is where the operator
finds the work, so name it in your reply. After the merge it holds nothing that the
integration branch does not, and `ExitWorktree` can remove it.

Sweep merged worktrees at the **start** of a session, when nothing is in flight:

```bash
python "${CLAUDE_SKILL_DIR}/scripts/install.py" --status
```

Remove the ones reported as `clean and landed` that are yours. Another session's
worktree is its business even after its branch merges — leave it and say it is there.
Claude Code's own periodic sweep already removes subagent and background-session
worktrees that hold no work.

## Cost, and where it actually is

The worktrees are not the expensive part. One costs a dependency install and no context
at all — a path prefix is the only trace of it that ever reaches a prompt. What
multiplies with N agents is **context**: every agent loads `CLAUDE.md` on every turn,
re-reads the same orienting docs, and rediscovers code the session that spawned it had
already read.

So the levers are on the briefing side:

- **Hand each agent a scoped brief** — the files to read, the one doc it may write —
  rather than "read the docs and work it out".
- **Delegate reads, keep writes.** A read-only subagent returns a summary instead of
  forty file reads, and needs no worktree of its own: it cannot cause the failure this
  protocol prevents. Writing subagents running at the same time as each other get
  `isolation: worktree` in the subagent definition.
- **Run the full gate once, at the end.** Mid-work iterations want a typecheck and a lint.
- **Parallelise by area, not by layer.** Two agents building the same feature conflict in
  the source, and no checkout discipline helps.

## Reference

- [references/guard-internals.md](references/guard-internals.md) — what the guard checks,
  its modes, what it deliberately does not cover, and how to debug it.
- [references/ticketing.md](references/ticketing.md) — working a ticket queue with
  several agents, including Matt Pocock's `to-tickets` → `implement` → `code-review` chain.
- [references/replacing-a-concurrent-writer-guard.md](references/replacing-a-concurrent-writer-guard.md)
  — migrating a repo that already ships a hook of its own.
