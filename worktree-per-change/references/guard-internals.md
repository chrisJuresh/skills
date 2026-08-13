# The guard: what it checks, and what it deliberately does not

`scripts/worktree_guard.py` runs as a `PreToolUse`, `SessionStart` and `Stop` hook. Read
this when it fires somewhere it should not, when a repo needs a different integration
branch, or before changing it.

## Contents

- [The rule set](#the-rule-set)
- [Which tree a rule is judged against](#which-tree-a-rule-is-judged-against)
- [How it reads a command](#how-it-reads-a-command)
- [How it tells a worktree from the main checkout](#how-it-tells-a-worktree-from-the-main-checkout)
- [Spent worktrees](#spent-worktrees)
- [The Stop hook](#the-stop-hook)
- [Configuration](#configuration)
- [Where it stands down](#where-it-stands-down)
- [What it does not protect](#what-it-does-not-protect)
- [Cost](#cost)
- [Debugging](#debugging)
- [Changing it](#changing-it)

## The rule set

| Denied | Why |
|---|---|
| `Edit`/`Write`/`NotebookEdit` whose target is inside the **main checkout** | The main checkout is read-and-pull only. Every write there is a write two sessions can collide on, and one that reaches the integration branch without a diff anyone read. |
| `git add`, `commit`, `checkout`, `switch`, `restore`, `reset`, `rebase`, `merge`, `cherry-pick`, `revert`, `am`, `apply`, `clean`, `rm`, `mv` in the **main checkout** | Same rule, other half. A protocol that denies the file edit and allows the commit has not denied anything. |
| An edit in a worktree sitting on the **integration branch** | It would put the change on the branch everything merges into, with no PR and no review. |
| An edit in a worktree whose **PR has already merged** | A merged branch that grows a commit reaches nobody: the PR that would have carried it is closed. The next change is a new worktree. |
| `git stash`, **everywhere** | `refs/stash` is one stack for the whole repository. A push in one worktree renumbers every other worktree's entries, so a later `pop` or `drop` in either takes the wrong one. The one hazard a worktree looks like it isolates and does not. |

Everything else proceeds: every read, every `push`, `fetch`, `log`, `diff`, `status`,
`branch`, `tag`, `worktree`, `stash list`, every `gh` call, and every edit in a live
worktree on its own branch — which is where all the work happens, so the guard is silent
for the whole of a normal change.

`worktree` and `branch` being on that list is load-bearing, not incidental: the teardown
(`git worktree remove <path>`, `git branch -D <branch>`) runs **in the main checkout**,
because nothing can remove the tree it is standing in. A guard that denied those would deny
the last step of its own protocol.

Writes to paths **outside** the repository are not the repository's business and are
allowed from anywhere: a scratchpad file, a note in `~/.claude/`, another repo entirely.

## Which tree a rule is judged against

The tree the operation **targets** — never the directory the session happens to sit in.
Those differ constantly, and where they differ the session's own status is the wrong answer:

| The operation names | Judged as |
|---|---|
| nothing (`git commit`, a relative `file_path`) | the session's own tree — what an ordinary command means |
| `cd <path> && git …`, `git -C <path>` | the tree containing `<path>` |
| an absolute `file_path` | the tree containing that file |
| a target this hook cannot read — `$VAR`, a backtick, a glob, `cd -` | the session's own tree, because guessing at a shell expansion is worse than being strict about the tree the guard is there to protect |

`cd` and `-C` compose left to right, and `~` is expanded because that expansion is
deterministic and needs no shell.

Two consequences, and they pull in opposite directions on purpose:

- **Another repository is not this repository's rule.** `cd ~/other-repo && git add -A`
  passes. It was denied until 2026-08-13, on nothing but the session's cwd, and that denial
  was unfollowable as well as wrong: the remedy it printed named an integration branch the
  other repo did not have. `Write` to that same path was allowed throughout, which is what
  showed the asymmetry was an oversight rather than a decision.
- **Naming the guarded tree from elsewhere buys no exemption.** `git -C <main-checkout>
  commit` from inside a worktree, or a `Write` to an absolute path in the main checkout, is
  denied wherever it was issued. Both were allowed before the same change.

"The same repository" is the shared **common git dir**, not a path prefix: linked worktrees
live *inside* the main checkout's tree, so a prefix test reads every worktree as the main
checkout and an unrelated clone sitting inside it as this repo's business. Paths are
compared with symlinks resolved — measured on macOS, a repo under `/var/folders/…` records
its worktrees' git dir as `/private/var/…`, and an unresolved comparison reads one
repository as two, which stands the guard down on the tree it is protecting.

## How it reads a command

It **lexes first** and looks for command boundaries in the token stream, so a quoted
argument arrives whole and a single token is never a command. Until 2026-08-13 it split the
raw characters on `&&`, `||`, `;`, `|` and newlines *before* lexing, and a quoted argument
holding any of those was read as shell: a `gh pr create --body "…"` whose body mentioned
`cd ~/x && git add -A` was denied as a `git add` in the main checkout, and a commit message
mentioning `git stash` was denied inside a legitimate worktree. `--body-file` was the
workaround; it is not needed now.

- **Newlines are punctuation here, not whitespace.** `punctuation_chars` alone leaves a
  newline as whitespace, which would erase the boundary between two commands on separate
  lines and stop a `cd` on the first from reaching the second. It is added to the
  punctuation set *and* removed from `whitespace`, because whitespace is tested first.
- **Quotes stay on** (`posix=False`) and come off only where a token is read as a path
  (`cd`, `-C`) or as a command name. The unquoting is hand-rolled rather than
  `shlex.split`, which is POSIX-mode and eats backslashes — that would turn a PowerShell
  `C:\Users\x` into `C:Usersx`.
- **A command name must be whitespace-free once unquoted.** `"git" add -A` is a command a
  shell runs, so a quoted spelling is read; `"cd ~/x && git add -A"` is data, because no
  command this guard cares about is spelled with a space.
- **Unbalanced quotes fall back to the old raw-text split.** It over-reports boundaries,
  costing a false denial, where declining to read the text would cost a missed one — and
  only the first of those is a failure a guard may have.

## How it tells a worktree from the main checkout

`.git` is a **directory** in the main checkout and a **file** holding a `gitdir:` pointer
in a linked worktree. That single stat is the entire test.

It is worth saying why it is not a path comparison against `.claude/worktrees/`: worktrees
live *inside* the main checkout's directory tree, so any prefix test says every worktree
write is a main-checkout write. The `.git` test is a property of the tree itself and cannot
be got wrong by path arithmetic, symlinks, or a worktree someone put somewhere else.

The branch is read straight out of `<git-dir>/HEAD` — `ref: refs/heads/<name>` — rather
than by shelling out. A detached HEAD reads as `None` and is treated as not-the-integration-
branch, which is the permissive direction and correct: nothing merges into a detached HEAD.

## Spent worktrees

"A new worktree every time" is only a real rule if reusing a finished one is refused.

The guard watches shell commands for `gh pr merge` and, when it sees one in a worktree,
writes `<git-common-dir>/claude-worktree-gate/spent/<worktree-name>.json`. Every later
`Edit`/`Write` in that tree is denied with a pointer at `EnterWorktree`.

The marker is written **before** the merge runs, because `PreToolUse` is the only hook that
sees the command and there is no after-hook that can tell a merge from a merge that failed.
That stays, but it is not the harmless direction it was once described as. It was harmless
while only the merging session saw it; the `Stop` block and the SessionStart sweep then began
reporting it to *everybody*, and a record of an attempt read as a statement of fact.

Measured on 2026-08-13, in the repo this guard came from: a PR the forge refused as `DIRTY`,
its worktree holding an unresolved rebase and ten modified files, had a spent marker — so the
sweep announced it to every new session as merged and asked for it to be removed, and the
tree's own session had every edit denied on the grounds that its work was already delivered.
Both are wrong in the expensive direction: the thing at stake is somebody's conflict
resolution.

So an **unfinished rebase, merge or cherry-pick outranks the marker**. `mid_operation()`
stats the worktree's git dir for `rebase-merge`, `rebase-apply`, `MERGE_HEAD` and
`CHERRY_PICK_HEAD` — no subprocess, because the sweep runs it per marker at every
SessionStart — and where one is present `is_spent` returns nothing and the sweep stays quiet
about the tree. A tree mid-conflict cannot be a delivered change, whatever a marker says.
The wording moved with it: both messages now say a merge was *recorded* and name
`gh pr view <n> --json state` as what confirms it.

This does open a way past a marker that is telling the truth — start a rebase, then edit. It
is a deliberate act rather than an accident, and it belongs with the shell redirect and the
aliased `git` under [Limits](#limits): the guard exists to stop a session continuing a merged
branch by mistake, not to win against one determined to. The alternative was a denial with no
remedy a session can reach, which is the failure this hook has already been fixed for once.

### `mid_operation` is not the whole of it, so the denial carries the escape

A merge refused for a **failing check** leaves no rebase behind, so it lands on the spent
denial with nothing to outrank the marker. `spent_doubt()` is that case: the `Edit` denial now
states that the mark records `gh pr merge` having *run*, names
`gh pr view <n> --json state --jq .state` as the arbiter, and prints the marker's own path to
`rm` when the answer is not `MERGED`.

**That is a change of stance, and worth naming as one.** Deleting a marker used to be described
here as the operator's escape, the same as the gate. It is not the same: the gate turns the
protocol *off*, which is exactly the decision a session must not be able to take, whereas
clearing one wrongly-written marker corrects a false statement about work the session is in the
middle of — and the forge is an arbiter the session cannot fake. Withholding it bought nothing
and cost a real failure. Measured 2026-08-13 in a downstream repo: a session read the old
wording ("this worktree's change has already landed") as fact, believed its work delivered, and
spent two turns reporting a guard bug instead of clearing a marker, with an open PR that could
not merge sitting behind it.

The general rule this is an instance of: **the text a denial prints is the only documentation
that reaches an agent at the moment it is blocked.** Anything a blocked session needs belongs in
the denial first and in a doc second. This file is lazily loaded, which makes it the one place a
mid-denial session has not read — so an escape documented only here is an escape nobody takes.

Three checks in `test_guard.py` hold that text — the claim is hedged, the forge is named, the
path is printed — and each was verified to fail against the old wording before the fix landed.

What is spent is a **branch in a tree**, not a directory name. The file has to be named
after something filesystem-safe and stable, and the leaf name is the only candidate — but a
leaf name is not unique (`../hermes-dev-x` and `.claude/worktrees/hermes-dev-x`) and, once
teardown is routine, it gets **reused**: same obvious name, cut again off the integration
branch, for the next change. So the marker records the tree path and the branch, and
`is_spent` requires both to match before it applies. A marker with no `branch` field predates
the field and still means what it said. Without this, cleaning up properly earns a fresh
worktree the strangest denial the guard can produce: *your change has already landed*.

State lives in the **common** git directory — the one every worktree shares, found through
the `commondir` pointer — so all the trees read the same markers, and so nothing the guard
writes can ever show up in `git status`.

## The Stop hook

`Stop` refuses to end a session sitting in a worktree that is holding uncommitted files or
commits that `origin/<integration>` has not got, and names which. That is the only place
the guard shells out to git: `status --porcelain` and `rev-list --count`, once per stop
attempt, never on the write path.

It refuses just as firmly when the tree's PR **has** merged and the tree is still standing,
printing the four teardown commands. That is the half of the protocol nothing used to hold —
delivery had two hooks, the teardown had a paragraph in a doc — and its failure is silent:
the change landed, the reply is true, and a stale checkout plus a live push target stay
behind. The spent check runs **before** the unlanded-work check, because a squash merge
leaves none of the branch's own commits in `origin/<integration>`, so a landed tree can also
read as holding unpushed work; telling a session to push what it has already merged is the
one wrong answer available here.

`SessionStart` closes the gap `Stop` cannot: a session that crashed or was killed never
reaches it. It lists landed worktrees still on disk — invisible otherwise, since a merged
worktree looks exactly like an in-progress one in `git worktree list` — and, in the same
pass, deletes markers whose tree is gone.

It blocks at most **twice** per session and then lets the session end. A hook that can block
forever hangs a session, and an agent that has ignored the same instruction twice will not
take it on the third telling. Every git call that fails resolves to "nothing to hold" —
blocking a session over a command that merely errored would strand it with no way out.

## Configuration

The integration branch is per repository, read in this order:

1. `CLAUDE_INTEGRATION_BRANCH` in the environment;
2. `integrationBranch` in the main checkout's `.claude/worktree-per-change.json`;
3. `development`.

It is committed next to the hook rather than inferred from the remote's default branch,
because the default branch is frequently *not* the integration branch, and guessing it wrong
sends every PR at the wrong target and every new worktree at the wrong base.

`CLAUDE_WORKTREE_GATE` controls the guard itself: `on` (default), `warn` — allow, but print
the reason it would have denied, which is how to watch what a repo would block before
committing to it — and `off`.

Both variables are read from the **hook's** environment, which is the Claude Code process's,
so they are the operator's switches and not a session's. A `CLAUDE_WORKTREE_GATE=off git add
…` prefix sets the variable for that one command, after the hook that vetted the command has
already run and denied it — verified, not assumed. Setting it for real means the environment
Claude Code starts in, or a settings `env` block, and it applies to sessions started
afterwards. The denial text says so, because a message that tells a session to flip a switch
it cannot reach costs a turn to disprove and teaches the reader the guard can be argued with.

## Where it stands down

- `CLAUDE_WORKTREE_GATE=off`;
- `cwd` is not inside a git repository, or the git metadata will not resolve;
- the payload has no `cwd`, or does not parse;
- the operation targets another repository, or no repository at all.

Note what is *not* on that list: unlike its predecessor, this guard does **not** stand down
for a repo that ships another concurrent-writer hook. There is no version of this protocol
that coexists with a guard permitting main-checkout writes. `install.py` removes such a hook
from the settings file it is writing and says so; `--keep-legacy` opts out, and then you own
the two-denials-one-action problem.

It fails open on every question it cannot answer. Blocking the only writer in a tree over
state it merely failed to read is the worse error, and it is the error that gets a hook
deleted.

## What it does not protect

Honest limits, so nobody assumes cover that is not there.

- **A human in an editor, or a plain `claude` CLI session**, writes without ever running
  this hook. Nothing here stops a person editing the main checkout, and nothing should.
- **A shell redirect into a file** — `echo x > file`, `sed -i`, a script that writes — is
  not parsed. A parser guessing at shell semantics would be a worse hole than the one it
  closed. The `git` rules cover the part that reaches history.
- **`git -C "$W" commit`** is judged against the session's own tree, because the `-C` read
  is textual and never expands a variable. A literal path *is* honoured; spell it out when
  you mean another tree.
- **A `git` call reached indirectly** — through a script, a `sh -c '…'` string, an alias, a
  variable holding the word `git` — is not seen at all, because the parser reads tokens and
  those hide the token. The same limit as the shell redirect above, and the same answer: a
  parser that guessed at shell semantics would be the bigger hole.
- **A heredoc body** is lexed as ordinary tokens, so a `git` line inside one is read as a
  call. That is the same trade as the redirect above pointing the other way, and it errs
  toward denying.
- **`gh pr merge` is still matched against raw text**, unlike everything else here, so a PR
  body quoting that phrase marks the worktree spent. Deliberate: the mark is written
  *before* the merge runs anyway, because no hook can tell a merge from a merge that
  failed. What that costs is bounded above, under [Spent worktrees](#spent-worktrees) — an
  in-progress rebase or merge overrides the marker, and both messages say a merge was
  recorded rather than that one happened.
- **An in-progress rebase suppresses the spent denial**, which a session could do on purpose
  to keep editing a genuinely merged tree. Left open with the two above, and for the same
  reason.
- **A PR merged through the web UI** leaves no `gh pr merge` for the guard to see, so that
  worktree is never marked spent — no spent-edit denial, and no teardown prompt at `Stop`
  either, which makes it the one route by which a merged worktree still reaches the operator.
  The `Stop` hook still catches the unlanded case, and `install.py --status` shows the tree as
  landed.
- **Everything a worktree does not isolate** — ports, databases, build outputs, the work
  item itself. Those are in `SKILL.md` and [ticketing.md](ticketing.md).

## Cost

Zero tokens when nothing is denied: the guard produces no output at all on the allow path.
That path is one stat of `.git`, one small read of `HEAD`, and one stat of a marker file —
no subprocess, no `git` call. Resolving a *named* target adds a second `.git` walk and one
small read of `commondir`, and only for the calls that name one. The repository is located
by walking up for `.git` rather than shelling out, precisely because a subprocess on every
write-tool call is the one cost that cannot be amortised.

Wall-clock is interpreter startup, roughly 75 ms per guarded tool call on Windows (the
install registers `-S` to skip site initialisation, about 13% of that). Against a tool call
that itself takes hundreds of milliseconds, that is the right trade.

## Debugging

```bash
python scripts/install.py --status
```

```bash
python scripts/test_guard.py
```

`--status` reports what is installed, which branch the repo integrates through, whether the
cwd may write, and every worktree with what it is still holding. To see a decision directly,
feed the guard a payload on stdin:

```bash
echo '{"session_id":"x","hook_event_name":"PreToolUse","cwd":".","tool_name":"Write","tool_input":{"file_path":"a.txt"}}' | python scripts/worktree_guard.py
```

Empty output means allowed. State is inert once the guard is uninstalled; delete
`<repo>/.git/claude-worktree-gate/` if you want it gone.

## Changing it

Run `test_guard.py` after any change. Every case in it is either a failure someone actually
hit or a false positive that would make someone delete the hook, and the second category is
the one that matters — a denial that lands on ordinary work in a legitimate worktree spends
trust the guard has to keep. Adding a subcommand to `MUTATORS` is cheap; broadening what
counts as a write is not.
