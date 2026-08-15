# The denial that is not the guard

Two different layers stop this protocol, they fail in opposite directions, and telling
them apart is the whole of this document.

**The guard** denies an action because the *protocol* forbids it — an edit in the main
checkout, a `git stash` anywhere. Its denials name a next move, and taking that move is
the work. A session cannot turn it off, and should not try.

**The permission layer** stops an action because nobody has said this machine may take it.
It is not about the protocol at all, and its denials have no next move inside the session:
the same command will be stopped again next turn, and the turn after that. `git status`
stopped this way teaches an agent to stop asking; `gh pr merge` stopped this way ends the
protocol one step from the end, leaving a branch on a disk nobody will look at — which is
the failure the `Stop` hook already refuses to end a session on.

The fix for the first is to obey. The fix for the second is a rule in `settings.json`,
written once, by the operator.

## Do not wrap a command to make it unrecognisable

The tempting move, when a permission layer stops `gh pr merge`, is a script that runs it
under another name. It is the wrong instinct twice over.

It does not work, because what is inspected is the command about to run, and a script that
shells out to `gh` is a script that shells out to `gh`. And it should not work: a
permission layer exists so a human can decide once what an unattended agent may do, and a
wrapper written to be unrecognisable is a decision taken away from them quietly. An agent
that finds itself designing one has misread a denial as an obstacle rather than an answer.

What is legitimate — and what `land.py` is — is a wrapper that makes the grant **smaller**.
That is the opposite move, and the section below is the case for it.

## Prefer a narrow grant to a wide one

`Bash(gh pr merge:*)` is the obvious entry and it is far wider than the protocol needs. It
merges any PR, in any repository the machine is authenticated to, on any base, at any time
— where what was actually wanted is "this agent may finish the change it is working on".

`scripts/land.py` is the narrow form. It accepts **no PR number and no branch**. It merges
the PR whose head is the branch checked out in the worktree it was run from, into the
branch that repository recorded in `.claude/worktree-per-change.json`, and refuses
everything else: the main checkout, a worktree sitting on the integration branch, a tree
with uncommitted work, a PR it cannot identify from the branch. One entry for that file
grants the protocol and nothing beside it.

It also prints every command before running it. An allowlisted script is one nobody
watches, so its transcript is the only record left of what it did.

## Matching is a prefix match, so name the subcommand

Every entry is matched against the start of the command string. This is the one thing to
get right, because the failure is silent and generous:

| Entry | Also allows | Why it matters |
|---|---|---|
| `git branch:*` | `git branch -D main` | Deletes branches. Use `git branch --list:*`, `-a`, `-r`, `-v`. |
| `git config:*` | `git config user.email …` | Rewrites identity and hooks config. Use `git config --get:*`, `--list:*`. |
| `git remote:*` | `git remote set-url origin …` | Repoints the remote. Use `git remote -v:*`, `get-url`, `show`. |
| `git worktree:*` | `git worktree remove --force` | Use the three specific forms. |
| `git reflog:*` | `git reflog expire --expire=now` | Destroys the recovery path. Use `git reflog show:*`. |
| `gh api:*` | `gh api -X DELETE /repos/…` | Everything the token can do. **There is no safe prefix; leave it out.** |

The inverse trap is worth knowing too: `git merge-base` starts with `git merge`, so an
entry for `git merge-base:*` is specific and safe, while one for `git merge:*` would quietly
cover it *and* real merges.

## The list

`install.py` writes these. They are also worth reading as a list, because a repo that wants
a different balance should change it deliberately rather than by deleting the block.

Each is written into `permissions.allow` as **`Bash(<entry>)`** — tool name and all. A bare
`git status:*` in that list is not a narrower rule, it is a rule that matches nothing, and
it fails in the quietest way there is: the file looks right, the entry is visibly there,
and every command it was meant to cover is still stopped. The entries below are printed
without the wrapper only so they stay readable.

**Read-only — written at both repo and user scope.** Nothing here changes a working tree,
a branch, a remote or an account.

```
git status:*            git log:*              git show:*             git diff:*
git diff-tree:*         git diff-index:*       git blame:*            git shortlog:*
git describe:*          git name-rev:*         git grep:*             git count-objects:*
git reflog show:*       git rev-parse:*        git rev-list:*         git merge-base:*
git for-each-ref:*      git show-ref:*         git ls-remote:*        git symbolic-ref --short:*
git branch --list:*     git branch --show-current:*                   git branch -a:*
git branch -r:*         git branch -v:*        git branch -vv:*       git branch --merged:*
git ls-files:*          git ls-tree:*          git cat-file:*         git check-ignore:*
git check-attr:*        git config --get:*     git config --get-all:* git config --list:*
git remote -v:*         git remote get-url:*   git remote show:*      git worktree list:*
git fetch:*

gh pr view:*            gh pr list:*           gh pr diff:*           gh pr checks:*
gh pr status:*          gh issue view:*        gh issue list:*        gh repo view:*
gh run view:*           gh run list:*          gh workflow view:*     gh workflow list:*
gh release view:*       gh release list:*      gh label list:*        gh search:*
gh auth status:*
```

`git fetch` is the one entry here that writes anything: it updates remote-tracking refs,
never the working tree, the index or a local branch. It earns its place because this
protocol cuts every worktree from a *fetched* tip, so a fetch that needs asking is a fetch
that gets skipped, and a stale base is the failure that costs a whole change.

`git stash list` is deliberately absent even though it is read-only. The guard denies
`git stash` outright, and an entry suggesting otherwise would only produce a denial with a
confusing provenance.

**Delivery — repo scope only.** These are the protocol's own writes.

```
python .claude/scripts/land.py:*
git add:*               git commit:*           git push -u origin HEAD:*
git switch -c:*         git worktree add:*     git worktree remove:*
git worktree prune:*    git branch -D:*
```

They look broad and are not, because **a hook's `deny` beats a permission `allow`**. The
guard is what scopes them: `git add` and `git commit` are denied outside a worktree, so
allowing them here means "in a worktree", which is the whole grant. This is why the
delivery entries are written at repo scope and not at user scope — at user scope they
would apply to repositories that have no such hook, and the scoping would not exist.

**Never written, at either scope:** `gh api`, `gh pr merge`, `gh pr comment`, `gh repo
delete`, `gh secret`, `gh auth token`, `git push` in any form but the protocol's own,
`git reset`, `git clean`, `git rebase`, `git merge`, `git checkout`, `git switch` other
than `-c`.

## Where the rules go, and the scoping that decides it

Repo scope (`.claude/settings.json`, committed, beside the hooks) is the usual home: the
rules travel with the guard they belong to, and everyone working in that repository gets
the same ones.

**But a session's permissions are scoped to its project directory, and that is not always
where the work is.** Measured 2026-08-15: a session rooted at one repository had `git fetch`
allowed there and stopped in a second repository on the same machine — the same command,
the same account, a different project directory. Anything that reaches across repositories
sees this, and installing a skill into other repos is exactly that shape of task.

So put the **read-only** entries in `~/.claude/settings.json` as well. They are safe
machine-wide by construction, and they are the ones whose absence in some other directory
turns into a session that has stopped asking anything. Once per machine:

```bash
python "${CLAUDE_SKILL_DIR}/scripts/install.py" --permissions-only
```

Keep the **delivery** entries per repo. Their safety is borrowed from the guard, and the
guard is per repo.

## When a denial is genuinely wrong

Say so plainly and stop — what you were doing, what it stopped, and why the rule does not
fit. That is the move that works, for both layers. For the guard it is the documented
escape; for the permission layer the operator is the only one who can change it, and a turn
spent looking for another route is a turn spent not telling them.
