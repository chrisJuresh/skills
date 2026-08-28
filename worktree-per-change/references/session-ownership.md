# One worktree, one session

`worktree_guard.py` isolates *changes*. This isolates *sessions*, which is a different
question it has nothing to say about: two agents in one worktree pass every check it makes.

Opt in per repository — it is off unless asked for:

```bash
python .claude/scripts/install.py --repo . --session-ownership
```

That records `sessionOwnership: true` in `.claude/worktree-per-change.json`, so a later
resync keeps it without the flag. `--no-session-ownership` turns it off, clears the
registration and deletes the script. It is never installed at user scope: claims are keyed
off a repository's common git dir, so a machine-wide registration would run it against
every repo on the box, including the ones with no worktree protocol at all.

## The incident it was written for

Two sessions drove one frontend worktree for half an hour on 2026-08-25:

- both dev servers wrote the same build output directory (the app set no `distDir`), and
  both died mid-run;
- one session's `pnpm dev` took the port from the server already there;
- one session's screenshot run captured the other's uncommitted edit, so the "after" image
  it delivered was of a change it did not author;
- the app's auth cookies were host-scoped rather than port-scoped, so switching role on one
  server switched it on the other.

None of that raises an error. It produces a screenshot that is wrong, and the ordinary
reading of a wrong screenshot is that the code is wrong. That is why this is a hook and not
a paragraph in `CLAUDE.md`: the failure is silent, so it cannot be caught by noticing.

## What claims a tree

The **first write into a linked worktree** claims it for that session, in
`<common-git-dir>/claude-worktree-gate/claims/<tree-name>.json` — beside the guard's spent
markers, for the guard's reasons: every worktree of the repo reads the same file, and
nothing either hook writes shows up in `git status`.

**Presence claims it too.** Any tool call whose cwd is inside a tree claims it, a read
included. Otherwise a session that spends its first twenty minutes reading its own worktree
has not claimed it, and the tree is free for someone else to take and then hold against its
owner.

The **main checkout is never claimed**. It is shared by every session by design, the guard
already refuses writes there, and a claim on it would deny the one place root tooling is
supposed to run.

## What is refused, and what is not

Judged by what a call **names**, not by where the session sits — the two differ constantly,
and a file tool carrying an absolute path into another tree is the exact shape of the
incident.

| Refused | Why it is one of the three shapes |
|---|---|
| `Edit` / `Write` / `NotebookEdit` on a path in a foreign tree | The session never left its own directory |
| `pnpm dev`, `cargo test`, `make` with cwd in a foreign tree | A package runner acts on the package it is standing in; the tree is named nowhere at all |
| `cd <tree> && …` | The shell tool's directory persists between calls, so everything after inherits the tree silently |
| `pnpm -C <tree> …`, `--dir`, `--prefix`, `--cwd` | A runner pointed somewhere else |
| `node <tree>/script.mjs`, `rm <tree>/…` | An interpreter or a writer acts on the file it is handed |

**Allowed, and each one deliberately:**

- **Reading a foreign tree** — `cat`, `grep`, `rg`, `ls`, `diff`, `head`, `find`. Comparing
  two branches on disk is ordinary work. A hook that refused it is one somebody turns off,
  and then nothing is enforced at all.
- **`git` anywhere.** The guard's business, and its rules are per-tree rather than
  per-session. One piece of state gets one owner.
- **The main checkout, and any unrelated repository.**
- **A session's own scratch script**, run from anywhere. Interpreters are matched on *path
  tokens* only, so `node ~/scratch/shoot.mjs` targets nothing.

That two-tier split — a package runner's target is its cwd, an interpreter's target is the
file it is handed — is the whole of the command lexing, plus one rule that keeps it honest:
**a token counts as a path only if it exists on disk.** That is what stops the payload of
`bash -lc '…'`, one token holding a whole command line, from being read as a directory, and
it means a command the hook cannot lex contributes nothing rather than something wrong.

## Lapsing, not locking

Liveness is the owning session's **transcript mtime**, which every turn touches — a
heartbeat the hook does not have to maintain and cannot get wrong. A session that is
actually working is never more than a turn from fresh; one that was killed frees its tree
in 45 minutes.

A pid was rejected as the signal: hooks are handed a session id and a transcript path, not
the agent's pid, and a pid recorded from the hook's own process dies at the end of every
hook call.

```
CLAUDE_WORKTREE_OWNER=off          turns it off
CLAUDE_WORKTREE_OWNER=warn         reports instead of denying
CLAUDE_WORKTREE_OWNER_TTL=<secs>   how long a quiet session keeps its claim (default 2700, floor 60)
```

Read from the hook's environment, so they are the operator's switches and not a session's.

## Releasing a tree

The deliberate override, printed by every denial:

```bash
python3 .claude/hooks/worktree-owner.py --release <tree path or leaf name>
```

**A teardown script should call it**, in the same place it removes the worktree — a claim
left behind denies the next session to be handed that path until it lapses. The guard's
spent marker is still the guard's; each piece of state has one owner.

The hook exempts that one invocation from its own lexing. Without the exemption it denies
the exact command its denial prints, because the lexer sees the tree path as an argument
and calls it a write target long before `main()` reaches the `--release` branch — confirmed
2026-08-26, and the escape hatch was reachable only by a session that already knew to spell
it another way.

The exemption matches **this script's filename plus the flag**, which is looser than
identity and is the correct looseness: under a committed install every worktree holds its
own copy of the file at its own path, so a `resolve() == __file__` test would exempt the
copy belonging to the session being denied and refuse the one it was told to run. It is
still narrow — a command that merely mentions the word, or names some other script, is
exempted from nothing, and a release riding alongside a real write is two segments, of
which only the first is exempt.

## What it deliberately is not

**Not an edit to the guard.** The guard answers "is this tree a worktree, on the right
branch, not already merged"; this answers "is it *yours*". They compose, they keep separate
state, and a repo can run either alone. It also keeps the guard a file downstream repos can
vendor by digest — which the skill tells them to do, and which they cannot do with a file
that grew a second rule.

**Not a port or dev-server arbiter.** It records who holds a tree. The claim files are
plain JSON so a repo's own tooling can read them and refuse to start a second server in a
tree it does not own; what to do about that is the repo's decision, not the hook's.

**Not a cut-time allocator.** Denying at the moment a worktree is created — one tree per
session — was rejected because the session that cuts a tree is frequently not the session
that works in it: tooling runs from the main checkout, and entering the tree happens
afterwards, sometimes in another chat.

**Fails open on everything**: no state directory, an unreadable claim, an unparseable
payload, a command it cannot lex. Blocking the only writer in a tree over state the hook
merely failed to read is the worse error — and the behaviour of a repo without this hook is
where everyone already was.

## Checking it

```bash
python scripts/test_worktree_owner.py
```

Real git worktrees in a temp directory. The denials are the easy half; the suite spends
most of itself on the **allows** — a read, a sibling repo, the main checkout, a session's
own scratch script, its own tree — because every false positive lands on ordinary work and
spends trust the hook has to keep.

`install.py --status` reports the ownership registration on its own line, and marks each
worktree with the session holding it — whether or not the hook is installed, since a claim
left behind is most interesting exactly when the hook has been turned off.
