---
name: worktree-per-change
description: >-
  One change, one worktree, one branch, one merged PR — the protocol for repositories
  where nothing is ever written in the main checkout, and the guard hook that enforces
  it. Use this skill before the first Edit or Write in any repository that has the guard
  installed, when a write, `git switch`, `git add` or `git stash` is denied, when
  EnterWorktree cuts from the wrong base, when a change is finished and has to be pushed,
  merged and then taken down, when a session is refused permission to stop, when a second
  change starts in a session that already merged one, when a session finds it has already
  been writing in the main checkout or has moved a shared tree's `HEAD`, and when the user
  wants this rule installed in a repository or on a machine.
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

This is the default, and a repository may have replaced its last three steps. Check
`.claude/worktree-per-change.json` for a `delivery` block before following the loop: a repo
that lands without pull requests, or enters worktrees by path rather than with
`EnterWorktree`, declares it there and the guard's own messages follow that instead. The
**invariant** never moves — one change, one worktree, one branch, and a branch that exists
only on this disk is not a delivered change — only the commands do. See
[references/guard-internals.md](references/guard-internals.md#configuration).

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
exists so that the delivery step can be *allowed* — see below.

**It also refuses a branch that has already merged, before pushing anything**, and running
it by hand means doing that check by hand. A landed change leaves no remote branch and no
open PR, so every check *after* the push reads exactly like a change that was never
delivered: the push recreates the deleted branch and succeeds, no open PR is found, and a
second PR is opened from a branch whose content is already on the integration branch — which
merges, because an empty diff is a mergeable one. Measured 2026-08-15: one change, landed
twice, `(#55)` and `(#56)` on `main` and the second changing no files. Ask first:

```bash
gh pr list --head <short-topic-name> --state merged --json number --jq '.[0].number'
```

Anything printed there means the change is finished and the worktree is spent — take it
down and cut a new one. Then the rest, keeping the verification:

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
git worktree remove --force <path>               # from the main checkout — see below
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

**`--force`, and do the check it costs you yourself.** Some git versions refuse to remove a
worktree holding untracked or ignored files, and under this protocol every worktree holds
them by construction: the dependency install, the build output, and whatever
`.worktreeinclude` copied in. So the flag is not optional here — but what it switches off
is git's own last look for work you have not landed. Take that check back explicitly, one
line earlier, where it can still say something useful:

```bash
git -C <path> status --porcelain --untracked-files=no   # anything here is unlanded work
```

Ignored files are the reason for the flag; a *tracked* file with changes in it is a change
that never landed, and that is a tree to go back into rather than one to clear.

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
from the integration branch you just merged into. The guard marks a worktree spent once a
merge — `gh pr merge`, or `land.py` — has run **in** it, and denies further edits there — a merged branch that grows
a new commit reaches nobody, because the PR that would have carried it is already
closed.

On Windows, write a multi-line PR body to a file and pass `--body-file`, and write that
file **without a BOM** — PowerShell's `Set-Content -Encoding utf8` emits one, it lands at
the top of the body, and it stops a leading markdown heading from rendering. Use
`[System.IO.File]::WriteAllText($path, $text, (New-Object System.Text.UTF8Encoding $false))`.

## Squash the topic branch; merge the branch that lives on

`--squash` is right for every PR this protocol opens, because the head branch dies on the
merge: one commit per change on the integration branch, and nothing ever merges *from* that
branch again. The rule stops there — and where it stops is a seam this protocol **creates**,
because an integration branch is a branch somebody afterwards promotes.

**A PR between two branches that both live on afterwards has to be a merge commit.** A
squash writes a single-parent commit, so the base takes the *content* and none of the
*ancestry* and the merge base of the two branches never advances. The next PR across that
seam diffs from the stale base and re-proposes the entire branch. Measured 2026-08-19, on a
promotion opened two hours after the previous one was squashed: **654 changed files,
+102,594 −3,876, and nine `add/add` conflicts** — every one a phantom, against a tree that
was byte-identical to the head branch's own tip at the moment the squash landed. There was
nothing to reconcile and nothing a human could usefully decide.

What makes it expensive is that it is quiet at the moment it happens. The squash merges
green and looks finished; the bill arrives on the *next* PR, looking like a legitimate
654-file conflict resolution.

**Repair it by restoring the parent link, never by resolving the conflicts.**
Hand-resolving yields a tree that was already correct and *still* records no ancestry, so
the PR after that one explodes the same way. On a branch cut from the side that is behind:

```bash
git diff <base> <the-squashed-commit's-source-tip>   # must be EMPTY before you start
git merge -s ours origin/<the-squashed-head>         # records the parent, changes no bytes
git diff origin/<the-squashed-head> HEAD             # must be EMPTY afterwards
```

`-s ours` discards the other side wholesale, so it is honest exactly when that side holds
nothing unique — which is the only thing those two `git diff`s are there to establish.
Then merge the repair itself **as a merge commit**: squashing it collapses the second
parent that is its entire reason to exist.

**Enforce it with a rule, not with discipline.** It is one green button among three, pressed
at the end of a piece of work that is already finished, and discipline was measured failing
inside a single afternoon — the repair PR for the first squashed seam was itself
squash-merged, flattening away the parent it had been opened to add. On GitHub a repository
ruleset over the long-lived bases carrying `allowed_merge_methods: ["merge"]` stops offering
the other two buttons there, while the integration branch stays outside its conditions and
goes on squashing. The rule is blunter than the principle — the damage depends on whether
the *head* survives the merge, and a ruleset can only condition on the *base* — so it also
forces a merge commit on a dependency bump or a hotfix aimed straight at a protected base,
where a squash would in fact have been harmless. That costs one extra merge commit on a
one-commit branch. The looser rule costs the 654 files.

## What a fresh worktree does not have

This is the cost that changed. Under a rule where worktrees were occasional, setting one
up was a rare tax; under this one it is paid on **every change**, so it is worth making
cheap rather than rediscovering it each time.

A worktree is a fresh checkout of tracked files and nothing else. Dependencies, build
output, local config and anything else `.gitignore` covers are simply absent:

- **Dependencies.** `node_modules/`, a virtualenv, a `pnpm install`. Some repos dodge
  most of this by committing build output — check before assuming a full install is
  needed; often only a change that touches source requires one. **Do not pipe the install
  through `tail` to keep it out of a context window.** A pipeline reports the *last*
  command's status, so a failed install exits 0 and hands back a checkout whose every
  later gate result means nothing. `set -o pipefail`, or read `${PIPESTATUS[0]}`.
- **Ignored-but-required config.** A `.claude/launch.json` that tells the preview how to
  start the dev server, an `.env`, an editor config. If it is ignored, no worktree has it,
  and the failure looks like the tool being broken rather than the file being missing.
- **This machine's permission mode**, which is the case above turned on the protocol
  itself. `.claude/settings.local.json` is ignored — it is nobody else's business what this
  machine allows — so a fresh worktree does not have it and falls back to the default mode,
  and the writes the guard *sent you to a worktree to make* start being refused. Measured
  2026-08-15: `git add` allowed in one worktree and denied in the next one cut minutes
  later, with nothing visible from inside either to say why. It reads exactly like the
  permission layer's unstable judgement (below) and is not — it is a missing file, and the
  only instance of that shape with a cause you can fix, so check it first. `install.py`
  now writes the `.worktreeinclude` entry for it; a repo installed before that gets it by
  re-running the installer, and `--status` says whether it is missing.
- **The toolchain selection.** A worktree inherits the shell's default interpreter, not the
  repository's pin — an `.nvmrc` is a file, not a shell hook, and a version manager that
  needs one is no help to a session. What makes this awkward rather than routine is that
  the obvious fix is unavailable: **while a session is inside `EnterWorktree`, Claude Code
  refuses a `PATH=… <cmd>` prefix**, and `env PATH=… <cmd>` with it, as a command whose
  effect it cannot verify. That is the harness's isolation and not this guard — outside a
  worktree the same prefix runs fine. Address the binary absolutely instead:
  `…/v22.16.0/bin/node …/npm/bin/npm-cli.js run <script>`, which reaches child processes
  too, because npm puts its own node directory at the front of `PATH` for lifecycle
  scripts. Measured 2026-08-19 on a repo whose gate scripts need Node 22, on a machine
  whose shell defaults to 16.
- **Untracked scratch state** the last session left in the main checkout.

Two ways to fix it, and the second is better for anything a *human* also needs:

- **`.worktreeinclude`** lists untracked paths Claude Code copies into each new worktree.
  Right for machine-local secrets and caches that must not be committed. A path is copied
  only when it is **both** listed there and gitignored, so it can never duplicate a tracked
  file. `install.py` writes the file with `.claude/settings.local.json` already in it, and
  appends rather than replaces, so a repo that keeps its own entries there keeps them. If a
  setup script of yours reads the same list — and having one list read by both paths is the
  whole point of it — have that reader take literal paths only and **skip a glob out
  loud**: `.gitignore` syntax has patterns and negation, and half-honouring one hands back
  a worktree that is missing a secret and says nothing.
- **Un-ignore the file.** If every worktree needs it and it holds nothing private, the
  honest answer is to commit it — a worktree only gets a file if git puts it there. This
  applies to the guard itself: `.claude/settings.json`, `.claude/hooks/worktree-guard.py`
  and `.claude/worktree-per-change.json` must be tracked, or the rule stops applying
  inside the very worktrees it sends you to. A repo that ignores `.claude/` wholesale
  needs its ignore narrowed to name them, keeping `settings.local.json` and
  `.claude/worktrees/` out — the second stays ignored for a reason of its own, and the
  first repository to adopt this guard arrived with the gitlink already in its history
  ([below](#the-worktrees-live-inside-the-repo-so-its-own-tooling-can-see-them)).
  `install.py` writes both ignore entries now, and asks *git* rather than the file, so a
  repo that already covers them under a broader pattern does not collect a second line
  saying the same thing. What it will not do is read your **machine's** global ignore as an
  answer: the question is whether the repository carries the rule, and a
  `core.excludesFile` is true only where it lives — measured, an honest check against it
  reported nothing missing and shipped the repo to everybody else without the entry.

**Whatever you copy those files *from* is at the revision the operator left the main
checkout on, which under this protocol is not the integration tip.** The base you cut from
is fetched, so it is current; the main checkout's *working tree* is not, and a setup
script, a `.worktreeinclude` or a config read out of it lags the integration branch by
however much has not been promoted. Measured on the second change ever made under this
rule: the worktree came out one merge behind and missing the very file that a merged change
had just added to the list, and the failure read as the tooling being broken. Pull the main
checkout first, and have a setup script read its *list* from the checkout the script itself
lives in — the files still come from the main one, but the list comes from the same commit
as the code reading it.

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

## The worktrees live inside the repo, so its own tooling can see them

`.claude/worktrees/<name>` is where `EnterWorktree` puts a tree and what every denial the
guard prints tells a session to type, so a repository under this rule grows N complete
copies of itself *inside* itself. Excluding that directory from the repo's own tooling is
not tidiness: a linter, a formatter, a test runner or a type checker pointed at the root
will walk every worktree on disk, so the gate slows down with the number of trees standing
and starts reporting *other branches'* failures as yours.

**The first tool that sees them is git, so `.gitignore` gets the entry before anything
else does.** This is the one that is easy to skip, because the noise it makes is small:
git stops at each nested `.git` rather than descending, so the main checkout's
`git status --short` grows exactly one line, `?? .claude/worktrees/`, however many trees
stand. It is a permanent line, though, and while it is there "is the main checkout clean"
is not a question the status answers. What it costs is the next `git add -A` run in the
main checkout — by a person in an editor, or by any session in a repo where the guard is
not installed yet, in `warn`, or failing open. Measured on a two-worktree probe:

```
$ git add -A                     # exit 0, and the only complaint is a hint
warning: adding embedded git repository: .claude/worktrees/topic
$ git ls-tree HEAD .claude/worktrees/
160000 commit 7895d72…  .claude/worktrees/other
160000 commit 7895d72…  .claude/worktrees/topic
```

Those are gitlinks to commits no clone can resolve, and they are quiet in both directions:
the commit succeeds, and every worktree cut from it afterwards materialises empty
directories named after other people's branches and then reports itself **clean**. So:

```gitignore
.claude/worktrees/
```

A repo that ignores `.claude/` wholesale already has this and needs the *narrowing*
described above instead; a repo that ignores nothing under `.claude/` has neither, and
nothing else in this protocol will tell it so. Measured: the repository this skill itself
lives in was one of them.

Each of the rest has to be told separately, and one of them may already be right for a
reason worth establishing rather than assuming. Measured on the first repository to adopt
this: ESLint needed `.claude/**` adding to `ignores` and Prettier needed `.claude/` in
`.prettierignore`, while the test runner was already safe only because its `include` globs
name three directories instead of the root. The type checker needed nothing — TypeScript's
wildcard `include` skips dot-directories — but that was settled with a three-line probe
rather than read off the docs: drop a deliberately broken file at
`.claude/worktrees/probe/src/broken.ts`, run the check, watch it stay green. Run the same
probe for the next tool you point at the tree, and add nothing on faith: a no-op `exclude`
reads as load-bearing to everyone after you.

**This is also what decides where the guard's own suite goes.** Once `.claude/**` sits
outside the linter and outside the test glob, a suite placed next to
`.claude/hooks/worktree-guard.py` never runs again — and a hook whose tests silently
stopped running looks exactly like a hook that had nothing to deny, which is the failure
the suite exists to catch. Put it where the repo's runner already looks and let it reach
across to its subject.

## Installing it

Per repository is the usual install, because the rule depends on what that repository's
branches mean:

```bash
python "${CLAUDE_SKILL_DIR}/scripts/install.py" --repo . --dry-run
```

Show the user that output, then run it without `--dry-run`. It copies the guard to
`.claude/hooks/` and `land.py` to `.claude/scripts/`, registers three hooks and the
allowlist in the committed `.claude/settings.json`, writes
`.claude/worktree-per-change.json` with the integration branch, adds `.claude/worktrees/`
and `.claude/settings.local.json` to `.gitignore` and `.claude/settings.local.json` to
`.worktreeinclude`, and links this skill into `~/.claude/skills/` so
`/worktree-per-change` resolves everywhere. Commit all six, and check `.gitignore` is not
swallowing the four that have to stay tracked. `--uninstall` leaves the `.gitignore` and
`.worktreeinclude` entries alone and says so — un-ignoring `.claude/worktrees/` is how a
stale checkout ends up committed, and that outlives the guard.

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
  whether the cwd may write, every worktree with what it is still holding, and whether the
  `.gitignore` and `.worktreeinclude` entries are there — which is how a repo installed
  before the installer wrote them finds out, since nothing at runtime repairs either.
- `--uninstall` removes it, including the allowlist entries it wrote — by exact match, so
  a rule the operator added or narrowed by hand survives. `--keep-legacy` leaves a
  predecessor concurrent-writer guard registered instead of replacing it.

New hooks apply to sessions started afterwards, so say so rather than letting the user
assume the current session is covered.

The integration branch has to exist on the remote before the first PR. If the repo
integrates through a branch it does not have yet, create it from the default branch and
push it once — and say you did, because it changes what everyone else's PRs target.

### What that branch has to be, and what it must not be

Two properties that sound contradictory, which is why no repository arrives with both, and
why it is worth settling while the answer to `--branch` is still being decided.

**It must not carry a required status check or a required review.** The whole shape of this
protocol is a session opening its own PR and merging it on the strength of a gate it ran on
its own disk. Aim that at a protected branch and every session ends in a waiting loop on CI
for a verdict it already holds. So the integration branch is ungated for *merging* and sits
one step removed from the branch that deploys, a human promotes it when a batch is ready,
and the required check lives on that promotion. Say the consequence out loud to whoever
adopts this, because it is the thing they will assume the other way round: **a merged agent
PR has not deployed anything.**

**And it must still be protected against deletion and force-pushes, admins included.**
Nothing here writes to that branch except merges, so the risk was never a bad commit — it is
the branch ceasing to exist, and the protocol has no step that survives that. Measured
2026-08-19: a promotion PR whose *head* was the integration branch merged with
`--delete-branch`, and the flag did exactly what it says. Nothing failed loudly.
`git fetch origin <integration>` started answering *"couldn't find remote ref"*, every
session's first step had no base, and a session holding a stale tracking ref went on
branching off a tip the remote no longer had. Classic branch protection with
`allow_deletions: false`, `allow_force_pushes: false` and `enforce_admins: true` — the
delete came *from* an admin path, so a rule admins bypass would not have stopped it — while
leaving required checks and required reviews `null`, gates destruction without gating
merging, which is exactly the split this one branch needs. The delete call is the test,
because a protected branch refuses it:

```bash
gh api --method DELETE repos/<owner>/<repo>/git/refs/heads/<integration>   # expect 422
```

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

### Four gates refuse worktree work, and they read alike

Naming the wrong one is worse than writing nothing down, because it sends the next session
to fix a repository that cannot fix it. Measured in the first repository to adopt this:
**three log entries and five refusals**, all filed against this guard, none of them its
doing — and an upstream fix for any of them would have moved nothing.

| gate | how you recognise it | where the fix is |
|---|---|---|
| this guard, `PreToolUse` | its own vocabulary: the main checkout, the integration branch, a spent worktree, `git stash` | upstream, in the skill — never in a repo's committed copy |
| this guard, `Stop` | it counts what the worktree is holding, then prescribes delivery and teardown | upstream too — but note it is **not** a `PreToolUse` hook, so grepping the guard's rule set for its words finds nothing and proves nothing |
| the machine's permission layer | it says permission rather than protocol, and names no next move | an allowlist entry, once — see [references/permissions.md](references/permissions.md) |
| Claude Code's own worktree isolation | `"This session is isolated in the worktree …"`, arriving as a tool **error**, not a hook denial | nowhere. No repository can change it — see below |

**Grep the refusal against the repo's committed `worktree-guard.py` before writing "fix it
upstream".** If the words are not in that file, this guard did not say them.

### What `EnterWorktree` costs

Entering is what the loop above asks for, and it buys a real thing: Claude Code enforces
the boundary itself from that moment, and the session reports the worktree as its `cwd`
rather than going on advertising the main checkout to every other session.

It is also a **second** gate on top of this guard, and it refuses more than the boundary.
It rejects every compound command it cannot statically verify — a heredoc, a pipe, a `for`
loop over two `curl` calls, an `echo "$VAR"` — including ones that touch no git and no path
outside the worktree and could not leave it by construction. And it refuses every `cd` to
the main checkout, including the legitimate one: removing a *sibling* worktree, which
nothing inside that worktree can do for itself.

```
This session is isolated in the worktree <path>, but this command is too complex to
verify that it stays inside the worktree; break it into plain, separate commands.
```

Measured: five refusals across four sessions in one repository, one call and one rewrite
each. If you are isolated and hit it — one command per call, a pipe counts as complexity,
the Write tool replaces a heredoc, and `git worktree remove ../<sibling>` is the relative
spelling that does the same job as the `cd` it refuses.

**A repository may decide the trade is not worth it**, and one has: where the guard is
installed, it already judges the *path a write targets*, so a session that never entered
still cannot write in the main checkout. Such a repository declares
`"delivery": {"enterWorktree": false}` (see
[references/guard-internals.md](references/guard-internals.md#configuration)), and every
message the guard prints then stops naming `EnterWorktree`. In one, work in the tree by
path — better still, start the session inside it, since entering mid-session pays the cold
start twice.

**Absent that declaration, enter.** This is a repository's decision to record, not a
session's to make on the day, and none of the above is licence to `cd` instead of entering
in a repository that has not made it. The refusals are a cost to know about when you meet
one, and they are the reason the declaration exists at all.

**In a repository that has made it, read the next paragraph, because "by path" does not
work for git.**

### Working in a worktree by path: `cd` once, alone, spelled out

Write and Edit are judged on the path they target, so they work on a worktree from a
session sitting anywhere. **`git` is judged on the directory the command runs in**, and
this guard works that out by reading the command's *tokens* — so a `cd` or a `-C` whose
argument is a shell **variable** is unreadable, the hook falls back to the tool's cwd, and
the call is denied as though it were in the main checkout:

```
Denied: `git add` does not run in the main checkout.
```

Both `cd "$W" && git add <paths>` and `git -C "$W" add <paths>` are denied, and neither is
a wrong denial — `git -C "$W" switch` is the guard's own worked example of a directory
argument it cannot read. The variable is the trap, not the `cd`. What works is one call
that is **only** `cd /full/literal/path`, with no `&&` and no variable; the tool's cwd
persists, so every later `git` in that session resolves inside the tree.

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

## When the rule was already broken

The guard has holes by design, and each one is defended somewhere above: it **fails open**
on every question it cannot answer, it sees only Claude's own tool calls, `warn` reports
without denying, and new hooks apply only to sessions started after the install. So
arriving in the state this protocol exists to prevent — a write in the main checkout, two
sessions in one tree — does not mean anything went wrong with the guard, and it is worth
knowing the move before you need it. Both of them are the opposite of the instinct.

**Never put back a `HEAD` you moved by accident.** A session that finds it has moved the
shared tree — checked out a branch there, left it somewhere new — **says which command it
ran and stops.** It does not restore anything, and it does not go looking through the
reflog for the value to restore. "Back" is not knowable from inside one session: the value
you are trying to return to is another session's, you cannot see what that session had, and
a wrong guess silently swaps the files under a live worker — which is the failure this whole
protocol is built to prevent, arriving disguised as the repair for it. Two sessions each
guessing leaves the tree somewhere neither of them intended, with the second guess hiding
the first. The session that owns the branch is the only one that can put it back, and it can
only do that if it is told. Reporting it is therefore the fix and not the preamble to one.

**If you find mid-change that you have been sharing a tree, move rather than finish.**
The instinct is to get to a stopping point first, and it is wrong in the direction that
costs most: the shared tree gets worse with every file written, and untangling it happens
later, when nobody can still say which hunk was whose. So stop where you are, **commit**
what is genuinely yours — never stash it, `refs/stash` is one stack for the whole
repository and the entry a later `pop` takes may not be the one you pushed — then cut a
worktree off the correct base, `git cherry-pick` the commit across, and carry on there.
A commit is the cheap move here precisely because it is addressable: it belongs to a
branch, it survives the next session's `git switch`, and it can be named in a reply.

Say both in the reply. An operator who is told which command moved the tree can put it
back in one step; one who is told nothing pays for it in the next session's diff.

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

**`git worktree remove` deregisters first and deletes the files second, and it keeps the
deregistration when the delete fails.** So the command reports an error, `git worktree
list` goes clean, the whole checkout is still sitting there, and running the same command
again refuses with `is not a working tree` — the directory is now the only thing that knows
it exists, and nothing you would think to run mentions it. Measured 2026-08-28 in the first
repository to adopt this guard: three leftover directories under `.claude/worktrees/`, one
of them a full checkout with `node_modules` in it, against a `git worktree list` naming
only the main checkout. **So check the directory as well as the listing**, and delete it
yourself — `SessionStart` reports these under a heading of their own, because the remedy
for a live worktree is the command that fails on a dead one.

Deleting it can fail too, with `Device or resource busy`, while something still holds a
file inside. It is worth naming what: on the machine this was measured on, four hung
`gk.exe ai hook` processes — one per commit the session had made, spawned by an editor's
git-hooks plugin and still alive after the session ended. A busy delete does not become
unbusy by being retried, so find the holder rather than looping. This is also the cause of
the leftovers above, one level up: it is what makes the delete half of
`git worktree remove` fail while the deregistration has already happened.

**What the sweep reports is a merge that was *attempted*.** The marker goes down before
`gh pr merge` runs, because no hook can tell a merge from one the forge refused, so confirm
with `gh pr view <n> --json state` before removing anything and read uncommitted changes as
a merge that did not land. A tree holding an unfinished rebase or merge is left out of the
report entirely and keeps its right to be edited — conflict resolution is the work, and it
is the most expensive thing a wrong cleanup could destroy.

### When `git worktree remove` is refused by a lock

`EBUSY: resource busy or locked, rmdir` on the worktree directory, or git's own refusal to
remove it. **The change has already landed by then** — the merge is first and the teardown
second, on purpose — so this is never a reason to redo anything, and never a reason to
stash. Three causes, in the order worth checking:

1. **Your own shell's cwd is inside the tree.** This is the default outcome rather than an
   edge case, because the teardown is run from the worktree. `cd` to the main checkout in a
   call of its own, then remove it.
2. **A server or watcher you started in it.** A `cmd &` inside one tool call does not
   survive the call; a backgrounded *tool* invocation does — stop that one first. A dev
   server that picked a different port than the one it was asked for is the version of this
   that wastes an afternoon: check what is actually listening, and confirm the process's
   command line points into *this* tree before killing it, because the obvious port is
   often another worktree's.
3. **A blocking `PreToolUse` hook from some other plugin, hung with the tree as its cwd.**
   The tell is that the directory is **empty** and still locked: the recursive delete got
   all the way through and only the top-level directory is held. A `--blocking` hook still
   alive minutes after the command that spawned it is hung, not working, and killing it
   loses nothing.

What it is usually *not* is `node_modules` still holding a file, though that does happen
and does clear: measured, a removal that failed succeeded about a minute later with nothing
done in between. So a teardown script should retry for a few seconds before reporting. If
waiting does not clear it, it is one of the three above.

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
