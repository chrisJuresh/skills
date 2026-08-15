---
name: worktree-per-change
description: >-
  One change, one worktree, one branch, one merged PR — the protocol for repositories
  where nothing is ever written in the main checkout, and the guard hook that enforces
  it. Use this skill before the first Edit or Write in any repository that has the guard
  installed, when a write, `git switch`, `git add` or `git stash` is denied, when
  EnterWorktree cuts from the wrong base, when a change is finished and has to be pushed,
  merged and then taken down, when a session is refused permission to stop, when a second
  change starts in a session that already merged one, and when the user wants this rule
  installed in a repository or on a machine.
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
# 1. before the first edit — a worktree cut from the FETCHED integration branch
git fetch origin <integration>
git worktree add .claude/worktrees/<name> -b <short-topic-name> origin/<integration>
```

Then call **`EnterWorktree`** with that path. Entering is what matters: Claude Code
enforces the boundary from that moment — edits, command working directories and
`git -C`/`GIT_DIR`/`cd`-then-git redirects that reach back into the main checkout are all
refused, for the session and every subagent under it. A `cd` into the worktree does none
of that, and the session goes on reporting the main checkout as its `cwd`, which quietly
breaks the signal every other session reads.

**The base is `origin/<integration>` — the fetched remote tip — and never anything else.**
Not local HEAD, not whatever branch the main checkout is sitting on, not a local
`<integration>` ref that has not been fetched since someone else merged. Fetch first: the
whole point of merging every change is that the next one starts from it, and a stale local
ref silently reintroduces work you already landed as a conflict.

That is also why a bare `EnterWorktree` (no `git worktree add` first) is only correct when
the repository's **default branch is also its integration branch** *and* you want it fresh
from the remote. `worktree.baseRef` never accepts a branch name — it chooses between two
values, and in a repo that integrates through anything but its default branch **both are
wrong**:

- `"fresh"` cuts from the repository's default branch, so in a repo that merges through
  `development` it cuts from `main` and carries the whole divergence between them into your
  diff;
- `"head"` cuts from local HEAD — whatever the last session or person left that directory
  on, including unmerged work and a branch that has since been squash-merged and deleted.

Neither complains, and both produce a diff containing changes you did not make. Check
`.claude/worktree-per-change.json` for which branch this repo integrates through, and cut
from `origin/` that.

```bash
# 2. work, then commit — name the paths, never `git add -A`
git add <path> ...
git commit -m "<what changed>"

# 3. deliver — all of it, unasked
python .claude/scripts/land.py        # push, PR if there is none, merge, verify
```

`land.py` is the same four commands with the verification that each of them needs, and it
exists so that the delivery step can be *allowed* — see below. Where a repo does not have
it, run them by hand and keep the verification:

```bash
git push -u origin HEAD
gh pr create --base <integration> --fill
gh pr merge --squash                       # NOT --delete-branch — see below
gh pr view <n> --json state --jq .state    # expect MERGED, whatever gh exited
git push origin --delete <short-topic-name>
```

**The delivery step is the one a permission layer stops, and a stopped delivery is not a
delivered change.** This is not the guard refusing — it is the machine not having been told
that this agent may push and merge — and the two want opposite responses: obey the guard,
and *fix* the permission, once, in `settings.json`. `install.py` writes the entries;
[references/permissions.md](references/permissions.md) is the list and the reasoning.

Never wrap a command to make it unrecognisable to that layer. It does not work — what is
inspected is the command about to run — and it takes a decision away from the person whose
decision it is. `land.py` is the legitimate shape: it makes the grant **smaller**, not
quieter. It takes no PR number and no branch, so it can only ever merge the PR whose head is
the branch in the worktree it was run from, into the branch that repo recorded. One entry
for it grants the protocol; `Bash(gh pr merge:*)` would grant every PR on the machine.

Pushing and merging are part of finishing, not a separate errand to be asked about. A
branch that exists only on this disk is not a delivered change: the operator is left
with a directory nobody will look in, and the next worktree is cut from an integration
branch that is missing your work. The `Stop` hook refuses to end a session that is
walking away from uncommitted or unpushed work, and says which — and equally refuses one
that walks away from a worktree it has already merged (step 4).

**Deleting the branch is not tidiness.** A merged branch left standing is a live push
target after the PR that reviewed it has closed — the same failure the spent-worktree
rule catches one level down, and harder to notice, because a commit pushed there looks
like ordinary work on an ordinary branch and reaches the integration branch never.
Deleting it makes that push fail loudly instead. It also keeps `git branch -r` readable,
which is what makes a genuinely unmerged branch visible at all. What must not do it for
you is `gh`'s `--delete-branch` — see below.

```bash
# 4. take the tree down — this is still finishing, not tidying
gh pr view <n> --json state --jq .state          # expect MERGED
#   ExitWorktree with action: "keep"             — puts the SESSION back in the main
#   checkout; the removal is git's job (see below)
git worktree remove <path>                       # from the main checkout
git branch -D <short-topic-name>
```

**Do not use `ExitWorktree` with `action: "remove"` for this.** It removes only a
worktree `EnterWorktree` *itself* created, and under this protocol the tree is made with
`git worktree add` and entered by `path` — out of its scope. Measured: it refuses, saying
the session does not own the worktree and to use `action: "keep"`, and it names the other
cause too — another live session holding that tree's liveness lock, where git will refuse
as well and tell you the owner. Ask for `"keep"`, then remove the tree with git.

The order is forced: nothing can remove the working tree it is standing in, and from
inside a worktree Claude Code refuses `git -C <main>` redirects back out. So the exit
comes first and the removal second — two steps, and no way to fold them into one.

**So do not pass `--delete-branch` under this protocol at all.** The flag makes `gh` do
local git work after the API call, and there is no arrangement of worktrees in which that
work can succeed here: to delete the merged branch it checks out the **base** branch, and
the main checkout is permanently sitting on the base. Measured 2026-08-15, landing
`land.py`'s own first change:

```
failed to run git: fatal: 'main' is already used by worktree at 'C:/Users/Chris/Desktop/skills'
```

`gh` exited 1, the pull request was **MERGED**, and the branch was still on the remote.
That is the shape to remember, because it is the expensive one: the flag fails *after* the
merge, so the exit code describes the cleanup and says nothing about whether the change
landed. The earlier reading of this failure was that the local delete runs first and
**abandons the remote one** when it fails — true, and it has the same consequence: the
branch you asked it to remove is exactly the branch left standing.

**A non-zero exit from `gh pr merge` is therefore a question, not an answer.** Ask the
forge before believing it. Reporting a merged change as unlanded is worse than the merge
failing outright, because the next session redoes work that is already on the integration
branch. `land.py` does this: it merges without the flag, checks `state` whatever `gh`
returned, deletes the remote branch itself, and says so when `gh` failed after a merge that
landed.

So verify, and finish by hand — asking the **remote**, not a tracking ref:

```bash
git ls-remote --heads origin <short-topic-name>   # is it still there?
git push origin --delete <short-topic-name>       # if so
```

**Not `git fetch --prune` then `git branch -r`**, which is what this recipe used to say, and
which is wrong twice over. `git branch -r` reads *tracking* refs, so it answers out of a
local cache rather than from the remote. And the fetch that refreshes that cache is the step
likeliest to fail at exactly this moment: `git fetch` compare-and-swaps every tracking ref it
touches, so anything that moved the integration branch underneath it fails the **whole**
fetch with `cannot lock ref … is at X but expected Y` — and merging your own PR moves
precisely that ref, so under this protocol it is the common case rather than a rare race.
`git branch -r` then answers from stale data, and the reading that costs you is the confident
one: the branch looks already gone, you skip the delete, and it is still standing. Measured
twice in one afternoon, on top of the `--delete-branch` failure above. `git ls-remote` asks
the remote and cannot be stale.

If you wrap this teardown in a script, keep the refresh — if you keep one at all — **out of
the failure path**. Under `set -e` a failed fetch aborts the run *after* the worktree and
local branch are gone and *before* the remote delete, which is the worst available ordering:
the destructive half has happened, the tidying half has not, and the exit code arrives too
late to mean anything. A genuinely unreachable remote is the one case worth failing on, and
worth failing loudly, because then the branch's fate is unknowable rather than merely
unrefreshed.

Freeing the worktree before deleting the local branch is right anyway: deleting a branch
out from under a live worktree leaves the worktree on a detached HEAD and git unsure
which of the two to believe.

**Use `-D`, and check the PR rather than the ancestry.** The instinct is `git branch -d`,
because refusing to delete an unmerged branch sounds like exactly the safety check you
want. It is the wrong check here. `-d` asks whether your commits are *ancestors* of the
branch you are on, and `--squash` does not preserve ancestry: it replays your diff as one
new commit, so a squash-merged branch looks completely unmerged to `-d` and to
`git merge-base --is-ancestor`. Under this protocol that is every branch. The forge is the
only thing that knows, so ask it — `gh pr view <n> --json state` reporting `MERGED` — and
then `-D`. A blind `-D` without that check is how genuinely unmerged work disappears.

The same trap catches `git branch --merged <integration>`: it lists nothing after a squash
merge, so it is not a sweep, and a branch missing from it has not necessarily survived.

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

**And it should gate the copy's provenance, which is a different failure.** A committed hook is
a fork the moment this skill moves, and a stale one is the one kind of broken hook that *still
looks like it works*: it denies confidently and prints a remedy that no longer fits. Its own
suite will not catch that, because the suite was copied at the same time and is equally old.

Measured 2026-08-13. A downstream repo's copy was one release behind the fix for exactly the
failure it then hit — a `gh pr merge` the forge refused marked the tree as landed, and every
edit needed to resolve the conflict was denied as work already delivered. "Resync it when
upstream moves" was written down and had nothing to say *when*, because nothing recorded which
upstream commit the copy came from.

So record it and check it. `install.py` writes the record itself, beside the branch name in
`.claude/worktree-per-change.json`, because it is the only thing that knows both halves at
the one moment they are both true — a hand-written record is right once and silently wrong
from the next resync on, which is this same failure one level up:

```json
{ "integrationBranch": "queue",
  "guard": { "source": "…/worktree_guard.py", "syncedFrom": "<sha>", "sha256": "<hash>" } }
```

It merges rather than replaces, so re-running it to resync keeps the branch and anything
else the repo keeps in that file. `syncedFrom` is absent when the skill directory is not a
git checkout — a tarball cannot name a commit, and saying nothing is honest where a stale
sha is not.

**`sha256` is over the file's LF-normalised bytes, and a gate checking it must normalise
too.** The record crosses platforms and the bytes on disk do not: a repo pinning
`* text=auto eol=lf` hands out LF everywhere, one leaving it to `core.autocrlf` hands out
CRLF on Windows, so a hash of the working copy is true on the machine that installed and
false on the Linux runner meant to check it. It then fails in the worst direction — drift
reported in a file nobody touched, which teaches people to ignore the check. Normalising
is also what git stores, so the two sides agree without knowing each other's settings.

The record is what makes the copy checkable; the checking is still the repo's to do. A
check in its gate asks two questions, and only the first is answerable on a CI
runner: **does the committed file match its record** (offline — catches an edit in place, since
the copy is not the repo's to edit, and a resync that forgot to record itself), and **has
upstream moved** (needs a clone, so it must *skip out loud* rather than fail — a check that goes
red over a clone nobody has is a gate nobody can turn green, and the first person to hit it
deletes the step). Print the skip as `UNVERIFIED`, never as a pass: the whole failure above was
something unverified reading as fine. `integration-console`'s
[`scripts/check-guard.mjs`](https://github.com/third-bridge/hermes-frontend) is a worked
example. Don't fetch this repo from CI — that puts a third-party's availability on a required
check — and don't auto-resync, because the suite has to run against the new file first.

## Installing it

Per repository is the usual install, because the rule depends on what that repository's
branches mean:

```bash
python "${CLAUDE_SKILL_DIR}/scripts/install.py" --repo . --dry-run
```

Show the user that output, then run it without `--dry-run`. It copies the guard to
`.claude/hooks/` and `land.py` to `.claude/scripts/`, registers three hooks and the
allowlist in the committed `.claude/settings.json`, writes
`.claude/worktree-per-change.json` with the integration branch, and links this skill into
`~/.claude/skills/` so `/worktree-per-change` resolves everywhere. Commit all four, and
check `.gitignore` is not swallowing them.

**It asks which branch changes merge into, and does not guess.** This is the setting that
is silently wrong: a guard pointed at the wrong integration branch denies nothing and
breaks nothing, it just aims every future PR at a branch nobody merges, and nothing looks
broken until somebody goes looking for the work. Repos differ on this in ways no
inspection settles — some integrate through their default branch, others hold changes on
`development` or a `queue` branch and promote from there — so **ask the user, do not read
it off `origin/HEAD`.** Measured 2026-08-15: a repo with both `main` and `development` had
its newest PRs on `main` and two older ones on `development`, and no rule over the branch
list gets that right. Pass `--branch <name>` once they have answered; that also makes the
install non-interactive, and with no answer available at all it refuses rather than picks.

It also writes a `permissions.allow` block — read-only `git` and `gh` at both scopes, and
the protocol's own writes at repo scope. That is the second half of making the rule
followable: the guard says what may not be done, and the allowlist stops the machine
querying the parts that must be. `--no-permissions` skips it;
[references/permissions.md](references/permissions.md) has the list, what is deliberately
left out, and why the read-only half also belongs in `~/.claude/settings.json`.

A repo install registers the hook as `python` against
`${CLAUDE_PROJECT_DIR}/.claude/hooks/worktree-guard.py`, deliberately: the file is
committed, so it must not carry the installing machine's interpreter path or this
checkout's absolute location, and `${CLAUDE_PROJECT_DIR}` resolves to whichever worktree
the session is actually in. `--python` overrides the interpreter where `python` is not on
`PATH`.

- Omit `--repo` to install at user scope for every repository on the machine. It applies
  one integration branch to repos that may not share it, so prefer per-repo.
- `--permissions-only` writes the allowlist and nothing else — no hooks, no guard, no
  config. Run it once at user scope on any machine doing this work: a repo-scoped rule
  cannot cover a session that has to read a *different* repository, and installing this
  guard into the next repo is exactly that shape of task.
- `--status` reports what is installed, which branch this repo integrates through,
  whether the cwd may write, and every worktree with what it is still holding.
- `--uninstall` removes it, including the allowlist entries it wrote — by exact match, so
  a rule the operator added or narrowed by hand survives. `--keep-legacy` leaves a
  predecessor concurrent-writer guard registered instead of replacing it.

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

**First check it is this guard denying you.** A denial that names no next move, or that
says permission rather than protocol, is the machine's permission layer and not the rule —
and it is the one denial with no move available inside the session. Retrying it, rephrasing
it, or wrapping the command to get it past are all the same wasted turn. Say what was
stopped and stop; the operator adds a rule once and it never happens again. Where no rule
covers a command that layer *judges*, and the judgement is not stable — the same command
can be stopped in one repository and allowed in another, or stopped and then allowed in the
same one. So do not reason about when it will stop you; write the rule. See
[references/permissions.md](references/permissions.md).

`git stash` is denied in worktrees too, and that is not an oversight: `refs/stash` is a
single stack for the whole repository, so a push in one worktree renumbers every other
worktree's entries and a later `pop` or `drop` in *either* takes the wrong one. It is the
one hazard a worktree looks like it isolates and does not.

Every one of those rules is scoped to the repository the operation **targets**, not the
directory the session sits in: `cd ../other-repo && git add -A` is that repository's
business and passes, and a `git -C` or an absolute path that reaches back into *this*
repository's main checkout is denied wherever it was issued from.

**A session cannot turn the guard off, so do not spend a turn trying.**
`CLAUDE_WORKTREE_GATE` is read from the hook's own environment, which is Claude Code's; a
`CLAUDE_WORKTREE_GATE=off git add …` prefix sets it for that one command, and by then the
hook has already vetted the command and denied it. Setting it on the Claude Code process,
or in a settings `env` block, is the **operator's** move and takes a new session — and the
same goes for `CLAUDE_WORKTREE_GATE=warn`, which reports without denying and is how an
operator watches what a repo would block before committing to it. So if a denial is
provably wrong, the move that works is to say so plainly in your reply — what you were
doing, what it blocked, why the guard is wrong — and stop.

## What a worktree still does not isolate

Under this rule there is always more than one worktree, so every item here is a live
hazard rather than an occasional one.

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
  index conflicts on every branch. Prefer one file per entry with a generated index, and
  keep doc edits to the narrowest diff, in one commit, last. **One file per entry does not
  finish the job** — see below, because the generated index is itself a shared insert point.

## Generated files: stop resolving what nobody wrote

One file per entry fixes the *entries*: two files that do not exist yet cannot conflict. The
**generated index** still conflicts on every parallel branch, because each one appends its
row and rewrites the same `N entries` line. That is a merge stop over text no agent authored
and nobody should be reading — pure time and tokens.

Give that one file git's built-in union driver, and keep regenerating:

```gitattributes
docs/decisions/README.md merge=union
```

- **`union` is built in; `ours` is not.** `merge=ours` needs
  `git config merge.ours.driver true` on every machine, and without it the attribute
  silently does nothing — measured: the merge conflicts exactly as if the file had no
  attribute. Anything requiring per-machine setup is not a repo rule.
- **It buys "the merge does not stop", not "the file is right."** Measured: union keeps both
  branches' rows but in *side* order rather than the generator's, and where the two `N
  entries` lines differ it keeps **both**. So the regenerate command stays in the pre-PR
  ritual, and it is needed *especially* on a merge that reported no conflict at all.
- **Pair it with a blocking check.** If the generator has a verify mode (`--check`) in CI,
  forgetting to regenerate is a red gate rather than a quietly wrong index. Without that
  check, do not add the attribute — you have traded a visible conflict for an invisible
  staleness.
- **Never on an authored file.** Keep-both-sides lands one agent's paragraph and another's
  rewrite of it, merged clean, wrong and unreviewed. A conflict you have to look at beats a
  merge you don't. The single safe case is a file with no authored content at all, because
  there are no two sides to choose between and a generator can re-derive the truth.
- **Don't untrack it instead.** A generated index exists so a teammate reading the forge can
  find "what did we decide and why" without running a script; deleting it from the repo ends
  the conflict by ending the feature.

## Housekeeping: cleaning up is finishing, not tidying

**A change is finished when its worktree is gone, not when its PR merges.** All three come
down together — the remote branch, the worktree, the local branch — because they only mean
anything together: a worktree with no live branch is a stale checkout, a merged branch is a
push target after the PR that reviewed it has closed, and either one left behind costs the
next session a status check before it can trust what it is looking at.

This is the half of the protocol that is easiest to leave for someone else, and leaving it
does not read as a failure: the change really did land, the reply is really true, and what
the operator gets is two commands they only have to run because the session that knew the
PR had merged stopped first. Measured in the first repository to adopt this: **19 linked
worktrees** standing after two days, nearly all merged. So the `Stop` hook holds the
teardown the same way it holds the push — it refuses to end a session sitting in a worktree
whose PR has merged, and prints the commands. Its escape hatch is a sentence: if the tree
is deliberately still standing (the operator wants the diff, a dev server is on it), say
so with the path and stop.

Name the **PR** in your reply. Name a path only for a tree you are deliberately leaving.

Confirm the merge against the **forge**, not against git's ancestry — see step 4. Every
local test of mergedness (`git branch -d`, `--merged`, `merge-base --is-ancestor`) reads
squash-merged work as unmerged, so under this protocol they are all false negatives.

What a *crashed* session leaves is a different problem: it never reaches `Stop`, and a
merged worktree is indistinguishable from an in-progress one to anyone reading
`git worktree list`. `SessionStart` reports those — worktrees that recorded a merge and are
still on disk — and for the full picture, at the start of a session when nothing is in flight:

```bash
python "${CLAUDE_SKILL_DIR}/scripts/install.py" --status
```

Remove the ones reported as `clean and landed` that are yours. Another session's
worktree is its business even after its branch merges — leave it and say it is there.
Claude Code's own periodic sweep already removes subagent and background-session
worktrees that hold no work.

**What the sweep reports is a merge that was *attempted*.** The marker goes down before
`gh pr merge` runs, because no hook can tell a merge from one the forge refused, so confirm
with `gh pr view <n> --json state` before removing anything and read uncommitted changes as
a merge that did not land. A tree holding an unfinished rebase or merge is left out of the
report entirely and keeps its right to be edited — conflict resolution is the work, and it
is the most expensive thing a wrong cleanup could destroy.

One consequence of cleaning up routinely: worktree **paths get reused**, because the next
change to the same area wants the same obvious name. The guard's spent marker is keyed by
the tree's leaf name, so it records the branch too and matches on both, and the
`SessionStart` sweep deletes markers whose tree is gone. Without that, a fresh worktree
inherits a dead marker and is denied its first edit on the grounds that its change has
already landed.

**This teardown is a recipe here and not a shipped script, deliberately.** Scripting it is
the right move in a repo that runs it often — one consumer has, with a test suite over the
branch-matching, the refusals and the remote delete — and promoting that script into
`scripts/` beside the guard was considered and dropped. The guard needs a committed
per-repo copy because Claude Code loads hooks from inside the repo, and that constraint is
what pays for the copy-plus-hash-plus-drift-gate machinery around it. A teardown script has
no such constraint: it runs by hand from the main checkout and never in CI. Shipping it
would mean a second installed artifact, a second provenance record and a second drift gate
guarding a file that, measured across 71 repositories on the machine this was written on,
exists in exactly **one** copy — while turning that one copy from a source into a gated
fork, which is the expensive half and is expensive *because* there is no second consumer.
What actually protects the next adopter is this section being right, which is cheaper and
already done.

**What would reopen it:** a second repository installing this guard. At that point
`install.py` placing a teardown script is the same small change it is today, made with two
data points about where it belongs and what it must not assume, instead of one.

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
- [references/permissions.md](references/permissions.md) — the two layers that stop this
  protocol and why only one of them is the guard; the allowlist `install.py` writes, entry
  by entry; the prefix-matching traps; and why wrapping a command to hide it from a
  permission layer is the one wrapper never to write.
- [references/replacing-a-concurrent-writer-guard.md](references/replacing-a-concurrent-writer-guard.md)
  — migrating a repo that already ships a hook of its own.
